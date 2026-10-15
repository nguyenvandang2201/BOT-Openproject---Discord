"""Permission policy shared by the bot and the offline administration tool."""

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16


def private_overwrites(guild_id: int, user_id: int, bot_id: int) -> list[dict]:
    return [
        {"id": str(guild_id), "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},
        {
            "id": str(user_id),
            "type": 1,
            "allow": str(VIEW_CHANNEL | READ_MESSAGE_HISTORY),
            "deny": str(SEND_MESSAGES),
        },
        {
            "id": str(bot_id),
            "type": 1,
            "allow": str(VIEW_CHANNEL | READ_MESSAGE_HISTORY | SEND_MESSAGES | EMBED_LINKS),
            "deny": "0",
        },
    ]


def channel_is_private(channel: dict, guild_id: int, category_id: int, user_id: int, bot_id: int):
    try:
        if (
            channel.get("type") != 0
            or str(channel.get("guild_id")) != str(guild_id)
            or str(channel.get("parent_id")) != str(category_id)
        ):
            return False
        overwrites = channel.get("permission_overwrites")
        if not isinstance(overwrites, list):
            return False
        by_id = {str(item.get("id")): item for item in overwrites}
        if len(by_id) != len(overwrites):
            return False
        for target, kind in ((guild_id, 0), (user_id, 1), (bot_id, 1)):
            item = by_id.get(str(target))
            if not item or item.get("type") != kind:
                return False
        if not int(by_id[str(guild_id)].get("deny", "0")) & VIEW_CHANNEL:
            return False
        if int(by_id[str(guild_id)].get("allow", "0")) & VIEW_CHANNEL:
            return False
        for target in (user_id, bot_id):
            if not int(by_id[str(target)].get("allow", "0")) & VIEW_CHANNEL:
                return False
            if int(by_id[str(target)].get("deny", "0")) & VIEW_CHANNEL:
                return False
        if not int(by_id[str(user_id)].get("deny", "0")) & SEND_MESSAGES:
            return False
        required_bot = SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY
        if int(by_id[str(bot_id)].get("allow", "0")) & required_bot != required_bot:
            return False
        for key, item in by_id.items():
            if key not in {str(guild_id), str(user_id), str(bot_id)}:
                if int(item.get("allow", "0")) & VIEW_CHANNEL:
                    return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False
