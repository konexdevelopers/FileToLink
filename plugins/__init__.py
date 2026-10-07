# Don't Remove Credit @VJ_Botz
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

from aiohttp import web
from .route import routes
from TechVJ.util.hls_manager import get_manager


async def _hls_startup(app):
    # wipes leftovers of earlier runs and starts the idle-cleanup loop
    await get_manager().startup()


async def _hls_cleanup(app):
    # stops every FFmpeg child process and deletes the cache
    await get_manager().shutdown()

async def web_server():
    web_app = web.Application(client_max_size=30000000)
    web_app.add_routes(routes)
    web_app.on_startup.append(_hls_startup)
    web_app.on_cleanup.append(_hls_cleanup)
    return web_app
