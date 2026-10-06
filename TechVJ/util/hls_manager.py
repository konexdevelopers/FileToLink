import asyncio
import logging
import shutil
from pathlib import Path

import imageio_ffmpeg

logger = logging.getLogger(__name__)

HLS_ROOT = Path("hls_cache")
HLS_ROOT.mkdir(parents=True, exist_ok=True)

_jobs = {}


def get_ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def get_job_dir(file_id: int) -> Path:
    return HLS_ROOT / str(file_id)


def is_running(file_id: int) -> bool:
    job = _jobs.get(file_id)

    if not job:
        return False

    process = job.get("process")

    return process is not None and process.returncode is None


async def stop_hls(file_id: int):
    job = _jobs.pop(file_id, None)

    if not job:
        return

    process = job.get("process")

    if process and process.returncode is None:
        try:
            process.terminate()

            await asyncio.wait_for(
                process.wait(),
                timeout=5
            )

        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass

        except Exception as e:
            logger.warning(
                "Error stopping HLS: %s",
                e
            )


def cleanup_hls(file_id: int):
    directory = get_job_dir(file_id)

    if directory.exists():
        try:
            shutil.rmtree(directory)
        except Exception as e:
            logger.warning(
                "HLS cleanup failed: %s",
                e
            )


def _safe_name(name: str) -> str:
    return "".join(
        c for c in str(name)
        if c.isalnum() or c in "_-"
    )


async def create_hls(
    file_id: int,
    source_url: str,
    audio_tracks: list
):
    """
    Creates:

        video.m3u8
        audio_0.m3u8
        audio_1.m3u8
        ...
        master.m3u8

    The master playlist contains proper
    EXT-X-MEDIA audio renditions.
    """

    if is_running(file_id):
        master = (
            get_job_dir(file_id)
            / "master.m3u8"
        )

        if master.exists():
            return master

    await stop_hls(file_id)
    cleanup_hls(file_id)

    output_dir = get_job_dir(file_id)
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    if not audio_tracks:
        raise ValueError(
            "No audio tracks detected"
        )

    ffmpeg = get_ffmpeg()

    video_playlist = (
        output_dir / "video.m3u8"
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",

        "-i",
        source_url,

        # -------------------------
        # VIDEO
        # -------------------------

        "-map",
        "0:v:0",

        "-c:v",
        "copy",

        "-f",
        "hls",

        "-hls_time",
        "4",

        "-hls_list_size",
        "0",

        "-hls_playlist_type",
        "vod",

        "-hls_flags",
        "independent_segments",

        "-hls_segment_filename",
        str(output_dir / "video_%05d.ts"),

        str(video_playlist),
    ]

    # -------------------------
    # AUDIO PLAYLISTS
    # -------------------------

    audio_playlists = []

    for position, track in enumerate(audio_tracks):

        audio_playlist = (
            output_dir
            / f"audio_{position}.m3u8"
        )

        audio_playlists.append(
            audio_playlist
        )

        audio_index = track["index"]

        command.extend([
            "-map",
            f"0:a:{audio_index}",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-ac",
            "2",

            "-ar",
            "48000",

            "-f",
            "hls",

            "-hls_time",
            "4",

            "-hls_list_size",
            "0",

            "-hls_playlist_type",
            "vod",

            "-hls_flags",
            "independent_segments",

            "-hls_segment_filename",
            str(
                output_dir
                / f"audio_{position}_%05d.ts"
            ),

            str(audio_playlist),
        ])

    logger.info(
        "Starting HLS generation for %s",
        file_id
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _jobs[file_id] = {
        "process": process,
        "directory": output_dir,
        "source_url": source_url,
        "audio_tracks": audio_tracks,
    }

    # Wait until video playlist exists
    for _ in range(150):

        if video_playlist.exists():
            break

        if process.returncode is not None:

            stderr = await process.stderr.read()

            raise RuntimeError(
                stderr.decode(
                    "utf-8",
                    errors="ignore"
                )
            )

        await asyncio.sleep(0.2)

    if not video_playlist.exists():

        if process.returncode is not None:

            stderr = await process.stderr.read()

            raise RuntimeError(
                stderr.decode(
                    "utf-8",
                    errors="ignore"
                )
            )

        raise RuntimeError(
            "FFmpeg did not create video playlist"
        )

    # Wait for first audio playlist
    for playlist in audio_playlists:

        for _ in range(150):

            if playlist.exists():
                break

            if process.returncode is not None:

                stderr = await process.stderr.read()

                raise RuntimeError(
                    stderr.decode(
                        "utf-8",
                        errors="ignore"
                    )
                )

            await asyncio.sleep(0.2)

        if not playlist.exists():
            raise RuntimeError(
                f"Audio playlist not created: {playlist.name}"
            )

    # -------------------------
    # CREATE MASTER PLAYLIST
    # -------------------------

    master = (
        output_dir / "master.m3u8"
    )

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
    ]

    # Audio groups
    for position, track in enumerate(audio_tracks):

        language = (
            track.get("language")
            or f"Track {position + 1}"
        )

        language_code = (
            track.get("code")
            or "und"
        )

        default = (
            "YES"
            if position == 0
            else "NO"
        )

        lines.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,'
            'GROUP-ID="audio",'
            f'NAME="{language}",'
            f'LANGUAGE="{language_code}",'
            f'DEFAULT={default},'
            'AUTOSELECT=YES,'
            f'URI="audio_{position}.m3u8"'
        )

    # Video
    lines.append(
        '#EXT-X-STREAM-INF:'
        'BANDWIDTH=5000000,'
        'CODECS="avc1.640028",'
        'AUDIO="audio"'
    )

    lines.append("video.m3u8")

    master.write_text(
        "\n".join(lines),
        encoding="utf-8"
    )

    logger.info(
        "HLS master playlist created: %s",
        master
    )

    return master


async def get_or_create_hls(
    file_id: int,
    source_url: str,
    audio_tracks: list
):

    master = (
        get_job_dir(file_id)
        / "master.m3u8"
    )

    if master.exists():
        return master

    return await create_hls(
        file_id,
        source_url,
        audio_tracks
    )


async def cleanup_all():

    for file_id in list(_jobs.keys()):
        await stop_hls(file_id)

    _jobs.clear()
