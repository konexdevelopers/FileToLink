# Don't Remove Credit @VJ_Botz
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import re
import math
import logging
import secrets
import mimetypes
import subprocess
import json
import asyncio
import urllib.parse
from pathlib import Path

from info import *
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine

from TechVJ.bot import multi_clients, work_loads, TechVJBot
from TechVJ.server.exceptions import FIleNotFound, InvalidHash
from TechVJ.util.custom_dl import ByteStreamer
from TechVJ.util.render_template import render_page
from TechVJ.util.hls_manager import get_or_create_hls


routes = web.RouteTableDef()

class_cache = {}

HLS_ROOT = Path("hls_cache")


# ============================================================
# ROOT
# ============================================================

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("BenFilterBot")


# ============================================================
# WATCH PAGE
# ============================================================

@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def watch_handler(request: web.Request):

    try:

        path = request.match_info["path"]

        match = re.search(
            r"^([a-zA-Z0-9_-]{6})(\d+)$",
            path
        )

        if match:

            secure_hash = match.group(1)
            file_id = int(match.group(2))

        else:

            id_match = re.search(
                r"(\d+)(?:/\S+)?",
                path
            )

            if not id_match:
                raise FIleNotFound

            file_id = int(
                id_match.group(1)
            )

            secure_hash = request.rel_url.query.get(
                "hash"
            )

        html = await render_page(
            file_id,
            secure_hash
        )

        return web.Response(
            text=html,
            content_type="text/html"
        )

    except InvalidHash as e:

        raise web.HTTPForbidden(
            text=e.message
        )

    except FIleNotFound as e:

        raise web.HTTPNotFound(
            text=e.message
        )

    except (
        AttributeError,
        BadStatusLine,
        ConnectionResetError
    ):

        raise web.HTTPBadRequest(
            text="Invalid request"
        )

    except Exception as e:

        logging.exception(
            "Watch page error"
        )

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# HLS MASTER / PLAYLIST / SEGMENTS
# ============================================================

@routes.get(
    r"/hls/{file_id:\d+}/{file_path:.*}",
    allow_head=True
)
async def hls_handler(request: web.Request):

    try:

        file_id = int(
            request.match_info["file_id"]
        )

        file_path = request.match_info[
            "file_path"
        ]

        if not file_path:

            raise web.HTTPNotFound(
                text="HLS file not found"
            )

        # ----------------------------------------------------
        # Prevent path traversal
        # ----------------------------------------------------

        requested_path = Path(file_path)

        if (
            ".." in requested_path.parts
            or requested_path.is_absolute()
        ):

            raise web.HTTPForbidden(
                text="Invalid path"
            )

        base_dir = (
            HLS_ROOT /
            str(file_id)
        ).resolve()

        target = (
            base_dir /
            requested_path
        ).resolve()

        try:

            target.relative_to(
                base_dir
            )

        except ValueError:

            raise web.HTTPForbidden(
                text="Invalid path"
            )

        # ----------------------------------------------------
        # File must already exist
        # ----------------------------------------------------

        if not target.exists():
            raise web.HTTPNotFound(
                text="HLS segment not ready"
            )

        if not target.is_file():
            raise web.HTTPNotFound(
                text="HLS file not found"
            )

        # ----------------------------------------------------
        # MIME type
        # ----------------------------------------------------

        suffix = target.suffix.lower()

        if suffix == ".m3u8":

            content_type = (
                "application/vnd.apple.mpegurl"
            )

        elif suffix == ".ts":

            content_type = (
                "video/mp2t"
            )

        elif suffix == ".m4s":

            content_type = (
                "video/iso.segment"
            )

        else:

            content_type = (
                mimetypes.guess_type(
                    target.name
                )[0]
                or "application/octet-stream"
            )

        # ----------------------------------------------------
        # Read HLS file
        # ----------------------------------------------------

        return web.FileResponse(
            path=target,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            }
        )

    except web.HTTPException:

        raise

    except Exception as e:

        logging.exception(
            "HLS handler error"
        )

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# AUDIO TRACK DETECTION
# ============================================================

