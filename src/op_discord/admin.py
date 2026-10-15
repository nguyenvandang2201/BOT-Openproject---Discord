"""Offline administration. Stop the bridge before running this command."""

import argparse
import asyncio
import csv
import getpass
import sqlite3
import sys
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from .channel_policy import VIEW_CHANNEL, channel_is_private, private_overwrites
from .config import Config
from .openproject import APIError, OpenProject, ProtocolError
from .store import Store


class AdminError(Exception):
    pass


class DiscordAdmin:
    def __init__(self, config, http):
        self.config = config
        self.http = http

    async def request(self, method, path, *, payload=None):
        try:
            response = await self.http.request(
                method,
                "https://discord.com/api/v10" + path,
                headers={"Authorization": "Bot " + self.config.discord_token},
                json=payload,
                follow_redirects=False,
                timeout=20,
            )
        except httpx.RequestError:
            raise AdminError("Discord request failed") from None
        if not 200 <= response.status_code < 300:
            raise AdminError(f"Discord HTTP {response.status_code} on {method} {path}")
        return response.json() if response.content else None

    async def bot_id(self):
        result = await self.request("GET", "/users/@me")
        return int(result["id"])

    async def member(self, user_id):
        return await self.request(
            "GET", f"/guilds/{self.config.guild_id}/members/{user_id}"
        )

    async def category(self):
        result = await self.request("GET", f"/channels/{self.config.category_id}")
        if result.get("type") != 4 or int(result.get("guild_id", 0)) != self.config.guild_id:
            raise AdminError("DISCORD_CATEGORY_ID is not a category in the configured server")

    async def channel(self, channel_id):
        return await self.request("GET", f"/channels/{channel_id}")

    async def create_channel(self, user_id, bot_id):
        return await self.request(
            "POST",
            f"/guilds/{self.config.guild_id}/channels",
            payload={
                "name": f"op-{user_id}",
                "type": 0,
                "parent_id": str(self.config.category_id),
                "permission_overwrites": private_overwrites(
                    self.config.guild_id, user_id, bot_id
                ),
            },
        )

    async def revoke_channel_access(self, channel_id, user_id):
        await self.request(
            "PUT",
            f"/channels/{channel_id}/permissions/{user_id}",
            payload={"allow": "0", "deny": str(VIEW_CHANNEL), "type": 1},
        )


def user_id(value):
    result = int(value)
    if result < 1 or len(value) > 24:
        raise argparse.ArgumentTypeError("Expected a positive Discord ID")
    return result


def parser():
    result = argparse.ArgumentParser(description="Manage private OpenProject channels while bot is stopped")
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    commands.add_parser("audit-channels")
    commands.add_parser("dm-list")
    commands.add_parser("dm-errors")
    commands.add_parser("dm-seed")
    dm_link = commands.add_parser("dm-link")
    dm_link.add_argument("--op-id", required=True, type=user_id)
    dm_link.add_argument("--discord-id", required=True, type=user_id)
    dm_link.add_argument("--name", default="")
    for action in ("dm-unlink", "dm-suspend", "dm-resume", "dm-retry"):
        item = commands.add_parser(action)
        item.add_argument("--op-id", required=True, type=user_id)
    for action in ("link", "migrate", "suspend", "resume", "unlink"):
        item = commands.add_parser(action)
        item.add_argument("--discord-id", required=True, type=user_id)
    return result


def show_accounts(store):
    for account in store.list_accounts():
        print(
            f"discord={account['discord_id']} op={account['op_id']} "
            f"status={account['status']} channel={account['channel_id'] or '-'} "
            f"pending={store.pending_count(account['discord_id'])} "
            f"error={account['error'] or '-'}"
        )


async def checked_channel(discord_api, config, user, bot_id, channel_id):
    channel = await discord_api.channel(channel_id)
    if not channel_is_private(channel, config.guild_id, config.category_id, user, bot_id):
        raise AdminError(f"Channel {channel_id} does not match private permission policy")
    return channel


