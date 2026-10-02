import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from bot import Ctx, build_router
from checkers import ApifyChecker, Pipeline, ProbeChecker
from config import load
from search import Searcher
from storage import ChannelPersistence, Store


def build_checkers(cfg, store):
    out = []
    for name in cfg.checkers:
        if name == "probe":
            out.append(ProbeChecker(cfg.proxies_file))
        elif name == "apify":
            out.append(ApifyChecker(cfg.apify_token, cfg.apify_actor, cfg.apify_monthly_budget, store))
    return out


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN yo'q")
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    store = Store(cfg.superadmins)
    persist = ChannelPersistence(bot, cfg.db_channel_id, store)
    started = time.time()

    async def health(_):
        return web.json_response({"status": "ok", "uptime_s": int(time.time() - started), "users": len(store.d["users"])})

    # Avval health server (Render tekshiruvi), keyin eski nusxa to'xtashini kutib, snapshotni yuklaymiz
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health)])  # GET va HEAD ikkalasi ishlaydi
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    if cfg.startup_grace:
        logging.info("Eski nusxa o'z snapshotini yozib bo'lishini kutyapman: %ss", cfg.startup_grace)
        await asyncio.sleep(cfg.startup_grace)
    logging.info("DB manbasi: %s", await persist.load())
    checkers = build_checkers(cfg, store)
    pipeline = Pipeline(checkers)
    ctx = Ctx(cfg, store, persist, pipeline, Searcher(cfg, store, pipeline), checkers)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(ctx))

    task = asyncio.create_task(persist.run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await persist.flush()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
