import re
import math
import logging
import secrets
import mimetypes
from pathlib import Path
from urllib.parse import quote_plus

from info import *
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine

from TechVJ.bot import (
    multi_clients,
    work_loads,
    TechVJBot
)

from TechVJ.server.exceptions import (
    FIleNotFound,
    InvalidHash
)

from TechVJ.util.custom_dl import ByteStreamer

from TechVJ.util.file_properties import (
    detect_audio_tracks
)

from TechVJ.util.hls_manager import (
    get_or_create_hls
)

from TechVJ.util.render_template import render_page


routes = web.RouteTableDef()

HLS_ROOT = Path("hls_cache")
HLS_ROOT.mkdir(parents=True, exist_ok=True)

class_cache = {}


# ============================================================
# ROOT
# ============================================================

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("BenFilterBot")


# ============================================================
# WATCH PAGE
# ============================================================

@routes.get(
    r"/watch/{path:\S+}",
    allow_head=True
)
async def watch_handler(request: web.Request):

    try:
        path = request.match_info["path"]

        match = re.search(
            r"^([a-zA-Z0-9_-]{6})(\d+)$",
            path
        )

        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))

        else:
            id_match = re.search(
                r"(\d+)(?:\/\S+)?",
                path
            )

            if not id_match:
                raise web.HTTPBadRequest(text="Invalid watch URL")

            id = int(id_match.group(1))

            secure_hash = request.rel_url.query.get("hash")

            if not secure_hash:
                raise web.HTTPForbidden(text="Hash required")

        return web.Response(
            text=await render_page(id, secure_hash),
            content_type="text/html"
        )

    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)

    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)

    except web.HTTPException:
        raise

    except (
        AttributeError,
        BadStatusLine,
        ConnectionResetError
    ):
        raise web.HTTPBadRequest(text="Invalid request")

    except Exception as e:
        logging.exception("Watch page error")

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# HLS FILES
# ============================================================

@routes.get(
    r"/hls/{file_id:\d+}/{filename:.+}",
    allow_head=True
)
async def hls_file_handler(request: web.Request):

    file_id = int(
        request.match_info["file_id"]
    )

    filename = request.match_info["filename"]

    base_dir = (
        HLS_ROOT / str(file_id)
    ).resolve()

    file_path = (
        base_dir / filename
    ).resolve()

    # Security: prevent ../ traversal
    try:
        file_path.relative_to(base_dir)

    except ValueError:
        raise web.HTTPForbidden(
            text="Invalid path"
        )

    if not file_path.exists():
        raise web.HTTPNotFound(
            text="HLS file not ready"
        )

    suffix = file_path.suffix.lower()

    if suffix == ".m3u8":

        content_type = (
            "application/vnd.apple.mpegurl"
        )

    elif suffix == ".ts":

        content_type = "video/mp2t"

    else:

        content_type = (
            mimetypes.guess_type(
                str(file_path)
            )[0]
            or "application/octet-stream"
        )

    return web.FileResponse(
        path=file_path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )


# ============================================================
# START HLS
# ============================================================

