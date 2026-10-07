import re
import math
import logging
import hmac
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

from TechVJ.util.hls_manager import (
    HlsError,
    get_manager
)

from TechVJ.util.render_template import render_page


routes = web.RouteTableDef()

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
# AUDIO TRACKS / HLS (new)
#
# GET /audio-info/{id}?hash=H              -> detected audio tracks (no processing started)
# GET /hls-start/{id}?hash=H               -> starts (or reuses) the HLS job, returns the master URL
# GET /hls/{id}/{hash}/{playlist|segment}  -> serves the generated files
#
# Every endpoint validates the SAME file id + hash pair as the normal download link
# (unique_id[:6]). FFmpeg only ever reads our own loopback stream, never a URL from a request.
# ============================================================

# Only these generated names can be served: master / video / audio_N playlists and their .ts segments
HLS_NAME_RE = re.compile(
    r"^(?:master|video|audio_\d{1,2})\.m3u8$"
    r"|^(?:video|audio_\d{1,2})_\d{1,6}\.ts$"
)

PUBLIC_TRACK_FIELDS = (
    "index", "label", "language", "code", "title", "codec",
    "channels", "channel_layout", "default",
)


def _json(data: dict, status: int = 200, headers: dict = None) -> web.Response:
    merged = {"Cache-Control": "no-store"}
    merged.update(headers or {})
    return web.json_response(data, status=status, headers=merged)


def _public_tracks(tracks: list) -> list:
    return [{key: track.get(key) for key in PUBLIC_TRACK_FIELDS} for track in tracks]


def _get_streamer() -> ByteStreamer:
    index = min(work_loads, key=work_loads.get)
    client = multi_clients[index]
    if client not in class_cache:
        class_cache[client] = ByteStreamer(client)
    return class_cache[client]


async def _verified_file(file_id: int, secure_hash: str):
    """Same check as the normal stream: hash must equal file_unique_id[:6] (constant-time compare)."""
    file_data = await _get_streamer().get_file_properties(file_id)
    expected = str(file_data.unique_id or "")[:6].encode()
    if not secure_hash or not hmac.compare_digest(expected, secure_hash.encode("utf-8", "ignore")):
        raise InvalidHash
    return file_data


def _local_source(file_id: int, secure_hash: str) -> str:
    """The server's own stream (supports Range). Built here from validated values only."""
    return f"http://127.0.0.1:{int(PORT)}/{secure_hash}{file_id}"


def _error_response(error: Exception) -> web.Response:
    if isinstance(error, InvalidHash):
        return _json({"status": "error", "code": "invalid_hash", "message": "Invalid hash"}, 403)
    if isinstance(error, FIleNotFound):
        return _json({"status": "error", "code": "not_found", "message": "File not found"}, 404)
    if isinstance(error, HlsError):
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return _json({"status": "error", "code": error.code, "message": error.message}, error.status, headers)
    logging.exception("Audio/HLS endpoint error")
    return _json({"status": "error", "code": "internal", "message": "Something went wrong"}, 500)


@routes.get(r"/audio-info/{id:\d+}")
async def audio_info_handler(request: web.Request):
    """Detects the REAL audio tracks of a file. Starts no FFmpeg conversion."""
    try:
        file_id = int(request.match_info["id"])
        secure_hash = request.rel_url.query.get("hash", "")
        file_data = await _verified_file(file_id, secure_hash)

        if not (file_data.mime_type or "").startswith(("video/", "audio/")):
            return _json({"status": "ok", "audio_tracks": [], "audio_count": 0, "hls": {"available": False}})

        manager = get_manager()
        probe = await manager.probe(file_id, _local_source(file_id, secure_hash))
        plan = manager.plan(probe, file_data.file_size or 0)

        logging.info(
            "Audio info | File ID: %s | Detected audio tracks: %s",
            file_id, [track["label"] for track in probe.audio],
        )
        video = probe.video
        return _json({
            "status": "ok",
            "audio_tracks": _public_tracks(probe.audio),
            "audio_count": len(probe.audio),
            "video": {"codec": video.codec, "width": video.width, "height": video.height} if video else None,
            "hls": {
                "available": plan["available"] and len(probe.audio) > 1,
                "mode": plan["mode"],
                "reason": plan["problem"][1] if plan["problem"] else None,
                "autostart": manager.cfg.autostart,
            },
        })
    except Exception as error:
        return _error_response(error)


@routes.get(r"/hls-start/{id:\d+}")
async def hls_start_handler(request: web.Request):
    """Starts (or reuses) the HLS job for a file and returns the master playlist URL."""
    try:
        file_id = int(request.match_info["id"])
        secure_hash = request.rel_url.query.get("hash", "")
        logging.info("HLS start requested | File ID: %s", file_id)

        file_data = await _verified_file(file_id, secure_hash)
        if not (file_data.mime_type or "").startswith("video/"):
            raise HlsError("unsupported", "Audio switching is only available for video files", 422)

        manager = get_manager()
        source = _local_source(file_id, secure_hash)
        probe = await manager.probe(file_id, source)
        logging.info(
            "Detected audio tracks: %s",
            [track["label"] for track in probe.audio],
        )
        job = await manager.get_or_start(file_id, secure_hash, source, probe, file_data.file_size or 0)

        return _json({
            "status": "ok",
            # relative + hash in the PATH, so every playlist/segment URL inside inherits it
            "master": f"/hls/{file_id}/{secure_hash}/master.m3u8",
            "audio_tracks": _public_tracks(job.tracks),
            "mode": job.mode,
            "complete": job.state == "complete",
        })
    except Exception as error:
        if isinstance(error, HlsError):
            logging.warning("HLS generation failed: %s (%s)", error.message, error.code)
        return _error_response(error)


@routes.get(
    r"/hls/{file_id:\d+}/{secure_hash:[A-Za-z0-9_\-]{6}}/{name}",
    allow_head=True
)
async def hls_file_handler(request: web.Request):
    """Serves a generated playlist/segment, only to a caller that knows the file's hash."""
    try:
        file_id = int(request.match_info["file_id"])
        secure_hash = request.match_info["secure_hash"]
        name = request.match_info["name"]

        # 1) strict whitelist of names: nothing else is ever opened
        if not HLS_NAME_RE.fullmatch(name):
            raise web.HTTPNotFound(text="Not found")

        # 2) same file id + hash validation as the normal download
        await _verified_file(file_id, secure_hash)

        # 3) the file must belong to a live job of exactly this file
        job = get_manager().get_job(file_id, secure_hash)
        if job is None:
            raise web.HTTPNotFound(text="HLS not ready")

        # 4) defence in depth: resolved path must stay inside the job directory
        base_dir = job.directory.resolve()
        file_path = (base_dir / name).resolve()
        try:
            file_path.relative_to(base_dir)
        except ValueError:
            raise web.HTTPForbidden(text="Invalid path")
        if not file_path.is_file():
            raise web.HTTPNotFound(text="HLS file not ready")

        get_manager().note_access(job, name)

        if name.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
            cache_control = "no-store"  # playlists grow while FFmpeg is still running
        else:
            content_type = "video/mp2t"
            cache_control = "private, max-age=600"

        return web.FileResponse(
            path=file_path,
            headers={
                "Content-Type": content_type,
                "Cache-Control": cache_control,
                "X-Content-Type-Options": "nosniff",
            }
        )

    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)

    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)

    except web.HTTPException:
        raise

    except Exception:
        logging.exception("HLS file error")
        raise web.HTTPInternalServerError(text="Something went wrong")


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
