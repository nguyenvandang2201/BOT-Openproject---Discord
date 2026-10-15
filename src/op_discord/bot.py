import json
import logging
import re

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
    if job["kind"] == "test":
        return discord.Embed(
            title="🧪 Kiểm tra thông báo OpenProject",
            description="DM thử từ trang quản lý. Liên kết Discord của bạn đang nhận được tin nhắn.",
            color=0x2ECC71,
        ), None
    if job["kind"] == "welcome":
        embed = discord.Embed(
            title="👋 Chào mừng đến với OpenProject Bot",
            description=(
                f"Tài khoản OpenProject **ID {job['op_id']}** đã được liên kết với Discord. "
                "Từ bây giờ bạn sẽ nhận DM khi được giao công việc, công việc đổi trạng thái "
                "hoặc có bình luận công khai. Dùng `/op status` và `/op notify` trong server "
                "để xem liên kết và tùy chọn thông báo."
            ),
            color=0x2ECC71,
        )
        return embed, None
    if job["kind"] == "digest":
        entries = json.loads(job.get("metadata") or "{}").get("entries", [])
        grouped = {}
        kind_labels = {
            "assignment": "Giao việc", "status": "Trạng thái", "comment": "Bình luận",
            "created": "Tạo mới", "summary": "Cập nhật", "details": "Chi tiết",
            "watcher_added": "Theo dõi", "accountable_added": "Chịu trách nhiệm",
        }
        for entry in entries[:20]:
            wp_id = entry["work_package_id"]
            group = grouped.setdefault(wp_id, {
                "subject": entry["subject"], "kinds": set(), "count": 0,
            })
            group["count"] += 1
            group["kinds"].add(kind_labels.get(entry["kind"], "Cập nhật"))
        embed = discord.Embed(
            title="📋 Tóm tắt OpenProject",
            description=f"{len(entries)} thông báo · {len(grouped)} công việc.",
            color=0x1ABC9C,
        )
        for wp_id, group in grouped.items():
            url = f"{op_url}/work_packages/{wp_id}"
            title = safe_text(group["subject"], 120)
            embed.add_field(
                name=f"#{wp_id} · {title}",
                value=(f"{safe_text(', '.join(sorted(group['kinds'])), 120)} "
                       f"({group['count']}) · [Mở công việc]({url})"),
                inline=False,
            )
        embed.set_footer(text="OpenProject · Tóm tắt hằng ngày")
        return embed, None
    labels = {
        "assignment": "📌 Giao việc",
        "status": "🔄 Đổi trạng thái",
        "comment": "💬 Bình luận mới",
        "created": "🆕 Công việc mới",
        "summary": "📋 Công việc được cập nhật",
        "watcher_added": "👀 Bạn được thêm theo dõi",
        "accountable_added": "🧭 Bạn là người chịu trách nhiệm",
        "details": "📝 Thay đổi công việc",
        "due": "📅 Công việc đến hạn hôm nay",
        "overdue": "⏰ Công việc quá hạn",
    }
    url = f"{op_url}/work_packages/{job['work_package_id']}"
    meta = json.loads(job.get("metadata") or "{}")
    roles = meta.get("recipient_roles") or []
    label = labels[job["kind"]]
    if "group_assignee" in roles:
        label = (
            "👥 Nhóm được giao việc" if job["kind"] == "assignment"
            else f"👥 Nhóm · {label.split(' ', 1)[-1]}"
        )
    if ("accountable" in roles and "assignee" not in roles
            and "group_assignee" not in roles and "watcher" not in roles
            and job["kind"] != "accountable_added"):
        label = f"🧭 Chịu trách nhiệm · {label.split(' ', 1)[-1]}"
    if "accountable" in roles and "accountable_added" in meta.get("changes", []):
        label = "🧭 Bạn là người chịu trách nhiệm"
    if ("watcher" in roles and "assignee" not in roles
            and "group_assignee" not in roles and job["kind"] != "watcher_added"):
        label = (
            "👀 Theo dõi · Đổi người được giao" if job["kind"] == "assignment"
            else f"👀 Theo dõi · {label.split(' ', 1)[-1]}"
        )
    embed = discord.Embed(
        title=f"{label} · #{job['work_package_id']}",
        url=url,
        description=safe_text(job["subject"], 500),
        color={"assignment": 0x3498DB, "status": 0xF39C12, "comment": 0x9B59B6,
               "created": 0x3498DB, "summary": 0x1ABC9C,
               "watcher_added": 0x1ABC9C, "accountable_added": 0x1ABC9C,
               "details": 0x1ABC9C, "due": 0xF39C12, "overdue": 0xE74C3C}[job["kind"]],
    )

    def field(label, value):
        embed.add_field(name=label, value=safe_text(value or "—", 500), inline=True)

    field("⚪ Trạng thái", meta.get("status"))
    field("🚨 Ưu tiên", meta.get("priority"))
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    field("📅 Hạn", meta.get("due_date"))
    field("📁 Dự án", job["project"])
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    assignee = meta.get("assignee") or (
        f"Nhóm ID {meta['assignee_group_id']}" if meta.get("assignee_group_id")
        else "—" if "watcher" in roles and "assignee" not in roles
        else f"ID {job['op_id']}"
    )
    changes = meta.get("changes") or [job["kind"]]
    if "assignment" in changes and meta.get("previous_assignee"):
        assignee = f"{meta['previous_assignee']} → {assignee}"
    field("👤 Người được giao", assignee)
    field("✍️ Người báo cáo", meta.get("reporter"))
    if "group_assignee" in roles and "watcher" in roles:
        embed.add_field(
            name="🔔 Lý do nhận",
            value="Thành viên nhóm được giao · Đang theo dõi",
            inline=False,
        )
    if "accountable" in roles:
        embed.add_field(
            name="🧭 Vai trò",
            value="Bạn là người chịu trách nhiệm (Accountable) cho công việc này.",
            inline=False,
        )
    if "status" in changes and meta.get("previous_status"):
        embed.add_field(
            name="🔄 Thay đổi",
            value=safe_text(f"{meta['previous_status']} → {meta['status']}", 500),
            inline=False,
        )
    if "comment" in changes:
        embed.add_field(name="💬 Nội dung", value=safe_text(job["detail"], 500), inline=False)
    detail_labels = {
        "priority": "Ưu tiên", "due_date": "Hạn", "start_date": "Ngày bắt đầu",
        "percentage_done": "Hoàn thành", "description": "Mô tả",
        "attachments": "Tệp đính kèm",
    }
    for change in (meta.get("field_changes") or [])[:6]:
        name = detail_labels.get(change.get("field"))
        if name and "details" in changes:
            before = change.get("before")
            after = change.get("after")
            if change["field"] == "percentage_done":
                before = f"{before}%" if before is not None else "—"
                after = f"{after}%" if after is not None else "—"
            before = "—" if before is None or before == "" else before
            after = "—" if after is None or after == "" else after
            change_text = f"{before} → {after}"
            if change["field"] == "attachments" and change.get("added"):
                change_text += "\nThêm: " + ", ".join(change["added"])
            embed.add_field(
                name=f"📝 {name}",
                value=safe_text(change_text, 500),
                inline=False,
            )
    if "watcher_added" in changes:
        embed.add_field(name="👀 Theo dõi", value="Bạn đã được thêm vào danh sách theo dõi.",
                        inline=False)
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

        @group.command(name="notify", description="Xem hoặc chỉnh cách nhận DM OpenProject")
        @app_commands.describe(
            mode="instant hoặc daily; bỏ trống để giữ nguyên",
            quiet="HH:MM-HH:MM hoặc off; bỏ trống để giữ nguyên",
            mute="Các loại cách nhau bằng dấu phẩy hoặc off",
        )
        async def notify(interaction: discord.Interaction, mode: str = "",
                         quiet: str = "", mute: str = ""):
            if interaction.guild_id != self.config.guild_id or self.config.delivery_mode != "webhook_dm":
                await interaction.response.send_message("Lệnh chỉ dùng trong server đã cấu hình.", ephemeral=True)
                return
            link = self.store.dm_link_for_discord(str(interaction.user.id))
            if not link or not link["enabled"]:
                await interaction.response.send_message("Bạn chưa có liên kết đang bật.", ephemeral=True)
                return
            preference = self.store.dm_preference(link["op_id"], self.config)
            if mode or quiet or mute:
                if not self.config.user_preferences_enabled:
                    await interaction.response.send_message(
                        "Quản lý đang giữ cấu hình thông báo chung.", ephemeral=True
                    )
                    return
                if mode and mode not in ("instant", "daily"):
                    await interaction.response.send_message("mode phải là instant hoặc daily.", ephemeral=True)
                    return
                if quiet:
                    if quiet == "off":
                        preference["quiet_start"] = preference["quiet_end"] = ""
                    elif re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]", quiet):
                        preference["quiet_start"], preference["quiet_end"] = quiet.split("-")
                    else:
                        await interaction.response.send_message(
                            "quiet phải là HH:MM-HH:MM hoặc off.", ephemeral=True
                        )
                        return
                allowed = {"assignment", "status", "comment", "created", "summary",
                           "watcher_added", "accountable_added", "details", "due", "overdue"}
                if mute:
                    events = set() if mute == "off" else set(mute.split(","))
                    if not events <= allowed:
                        await interaction.response.send_message(
                            "Loại thông báo không hợp lệ. Dùng: " + ", ".join(sorted(allowed)),
                            ephemeral=True,
                        )
                        return
                    preference["muted_events"] = sorted(events)
                preference["mode"] = mode or preference["mode"]
                self.store.set_dm_preference(
                    link["op_id"], preference["mode"], preference["quiet_start"],
                    preference["quiet_end"], preference["muted_events"],
                )
            quiet_value = (
                f"{preference['quiet_start']}-{preference['quiet_end']}"
                if preference["quiet_start"] else "tắt"
            )
            await interaction.response.send_message(
                f"Chế độ: {preference['mode']}\nGiờ yên lặng: {quiet_value} "
                f"(Asia/Ho_Chi_Minh)\nĐã tắt: {', '.join(preference['muted_events']) or 'không'}",
                ephemeral=True,
            )

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

    async def member_summary(self, discord_id):
        member = await self.verify_member(discord_id)
        return {"id": str(member.id), "name": member.display_name}
