import asyncio
import re

from pyrogram import Client
from typing import Any, Optional

from pyrogram.types import Message
from pyrogram.file_id import FileId
from pyrogram.raw.types.messages import Messages

from TechVJ.server.exceptions import FIleNotFound


AUDIO_LANGUAGE_MAP = {
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

    "spa": "Spanish",
    "es": "Spanish",

    "fra": "French",
    "fr": "French",

    "deu": "German",
    "ger": "German",
    "de": "German",

    "jpn": "Japanese",
    "ja": "Japanese",

    "kor": "Korean",
    "ko": "Korean",

    "ara": "Arabic",
    "ar": "Arabic",

    "rus": "Russian",
    "ru": "Russian",
}


async def parse_file_id(
    message: "Message"
) -> Optional[FileId]:

    media = get_media_from_message(
        message
    )

    if media:
        return FileId.decode(
            media.file_id
        )


async def parse_file_unique_id(
    message: "Messages"
) -> Optional[str]:

    media = get_media_from_message(
        message
    )

    if media:
        return media.file_unique_id


async def get_file_ids(
    client: Client,
    chat_id: int,
    id: int
) -> Optional[FileId]:

    message = await client.get_messages(
        chat_id,
        id
    )

    if message.empty:
        raise FIleNotFound

    media = get_media_from_message(
        message
    )

    file_unique_id = (
        await parse_file_unique_id(
            message
        )
    )

    file_id = await parse_file_id(
        message
    )

    setattr(
        file_id,
        "file_size",
        getattr(
            media,
            "file_size",
            0
        )
    )

    setattr(
        file_id,
        "mime_type",
        getattr(
            media,
            "mime_type",
            ""
        )
    )

    setattr(
        file_id,
        "file_name",
        getattr(
            media,
            "file_name",
            ""
        )
    )

    setattr(
        file_id,
        "unique_id",
        file_unique_id
    )

    return file_id


def get_media_from_message(
    message: "Message"
) -> Any:

    media_types = (
        "audio",
        "document",
        "photo",
        "sticker",
        "animation",
        "video",
        "voice",
        "video_note",
    )

    for attr in media_types:

        media = getattr(
            message,
            attr,
            None
        )

        if media:
            return media


def get_hash(
    media_msg: Message
) -> str:

    media = get_media_from_message(
        media_msg
    )

    return getattr(
        media,
        "file_unique_id",
        ""
    )[:6]


def get_name(
    media_msg: Message
) -> str:

    media = get_media_from_message(
        media_msg
    )

    return getattr(
        media,
        "file_name",
        ""
    )


def get_media_file_size(m):

    media = get_media_from_message(m)

    return getattr(
        media,
        "file_size",
        0
    )


# ============================================================
# AUDIO DETECTION
# ============================================================

async def detect_audio_tracks(
    source_url: str
):

    try:

        from imageio_ffmpeg import (
            get_ffmpeg_exe
        )

        ffmpeg = get_ffmpeg_exe()

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-i",
            source_url,

            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        output = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        tracks = []

        # Example:
        # Stream #0:1(eng): Audio:
        # Stream #0:2(hin): Audio:

        pattern = re.compile(
            r"Stream #0:(\d+)"
            r"(?:\(([^)]+)\))?"
            r": Audio:"
        )

        for match in pattern.finditer(
            output
        ):

            stream_number = int(
                match.group(1)
            )

            language_code = (
                match.group(2)
                or "und"
            ).lower()

            language = (
                AUDIO_LANGUAGE_MAP.get(
                    language_code,
                    language_code.upper()
                    if language_code != "und"
                    else f"Track {len(tracks) + 1}"
                )
            )

            # FFmpeg's 0:a:N uses audio
            # stream order, not absolute stream ID.
            tracks.append({
                "index": len(tracks),
                "stream": stream_number,
                "code": language_code,
                "language": language,
            })

        return tracks

    except Exception as e:

        print(
            "Audio detection error:",
            e
        )

        return []
