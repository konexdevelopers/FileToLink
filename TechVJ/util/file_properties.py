from pyrogram import Client
from typing import Any, Optional
from pyrogram.types import Message
from pyrogram.file_id import FileId
from pyrogram.raw.types.messages import Messages
from TechVJ.server.exceptions import FIleNotFound

import json
import subprocess
import imageio_ffmpeg


# ============================================================
# AUDIO LANGUAGE MAP
# ============================================================

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

    "ori": "Odia",
    "or": "Odia",

    "asm": "Assamese",
    "as": "Assamese",

    "urd": "Urdu",
    "ur": "Urdu",

    "nep": "Nepali",
    "ne": "Nepali",

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

    "chi": "Chinese",
    "zho": "Chinese",
    "zh": "Chinese",

    "rus": "Russian",
    "ru": "Russian",

    "ara": "Arabic",
    "ar": "Arabic",
}


# ============================================================
# BASIC TELEGRAM FILE FUNCTIONS
# ============================================================

async def parse_file_id(message: "Message") -> Optional[FileId]:
    media = get_media_from_message(message)

    if media:
        return FileId.decode(media.file_id)

    return None


async def parse_file_unique_id(message: "Messages") -> Optional[str]:
    media = get_media_from_message(message)

    if media:
        return media.file_unique_id

    return None


async def get_file_ids(
    client: Client,
    chat_id: int,
    id: int
) -> Optional[FileId]:

    message = await client.get_messages(chat_id, id)

    if message.empty:
        raise FIleNotFound

    media = get_media_from_message(message)

    if not media:
        raise FIleNotFound

    file_unique_id = await parse_file_unique_id(message)
    file_id = await parse_file_id(message)

    if not file_id:
        raise FIleNotFound

    setattr(
        file_id,
        "file_size",
        getattr(media, "file_size", 0)
    )

    setattr(
        file_id,
        "mime_type",
        getattr(media, "mime_type", "")
    )

    setattr(
        file_id,
        "file_name",
        getattr(media, "file_name", "")
    )

    setattr(
        file_id,
        "unique_id",
        file_unique_id
    )

    return file_id


def get_media_from_message(message: "Message") -> Any:

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

        media = getattr(message, attr, None)

        if media:
            return media

    return None


def get_hash(media_msg: Message) -> str:

    media = get_media_from_message(media_msg)

    return getattr(
        media,
        "file_unique_id",
        ""
    )[:6]


def get_name(media_msg: Message) -> str:

    media = get_media_from_message(media_msg)

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
# AUDIO LANGUAGE FUNCTIONS
# ============================================================

def normalize_audio_language(code):

    if not code:
        return None

    code = str(code).lower().strip()

    return AUDIO_LANGUAGE_MAP.get(
        code,
        code.upper()
    )


def detect_audio_tracks(file_path):

    """
    Detect audio tracks using FFprobe.

    Example:

    1 track:
        Single Audio
        English

    2 tracks:
        Dual Audio
        English + Hindi

    3+ tracks:
        Multi Audio
        English + Hindi + Tamil
    """

    try:

        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

        # imageio-ffmpeg normally provides ffmpeg.
        # Try to locate ffprobe beside it.
        ffprobe_path = ffmpeg_path.replace(
            "ffmpeg",
            "ffprobe"
        )

        command = [
            ffprobe_path,

            "-v",
            "error",

            "-select_streams",
            "a",

            "-show_entries",
            "stream=index:stream_tags=language,title",

            "-of",
            "json",

            str(file_path)
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return {
                "type": "Unknown",
                "count": 0,
                "tracks": []
            }

        data = json.loads(
            result.stdout or "{}"
        )

        streams = data.get(
            "streams",
            []
        )

        tracks = []

        for index, stream in enumerate(
            streams,
            start=1
        ):

            tags = stream.get(
                "tags",
                {}
            )

            language = normalize_audio_language(
                tags.get("language")
            )

            title = tags.get(
                "title"
            )

            display_language = (
                language
                or title
                or f"Track {index}"
            )

            tracks.append({
                "index": index,
                "language": display_language
            })

        count = len(tracks)

        if count == 0:

            audio_type = "Unknown"

        elif count == 1:

            audio_type = "Single Audio"

        elif count == 2:

            audio_type = "Dual Audio"

        else:

            audio_type = "Multi Audio"

        return {
            "type": audio_type,
            "count": count,
            "tracks": tracks
        }

    except Exception:

        return {
            "type": "Unknown",
            "count": 0,
            "tracks": []
        }
