import discord

from op_discord.bot import BridgeBot, notification_embed
from op_discord.openproject import OpenProject


async def test_slash_commands_and_minimal_intents(config, store, api):
    async with BridgeBot(config, store, api) as bot:
        group = bot.tree.get_command("op", guild=discord.Object(id=config.guild_id))
        assert {c.name for c in group.commands} == {"status", "notify", "admin"}
        assert bot.intents.guilds
        assert not bot.intents.members
        assert not bot.allowed_mentions.everyone
        assert not bot.allowed_mentions.users


def test_embed_limits_mentions_and_untrusted_links(config):
    api = OpenProject(config, None)
    notification = {
        "id": 1,
        "subject": "@everyone " + "X" * 2000,
        "reason": "@here",
        "_links": {},
        "_embedded": {
            "resource": {"_links": {"show": {"href": "https://evil.example", "type": "text/html"}}}
        },
    }
    embed = notification_embed(notification, api)
    assert len(embed.title) <= 256
    assert "@everyone" not in embed.title
    assert embed.url == config.op_url
    assert all(len(field.value) <= 512 for field in embed.fields)


def test_html_resource_link_used_without_inventing_routes(config):
    api = OpenProject(config, None)
    notification = {
        "id": 1,
        "_embedded": {
            "resource": {
                "_links": {
                    "showWorkPackage": {"href": "/actual-link-from-server", "type": "text/html"}
                }
            }
        },
    }
    assert notification_embed(notification, api).url == config.op_url + "/actual-link-from-server"
