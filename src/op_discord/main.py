import asyncio
import logging
import time
from pathlib import Path

import httpx
import uvicorn
from filelock import FileLock

from .bot import BridgeBot
from .config import Config
from .openproject import OpenProject
from .service import Service
from .store import Store
from .web import create_app
from .webhook import WebhookService


async def run(config):
    store = Store(config.db_path, config.encryption_key)
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            api = OpenProject(config, http)
            bot = BridgeBot(config, store, api)
            service = (
                WebhookService(config, store, bot.send_dm)
                if config.delivery_mode == "webhook_dm"
                else Service(config, store, api, bot.send_notification)
            )
            bot.service = service

            async def worker():
                await bot.wait_until_ready()
                await service.run()

            worker_task = None
            app = create_app(
                store,
                api,
                ready=lambda: (
                    bot.is_ready()
                    and worker_task is not None
                    and not worker_task.done()
                    and time.time() - service.last_tick < 300
                ),
                webhook_secret=config.webhook_secret if config.delivery_mode == "webhook_dm" else "",
                admin_bot=bot if config.delivery_mode == "webhook_dm" else None,
                admin_url=config.admin_url if config.delivery_mode == "webhook_dm" else "",
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="0.0.0.0",
                    port=config.port,
                    access_log=False,
                    proxy_headers=False,
                    log_level="warning",
                )
            )
            async with bot:
                bot_task = asyncio.create_task(bot.start(config.discord_token))
                worker_task = asyncio.create_task(worker())
                web_task = asyncio.create_task(server.serve())
                tasks = [bot_task, worker_task, web_task]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    server.should_exit = True
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        store.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # These libraries otherwise log request URLs containing OAuth state/code/tickets.
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    config = Config.from_env()
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    # SQLite MVP is intentionally one process and one local volume.
    with FileLock(config.db_path + ".lock", timeout=0):
        try:
            asyncio.run(run(config))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            logging.error("service_stopped kind=%s", type(exc).__name__)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