@routes.get(
    r"/hls-start/{id:\d+}",
    allow_head=True
)
async def hls_start_handler(request: web.Request):

    try:

        # ----------------------------------------------------
        # File ID
        # ----------------------------------------------------

        file_id = int(
            request.match_info["id"]
        )

        # ----------------------------------------------------
        # Hash
        # ----------------------------------------------------

        secure_hash = (
            request.rel_url.query.get("hash")
        )

        if not secure_hash:

            raise web.HTTPForbidden(
                text="Hash required"
            )

        # ----------------------------------------------------
        # Telegram client
        # ----------------------------------------------------

        index = min(
            work_loads,
            key=work_loads.get
        )

        faster_client = (
            multi_clients[index]
        )

        # ----------------------------------------------------
        # ByteStreamer
        # ----------------------------------------------------

        if faster_client in class_cache:

            tg_connect = (
                class_cache[faster_client]
            )

        else:

            tg_connect = ByteStreamer(
                faster_client
            )

            class_cache[
                faster_client
            ] = tg_connect

        # ----------------------------------------------------
        # Get file properties
        # ----------------------------------------------------

        file_data = await (
            tg_connect.get_file_properties(
                file_id
            )
        )

        # ----------------------------------------------------
        # Verify hash
        # ----------------------------------------------------

        if (
            file_data.unique_id[:6]
            != secure_hash
        ):

            raise InvalidHash

        # ----------------------------------------------------
        # Build source URL
        #
        # IMPORTANT:
        # filename MUST be URL encoded.
        # ----------------------------------------------------

        filename = (
            file_data.file_name
            or f"{file_id}.bin"
        )

        source_url = (
            URL.rstrip("/")
            + "/"
            + str(file_id)
            + "/"
            + quote_plus(filename)
            + "?hash="
            + quote_plus(secure_hash)
        )

        logging.info(
            "HLS source URL created for file %s",
            file_id
        )

        # ----------------------------------------------------
        # Detect audio tracks
        # ----------------------------------------------------

        audio_tracks = await detect_audio_tracks(
            source_url
        )

        logging.info(
            "Detected audio tracks for %s: %s",
            file_id,
            audio_tracks
        )

        if not audio_tracks:

            raise web.HTTPBadRequest(
                text="No audio tracks detected by FFmpeg"
            )

        # ----------------------------------------------------
        # Generate HLS
        # ----------------------------------------------------

        master = await get_or_create_hls(
            file_id,
            source_url,
            audio_tracks
        )

        if not master.exists():

            raise web.HTTPInternalServerError(
                text="HLS playlist was not created"
            )

        # ----------------------------------------------------
        # Master playlist URL
        # ----------------------------------------------------

        master_url = (
            URL.rstrip("/")
            + "/hls/"
            + str(file_id)
            + "/master.m3u8"
            + "?hash="
            + quote_plus(secure_hash)
        )

        # ----------------------------------------------------
        # Response
        # ----------------------------------------------------

        return web.json_response({
            "status": "ok",
            "master": master_url,
            "audio_tracks": audio_tracks
        })

    except InvalidHash:

        raise web.HTTPForbidden(
            text="Invalid hash"
        )

    except web.HTTPException:

        raise

    except Exception as e:

        logging.exception(
            "HLS start failed"
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
async def stream_handler(request: web.Request):

    try:

        path = request.match_info["path"]

        match = re.search(
            r"^([a-zA-Z0-9_-]{6})(\d+)$",
            path
        )

        if match:

            secure_hash = match.group(1)
            id = int(match.group(2))

        else:

            id_match = re.search(
                r"(\d+)(?:\/\S+)?",
                path
            )

            if not id_match:
                raise web.HTTPBadRequest(
                    text="Invalid file URL"
                )

            id = int(
                id_match.group(1)
            )

            secure_hash = (
                request.rel_url.query.get(
                    "hash"
                )
            )

            if not secure_hash:
                raise web.HTTPForbidden(
                    text="Hash required"
                )

        return await media_streamer(
            request,
            id,
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

    except web.HTTPException:

        raise

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
# BYTE STREAMER
# ============================================================

async def media_streamer(
    request: web.Request,
    id: int,
    secure_hash: str
):

    range_header = request.headers.get(
        "Range"
    )

    index = min(
        work_loads,
        key=work_loads.get
    )

    faster_client = (
        multi_clients[index]
    )

    if MULTI_CLIENT:

        logging.info(
            f"Client {index} is now serving "
            f"{request.remote}"
        )

    # --------------------------------------------------------
    # ByteStreamer cache
    # --------------------------------------------------------

    if faster_client in class_cache:

        tg_connect = (
            class_cache[faster_client]
        )

    else:

        tg_connect = ByteStreamer(
            faster_client
        )

        class_cache[
            faster_client
        ] = tg_connect

    # --------------------------------------------------------
    # File properties
    # --------------------------------------------------------

    file_id = await (
        tg_connect.get_file_properties(
            id
        )
    )

    # --------------------------------------------------------
    # Hash validation
    # --------------------------------------------------------

    if (
        file_id.unique_id[:6]
        != secure_hash
    ):

        raise InvalidHash

    file_size = file_id.file_size

    # --------------------------------------------------------
    # Range
    # --------------------------------------------------------

    if range_header:

        try:

            from_bytes, until_bytes = (
                range_header
                .replace("bytes=", "")
                .split("-")
            )

            from_bytes = int(
                from_bytes
            )

            until_bytes = (
                int(until_bytes)
                if until_bytes
                else file_size - 1
            )

        except ValueError:

            raise web.HTTPRequestRangeNotSatisfiable()

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
    # Range validation
    # --------------------------------------------------------

    if (
        until_bytes >= file_size
        or from_bytes < 0
        or until_bytes < from_bytes
    ):

        return web.Response(
            status=416,
            body=b"416: Range not satisfiable",
            headers={
                "Content-Range":
                    f"bytes */{file_size}"
            }
        )

    # --------------------------------------------------------
    # Chunk settings
    # --------------------------------------------------------

    chunk_size = 1024 * 1024

    until_bytes = min(
        until_bytes,
        file_size - 1
    )

    offset = (
        from_bytes
        - (from_bytes % chunk_size)
    )

    first_part_cut = (
        from_bytes - offset
    )

    last_part_cut = (
        until_bytes % chunk_size
        + 1
    )

    req_length = (
        until_bytes
        - from_bytes
        + 1
    )

    part_count = (
        math.ceil(
            until_bytes / chunk_size
        )
        - math.floor(
            offset / chunk_size
        )
    )

    # --------------------------------------------------------
    # Telegram stream
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
    # --------------------------------------------------------

    mime_type = file_id.mime_type
    file_name = file_id.file_name

    disposition = "attachment"

    if mime_type:

        if not file_name:

            try:

                extension = (
                    mime_type.split(
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
                    f"{secrets.token_hex(2)}"
                    ".unknown"
                )

    else:

        if file_name:

            mime_type = (
                mimetypes.guess_type(
                    file_name
                )[0]
                or "application/octet-stream"
            )

        else:

            mime_type = (
                "application/octet-stream"
            )

            file_name = (
                f"{secrets.token_hex(2)}"
                ".unknown"
            )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return web.Response(

        status=206 if range_header else 200,

        body=body,

        headers={

            "Content-Type":
                mime_type,

            "Content-Range":
                f"bytes "
                f"{from_bytes}-"
                f"{until_bytes}/"
                f"{file_size}",

            "Content-Length":
                str(req_length),

            "Content-Disposition":
                f'{disposition}; '
                f'filename="{file_name}"',

            "Accept-Ranges":
                "bytes",
        }
            )