async def detect_audio_tracks_from_url(
    source_url: str
):
    """
    Uses FFmpeg to inspect the media URL and detect
    audio streams.

    Returns:

    [
        {
            "index": 0,
            "language": "English"
        },
        {
            "index": 1,
            "language": "Hindi"
        }
    ]
    """

    try:

        import imageio_ffmpeg

        ffmpeg = (
            imageio_ffmpeg
            .get_ffmpeg_exe()
        )

    except Exception as e:

        logging.exception(
            "FFmpeg is not available"
        )

        return []

    command = [
        ffmpeg,

        "-hide_banner",

        "-i",
        source_url,
    ]

    try:

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=45
        )

        output = (
            stderr.decode(
                errors="ignore"
            )
        )

    except asyncio.TimeoutError:

        logging.warning(
            "Audio detection timed out"
        )

        try:
            process.kill()
        except Exception:
            pass

        return []

    except Exception:

        logging.exception(
            "Audio detection failed"
        )

        return []

    tracks = []

    # --------------------------------------------------------
    # Example FFmpeg line:
    #
    # Stream #0:1(eng): Audio: aac
    #
    # Stream #0:2(hin): Audio: aac
    # --------------------------------------------------------

    pattern = re.compile(
        r"Stream #0:(\d+)"
        r"(?:\(([^)]+)\))?"
        r".*?: Audio:",
        re.IGNORECASE
    )

    for match in pattern.finditer(
        output
    ):

        stream_index = int(
            match.group(1)
        )

        language_code = (
            match.group(2)
            or ""
        ).lower()

        # -----------------------------------------------
        # Convert language code
        # -----------------------------------------------

        language_map = {
            "eng": "English",
            "en": "English",

            "hin": "Hindi",
            "hi": "Hindi",

            "tam": "Tamil",
            "ta": "Tamil",

            "tel": "Telugu",
            "te": "Telugu",

            "mal": "Malayalam",
            "ml": "Malayalam",

            "kan": "Kannada",
            "kn": "Kannada",

            "ben": "Bengali",
            "bn": "Bengali",

            "mar": "Marathi",
            "mr": "Marathi",

            "guj": "Gujarati",
            "gu": "Gujarati",

            "pan": "Punjabi",
            "pa": "Punjabi",

            "jpn": "Japanese",
            "ja": "Japanese",

            "kor": "Korean",
            "ko": "Korean",

            "spa": "Spanish",
            "es": "Spanish",

            "fra": "French",
            "fr": "French",

            "deu": "German",
            "ger": "German",
            "de": "German",

            "ara": "Arabic",
            "ar": "Arabic",

            "rus": "Russian",
            "ru": "Russian",
        }

        language = (
            language_map.get(
                language_code,
                language_code.upper()
                if language_code
                else None
            )
            or f"Track {len(tracks) + 1}"
        )

        tracks.append({
            "index": len(tracks),
            "stream_index": stream_index,
            "language": language,
        })

    return tracks


# ============================================================
# CREATE HLS
# ============================================================

