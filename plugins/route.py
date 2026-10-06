import re
import math
import logging
import secrets
import mimetypes
from pathlib import Path

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
    get_file_ids,
    detect_audio_tracks
)

from TechVJ.util.hls_manager import (
    get_or_create_hls
)

from TechVJ.util.render_template import render_page


routes = web.RouteTableDef()

HLS_ROOT = Path("hls_cache")


# ============================================================
# ROOT
# ============================================================

@routes.get("/", allow_head=True)
async def root_route_handler(request):

    return web.json_response(
        "BenFilterBot"
    )


# ============================================================
# WATCH PAGE
# ============================================================

@routes.get(
    r"/watch/{path:\S+}",
    allow_head=True
)
async def watch_handler(
    request: web.Request
):

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

            id = int(
                re.search(
                    r"(\d+)(?:\/\S+)?",
                    path
                ).group(1)
            )

            secure_hash = (
                request.rel_url.query.get(
                    "hash"
                )
            )

        return web.Response(
            text=await render_page(
                id,
                secure_hash
            ),
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

        pass

    except Exception as e:

        logging.critical(
            e.with_traceback(None)
        )

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
async def hls_file_handler(
    request: web.Request
):

    file_id = int(
        request.match_info["file_id"]
    )

    filename = request.match_info[
        "filename"
    ]

    base_dir = (
        HLS_ROOT / str(file_id)
    ).resolve()

    file_path = (
        base_dir / filename
    ).resolve()

    # Security
    try:

        file_path.relative_to(
            base_dir
        )

    except ValueError:

        raise web.HTTPForbidden(
            text="Invalid path"
        )

    if not file_path.exists():

        raise web.HTTPNotFound(
            text="HLS file not ready"
        )

    if file_path.suffix.lower() == ".m3u8":

        content_type = (
            "application/vnd.apple.mpegurl"
        )

    elif file_path.suffix.lower() == ".ts":

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
        }
    )


# ============================================================
# START HLS
# ============================================================

@routes.get(
    r"/hls-start/{id:\d+}",
    allow_head=True
)
async def hls_start_handler(
    request: web.Request
):

    try:

        file_id = int(
            request.match_info["id"]
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

        # Telegram client
        index = min(
            work_loads,
            key=work_loads.get
        )

        faster_client = (
            multi_clients[index]
        )

        # ByteStreamer
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

        file_data = await (
            tg_connect.get_file_properties(
                file_id
            )
        )

        if (
            file_data.unique_id[:6]
            != secure_hash
        ):

            raise InvalidHash

        # Source URL
        source_url = (
            URL
            + f"{file_id}/"
            + f"{file_data.file_name}"
            + f"?hash={secure_hash}"
        )

        # Detect audio
        audio_tracks = (
            await detect_audio_tracks(
                source_url
            )
        )

        if not audio_tracks:

            raise web.HTTPBadRequest(
                text="No audio tracks found"
            )

        # Generate HLS
        master = await (
            get_or_create_hls(
                file_id,
                source_url,
                audio_tracks
            )
        )

        master_url = (
            URL.rstrip("/")
            + f"/hls/{file_id}/master.m3u8"
            + f"?hash={secure_hash}"
        )

        return web.json_response({
            "status": "ok",
            "master": master_url,
            "audio_tracks": audio_tracks
        })

    except InvalidHash:

        raise web.HTTPForbidden(
            text="Invalid hash"
        )

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
async def stream_handler(
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
            id = int(match.group(2))

        else:

            id = int(
                re.search(
                    r"(\d+)(?:\/\S+)?",
                    path
                ).group(1)
            )

            secure_hash = (
                request.rel_url.query.get(
                    "hash"
                )
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

    except (
        AttributeError,
        BadStatusLine,
        ConnectionResetError
    ):

        pass

    except Exception as e:

        logging.critical(
            e.with_traceback(None)
        )

        raise web.HTTPInternalServerError(
            text=str(e)
        )


# ============================================================
# BYTE STREAMER
# ============================================================

class_cache = {}


async def media_streamer(
    request: web.Request,
    id: int,
    secure_hash: str
):

    range_header = request.headers.get(
        "Range",
        0
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

    file_id = await (
        tg_connect.get_file_properties(
            id
        )
    )

    if (
        file_id.unique_id[:6]
        != secure_hash
    ):

        raise InvalidHash

    file_size = file_id.file_size

    if range_header:

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

    else:

        from_bytes = (
            request.http_range.start
            or 0
        )

        until_bytes = (
            request.http_range.stop
            or file_size
        ) - 1

    if (
        until_bytes >= file_size
        or from_bytes < 0
        or until_bytes < from_bytes
    ):

        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={
                "Content-Range":
                    f"bytes */{file_size}"
            }
        )

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

    body = tg_connect.yield_file(
        file_id,
        index,
        offset,
        first_part_cut,
        last_part_cut,
        part_count,
        chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name

    disposition = "attachment"

    if mime_type:

        if not file_name:

            try:

                file_name = (
                    f"{secrets.token_hex(2)}."
                    f"{mime_type.split('/')[1]}"
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
                    file_id.file_name
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
