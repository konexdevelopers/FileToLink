import asyncio
import logging
import shutil
from pathlib import Path

import imageio_ffmpeg


logger = logging.getLogger(__name__)


# Temporary HLS files
HLS_ROOT = Path("hls_cache")
HLS_ROOT.mkdir(parents=True, exist_ok=True)


# Keep a limited number of active HLS jobs
_jobs = {}


def get_ffmpeg():
    """
    Get the FFmpeg executable supplied by imageio-ffmpeg.
    """
    return imageio_ffmpeg.get_ffmpeg_exe()


def get_job_dir(file_id: int):
    return HLS_ROOT / str(file_id)


def is_running(file_id: int):
    job = _jobs.get(file_id)

    if not job:
        return False

    process = job.get("process")

    if not process:
        return False

    return process.returncode is None


async def stop_hls(file_id: int):
    """
    Stop an existing HLS process.
    """

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

        except Exception:
            pass


def cleanup_hls(file_id: int):
    """
    Remove generated HLS files.
    """

    directory = get_job_dir(file_id)

    if directory.exists():

        try:
            shutil.rmtree(directory)

        except Exception as e:
            logger.warning(
                "Unable to remove HLS directory %s: %s",
                directory,
                e
            )


async def create_hls(
    file_id: int,
    source_url: str
):
    """
    Create an HLS stream from the supplied media URL.

    The source can be the bot's existing media URL.
    """

    if is_running(file_id):

        return get_job_dir(file_id)

    # Remove previous files
    cleanup_hls(file_id)

    output_dir = get_job_dir(file_id)
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    ffmpeg = get_ffmpeg()

    video_playlist = output_dir / "video.m3u8"

    command = [
        ffmpeg,

        "-hide_banner",
        "-loglevel", "warning",

        # Input
        "-i",
        source_url,

        # First video stream
        "-map",
        "0:v:0",

        # Copy video whenever possible
        "-c:v",
        "copy",

        # HLS
        "-f",
        "hls",

        "-hls_time",
        "4",

        "-hls_list_size",
        "6",

        "-hls_flags",
        "independent_segments",

        "-hls_segment_filename",
        str(output_dir / "video_%05d.ts"),

        str(video_playlist)
    ]

    logger.info(
        "Starting HLS video process for file %s",
        file_id
    )

    try:

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )

    except Exception as e:

        logger.exception(
            "Unable to start FFmpeg"
        )

        cleanup_hls(file_id)

        raise e

    _jobs[file_id] = {
        "process": process,
        "directory": output_dir,
        "source_url": source_url
    }

    return output_dir


async def create_audio_hls(
    file_id: int,
    source_url: str,
    audio_index: int,
    language: str
):
    """
    Create one HLS audio rendition.

    audio_index:
        0 = first audio stream
        1 = second audio stream
        2 = third audio stream
        ...
    """

    directory = get_job_dir(file_id)

    directory.mkdir(
        parents=True,
        exist_ok=True
    )

    ffmpeg = get_ffmpeg()

    safe_language = "".join(
        c for c in language
        if c.isalnum() or c in ("_", "-")
    )

    playlist = directory / (
        f"audio_{audio_index}_{safe_language}.m3u8"
    )

    command = [
        ffmpeg,

        "-hide_banner",
        "-loglevel", "warning",

        "-i",
        source_url,

        "-map",
        f"0:a:{audio_index}",

        # Browser-friendly audio
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
        "6",

        "-hls_flags",
        "independent_segments",

        "-hls_segment_filename",
        str(
            directory /
            f"audio_{audio_index}_{safe_language}_%05d.ts"
        ),

        str(playlist)
    ]

    logger.info(
        "Starting HLS audio process: file=%s audio=%s language=%s",
        file_id,
        audio_index,
        language
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    return process, playlist


def create_master_playlist(
    file_id: int,
    audio_tracks
):
    """
    Create the HLS master playlist.

    audio_tracks example:

    [
        {
            "index": 0,
            "language": "English",
            "playlist": "audio_0_English.m3u8"
        },
        {
            "index": 1,
            "language": "Hindi",
            "playlist": "audio_1_Hindi.m3u8"
        }
    ]
    """

    directory = get_job_dir(file_id)

    master = directory / "master.m3u8"

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        ""
    ]

    for position, track in enumerate(audio_tracks):

        language = track["language"]
        playlist = track["playlist"]

        default = "YES" if position == 0 else "NO"

        lines.append(
            "#EXT-X-MEDIA:"
            f'TYPE=AUDIO,'
            f'GROUP-ID="audio",'
            f'NAME="{language}",'
            f'LANGUAGE="{language.lower()}",'
            f'DEFAULT={default},'
            f'AUTOSELECT=YES,'
            f'URI="{playlist}"'
        )

    lines.extend([
        "",
        '#EXT-X-STREAM-INF:BANDWIDTH=2500000,'
        'CODECS="avc1.640028,mp4a.40.2",'
        'AUDIO="audio"',
        "video.m3u8",
        ""
    ])

    master.write_text(
        "\n".join(lines),
        encoding="utf-8"
    )

    return master


async def get_or_create_hls(
    file_id: int,
    source_url: str,
    audio_tracks
):
    """
    Start HLS generation if it isn't already running.
    """

    directory = get_job_dir(file_id)

    master = directory / "master.m3u8"

    if master.exists() and is_running(file_id):

        return master

    await stop_hls(file_id)

    cleanup_hls(file_id)

    directory.mkdir(
        parents=True,
        exist_ok=True
    )

    # Start video HLS
    await create_hls(
        file_id,
        source_url
    )

    generated_audio = []

    # Start one audio HLS process per audio track
    for track in audio_tracks:

        index = track["index"]
        language = track["language"]

        process, playlist = await create_audio_hls(
            file_id,
            source_url,
            index,
            language
        )

        generated_audio.append({
            "index": index,
            "language": language,
            "playlist": playlist.name
        })

    # Create master playlist
    create_master_playlist(
        file_id,
        generated_audio
    )

    return master


async def cleanup_all():
    """
    Stop all active HLS processes.
    """

    for file_id in list(_jobs.keys()):

        await stop_hls(file_id)

    if HLS_ROOT.exists():

        try:
            shutil.rmtree(HLS_ROOT)

        except Exception:
            pass