async def execute(config, store, action, discord_id=None, *, op_id=None, name="", transport=None):
    if action == "list":
        show_accounts(store)
        return
    if action == "dm-list":
        for link in store.dm_links():
            print(
                f"op={link['op_id']} discord={link['discord_id']} "
                f"enabled={bool(link['enabled'])} name={link['name']} "
                f"pending={store.dm_pending_count(link['discord_id'])}"
            )
        return
    if action == "dm-errors":
        for job in store.dm_failures():
            print(
                f"op={job['op_id']} discord={job['discord_id']} "
                f"event={job['event_key']} error={job['error']}"
            )
        return
    if action == "dm-retry":
        print(f"Queued {store.dm_retry_failed(op_id)} failed DM jobs for OpenProject {op_id}")
        return
    if action == "dm-seed":
        def rows():
            for line in csv.reader(sys.stdin):
                if len(line) != 4:
                    raise AdminError("Expected CSV: work_package_id,assignee_id,status_id,lock_version")
                work_id, assignee, status, version = line
                values = (int(work_id), int(assignee) if assignee else None,
                          int(status), int(version))
                if values[0] < 1 or values[2] < 1 or values[3] < 0 or (
                    values[1] is not None and values[1] < 1
                ):
                    raise AdminError("Invalid work package snapshot")
                yield values
        count = store.dm_seed_states(rows())
        print(f"Seeded {count} work package snapshots")
        return
    if action in ("dm-unlink", "dm-suspend", "dm-resume"):
        changed = (
            store.dm_unlink(op_id) if action == "dm-unlink" else
            store.dm_set_enabled(op_id, action == "dm-resume")
        )
        if not changed:
            raise AdminError("OpenProject ID is not linked")
        print(f"{action} OpenProject {op_id}")
        return

    async with httpx.AsyncClient(trust_env=False, transport=transport) as http:
        discord_api = DiscordAdmin(config, http)
        if action == "dm-link":
            await discord_api.member(discord_id)
            store.dm_link(op_id, str(discord_id), name[:200])
            print(f"Linked OpenProject {op_id} to Discord {discord_id} for DM")
            return
        bot_id = await discord_api.bot_id()
        if action == "audit-channels":
            for account in store.list_accounts():
                channel_id = account["channel_id"]
                if not channel_id:
                    print(f"discord={account['discord_id']} channel=missing")
                    continue
                try:
                    await discord_api.member(int(account["discord_id"]))
                    await checked_channel(
                        discord_api, config, int(account["discord_id"]), bot_id, channel_id
                    )
                    print(f"discord={account['discord_id']} channel={channel_id} OK")
                except AdminError:
                    print(f"discord={account['discord_id']} channel={channel_id} NEEDS_ATTENTION")
            return

        uid = str(discord_id)
        account = store.account(uid)
        if action in ("link", "migrate"):
            if action == "link" and account:
                raise AdminError("Discord ID is already linked")
            if action == "migrate" and (not account or account["status"] != "migration_pending"):
                raise AdminError("Account is not awaiting migration")
            await discord_api.member(discord_id)
            await discord_api.category()
            op_id = name = key = ids = None
            if action == "link":
                key = getpass.getpass("OpenProject personal API token (hidden): ").strip()
                if not key:
                    raise AdminError("API token is required")
                op_api = OpenProject(config, http)
                auth = httpx.BasicAuth("apikey", key)
                me = await op_api.me(auth)
                op_id, name = me["id"], str(me.get("name", me["id"]))
                if any(row["op_id"] == op_id for row in store.list_accounts()):
                    raise AdminError("OpenProject account is already linked")
                ids = await op_api.notifications(auth)
            created = await discord_api.create_channel(discord_id, bot_id)
            channel_id = created.get("id")
            if not channel_id:
                raise AdminError("Discord did not return a channel ID")
            try:
                await checked_channel(discord_api, config, discord_id, bot_id, channel_id)
                if action == "link":
                    store.provision_api_key(
                        uid, op_id, name, key, str(config.guild_id), str(channel_id), ids
                    )
                else:
                    store.assign_channel(uid, str(config.guild_id), str(channel_id))
            except Exception:
                print(f"Channel {channel_id} was created but not linked; inspect it manually")
                raise
            print(f"Linked Discord {uid} to channel {channel_id}")
            return

        if not account:
            raise AdminError("Discord ID is not linked")
        if action == "suspend":
            store.suspend(uid)
            print(f"Suspended Discord {uid}")
        elif action == "resume":
            await discord_api.member(discord_id)
            await checked_channel(discord_api, config, discord_id, bot_id, account["channel_id"])
            store.resume(uid)
            print(f"Resumed Discord {uid}")
        elif action == "unlink":
            if account["channel_id"]:
                await checked_channel(
                    discord_api, config, discord_id, bot_id, account["channel_id"]
                )
                await discord_api.revoke_channel_access(account["channel_id"], discord_id)
            store.unlink(uid)
            print(f"Unlinked Discord {uid}; channel history was retained")


def main():
    arguments = parser().parse_args()
    try:
        config = Config.from_env()
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
        with FileLock(config.db_path + ".lock", timeout=0):
            store = Store(config.db_path, config.encryption_key)
            try:
                asyncio.run(execute(
                    config, store, arguments.action,
                    getattr(arguments, "discord_id", None),
                    op_id=getattr(arguments, "op_id", None),
                    name=getattr(arguments, "name", ""),
                ))
            finally:
                store.close()
    except (AdminError, APIError, ProtocolError, Timeout, sqlite3.Error, ValueError) as error:
        raise SystemExit(f"Admin operation failed: {type(error).__name__}: {error}") from None


if __name__ == "__main__":
    main()