@routes.get(
    r"/hls-start/{file_id:\d+}",
    allow_head=True
)
async def start_hls_handler(
    request: web.Request
):

    try:

        file_id = int(
            request.match_info[
                "file_id"
            ]
        )

        secure_hash = (
            request.rel_url.query.get(
                "hash"
            )
        )

        if not secure_hash:

            raise web.HTTPBadRequest(
                text="Hash is required"
            )

        # ----------------------------------------------------
        # Get Telegram file
        # ----------------------------------------------------

        index = min(
            work_loads,
            key=work_loads.get
        )

        faster_client = (
            multi_clients[index]
        )

        if faster_client in class_cache:

            tg_connect = (
                class_cache[
                    faster_client
                ]
            )

        else:

            tg_connect = ByteStreamer(
                faster_client
            )

            class_cache[
                faster_client
            ] = tg_connect

        file_data = (
            await tg_connect
            .get_file_properties(
                file_id
            )
        )

        # ----------------------------------------------------
        # Validate hash
        # ----------------------------------------------------

        if (
            not file_data.unique_id
            or
            file_data.unique_id[:6]
            != secure_hash
        ):

            raise InvalidHash

        file_name = (
            file_data.file_name
            or f"{file_id}.mkv"
        )

        # ----------------------------------------------------
        # Build normal streaming URL
        # ----------------------------------------------------

        source_url = urllib.parse.urljoin(
            URL,
            (
                f"{file_id}/"
                f"{urllib.parse.quote_plus(file_name)}"
                f"?hash={secure_hash}"
            )
        )

        # ----------------------------------------------------
        # Detect audio
        # ----------------------------------------------------

        audio_tracks = (
            await detect_audio_tracks_from_url(
                source_url
            )
        )

        if not audio_tracks:

            raise web.HTTPBadRequest(
                text="No audio tracks detected"
            )

        # ----------------------------------------------------
        # Create HLS
        # ----------------------------------------------------

        master = (
            await get_or_create_hls(
                file_id=file_id,
                source_url=source_url,
                audio_tracks=audio_tracks,
            )
        )

        # ----------------------------------------------------
        # Return master playlist URL
        # ----------------------------------------------------

        master_url = (
            urllib.parse.urljoin(
                URL,
                (
                    f"hls/"
                    f"{file_id}/"
                    f"{master.name}"
                )
            )
        )

        return web.json_response({
            "success": True,

            "type": (
                "Single Audio"
                if len(audio_tracks) == 1
                else
                "Dual Audio"
                if len(audio_tracks) == 2
                else
                "Multi Audio"
            ),

            "tracks": audio_tracks,

            "master_url": master_url,
        })

    except InvalidHash:

        raise web.HTTPForbidden(
            text="Invalid hash"
        )

    except web.HTTPException:

        raise

    except Exception as e:

        logging.exception(
            "HLS creation failed"
        )

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# NORMAL DOWNLOAD / STREAM
# ============================================================

@routes.get(
    r"/{path:\S+}",
    allow_head=True
)
async def media_handler(
    request: web.Request
):

    try:

        path = request.match_info[
            "path"
        ]

        match = re.search(
            r"^([a-zA-Z0-9_-]{6})(\d+)$",
            path
        )

        if match:

            secure_hash = match.group(1)

            file_id = int(
                match.group(2)
            )

        else:

            id_match = re.search(
                r"(\d+)(?:/\S+)?",
                path
            )

            if not id_match:

                raise FIleNotFound

            file_id = int(
                id_match.group(1)
            )

            secure_hash = (
                request.rel_url.query.get(
                    "hash"
                )
            )

        return await media_streamer(
            request,
            file_id,
            secure_hash
        )

    except InvalidHash as e:

        raise web.HTTPForbidden(
            text=e.message
        )

    except FIleNotFound as e:

        raise web.HTTPNotFound(
            text=e.message
        )

    except (
        AttributeError,
        BadStatusLine,
        ConnectionResetError
    ):

        raise web.HTTPBadRequest(
            text="Invalid request"
        )

    except Exception as e:

        logging.exception(
            "Media streaming error"
        )

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# TELEGRAM MEDIA STREAMER
# ============================================================

