from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from ghoster import GhostError, ghost_jar

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ghostbot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
SAFETY_MARGIN = 512 * 1024
FALLBACK_UPLOAD_LIMIT = 25 * 1024 * 1024


def _id_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            values.add(int(part))
    return values


ALLOWED_USER_IDS = _id_set(os.getenv("ALLOWED_USER_IDS", ""))


class GhostBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        if GUILD_ID:
            try:
                guild_id = int(GUILD_ID)
            except ValueError as exc:
                raise SystemExit("GUILD_ID must be an integer") from exc
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("synced commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("synced global commands (Discord can take up to an hour to show them)")


bot = GhostBot()


@bot.event
async def on_ready() -> None:
    log.info("logged in as %s (%s)", bot.user, getattr(bot.user, "id", "?"))
    if bot.user:
        invite = discord.utils.oauth_url(bot.user.id, scopes=("bot", "applications.commands"))
        log.info("invite: %s", invite)


@bot.tree.command(name="ghost", description="Rebrand a Fabric mod jar as a cover mod")
@app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
@app_commands.describe(
    source="The jar to hide (the client or cheat)",
    cover="The jar to impersonate (name, icon, description, version)",
    id_mode="keep = source ID, cover = cover ID, custom = your own ID",
    custom_id="Required when id_mode is custom",
    version="Override the output version (defaults to the cover's version)",
    filename="Override the output filename",
    replace_icon="Also overwrite the source's own icon with the cover icon",
    copy_lang="Copy the cover's language files into the output",
)
async def ghost(
    interaction: discord.Interaction,
    source: discord.Attachment,
    cover: discord.Attachment,
    id_mode: Literal["keep", "cover", "custom"] = "keep",
    custom_id: Optional[str] = None,
    version: Optional[str] = None,
    filename: Optional[str] = None,
    replace_icon: bool = True,
    copy_lang: bool = True,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    if ALLOWED_USER_IDS and interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.followup.send("Not authorized to use this bot.", ephemeral=True)
        return

    if not source.filename.lower().endswith(".jar") or not cover.filename.lower().endswith(".jar"):
        await interaction.followup.send("Both attachments must be `.jar` files.", ephemeral=True)
        return

    upload_limit = FALLBACK_UPLOAD_LIMIT
    guild = interaction.guild
    if guild is not None:
        upload_limit = min(getattr(guild, "filesize_limit", FALLBACK_UPLOAD_LIMIT) or FALLBACK_UPLOAD_LIMIT, FALLBACK_UPLOAD_LIMIT)
    if source.size > upload_limit:
        await interaction.followup.send(
            f"Source jar is {source.size / 1024 / 1024:.1f} MB; this bot accepts up to "
            f"{upload_limit / 1024 / 1024:.0f} MB.",
            ephemeral=True,
        )
        return

    workdir = Path(tempfile.mkdtemp(prefix="ghostbot-"))
    try:
        source_path = workdir / "source.jar"
        cover_path = workdir / "cover.jar"
        with source_path.open("wb") as handle:
            await source.save(handle)
        with cover_path.open("wb") as handle:
            await cover.save(handle)

        result = await asyncio.to_thread(
            ghost_jar,
            source_path,
            cover_path,
            workdir / "out",
            id_mode=id_mode,
            custom_id=custom_id,
            version=version,
            filename=filename or cover.filename,
            replace_icon=replace_icon,
            copy_lang=copy_lang,
        )

        if result.size > upload_limit - SAFETY_MARGIN:
            await interaction.followup.send(
                f"Ghost built at {result.size / 1024 / 1024:.1f} MB, but Discord's upload cap here is "
                f"{(upload_limit - SAFETY_MARGIN) / 1024 / 1024:.1f} MB. Use a smaller source jar.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="Ghost stashed", colour=0x2B2D31)
        embed.add_field(
            name="Cover",
            value=f"{result.meta.get('name', '?')} `{result.meta.get('version', '?')}`",
            inline=False,
        )
        embed.add_field(name="Internal ID", value=f"`{result.meta.get('id', '?')}`", inline=True)
        embed.add_field(name="Contents", value=f"{result.entries} entries / {result.classes} classes", inline=True)
        embed.add_field(name="Size", value=f"{result.size / 1024 / 1024:.2f} MB", inline=True)
        if result.warnings:
            embed.add_field(
                name="Notes",
                value="\n".join(f"- {warning}" for warning in result.warnings)[:1024],
                inline=False,
            )

        await interaction.followup.send(
            embed=embed,
            file=discord.File(result.path, filename=result.filename),
            ephemeral=True,
        )
    except GhostError as exc:
        await interaction.followup.send(f"Ghost failed: {exc}", ephemeral=True)
    except Exception:
        log.exception("unhandled ghost failure")
        await interaction.followup.send(
            "Ghost failed: unexpected error. Check the bot console.",
            ephemeral=True,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.error("command error: %r", error)
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Cooldown: try again in {error.retry_after:.1f}s."
    else:
        message = "Command failed. Check the bot console."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN before starting the bot.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
