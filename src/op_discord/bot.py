import asyncio
import logging
from urllib.parse import urlencode

import discord
from discord import app_commands

from .openproject import ProtocolError
from .service import DeliveryError

log = logging.getLogger(__name__)


def safe_text(value, limit):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[:limit] or "—"


def notification_embed(notification, api):
    embedded = notification.get("_embedded", {})
    resource = embedded.get("resource") or {}
    links = notification.get("_links", {})
    title = notification.get("subject") or resource.get("subject") or "Thông báo OpenProject"
    url = api.config.op_url
    # Use only an HTML link actually supplied by OpenProject, never invent UI routes.
    for link in resource.get("_links", {}).values():
        if isinstance(link, dict) and link.get("type") == "text/html" and link.get("href"):
            try:
                url = api.safe_url(link["href"])
                break
            except ProtocolError:
                continue
    embed = discord.Embed(title=safe_text(title, 256), url=url, color=0x0077B6)
    for label, value in [
        ("Lý do", notification.get("reason", "notification")),
        ("Dự án", (links.get("project") or {}).get("title", "—")),
        ("Người thực hiện", (links.get("actor") or {}).get("title", "Hệ thống")),
    ]:
        embed.add_field(name=label, value=safe_text(value, 512), inline=False)
    embed.set_footer(text=f"OpenProject • Thông báo #{notification['id']}")
    return embed


class BridgeBot(discord.Client):
    def __init__(self, config, store, api):
        super().__init__(
            intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none()
        )
        self.config, self.store, self.api = config, store, api
        self.service = None
        self.tree = app_commands.CommandTree(self)
        group = app_commands.Group(name="op", description="Liên kết và nhận thông báo OpenProject")
        self.tree.add_command(group)

        @group.command(name="link", description="Liên kết tài khoản OpenProject bằng OAuth")
        @app_commands.checks.cooldown(1, 15, key=lambda i: i.user.id)
        async def link(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            if self.store.account(uid):
                await interaction.response.send_message(
                    "Bạn đã liên kết. Dùng /op status hoặc /op unlink trước khi liên kết lại.",
                    ephemeral=True,
                )
                return
            ticket = self.store.start_flow(uid)
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="Liên kết OpenProject",
                    url=(self.config.public_url + "/link?" + urlencode({"ticket": ticket})),
                )
            )
            await interaction.response.send_message(
                "Mở liên kết riêng này trong 10 phút, cấp quyền rồi xác nhận lại trong Discord. "
                "Bot sẽ gửi tiêu đề thông báo cá nhân qua DM; hãy bật nhận DM từ bot.",
                view=view,
                ephemeral=True,
            )

        @group.command(
            name="confirm", description="Xác nhận tài khoản vừa đăng nhập trên trình duyệt"
        )
        @app_commands.checks.cooldown(3, 60, key=lambda i: i.user.id)
        async def confirm(interaction: discord.Interaction, code: str):
            await interaction.response.defer(ephemeral=True)
            try:
                async with asyncio.timeout(120):
                    name = await self.service.confirm(str(interaction.user.id), code.strip())
                message = (
                    f"Đã liên kết với {safe_text(name, 200)}. Sẽ gửi các thông báo mới qua DM."
                )
            except ValueError as exc:
                message = str(exc)
            except Exception as exc:
                log.warning("confirm_failed kind=%s", type(exc).__name__)
                message = "Chưa liên kết được. Kiểm tra API/quyền truy cập rồi chạy /op link lại."
            await interaction.followup.send(message, ephemeral=True)

        @group.command(name="status", description="Xem trạng thái liên kết và hàng đợi DM")
        async def status(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            account = self.store.account(uid)
            if not account:
                message = "Chưa liên kết. Dùng /op link để bắt đầu."
            else:
                message = (
                    f"OpenProject: {safe_text(account['name'], 200)} (ID {account['op_id']})\n"
                    f"Trạng thái: {account['status']}\n"
                    f"Đang chờ gửi: {self.store.pending_count(uid)}\n"
                    f"Lỗi gần nhất: {account['error'] or 'không có'}"
                )
                if account["last_poll"]:
                    message += f"\nLần lấy thông báo thành công: <t:{int(account['last_poll'])}:R>"
                if account["status"] == "needs_attention":
                    message += "\nKiểm tra quyền/API, sau đó /op unlink và /op link lại."
                if account["error"] == "discord_dm_blocked":
                    message += "\nHãy bật DM từ bot. Bot sẽ tự thử lại."
            await interaction.response.send_message(message, ephemeral=True)

        @group.command(name="unlink", description="Dừng nhận DM, xóa token và liên kết trong bot")
        async def unlink(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.service.unlink(str(interaction.user.id))
            await interaction.followup.send(
                "Đã xóa liên kết, token và hàng đợi trong bot. "
                "Bạn có thể thu hồi quyền ứng dụng ở phần token OAuth trong OpenProject.",
                ephemeral=True,
            )

        @self.tree.error
        async def command_error(interaction, error):
            message = "Lệnh chưa thực hiện được. Vui lòng thử lại sau."
            if isinstance(error, app_commands.CommandOnCooldown):
                message = f"Vui lòng đợi {int(error.retry_after) + 1} giây rồi thử lại."
            log.warning("command_failed kind=%s", type(error).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self):
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def send_notification(self, discord_id, notification):
        try:
            user = await self.fetch_user(int(discord_id))
            await user.send(
                embed=notification_embed(notification, self.api),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            raise DeliveryError("discord_dm_blocked") from None
        except (discord.HTTPException, OSError):
            raise DeliveryError() from None
