"""
Stream detection for the HLS / audio-track feature.

Primary path : ``ffprobe -print_format json`` (used when an ffprobe binary exists).
Fallback path: parse the banner printed by ``ffmpeg -i`` (imageio-ffmpeg ships ffmpeg only).

Nothing here is guessed: if a stream has no usable language tag the track is labelled
"Track N" - the file name, title text or anything else is never used to invent a language.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


PROBE_SIZE = os.environ.get("HLS_PROBE_SIZE", "20M").strip() or "20M"
ANALYZE_DURATION = str(env_int("HLS_ANALYZE_DURATION", 10_000_000))  # microseconds
PROBE_TIMEOUT = max(10, env_int("HLS_PROBE_TIMEOUT", 60))

# Only plain HTTP to our own server is allowed as an FFmpeg/ffprobe input.
INPUT_PROTOCOLS = "http,tcp"


# ============================================================
# BINARIES
# ============================================================

def find_ffmpeg() -> Optional[str]:
    """FFMPEG_PATH env -> imageio-ffmpeg -> ffmpeg on PATH."""
    configured = os.environ.get("FFMPEG_PATH", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def find_ffprobe(ffmpeg: Optional[str] = None) -> Optional[str]:
    """FFPROBE_PATH env -> ffprobe next to ffmpeg -> ffprobe on PATH. May be None."""
    configured = os.environ.get("FFPROBE_PATH", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.path.isfile(sibling):
            return sibling
    return shutil.which("ffprobe")


# ============================================================
# LANGUAGES
# ============================================================

# ISO 639-1 / 639-2 (B and T variants) -> readable name
_LANGS = {
    ("en", "eng"): "English", ("hi", "hin"): "Hindi", ("ta", "tam"): "Tamil",
    ("te", "tel"): "Telugu", ("ml", "mal"): "Malayalam", ("kn", "kan"): "Kannada",
    ("bn", "ben"): "Bengali", ("mr", "mar"): "Marathi", ("gu", "guj"): "Gujarati",
    ("pa", "pan"): "Punjabi", ("ur", "urd"): "Urdu", ("or", "ori"): "Odia",
    ("as", "asm"): "Assamese", ("ne", "nep"): "Nepali", ("si", "sin"): "Sinhala",
    ("sa", "san"): "Sanskrit", ("kok",): "Konkani", ("mai",): "Maithili",
    ("ja", "jpn"): "Japanese", ("ko", "kor"): "Korean",
    ("zh", "zho", "chi"): "Chinese", ("yue",): "Cantonese", ("cmn",): "Mandarin",
    ("es", "spa"): "Spanish", ("fr", "fra", "fre"): "French",
    ("de", "deu", "ger"): "German", ("it", "ita"): "Italian",
    ("pt", "por"): "Portuguese", ("ru", "rus"): "Russian",
    ("ar", "ara"): "Arabic", ("tr", "tur"): "Turkish", ("th", "tha"): "Thai",
    ("vi", "vie"): "Vietnamese", ("id", "ind"): "Indonesian", ("ms", "msa", "may"): "Malay",
    ("fil", "tl", "tgl"): "Filipino", ("nl", "nld", "dut"): "Dutch",
    ("pl", "pol"): "Polish", ("sv", "swe"): "Swedish", ("no", "nor"): "Norwegian",
    ("nb", "nob"): "Norwegian Bokmal", ("da", "dan"): "Danish", ("fi", "fin"): "Finnish",
    ("cs", "ces", "cze"): "Czech", ("sk", "slk", "slo"): "Slovak",
    ("hu", "hun"): "Hungarian", ("ro", "ron", "rum"): "Romanian",
    ("bg", "bul"): "Bulgarian", ("el", "ell", "gre"): "Greek", ("he", "heb"): "Hebrew",
    ("fa", "fas", "per"): "Persian", ("uk", "ukr"): "Ukrainian", ("hr", "hrv"): "Croatian",
    ("sr", "srp"): "Serbian", ("sl", "slv"): "Slovenian", ("lt", "lit"): "Lithuanian",
    ("lv", "lav"): "Latvian", ("et", "est"): "Estonian", ("is", "isl", "ice"): "Icelandic",
    ("ga", "gle"): "Irish", ("ca", "cat"): "Catalan", ("eu", "eus", "baq"): "Basque",
    ("gl", "glg"): "Galician", ("sq", "sqi", "alb"): "Albanian", ("hy", "hye", "arm"): "Armenian",
    ("ka", "kat", "geo"): "Georgian", ("az", "aze"): "Azerbaijani", ("kk", "kaz"): "Kazakh",
    ("uz", "uzb"): "Uzbek", ("mn", "mon"): "Mongolian", ("my", "mya", "bur"): "Burmese",
    ("km", "khm"): "Khmer", ("lo", "lao"): "Lao", ("sw", "swa"): "Swahili",
    ("af", "afr"): "Afrikaans", ("am", "amh"): "Amharic", ("ps", "pus"): "Pashto",
    ("la", "lat"): "Latin",
}
LANGUAGE_NAMES: Dict[str, str] = {code: name for codes, name in _LANGS.items() for code in codes}

# code -> the preferred (shortest) BCP-47 primary tag, e.g. "hin" -> "hi", "fre" -> "fr"
LANGUAGE_ALPHA2: Dict[str, str] = {
    code: next((c for c in codes if len(c) == 2), codes[0]) for codes in _LANGS for code in codes
}

# Backwards compatible name used by older code
AUDIO_LANGUAGE_MAP = LANGUAGE_NAMES

# Codes that mean "no usable language information"
_UNKNOWN_LANGS = {"", "und", "unk", "zxx", "mis", "mul", "mo", "n/a", "none", "null"}


def normalize_language(raw: Any) -> Tuple[str, str]:
    """
    Returns (code, readable_name).

    ("", "")        -> metadata missing / undetermined  -> caller shows "Track N"
    ("kok", "")     -> a real code we have no name for  -> caller shows the code
    ("hin", "Hindi")
    """
    code = str(raw or "").strip().lower().replace("_", "-")
    primary = code.split("-")[0]
    if primary in _UNKNOWN_LANGS:
        return "", ""
    if len(primary) == 3 and "qaa" <= primary <= "qtz":  # ISO 639-2 private-use range
        return "", ""
    if not re.fullmatch(r"[a-z]{2,3}", primary):
        return "", ""
    return primary, LANGUAGE_NAMES.get(primary, "")


# ============================================================
# MODELS
# ============================================================

@dataclass
class VideoInfo:
    stream_index: int = 0
    codec: str = ""
    profile: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    level: Optional[int] = None  # H.264 level_idc, e.g. 41 for 4.1
    fps: Optional[float] = None
    bit_rate: Optional[int] = None


@dataclass
class ProbeResult:
    video: Optional[VideoInfo] = None
    audio: List[Dict[str, Any]] = field(default_factory=list)
    duration: Optional[float] = None
    bit_rate: Optional[int] = None
    container: str = ""
    source: str = ""  # "ffprobe" | "ffmpeg"


# ============================================================
# LABELS
# ============================================================

def _clean_text(value: Any, limit: int = 80) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _channel_text(track: Dict[str, Any]) -> str:
    layout = (track.get("channel_layout") or "").strip()
    if layout:
        return layout
    channels = track.get("channels")
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    if channels:
        return f"{channels}ch"
    return ""


def build_labels(tracks: List[Dict[str, Any]]) -> None:
    """Fills ``label`` on every track (unique, never invented)."""
    base_labels: List[str] = []
    for position, track in enumerate(tracks):
        name = track.get("language") or (track.get("code") or "").upper()
        title = track.get("title") or ""
        if name:
            label = name
            # Real title text helps tell two English tracks apart (e.g. "Commentary")
            if title and name.lower() not in title.lower():
                label = f"{name} - {title}"
        else:
            label = f"Track {position + 1}"
            if title:
                label = f"{label} - {title}"
        base_labels.append(label)

    counts: Dict[str, int] = {}
    for label in base_labels:
        counts[label.lower()] = counts.get(label.lower(), 0) + 1

    seen: Dict[str, int] = {}
    for track, label in zip(tracks, base_labels):
        if counts[label.lower()] > 1:
            extra = _channel_text(track)
            candidate = f"{label} ({extra})" if extra else label
            key = candidate.lower()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                candidate = f"{candidate} #{seen[key]}"
            label = candidate
        track["label"] = label


def _finish_audio(raw_tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    for position, raw in enumerate(raw_tracks):
        code, name = normalize_language(raw.get("language"))
        tracks.append({
            "index": position,                       # position among audio streams == ffmpeg 0:a:N
            "stream": raw.get("stream"),             # absolute stream index inside the file
            "language": name,                        # "" when unknown
            "code": code,                            # "" when unknown
            "title": _clean_text(raw.get("title")),
            "codec": raw.get("codec") or "",
            "channels": raw.get("channels"),
            "channel_layout": raw.get("channel_layout") or "",
            "sample_rate": raw.get("sample_rate"),
            "default": bool(raw.get("default")),
        })
    defaults = [t for t in tracks if t["default"]]
    if tracks and not defaults:
        tracks[0]["default"] = True
    elif len(defaults) > 1:
        for extra in defaults[1:]:
            extra["default"] = False
    build_labels(tracks)
    return tracks


# ============================================================
# ffprobe (JSON)
# ============================================================

def _to_int(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> Optional[float]:
    try:
        num, den = str(value).split("/")
        den_f = float(den)
        return round(float(num) / den_f, 3) if den_f else None
    except (TypeError, ValueError):
        return None


def parse_ffprobe(data: Dict[str, Any]) -> ProbeResult:
    result = ProbeResult(source="ffprobe")
    fmt = data.get("format") or {}
    result.duration = _to_float(fmt.get("duration"))
    result.bit_rate = _to_int(fmt.get("bit_rate"))
    result.container = str(fmt.get("format_name") or "")

    raw_audio: List[Dict[str, Any]] = []
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        tags = {str(k).lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}

        if kind == "video":
            if disposition.get("attached_pic"):
                continue  # cover art, not a real video stream
            if result.video is not None:
                continue
            result.video = VideoInfo(
                stream_index=_to_int(stream.get("index")) or 0,
                codec=str(stream.get("codec_name") or "").lower(),
                profile=str(stream.get("profile") or ""),
                pix_fmt=str(stream.get("pix_fmt") or "").lower(),
                width=_to_int(stream.get("width")) or 0,
                height=_to_int(stream.get("height")) or 0,
                level=_to_int(stream.get("level")),
                fps=_fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate")),
                bit_rate=_to_int(stream.get("bit_rate")),
            )
        elif kind == "audio":
            raw_audio.append({
                "stream": _to_int(stream.get("index")),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "codec": str(stream.get("codec_name") or "").lower(),
                "channels": _to_int(stream.get("channels")),
                "channel_layout": stream.get("channel_layout") or "",
                "sample_rate": _to_int(stream.get("sample_rate")),
                "default": bool(disposition.get("default")),
            })

    result.audio = _finish_audio(raw_audio)
    return result


# ============================================================
# ffmpeg banner (fallback)
# ============================================================

_STREAM_RE = re.compile(
    r"^(?P<indent>\s*)Stream #(?P<input>\d+):(?P<index>\d+)"
    r"(?:\[(?P<id>[^\]]*)\])?"          # [0x1100]
    r"(?:\((?P<lang>[^)]*)\))?"         # (eng)
    r":\s*(?P<kind>Audio|Video|Subtitle|Data|Attachment)\s*:\s*(?P<rest>.*)$"
)
_META_RE = re.compile(r"^(?P<key>[A-Za-z0-9_\-\. ]+?)\s*:\s?(?P<value>.*)$")
_CHANNELS_RE = re.compile(
    r"\b(mono|stereo|quad|hexadecagonal|octagonal|\d(?:\.\d)(?:\([a-z]+\))?|\d+ channels?)\b",
    re.I,
)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_ffmpeg_info(text: str) -> ProbeResult:
    result = ProbeResult(source="ffmpeg")
    lines = text.splitlines()

    for line in lines:
        header = re.match(r"\s*Input #0,\s*([^,]+(?:,[^,]+)*?),\s*from", line)
        if header and not result.container:
            result.container = header.group(1).strip()
        duration = re.match(r"\s*Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", line)
        if duration and result.duration is None:
            result.duration = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
            bitrate = re.search(r"bitrate:\s*(\d+)\s*kb/s", line)
            if bitrate:
                result.bit_rate = int(bitrate.group(1)) * 1000

    raw_audio: List[Dict[str, Any]] = []
    position = 0
    while position < len(lines):
        match = _STREAM_RE.match(lines[position])
        if not match or match.group("input") != "0":
            position += 1
            continue

        stream_indent = len(match.group("indent"))
        kind = match.group("kind")
        rest = match.group("rest")
        stream_line_lang = match.group("lang")
        stream_no = int(match.group("index"))

        # Indented block that follows a stream line: "Metadata:" + "key : value" rows
        metadata: Dict[str, str] = {}
        cursor = position + 1
        in_metadata = False
        while cursor < len(lines):
            current = lines[cursor]
            if not current.strip():
                break
            if _indent_of(current) <= stream_indent:
                break  # next stream, "Stream mapping:", "At least one output..." etc.
            stripped = current.strip()
            if stripped.lower() == "metadata:":
                in_metadata = True
            elif in_metadata:
                meta = _META_RE.match(stripped)
                if meta:
                    metadata[meta.group("key").strip().lower()] = meta.group("value").strip()
            cursor += 1

        if kind == "Video" and "attached pic" not in rest and result.video is None:
            codec_match = re.match(r"([A-Za-z0-9_\-]+)(?:\s+\(([^)]*)\))?", rest)
            size_match = None
            for candidate in re.finditer(r"(?<![0-9A-Za-z])(\d{2,5})x(\d{2,5})(?![0-9A-Za-z])", rest):
                if int(candidate.group(1)) >= 16 and int(candidate.group(2)) >= 16:
                    size_match = candidate
                    break
            pix = re.search(r",\s*((?:yuvj?|gray|nv|rgb|bgr|p0|ayuv|argb)[a-z0-9_]*)", rest)
            fps = re.search(r"([\d.]+)\s*fps", rest)
            kbps = re.search(r"(\d+)\s*kb/s", rest)
            result.video = VideoInfo(
                stream_index=stream_no,
                codec=(codec_match.group(1).lower() if codec_match else ""),
                profile=(codec_match.group(2) or "" if codec_match else ""),
                pix_fmt=(pix.group(1).lower() if pix else ""),
                width=int(size_match.group(1)) if size_match else 0,
                height=int(size_match.group(2)) if size_match else 0,
                level=None,  # not printed by the ffmpeg banner
                fps=float(fps.group(1)) if fps else None,
                bit_rate=int(kbps.group(1)) * 1000 if kbps else None,
            )
        elif kind == "Audio":
            codec_match = re.match(r"([A-Za-z0-9_\-]+)", rest)
            layout = _CHANNELS_RE.search(rest)
            hz = re.search(r"(\d+)\s*Hz", rest)
            channels = None
            layout_text = layout.group(1) if layout else ""
            if layout_text:
                lowered = layout_text.lower()
                if lowered == "mono":
                    channels = 1
                elif lowered == "stereo":
                    channels = 2
                else:
                    digits = re.match(r"(\d+)(?:\.(\d))?", lowered)
                    if digits:
                        channels = int(digits.group(1)) + (int(digits.group(2)) if digits.group(2) else 0)
            raw_audio.append({
                "stream": stream_no,
                # Prefer the explicit Metadata value, fall back to the "(eng)" in the stream line
                "language": metadata.get("language") or stream_line_lang,
                "title": metadata.get("title"),
                "codec": codec_match.group(1).lower() if codec_match else "",
                "channels": channels,
                "channel_layout": layout_text if layout_text and not layout_text.lower().endswith("channels") else "",
                "sample_rate": int(hz.group(1)) if hz else None,
                "default": "(default)" in rest,
            })
        position = max(cursor, position + 1)

    result.audio = _finish_audio(raw_audio)
    return result


# ============================================================
# RUNNING THE TOOLS
# ============================================================

async def _run(cmd: List[str], timeout: int) -> Tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    return process.returncode, stdout, stderr


async def probe_source(source_url: str) -> ProbeResult:
    """
    Probes an HTTP source that WE built (never a user supplied URL).
    Raises RuntimeError with a short, non-sensitive message on failure.
    """
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)

    if ffprobe:
        cmd = [
            ffprobe, "-v", "error", "-hide_banner",
            "-protocol_whitelist", INPUT_PROTOCOLS,
            "-probesize", PROBE_SIZE, "-analyzeduration", ANALYZE_DURATION,
            "-print_format", "json", "-show_streams", "-show_format",
            "-i", source_url,
        ]
        try:
            code, stdout, _ = await _run(cmd, PROBE_TIMEOUT)
            if code == 0 and stdout:
                result = parse_ffprobe(json.loads(stdout.decode("utf-8", "ignore")))
                if result.audio or result.video:
                    return result
            logger.warning("ffprobe returned no usable streams (exit %s); trying the ffmpeg fallback", code)
        except asyncio.TimeoutError:
            logger.warning("ffprobe timed out; trying the ffmpeg fallback")
        except (ValueError, OSError) as error:
            logger.warning("ffprobe failed (%s); trying the ffmpeg fallback", type(error).__name__)

    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available on this server")

    cmd = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-protocol_whitelist", INPUT_PROTOCOLS,
        "-probesize", PROBE_SIZE, "-analyzeduration", ANALYZE_DURATION,
        "-i", source_url,
    ]
    try:
        # `ffmpeg -i` with no output exits with code 1 after printing the banner: that is expected
        _, _, stderr = await _run(cmd, PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("Reading the file timed out")
    except OSError as error:
        raise RuntimeError(f"Could not start FFmpeg ({type(error).__name__})")

    result = parse_ffmpeg_info(stderr.decode("utf-8", "ignore"))
    if not result.audio and result.video is None:
        raise RuntimeError("FFmpeg could not read any stream from this file")
    return result


async def detect_audio_tracks(source_url: str) -> List[Dict[str, Any]]:
    """Compatibility wrapper: returns only the audio-track list ([] on any error)."""
    try:
        return (await probe_source(source_url)).audio
    except Exception as error:
        logger.warning("Audio detection failed: %s", error)
        return []


# ============================================================
# VIDEO -> HLS PLAN
# ============================================================

_H264_COPY_PROFILES = {"baseline", "constrained baseline", "main", "high", "extended"}
_H264_PROFILE_IDC = {"baseline": "42", "constrained baseline": "42", "extended": "58", "main": "4d", "high": "64"}
_H264_CONSTRAINTS = {"baseline": "00", "constrained baseline": "e0", "extended": "00", "main": "40", "high": "00"}


def decide_video_mode(video: Optional[VideoInfo], allow_transcode: bool) -> Tuple[Optional[str], str]:
    """
    ("copy" | "transcode" | None, reason).
    Only 8-bit 4:2:0 H.264 is remuxed untouched; everything else must be re-encoded
    (browsers cannot play VP9/AV1/MPEG-4 inside HLS-TS, and most cannot play H.264 10-bit).
    """
    if video is None:
        return None, "no_video"
    profile = video.profile.lower()
    copyable = (
        video.codec == "h264"
        and video.pix_fmt in ("yuv420p", "yuvj420p")
        and profile in _H264_COPY_PROFILES
        and (video.level is None or video.level <= 52)
    )
    if copyable:
        return "copy", "browser compatible H.264"
    if allow_transcode:
        return "transcode", f"{video.codec or 'unknown'} {video.pix_fmt or ''} {video.profile or ''}".strip()
    return None, "video_not_compatible"


def avc1_codec_string(video: VideoInfo, mode: str, out_height: int) -> Optional[str]:
    """
    Real RFC 6381 string for the video we output, or None when we cannot know it
    (the CODECS attribute is optional, so omitting it beats inventing one).
    """
    if mode == "transcode":
        level = "33" if out_height > 1080 else "29"  # 5.1 / 4.1 (matches the -level we pass to x264)
        return f"avc1.6400{level}"
    profile = video.profile.lower()
    if profile not in _H264_PROFILE_IDC or video.level is None:
        return None
    return f"avc1.{_H264_PROFILE_IDC[profile]}{_H264_CONSTRAINTS[profile]}{video.level:02x}"
