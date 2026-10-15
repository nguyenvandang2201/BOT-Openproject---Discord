import json
import logging

import discord
from discord import app_commands

from .channel_policy import channel_is_private
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


def dm_card(job, op_url):
    if job["kind"] == "welcome":
        embed = discord.Embed(
            title="👋 Chào mừng đến với OpenProject Bot",
            description=(
                f"Tài khoản OpenProject **ID {job['op_id']}** đã được liên kết với Discord. "
                "Từ bây giờ bạn sẽ nhận DM khi được giao công việc, công việc đổi trạng thái "
                "hoặc có bình luận công khai. Dùng `/op status` trong server để xem liên kết."
            ),
            color=0x2ECC71,
        )
        return embed, None
    labels = {
        "assignment": "📌 Giao việc",
        "status": "🔄 Đổi trạng thái",
        "comment": "💬 Bình luận mới",
    }
    url = f"{op_url}/work_packages/{job['work_package_id']}"
    meta = json.loads(job.get("metadata") or "{}")
    embed = discord.Embed(
        title=f"{labels[job['kind']]} · #{job['work_package_id']}",
        url=url,
        description=safe_text(job["subject"], 500),
        color={"assignment": 0x3498DB, "status": 0xF39C12, "comment": 0x9B59B6}[job["kind"]],
    )

    def field(label, value):
        embed.add_field(name=label, value=safe_text(value or "—", 500), inline=True)

    field("⚪ Trạng thái", meta.get("status"))
    field("🚨 Ưu tiên", meta.get("priority"))
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    field("📅 Hạn", meta.get("due_date"))
    field("📁 Dự án", job["project"])
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    assignee = meta.get("assignee") or f"ID {job['op_id']}"
    if job["kind"] == "assignment" and meta.get("previous_assignee"):
        assignee = f"{meta['previous_assignee']} → {assignee}"
    field("👤 Người được giao", assignee)
    field("✍️ Người báo cáo", meta.get("reporter"))
    if job["kind"] == "status" and meta.get("previous_status"):
        embed.add_field(
            name="🔄 Thay đổi",
            value=safe_text(f"{meta['previous_status']} → {meta['status']}", 500),
            inline=False,
        )
    if job["kind"] == "comment":
        embed.add_field(name="💬 Nội dung", value=safe_text(job["detail"], 500), inline=False)
    if job["actor"]:
        embed.add_field(name="🛠️ Cập nhật bởi", value=safe_text(job["actor"], 100), inline=False)
    embed.set_footer(text="OpenProject • Nhấn nút bên dưới để mở công việc")
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Mở trong OpenProject", url=url))
    return embed, view


