import jinja2
from info import *
from TechVJ.bot import TechVJBot
from TechVJ.util.human_readable import humanbytes
from TechVJ.util.file_properties import get_file_ids
from TechVJ.server.exceptions import InvalidHash
import urllib.parse
import logging


async def render_page(id, secure_hash, src=None):

    # Get Telegram message
    file = await TechVJBot.get_messages(
        int(LOG_CHANNEL),
        int(id)
    )

    # Get file properties
    file_data = await get_file_ids(
        TechVJBot,
        int(LOG_CHANNEL),
        int(id)
    )

    # Validate hash
    if file_data.unique_id[:6] != secure_hash:

        logging.debug(
            f"link hash: {secure_hash} - "
            f"{file_data.unique_id[:6]}"
        )

        logging.debug(
            f"Invalid hash for message with - ID {id}"
        )

        raise InvalidHash

    # Normal Telegram stream URL
    src = urllib.parse.urljoin(
        URL,
        f"{id}/{urllib.parse.quote_plus(file_data.file_name)}"
        f"?hash={secure_hash}",
    )

    # Detect media type
    mime_type = file_data.mime_type or ""

    tag = mime_type.split("/")[0].strip()

    file_size = humanbytes(
        file_data.file_size
    )

    # Select template
    if tag in ["video", "audio"]:

        template_file = (
            "TechVJ/template/req.html"
        )

    else:

        template_file = (
            "TechVJ/template/dl.html"
        )

    # Clean filename for display
    file_name = (
        file_data.file_name or "Unknown File"
    ).replace("_", " ")

    # ---------------------------------------------------------
    # AUDIO INFORMATION
    # ---------------------------------------------------------

    # This will be populated by the HLS/audio system later.
    #
    # We keep it empty for now so the existing streaming system
    # continues working normally.

    audio_tracks = []

    audio_type = "Unknown"

    # ---------------------------------------------------------
    # LOAD TEMPLATE
    # ---------------------------------------------------------

    try:

        with open(
            template_file,
            encoding="utf-8"
        ) as f:

            template = jinja2.Template(
                f.read()
            )

    except FileNotFoundError:

        logging.exception(
            "Template file not found: %s",
            template_file
        )

        raise

    # ---------------------------------------------------------
    # RENDER PAGE
    # ---------------------------------------------------------

    return template.render(

        # Existing variables
        file_name=file_name,
        file_url=src,
        file_size=file_size,
        file_unique_id=file_data.unique_id,

        # New audio variables
        audio_tracks=audio_tracks,
        audio_type=audio_type,

        # File information
        file_id=id,
        secure_hash=secure_hash,
    )
