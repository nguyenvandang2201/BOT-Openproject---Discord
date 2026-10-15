import asyncio
import logging
import random
import sqlite3
import time

from .openproject import APIError

log = logging.getLogger(__name__)


class DeliveryError(Exception):
    def __init__(self, code="discord_error"):
        self.code = code


class Service:
    def __init__(self, config, store, api, send):
        self.config, self.store, self.api, self.send = config, store, api, send
        # Bounded locks serialize refresh, delivery and unlink for the same Discord user.
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.last_tick = time.time()

    def lock(self, discord_id):
        return self.locks[int(discord_id) % len(self.locks)]

    async def confirm(self, discord_id, code):
        async with self.lock(discord_id):
            pending = self.store.pending(discord_id, code)
            if not pending:
                raise ValueError("Mã xác nhận sai hoặc hết hạn. Chạy /op link lại.")
            tokens = pending["tokens"]
            if tokens["expires_at"] <= time.time() + 30:
                raise ValueError("Phiên xác thực hết hạn. Chạy /op link lại.")
            me = await self.api.me(tokens["access_token"])
            if me["id"] != pending["id"]:
                raise ValueError("Tài khoản không khớp. Chạy /op link lại.")
            # Baseline must succeed completely before the link becomes active.
            ids = await self.api.notifications(tokens["access_token"])
            try:
                self.store.activate(discord_id, code, ids)
            except sqlite3.IntegrityError:
                raise ValueError(
                    "Tài khoản OpenProject này đã được liên kết với Discord khác."
                ) from None
            return pending["name"]

    async def unlink(self, discord_id):
        async with self.lock(discord_id):
            self.store.unlink(discord_id)

    async def access_token(self, discord_id):
        account = self.store.account(discord_id)
        tokens = self.store.unseal(account["tokens"])
        if tokens["expires_at"] <= time.time() + 60:
            tokens = await self.api.refresh(tokens)
            # Persist rotated refresh token before any subsequent network operation.
            self.store.save_tokens(discord_id, tokens)
        return tokens["access_token"]

    async def poll_account(self, discord_id):
        async with self.lock(discord_id):
            account = self.store.account(discord_id)
            if not account or account["status"] != "active":
                return
            try:
                async with asyncio.timeout(120):
                    token = await self.access_token(discord_id)
                    ids = await self.api.notifications(token)
                    self.store.discover(discord_id, ids)
                    for job in self.store.jobs(discord_id, self.config.delivery_batch):
                        token = await self.access_token(discord_id)
                        nid = job["notification_id"]
                        try:
                            # Re-check visibility at delivery time, do not persist notification text.
                            notification = await self.api.notification(token, nid)
                        except APIError as error:
                            if error.status == 404:
                                self.store.finish(discord_id, nid, "unavailable")
                                continue
                            raise
                        try:
                            await self.send(discord_id, notification)
                        except DeliveryError as error:
                            delay = min(3600, 30 * 2 ** min(job["attempts"], 7))
                            self.store.retry(job, delay)
                            self.store.account_result(discord_id, error=error.code, delay=delay)
                            return  # Stop this user's batch, continue other users.
                        self.store.finish(discord_id, nid, "sent")
                    self.store.account_result(discord_id, delay=self.config.poll_interval)
            except APIError as error:
                permanent = error.status in (400, 401, 403, 404)
                delay = max(error.retry_after, min(3600, 30 * 2 ** min(account["failures"], 7)))
                self.store.account_result(
                    discord_id,
                    status="needs_attention" if permanent else "active",
                    error=f"openproject_http_{error.status}",
                    delay=delay + random.uniform(0, 5),
                )
                log.warning("account_poll_failed user=%s http=%s", discord_id, error.status)
            except Exception as error:
                # Exception bodies can contain OAuth codes, request URLs or tokens. Never log them.
                self.store.account_result(discord_id, error="protocol_or_internal_error", delay=300)
                log.error("account_poll_failed user=%s kind=%s", discord_id, type(error).__name__)

    async def run(self):
        while True:
            self.store.cleanup()
            for account in self.store.accounts():
                self.last_tick = time.time()
                await self.poll_account(account["discord_id"])
            self.last_tick = time.time()
            await asyncio.sleep(5)
