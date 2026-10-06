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

    return (
        process is not None
        and process.returncode is None
    )


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
                "Error stopping HLS process: %s",
                e
            )


def cleanup_hls(file_id: int):
    directory = get_job_dir(file_id)

    if directory.exists():
        try:
            shutil.rmtree(directory)
        except Exception as e:
            logger.warning(
                "Unable to clean HLS directory: %s",
                e
            )


def _safe_name(name: str) -> str:
    return "".join(
        char
        for char in str(name)
        if char.isalnum() or char in "_-"
    )


async def create_hls(
    file_id: int,
    source_url: str,
    audio_tracks: list
):
    """
    Creates one HLS process containing:

        Video + Audio 1
        Video + Audio 2
        Video + Audio 3
        ...

    This allows the player to switch between audio renditions.
    """

    if is_running(file_id):
        return get_job_dir(file_id) / "master.m3u8"

    await stop_hls(file_id)

    cleanup_hls(file_id)

    output_dir = get_job_dir(file_id)

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    if not audio_tracks:
        raise ValueError(
            "No audio tracks available"
        )

    ffmpeg = get_ffmpeg()

    command = [
        ffmpeg,

        "-hide_banner",
        "-loglevel",
        "warning",

        "-i",
        source_url,
    ]

    # ---------------------------------------------------------
    # MAP VIDEO
    # ---------------------------------------------------------

    command.extend([
        "-map",
        "0:v:0",
    ])

    # ---------------------------------------------------------
    # MAP EVERY AUDIO TRACK
    # ---------------------------------------------------------

    for track in audio_tracks:

        index = track["index"]

        command.extend([
            "-map",
            f"0:a:{index}",
        ])

    # ---------------------------------------------------------
    # VIDEO
    # ---------------------------------------------------------

    command.extend([
        "-c:v",
        "copy",
    ])

    # ---------------------------------------------------------
    # AUDIO
    # ---------------------------------------------------------

    command.extend([
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ac",
        "2",

        "-ar",
        "48000",
    ])

    # ---------------------------------------------------------
    # HLS
    # ---------------------------------------------------------

    command.extend([
        "-f",
        "hls",

        "-hls_time",
        "4",

        "-hls_list_size",
        "8",

        "-hls_flags",
        "independent_segments",

        "-master_pl_name",
        "master.m3u8",
    ])

    # ---------------------------------------------------------
    # CREATE VARIANTS
    #
    # Example:
    #
    # v:0,a:0
    # v:0,a:1
    # v:0,a:2
    # ---------------------------------------------------------

    variants = []

    for position, track in enumerate(audio_tracks):

        language = _safe_name(
            track.get(
                "language",
                f"Track_{position + 1}"
            )
        )

        variants.append(
            f"v:0,a:{position},"
            f"name:{language}"
        )

    var_stream_map = " ".join(variants)

    command.extend([
        "-var_stream_map",
        var_stream_map,

        "-hls_segment_filename",
        str(
            output_dir /
            "stream_%v_%05d.ts"
        ),

        str(
            output_dir /
            "stream_%v.m3u8"
        ),
    ])

    logger.info(
        "Starting single HLS process for file %s",
        file_id
    )

    logger.debug(
        "FFmpeg command: %s",
        " ".join(command)
    )

    try:

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.DEVNULL,

            stderr=asyncio.subprocess.PIPE
        )

    except Exception as e:

        logger.exception(
            "Failed to start FFmpeg"
        )

        cleanup_hls(file_id)

        raise e

    _jobs[file_id] = {
        "process": process,
        "directory": output_dir,
        "source_url": source_url,
        "audio_tracks": audio_tracks,
    }

    # ---------------------------------------------------------
    # Wait briefly for master playlist
    # ---------------------------------------------------------

    master = output_dir / "master.m3u8"

    for _ in range(50):

        if master.exists():
            break

        if process.returncode is not None:

            stderr = b""

            try:
                stderr = await process.stderr.read()
            except Exception:
                pass

            logger.error(
                "FFmpeg stopped while creating HLS: %s",
                stderr.decode(
                    errors="ignore"
                )
            )

            cleanup_hls(file_id)

            raise RuntimeError(
                "FFmpeg could not create HLS stream"
            )

        await asyncio.sleep(0.2)

    if not master.exists():

        await stop_hls(file_id)

        cleanup_hls(file_id)

        raise RuntimeError(
            "HLS master playlist was not created"
        )

    return master


async def get_or_create_hls(
    file_id: int,
    source_url: str,
    audio_tracks: list
):
    """
    Return existing HLS playlist or create a new one.
    """

    master = (
        get_job_dir(file_id) /
        "master.m3u8"
    )

    if (
        master.exists()
        and is_running(file_id)
    ):
        return master

    return await create_hls(
        file_id=file_id,
        source_url=source_url,
        audio_tracks=audio_tracks,
    )


async def cleanup_all():
    """
    Stop all FFmpeg processes and
    remove generated HLS files.
    """

    for file_id in list(_jobs.keys()):
        await stop_hls(file_id)

    if HLS_ROOT.exists():
        try:
            shutil.rmtree(HLS_ROOT)
        except Exception as e:
            logger.warning(
                "Unable to clean HLS root: %s",
                e
            )

    HLS_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )
