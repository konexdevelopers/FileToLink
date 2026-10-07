"""
On-demand HLS generation with real, switchable audio renditions.

For one Telegram file this builds

    master.m3u8            (#EXT-X-MEDIA audio group + one #EXT-X-STREAM-INF)
    video.m3u8  video_00000.ts ...
    audio_0.m3u8 audio_0_00000.ts ...   (one rendition per real audio stream)
    audio_1.m3u8 ...

with a SINGLE FFmpeg process that reads the file through the bot's own HTTP stream
(http://127.0.0.1:PORT/<hash><id>) - the input URL is always built by the server, never
taken from a request.

Resource rules (limited worker):
  * one job per file, started under a per-file lock (no duplicate FFmpeg runs)
  * at most HLS_MAX_JOBS jobs; idle jobs are evicted LRU
  * jobs are deleted after HLS_IDLE_TTL seconds without any request (cache is NOT permanent)
  * hard runtime limit, disk-space / cache-size guards, FFmpeg runs at low CPU priority
  * optional "lead window": FFmpeg is paused (SIGSTOP) when it is far ahead of the viewer
  * every child process is terminated on shutdown / eviction / failure
"""

import asyncio
import atexit
import collections
import contextlib
import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional

from TechVJ.util import media_probe as mp

logger = logging.getLogger(__name__)

MASTER = "master.m3u8"
VIDEO_PLAYLIST = "video.m3u8"
AUDIO_GROUP = "audio"
AUDIO_CODEC = "mp4a.40.2"  # we always encode AAC-LC


# ============================================================
# ERRORS
# ============================================================