async def media_streamer(
    request: web.Request,
    id: int,
    secure_hash: str
):

    range_header = (
        request.headers.get(
            "Range"
        )
    )

    # --------------------------------------------------------
    # Select fastest Telegram client
    # --------------------------------------------------------

    index = min(
        work_loads,
        key=work_loads.get
    )

    faster_client = (
        multi_clients[index]
    )

    if MULTI_CLIENT:

        logging.info(
            "Client %s serving %s",
            index,
            request.remote
        )

    # --------------------------------------------------------
    # ByteStreamer cache
    # --------------------------------------------------------

    if faster_client in class_cache:

        tg_connect = (
            class_cache[
                faster_client
            ]
        )

    else:

        tg_connect = ByteStreamer(
            faster_client
        )

        class_cache[
            faster_client
        ] = tg_connect

    # --------------------------------------------------------
    # Get file properties
    # --------------------------------------------------------

    file_id = (
        await tg_connect
        .get_file_properties(
            id
        )
    )

    # --------------------------------------------------------
    # Validate hash
    # --------------------------------------------------------

    if (
        not file_id.unique_id
        or
        file_id.unique_id[:6]
        != secure_hash
    ):

        logging.debug(
            "Invalid hash for message %s",
            id
        )

        raise InvalidHash

    file_size = (
        file_id.file_size
    )

    # --------------------------------------------------------
    # Range handling
    # --------------------------------------------------------

    if range_header:

        try:

            range_value = (
                range_header
                .replace(
                    "bytes=",
                    ""
                )
            )

            parts = range_value.split(
                "-"
            )

            from_bytes = int(
                parts[0]
            )

            if parts[1]:

                until_bytes = int(
                    parts[1]
                )

            else:

                until_bytes = (
                    file_size - 1
                )

        except (
            ValueError,
            IndexError
        ):

            raise web.HTTPRequestRangeNotSatisfiable(
                headers={
                    "Content-Range":
                    f"bytes */{file_size}"
                }
            )

    else:

        from_bytes = (
            request.http_range.start
            or 0
        )

        until_bytes = (
            request.http_range.stop
            or file_size
        ) - 1

    # --------------------------------------------------------
    # Validate range
    # --------------------------------------------------------

    if (
        from_bytes < 0
        or until_bytes < from_bytes
        or from_bytes >= file_size
    ):

        return web.Response(
            status=416,
            text="416: Range not satisfiable",
            headers={
                "Content-Range":
                f"bytes */{file_size}"
            }
        )

    until_bytes = min(
        until_bytes,
        file_size - 1
    )

    # --------------------------------------------------------
    # Chunk calculations
    # --------------------------------------------------------

    chunk_size = (
        1024 * 1024
    )

    offset = (
        from_bytes
        - (
            from_bytes
            % chunk_size
        )
    )

    first_part_cut = (
        from_bytes
        - offset
    )

    last_part_cut = (
        until_bytes
        % chunk_size
    ) + 1

    req_length = (
        until_bytes
        - from_bytes
        + 1
    )

    part_count = (
        math.ceil(
            until_bytes
            / chunk_size
        )
        -
        math.floor(
            offset
            / chunk_size
        )
    )

    # --------------------------------------------------------
    # Telegram byte generator
    # --------------------------------------------------------

    body = tg_connect.yield_file(
        file_id,
        index,
        offset,
        first_part_cut,
        last_part_cut,
        part_count,
        chunk_size
    )

    # --------------------------------------------------------
    # MIME / filename
    # ------------------

mime_type = (
        file_id.mime_type
    )

    file_name = (
        file_id.file_name
    )

    disposition = "attachment"

    if mime_type:

        if not file_name:

            try:

                extension = (
                    mime_type
                    .split(
                        "/"
                    )[1]
                )

                file_name = (
                    f"{secrets.token_hex(2)}."
                    f"{extension}"
                )

            except (
                IndexError,
                AttributeError
            ):

                file_name = (
                    f"{secrets.token_hex(2)}.unknown"
                )

    else:

        if file_name:

            guessed = (
                mimetypes.guess_type(
                    file_name
                )[0]
            )

            mime_type = (
                guessed
                or
                "application/octet-stream"
            )

        else:

            mime_type = (
                "application/octet-stream"
            )

            file_name = (
                f"{secrets.token_hex(2)}.unknown"
            )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return web.Response(

        status=(
            206
            if range_header
            else
            200
        ),

        body=body,

        headers={

            "Content-Type":
            mime_type,

            "Content-Range":
            (
                f"bytes "
                f"{from_bytes}-"
                f"{until_bytes}/"
                f"{file_size}"
            ),

            "Content-Length":
            str(req_length),

            "Content-Disposition":
            (
                f'{disposition}; '
                f'filename="{file_name}"'
            ),

            "Accept-Ranges":
            "bytes",

            "Access-Control-Allow-Origin":
            "*",
        }
    )