class BridgeBot(discord.Client):
    def __init__(self, config, store, api):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self.config, self.store, self.api = config, store, api
        self.service = None
        self.tree = app_commands.CommandTree(self)
        group = app_commands.Group(name="op", description="Trạng thái thông báo OpenProject")
        self.tree.add_command(group, guild=discord.Object(id=config.guild_id))

        @group.command(name="status", description="Xem trạng thái liên kết và hàng đợi thông báo")
        async def status(interaction: discord.Interaction):
            if interaction.guild_id != self.config.guild_id:
                await interaction.response.send_message("Lệnh chỉ dùng trong server đã cấu hình.", ephemeral=True)
                return
            uid = str(interaction.user.id)
            if self.config.delivery_mode == "webhook_dm":
                link = self.store.dm_link_for_discord(uid)
                if link:
                    message = (
                        f"OpenProject ID: {link['op_id']}\n"
                        f"Thông báo DM: {'đang bật' if link['enabled'] else 'đã tạm dừng'}\n"
                        f"Đang chờ gửi: {self.store.dm_pending_count(uid)}"
                    )
                else:
                    message = "Chưa được quản lý liên kết OpenProject."
                await interaction.response.send_message(message, ephemeral=True)
                return
            account = self.store.account(uid)
            if not account:
                message = "Chưa được quản lý liên kết OpenProject."
            else:
                destination = (
                    f"<#{account['channel_id']}>" if account["channel_id"] else "chưa được gán"
                )
                message = (
                    f"OpenProject: {safe_text(account['name'], 200)} (ID {account['op_id']})\n"
                    f"Trạng thái: {account['status']}\n"
                    f"Channel: {destination}\n"
                    f"Đang chờ gửi: {self.store.pending_count(uid)}\n"
                    f"Lỗi gần nhất: {account['error'] or 'không có'}"
                )
                if account["last_poll"]:
                    message += f"\nLần lấy thông báo thành công: <t:{int(account['last_poll'])}:R>"
                if account["status"] == "needs_attention":
                    message += "\nLiên hệ quản lý để kiểm tra tài khoản và channel."
            await interaction.response.send_message(message, ephemeral=True)

        @group.command(name="admin", description="Mở trang quản lý liên kết OpenProject")
        async def admin(interaction: discord.Interaction):
            if interaction.guild_id != self.config.guild_id or not self.config.admin_url:
                await interaction.response.send_message("Trang quản lý chưa được bật.", ephemeral=True)
                return
            permissions = getattr(interaction.user, "guild_permissions", None)
            if not permissions or not permissions.manage_guild:
                await interaction.response.send_message("Bạn cần quyền Manage Server.", ephemeral=True)
                return
            ticket = self.store.admin_ticket(str(interaction.user.id))
            await interaction.response.send_message(
                f"Liên kết quản lý dùng một lần, có hiệu lực 5 phút:\n"
                f"{self.config.admin_url}/admin/login?ticket={ticket}",
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
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        await self.tree.sync(guild=discord.Object(id=self.config.guild_id))

    async def send_notification(self, discord_id, notification):
        account = self.store.account(discord_id)
        if not account or not account["channel_id"]:
            raise DeliveryError("channel_missing", permanent=True)
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise DeliveryError("guild_unavailable")
        try:
            await guild.fetch_member(int(discord_id))
        except discord.NotFound:
            raise DeliveryError("member_absent", permanent=True) from None
        except discord.HTTPException:
            raise DeliveryError() from None
        try:
            channel = await self.fetch_channel(int(account["channel_id"]))
        except discord.NotFound:
            raise DeliveryError("channel_missing", permanent=True) from None
        except discord.HTTPException:
            raise DeliveryError() from None
        if not isinstance(channel, discord.TextChannel):
            raise DeliveryError("channel_permissions_changed", permanent=True)
        overwrites = []
        for target, permissions in channel.overwrites.items():
            allow, deny = permissions.pair()
            overwrites.append(
                {
                    "id": str(target.id),
                    "type": 0 if isinstance(target, discord.Role) else 1,
                    "allow": str(allow.value),
                    "deny": str(deny.value),
                }
            )
        details = {
            "type": 0,
            "guild_id": channel.guild.id,
            "parent_id": channel.category_id,
            "permission_overwrites": overwrites,
        }
        if not channel_is_private(
            details, self.config.guild_id, self.config.category_id, int(discord_id), self.user.id
        ):
            raise DeliveryError("channel_permissions_changed", permanent=True)
        try:
            await channel.send(
                embed=notification_embed(notification, self.api),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            raise DeliveryError("channel_permissions_changed", permanent=True) from None
        except (discord.HTTPException, OSError):
            raise DeliveryError() from None

    async def send_dm(self, job):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise DeliveryError("guild_unavailable")
        try:
            member = await guild.fetch_member(int(job["discord_id"]))
        except discord.NotFound:
            raise DeliveryError("member_absent", permanent=True) from None
        except discord.HTTPException:
            raise DeliveryError() from None
        embed, view = dm_card(job, self.config.op_url)
        try:
            await member.send(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden:
            raise DeliveryError("dm_forbidden", permanent=True) from None
        except (discord.HTTPException, OSError):
            raise DeliveryError() from None

    async def is_manager(self, discord_id):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            return False
        try:
            member = await guild.fetch_member(int(discord_id))
        except (discord.NotFound, discord.HTTPException):
            return False
        return member.guild_permissions.manage_guild

    async def verify_member(self, discord_id):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise DeliveryError("guild_unavailable")
        try:
            return await guild.fetch_member(int(discord_id))
        except discord.NotFound:
            raise DeliveryError("member_absent", permanent=True) from None
        except discord.HTTPException:
            raise DeliveryError("discord_error") from None