class HlsError(Exception):
    """Carries a machine readable code and a message that is safe to show to the viewer."""

    def __init__(self, code: str, message: str, status: int = 500, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


# ============================================================
# CONFIG
# ============================================================

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class HlsConfig:
    cache_dir: Path
    max_jobs: int
    idle_ttl: int
    max_runtime: int
    start_timeout: int
    segment_seconds: int
    max_cache_bytes: int
    min_free_bytes: int
    max_source_bytes: int
    allow_transcode: bool
    preset: str
    crf: int
    max_height: int
    audio_bitrate: str
    readrate: float
    lead_segments: int
    nice: int
    autostart: bool
    evict_min_idle: int = 45


def load_config() -> HlsConfig:
    env_int = mp.env_int
    preset = os.environ.get("HLS_TRANSCODE_PRESET", "veryfast").strip().lower()
    if preset not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"):
        preset = "veryfast"
    audio_bitrate = os.environ.get("HLS_AUDIO_BITRATE", "128k").strip().lower()
    if not re.fullmatch(r"\d{2,3}k", audio_bitrate):
        audio_bitrate = "128k"
    try:
        readrate = max(0.0, float(os.environ.get("HLS_READRATE", "0")))
    except ValueError:
        readrate = 0.0
    return HlsConfig(
        cache_dir=Path(os.environ.get("HLS_CACHE_DIR", "hls_cache")),
        max_jobs=max(1, env_int("HLS_MAX_JOBS", 2)),
        idle_ttl=max(60, env_int("HLS_IDLE_TTL", 900)),
        max_runtime=max(300, env_int("HLS_MAX_RUNTIME", 3 * 3600)),
        start_timeout=max(15, env_int("HLS_START_TIMEOUT", 75)),
        segment_seconds=min(10, max(2, env_int("HLS_SEGMENT_SECONDS", 4))),
        max_cache_bytes=max(256, env_int("HLS_MAX_CACHE_MB", 6144)) * 1024 * 1024,
        min_free_bytes=max(0, env_int("HLS_MIN_FREE_MB", 1024)) * 1024 * 1024,
        max_source_bytes=max(0, env_int("HLS_MAX_SOURCE_MB", 4096)) * 1024 * 1024,
        allow_transcode=_env_bool("HLS_ALLOW_TRANSCODE", True),
        preset=preset,
        crf=min(35, max(15, env_int("HLS_TRANSCODE_CRF", 23))),
        max_height=max(0, env_int("HLS_MAX_HEIGHT", 0)),
        audio_bitrate=audio_bitrate,
        readrate=readrate,
        lead_segments=max(0, env_int("HLS_LEAD_SEGMENTS", 0)),
        nice=min(19, max(0, env_int("HLS_NICE", 10))),
        autostart=_env_bool("HLS_AUTOSTART", True),
    )


# ============================================================
# PURE HELPERS (unit tested)
# ============================================================

def _q(value: str) -> str:
    """Safe value for a quoted HLS attribute."""
    return re.sub(r"[\x00-\x1f\x7f\"]", " ", str(value or "")).strip()


def playlist_stats(text: str) -> Dict[str, int]:
    return {
        "segments": text.count("#EXTINF"),
        "ended": 1 if "#EXT-X-ENDLIST" in text else 0,
    }


def estimate_bandwidth(
    probe: mp.ProbeResult, mode: str, out_width: int, out_height: int,
    audio_bitrate: str, file_size: int,
) -> int:
    """
    BANDWIDTH is a required master-playlist attribute and is by nature an ESTIMATE.
    Order of preference: measured stream/container bitrate -> size/duration -> pixel based guess.
    """
    audio_bps = int(audio_bitrate[:-1]) * 1000
    video = probe.video
    bps = 0
    if mode == "copy" and video and video.bit_rate:
        bps = video.bit_rate
    elif probe.bit_rate:
        bps = probe.bit_rate
    elif file_size and probe.duration:
        bps = int(file_size * 8 / probe.duration)
    if mode == "transcode" and not bps and out_width and out_height:
        bps = int(out_width * out_height * (video.fps if video and video.fps else 24) * 0.1)
    if not bps:
        bps = 2_500_000  # nothing is known about this file at all
    return int(bps + audio_bps)


def build_master_playlist(
    tracks: List[Dict], video_codec: Optional[str], bandwidth: int,
    resolution: Optional[str], frame_rate: Optional[float],
) -> str:
    """
    Master playlist with a real audio group. CODECS is only written when the video codec is
    actually known (see h264_codec_from_ts / avc1_codec_string); otherwise the attribute is omitted,
    which the HLS spec allows, instead of guessing.
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:4", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for position, track in enumerate(tracks):
        attrs = [
            "TYPE=AUDIO",
            f'GROUP-ID="{AUDIO_GROUP}"',
            f'NAME="{_q(track.get("label") or f"Track {position + 1}")}"',
        ]
        code = track.get("code") or ""
        if code:
            # only when real metadata exists; BCP-47 primary tag ("hin" -> "hi")
            attrs.append(f'LANGUAGE="{_q(mp.LANGUAGE_ALPHA2.get(code, code))}"')
        attrs.append(f'DEFAULT={"YES" if track.get("default") else "NO"}')
        # AUTOSELECT=NO on the rest: the player must not silently pick a track by system language,
        # the viewer chooses explicitly in the Audio menu
        attrs.append(f'AUTOSELECT={"YES" if track.get("default") else "NO"}')
        attrs.append('CHANNELS="2"')  # renditions are always encoded as stereo AAC
        attrs.append(f'URI="audio_{position}.m3u8"')
        lines.append("#EXT-X-MEDIA:" + ",".join(attrs))

    stream = [f"BANDWIDTH={bandwidth}", f"AVERAGE-BANDWIDTH={max(1, int(bandwidth * 0.9))}"]
    if video_codec:
        stream.append(f'CODECS="{video_codec},{AUDIO_CODEC}"')
    if resolution:
        stream.append(f"RESOLUTION={resolution}")
    if frame_rate:
        stream.append(f"FRAME-RATE={frame_rate:.3f}")
    stream.append(f'AUDIO="{AUDIO_GROUP}"')
    lines.append("#EXT-X-STREAM-INF:" + ",".join(stream))
    lines.append(VIDEO_PLAYLIST)
    return "\n".join(lines) + "\n"


def output_geometry(video: mp.VideoInfo, mode: str, max_height: int):
    """(width, height) of the video we will actually output."""
    width, height = video.width, video.height
    if mode == "transcode" and max_height and height > max_height:
        scaled_width = int(round(width * max_height / height / 2.0)) * 2
        return max(2, scaled_width), max_height
    return width, height


def build_ffmpeg_command(
    ffmpeg: str, source_url: str, out_dir: Path, tracks: List[Dict],
    video: mp.VideoInfo, mode: str, cfg: HlsConfig, use_nice: bool = False,
) -> List[str]:
    cmd: List[str] = []
    if use_nice and cfg.nice:
        cmd += ["nice", "-n", str(cfg.nice)]
    cmd += [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
        # input: only plain HTTP to our own server, with reconnects and I/O timeout
        "-protocol_whitelist", mp.INPUT_PROTOCOLS,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "30000000",
        "-probesize", mp.PROBE_SIZE, "-analyzeduration", mp.ANALYZE_DURATION,
    ]
    if cfg.readrate:
        cmd += ["-readrate", f"{cfg.readrate:g}"]
    cmd += ["-i", source_url]

    seg = str(cfg.segment_seconds)
    hls_out = [
        "-f", "hls", "-hls_time", seg, "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+temp_file", "-hls_segment_type", "mpegts",
    ]

    # ---- video rendition -------------------------------------------------
    cmd += ["-map", f"0:{video.stream_index}", "-an", "-sn", "-dn"]
    if mode == "copy":
        cmd += ["-c:v", "copy"]
    else:
        out_w, out_h = output_geometry(video, mode, cfg.max_height)
        level = "5.1" if out_h > 1080 else "4.1"
        if (out_w, out_h) != (video.width, video.height):
            cmd += ["-vf", f"scale={out_w}:{out_h}"]
        cmd += [
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", level,
            "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{seg})",
            "-max_muxing_queue_size", "1024",
        ]
    cmd += hls_out + [
        "-hls_segment_filename", str(out_dir / "video_%05d.ts"), str(out_dir / VIDEO_PLAYLIST),
    ]

    # ---- one real audio rendition per audio stream ----------------------
    for position, track in enumerate(tracks):
        stream = track.get("stream")
        selector = f"0:{stream}" if isinstance(stream, int) else f"0:a:{position}"
        cmd += [
            "-map", selector, "-vn", "-sn", "-dn",
            "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ac", "2", "-ar", "48000",
            "-af", "aresample=async=1:first_pts=0",
        ]
        cmd += hls_out + [
            "-hls_segment_filename", str(out_dir / f"audio_{position}_%05d.ts"),
            str(out_dir / f"audio_{position}.m3u8"),
        ]
    return cmd


def _ts_payloads(data: bytes) -> List[bytes]:
    """Concatenated payload bytes per PID of an MPEG-TS buffer (no PAT/PMT, no headers)."""
    streams: Dict[int, bytearray] = {}
    for offset in range(0, len(data) - 187, 188):
        packet = data[offset:offset + 188]
        if packet[0] != 0x47:
            continue
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid in (0, 0x1FFF):
            continue
        control = (packet[3] >> 4) & 0x3
        if control in (0, 2):
            continue  # no payload
        start = 4
        if control == 3:
            start += 1 + packet[4]
        if start < 188:
            streams.setdefault(pid, bytearray()).extend(packet[start:])
    return [bytes(chunk) for chunk in streams.values()]


def h264_codec_from_ts(path: Path, limit: int = 600_000) -> Optional[str]:
    """
    Reads the real SPS (profile_idc, constraint flags, level_idc) from the first video segment and
    returns the exact RFC 6381 string, e.g. "avc1.640028". None if no SPS can be found.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit)
    except OSError:
        return None
    for payload in _ts_payloads(data):
        for marker in (b"\x00\x00\x01\x67", b"\x00\x00\x00\x01\x67"):
            at = payload.find(marker)
            if at >= 0:
                sps = payload[at + len(marker): at + len(marker) + 3]
                if len(sps) == 3:
                    return "avc1." + sps.hex()
    return None


# ============================================================
# JOB MODEL
# ============================================================

@dataclass
class HlsJob:
    file_id: int
    secure_hash: str
    directory: Path
    tracks: List[Dict]
    mode: str
    probe: mp.ProbeResult
    process: Optional[asyncio.subprocess.Process] = None
    state: str = "starting"  # starting | running | complete | failed | stopped
    error: str = ""
    created: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    finished: Optional[float] = None
    max_requested: int = 0
    paused: bool = False
    stderr_tail: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=30))
    tasks: List[asyncio.Task] = field(default_factory=list)

    @property
    def master_path(self) -> Path:
        return self.directory / MASTER

    def touch(self) -> None:
        self.last_access = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


def _dir_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return total


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


# ============================================================
# MANAGER
# ============================================================

class HlsManager:
    def __init__(self, cfg: Optional[HlsConfig] = None):
        self.cfg = cfg or load_config()
        self.jobs: Dict[int, HlsJob] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._probe_cache: Dict[int, tuple] = {}
        self._probe_locks: Dict[int, asyncio.Lock] = {}
        self._probe_slots = asyncio.Semaphore(2)
        self._failures: Dict[int, tuple] = {}
        self._janitor: Optional[asyncio.Task] = None
        self.ffmpeg = mp.find_ffmpeg()
        self.ffprobe = mp.find_ffprobe(self.ffmpeg)
        atexit.register(self._kill_all_sync)

    # ---------------------------------------------------- lifecycle ----

    async def startup(self) -> None:
        """Called once when the web app starts: wipe leftovers of earlier runs, start the janitor."""
        root = self.cfg.cache_dir
        root.mkdir(parents=True, exist_ok=True)
        for child in root.iterdir():
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        logger.info(
            "HLS ready | ffmpeg=%s | ffprobe=%s | max_jobs=%s | idle_ttl=%ss | cache_dir=%s",
            self.ffmpeg or "NOT FOUND", self.ffprobe or "not installed (ffmpeg fallback)",
            self.cfg.max_jobs, self.cfg.idle_ttl, root,
        )
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.create_task(self._janitor_loop())

    async def shutdown(self) -> None:
        if self._janitor:
            self._janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._janitor
        for file_id in list(self.jobs):
            await self._drop(file_id, "shutdown")

    def _kill_all_sync(self) -> None:
        for job in list(self.jobs.values()):
            process = job.process
            if process is not None and process.returncode is None:
                with contextlib.suppress(Exception):
                    os.kill(process.pid, signal.SIGKILL)

    # --------------------------------------------------------- probe ----

    async def probe(self, file_id: int, source_url: str) -> mp.ProbeResult:
        """Cached per file (6 h). Concurrent callers share one run."""
        cached = self._probe_cache.get(file_id)
        if cached and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        lock = self._probe_locks.setdefault(file_id, asyncio.Lock())
        async with lock:
            cached = self._probe_cache.get(file_id)
            if cached and time.monotonic() - cached[0] < 6 * 3600:
                return cached[1]
            async with self._probe_slots:
                try:
                    result = await mp.probe_source(source_url)
                except Exception as error:
                    raise HlsError("probe_failed", "The file could not be analysed", 502) from error
            if len(self._probe_cache) >= 128:
                self._probe_cache.pop(min(self._probe_cache, key=lambda k: self._probe_cache[k][0]), None)
            self._probe_cache[file_id] = (time.monotonic(), result)
            return result

    # ---------------------------------------------------------- jobs ----

    def get_job(self, file_id: int, secure_hash: str) -> Optional[HlsJob]:
        job = self.jobs.get(file_id)
        if job and job.secure_hash == secure_hash and job.state in ("running", "complete", "starting"):
            return job
        return None

    def note_access(self, job: HlsJob, name: str) -> None:
        job.touch()
        match = re.fullmatch(r"video_(\d{5})\.ts", name)
        if match:
            job.max_requested = max(job.max_requested, int(match.group(1)))

    def plan(self, probe: mp.ProbeResult, file_size: int) -> Dict:
        """Everything the page needs to know before it asks for HLS (no process is started)."""
        mode, reason = mp.decide_video_mode(probe.video, self.cfg.allow_transcode)
        problem = None
        if not probe.audio:
            problem = ("no_audio", "This file has no audio track")
        elif probe.video is None:
            problem = ("no_video", "This file has no video track")
        elif mode is None:
            problem = ("unsupported", "This video format cannot be converted on this server")
        elif self.cfg.max_source_bytes and file_size > self.cfg.max_source_bytes:
            problem = ("too_large", "This file is too large for audio switching on this server")
        elif not self.ffmpeg:
            problem = ("no_ffmpeg", "FFmpeg is not available on this server")
        return {"mode": mode, "reason": reason, "available": problem is None, "problem": problem}

    async def get_or_start(
        self, file_id: int, secure_hash: str, source_url: str, probe: mp.ProbeResult, file_size: int,
    ) -> HlsJob:
        lock = self._locks.setdefault(file_id, asyncio.Lock())
        async with lock:
            job = self.jobs.get(file_id)
            if job:
                if job.state in ("running", "complete") and job.master_path.exists():
                    job.touch()
                    return job
                await self._drop(file_id, "stale")

            failed = self._failures.get(file_id)
            if failed and time.monotonic() - failed[0] < 30:
                raise failed[1]  # do not hammer FFmpeg for a file that just failed

            plan = self.plan(probe, file_size)
            if not plan["available"]:
                code, message = plan["problem"]
                raise HlsError(code, message, 422)

            await self._make_room(file_size)

            directory = (self.cfg.cache_dir / str(int(file_id))).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            job = HlsJob(
                file_id=file_id, secure_hash=secure_hash, directory=directory,
                tracks=probe.audio, mode=plan["mode"], probe=probe,
            )
            self.jobs[file_id] = job
            try:
                await self._spawn(job, source_url)
                await self._wait_ready(job)
                await self._write_master(job, file_size)
            except HlsError as error:
                self._failures[file_id] = (time.monotonic(), error)
                await self._drop(file_id, f"failed: {error.code}")
                raise
            except Exception as error:
                logger.exception("HLS start failed for file %s", file_id)
                failure = HlsError("failed", "Audio switching could not be started", 500)
                self._failures[file_id] = (time.monotonic(), failure)
                await self._drop(file_id, "failed: unexpected")
                raise failure from error

            if job.state == "starting":  # _watch_exit may already have recorded complete/failed
                job.state = "running"
            return job

    # ------------------------------------------------------ internals ----

    async def _spawn(self, job: HlsJob, source_url: str) -> None:
        command = build_ffmpeg_command(
            self.ffmpeg, source_url, job.directory, job.tracks, job.probe.video, job.mode,
            self.cfg, use_nice=bool(shutil.which("nice")),
        )
        job.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        logger.info(
            "FFmpeg command started | file=%s mode=%s audio_tracks=%d pid=%s",
            job.file_id, job.mode, len(job.tracks), job.process.pid,
        )
        job.tasks.append(asyncio.create_task(self._drain_stderr(job)))
        job.tasks.append(asyncio.create_task(self._watch_exit(job)))

    async def _drain_stderr(self, job: HlsJob) -> None:
        """Never let the stderr pipe fill up (that would freeze FFmpeg)."""
        process = job.process
        try:
            while process and process.stderr:
                line = await process.stderr.readline()
                if not line:
                    break
                job.stderr_tail.append(line.decode("utf-8", "ignore").rstrip()[:300])
        except Exception:
            pass

    async def _watch_exit(self, job: HlsJob) -> None:
        process = job.process
        code = await process.wait()
        job.finished = time.monotonic()
        if job.state == "stopped":
            return
        if code == 0:
            job.state = "complete"
            logger.info(
                "HLS generation completed | file=%s | %.1fs", job.file_id, job.finished - job.created,
            )
        else:
            job.state = "failed"
            job.error = f"ffmpeg exited with code {code}"
            logger.error(
                "HLS generation failed: %s | file=%s | last ffmpeg output: %s",
                job.error, job.file_id, " | ".join(list(job.stderr_tail)[-4:]),
            )
            self._finalize_playlists(job)

    def _finalize_playlists(self, job: HlsJob) -> None:
        """If FFmpeg died mid-way, close the playlists so players stop waiting instead of hanging."""
        for playlist in job.directory.glob("*.m3u8"):
            if playlist.name == MASTER:
                continue
            text = _read(playlist)
            if text and "#EXT-X-ENDLIST" not in text:
                with contextlib.suppress(OSError):
                    with open(playlist, "a", encoding="utf-8") as handle:
                        handle.write("#EXT-X-ENDLIST\n")

    def _playlists_ready(self, job: HlsJob) -> bool:
        names = [VIDEO_PLAYLIST] + [f"audio_{i}.m3u8" for i in range(len(job.tracks))]
        for name in names:
            stats = playlist_stats(_read(job.directory / name))
            if not (stats["ended"] or stats["segments"] >= 2):
                return False
        return True

    async def _wait_ready(self, job: HlsJob) -> None:
        deadline = time.monotonic() + self.cfg.start_timeout
        while True:
            if self._playlists_ready(job):
                return
            if job.process.returncode is not None:
                await asyncio.sleep(0.2)  # let the stderr reader catch up
                if self._playlists_ready(job):
                    return
                logger.error(
                    "HLS generation failed: FFmpeg stopped before any output | file=%s | %s",
                    job.file_id, " | ".join(list(job.stderr_tail)[-4:]),
                )
                raise HlsError("failed", "The video could not be prepared", 502)
            if time.monotonic() > deadline:
                logger.error("HLS generation failed: timed out waiting for the first segments | file=%s", job.file_id)
                raise HlsError("timeout", "Preparing the video took too long", 504)
            await asyncio.sleep(0.25)

    async def _write_master(self, job: HlsJob, file_size: int) -> None:
        video = job.probe.video
        out_w, out_h = output_geometry(video, job.mode, self.cfg.max_height)

        if job.mode == "transcode":
            codec = mp.avc1_codec_string(video, "transcode", out_h)
        else:
            codec = h264_codec_from_ts(job.directory / "video_00000.ts")
            if not codec:
                codec = mp.avc1_codec_string(video, "copy", out_h)  # ffprobe-derived, or None

        bandwidth = estimate_bandwidth(job.probe, job.mode, out_w, out_h, self.cfg.audio_bitrate, file_size)
        text = build_master_playlist(
            job.tracks, codec, bandwidth,
            f"{out_w}x{out_h}" if out_w and out_h else None, video.fps,
        )
        temp = job.directory / ".master.tmp"
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, job.master_path)
        logger.info("HLS master playlist created | file=%s | codecs=%s | bandwidth=%s", job.file_id, codec or "n/a", bandwidth)

    async def _terminate(self, process: Optional[asyncio.subprocess.Process]) -> None:
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGCONT)  # a paused process cannot handle SIGTERM
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 5)

    async def _drop(self, file_id: int, reason: str) -> None:
        job = self.jobs.pop(file_id, None)
        if not job:
            return
        job.state = "stopped"
        await self._terminate(job.process)
        for task in job.tasks:
            task.cancel()
        directory = job.directory
        await asyncio.get_running_loop().run_in_executor(None, lambda: shutil.rmtree(directory, ignore_errors=True))
        logger.info("HLS job removed | file=%s | reason=%s", file_id, reason)

    async def _make_room(self, needed: int) -> None:
        """Evict idle jobs (least recently used first) until a new job fits; else report 'busy'."""
        def total_size() -> int:
            return sum(_dir_size(job.directory) for job in self.jobs.values())

        def free_space() -> int:
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.cfg.cache_dir).free

        while True:
            too_many = len(self.jobs) >= self.cfg.max_jobs
            too_big = total_size() + needed > self.cfg.max_cache_bytes
            no_space = free_space() < needed + self.cfg.min_free_bytes
            if not (too_many or too_big or no_space):
                return
            now = time.monotonic()
            idle = [
                job for job in self.jobs.values()
                if now - job.last_access >= self.cfg.evict_min_idle
            ]
            if not idle:
                raise HlsError("busy", "The server is busy preparing other videos. Please try again shortly.", 503, retry_after=30)
            oldest = min(idle, key=lambda job: job.last_access)
            await self._drop(oldest.file_id, "evicted (cache limits)")

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("HLS janitor error")

    async def _tick(self) -> None:
        now = time.monotonic()
        for file_id, job in list(self.jobs.items()):
            if job.state == "starting":
                continue
            if now - job.last_access > self.cfg.idle_ttl:
                await self._drop(file_id, f"idle for {self.cfg.idle_ttl}s")
                continue
            if job.alive and now - job.created > self.cfg.max_runtime:
                await self._drop(file_id, "runtime limit")
                continue
            if self.cfg.lead_segments and job.alive:
                self._apply_lead_window(job)
        for file_id in [k for k, v in self._failures.items() if now - v[0] > 60]:
            self._failures.pop(file_id, None)
        for file_id in [k for k, v in self._locks.items() if k not in self.jobs and not v.locked()]:
            self._locks.pop(file_id, None)

    def _apply_lead_window(self, job: HlsJob) -> None:
        """Pause FFmpeg when it is far ahead of what the viewer has requested; resume when it is not."""
        try:
            generated = sum(1 for n in os.listdir(job.directory) if n.startswith("video_") and n.endswith(".ts"))
        except OSError:
            return
        lead = generated - job.max_requested
        process = job.process
        try:
            if not job.paused and lead > self.cfg.lead_segments:
                process.send_signal(signal.SIGSTOP)
                job.paused = True
            elif job.paused and lead <= max(1, self.cfg.lead_segments // 2):
                process.send_signal(signal.SIGCONT)
                job.paused = False
        except ProcessLookupError:
            pass

    def stats(self) -> Dict:
        return {
            "jobs": {
                job.file_id: {"state": job.state, "mode": job.mode, "paused": job.paused, "tracks": len(job.tracks)}
                for job in self.jobs.values()
            }
        }


# One manager per process (created lazily so importing this module never touches the disk)
_manager: Optional[HlsManager] = None


def get_manager() -> HlsManager:
    global _manager
    if _manager is None:
        _manager = HlsManager()
    return _manager
