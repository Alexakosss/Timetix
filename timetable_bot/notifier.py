"""Discord delivery, attachment limits and optional large-file hosting."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import discord

from .config import Config
from .messages import build_announcement
from .scraper import DownloadedDocument
from .utils import ensure_directory, format_bytes

logger = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    """Discord rejected a post or the destination channel is unusable."""


@dataclass(frozen=True, slots=True)
class PostResult:
    sent: bool
    used_link: bool
    message_id: int | None = None
    reason: str | None = None


class Notifier:
    def __init__(self, config: Config, bot: Any | None) -> None:
        self.config = config
        self.bot = bot
        self._warned_test_channel = False

    async def resolve_channel(self) -> discord.abc.Messageable:
        if self.bot is None:
            raise NotificationError("Discord client is not available.")
        channel_id = self.config.destination_channel_id
        if channel_id is None:
            raise NotificationError(
                "No Discord channel is configured (CHANNEL_ID / TEST_CHANNEL_ID)."
            )

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound as exc:
                raise NotificationError(
                    f"Discord channel {channel_id} was not found. Check CHANNEL_ID."
                ) from exc
            except discord.Forbidden as exc:
                raise NotificationError(
                    f"The bot cannot access Discord channel {channel_id}."
                ) from exc

        if not hasattr(channel, "send"):
            raise NotificationError(
                f"Discord channel {channel_id} cannot receive messages."
            )

        if self.config.test_channel_id and not self._warned_test_channel:
            logger.warning(
                "TEST_CHANNEL_ID is set: all announcements go to channel %s instead of %s",
                self.config.test_channel_id,
                self.config.channel_id,
            )
            self._warned_test_channel = True

        return channel

    async def post(
        self,
        downloaded: DownloadedDocument,
        *,
        is_update: bool,
    ) -> PostResult:
        if self.config.dry_run:
            logger.info(
                "[DRY RUN] Would post [%s] %s (%s, sha256=%s)",
                downloaded.document.category.value,
                downloaded.attachment_filename,
                format_bytes(downloaded.size),
                downloaded.content_hash[:12],
            )
            return PostResult(sent=False, used_link=False, reason="dry-run")

        channel = await self.resolve_channel()
        attachment_limit = self._attachment_limit(channel)
        attachment_filename = downloaded.attachment_filename

        if downloaded.size <= attachment_limit:
            content = build_announcement(
                downloaded.document,
                is_update=is_update,
                detected_at=downloaded.detected_at,
                last_modified=downloaded.last_modified,
                attachment_filename=attachment_filename,
                timetable_page_url=self.config.timetable_url,
            )
            try:
                file = discord.File(
                    io.BytesIO(downloaded.data), filename=attachment_filename
                )
                message = await channel.send(
                    content=content,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                logger.info(
                    "Posted [%s] %s to channel %s (message %s)",
                    downloaded.document.category.value,
                    attachment_filename,
                    getattr(channel, "id", "?"),
                    message.id,
                )
                return PostResult(sent=True, used_link=False, message_id=message.id)
            except discord.Forbidden as exc:
                raise NotificationError(
                    "Missing Discord permissions. The bot needs View Channel, "
                    "Send Messages and Attach Files."
                ) from exc
            except discord.HTTPException as exc:
                if exc.status == 413 or exc.code == 40005:
                    logger.warning(
                        "Discord rejected attachment %s (%s); falling back to a link.",
                        attachment_filename,
                        exc,
                    )
                else:
                    raise NotificationError(f"Discord upload failed: {exc}") from exc

        # Too large (or rejected by Discord): send a link-only announcement.
        public_url = await self._publish_large_file(downloaded)
        link_url = public_url or downloaded.document.url
        content = build_announcement(
            downloaded.document,
            is_update=is_update,
            detected_at=downloaded.detected_at,
            last_modified=downloaded.last_modified,
            attachment_filename=attachment_filename,
            timetable_page_url=self.config.timetable_url,
            link_only=True,
            link_only_url=link_url,
            file_size=format_bytes(downloaded.size),
            attachment_limit=format_bytes(attachment_limit),
        )
        try:
            message = await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            raise NotificationError(
                "Missing Discord permissions. The bot needs Send Messages."
            ) from exc
        except discord.HTTPException as exc:
            raise NotificationError(f"Discord link message failed: {exc}") from exc

        logger.info(
            "Posted link-only [%s] %s to channel %s (message %s)",
            downloaded.document.category.value,
            attachment_filename,
            getattr(channel, "id", "?"),
            message.id,
        )
        return PostResult(sent=True, used_link=True, message_id=message.id)

    async def send_test_message(self) -> discord.Message:
        if self.config.dry_run:
            raise NotificationError(
                "DRY_RUN=true. Set DRY_RUN=false (preferably with TEST_CHANNEL_ID) "
                "to send a test message."
            )
        channel = await self.resolve_channel()
        try:
            return await channel.send(
                "✅ Δοκιμαστικό μήνυμα από το bot Ωρολογίου Προγράμματος του "
                "1ου ΕΠΑΛ Αιγάλεω.\nΤο κανάλι και τα δικαιώματα λειτουργούν σωστά.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            raise NotificationError(
                "Missing Discord permissions (View Channel / Send Messages)."
            ) from exc
        except discord.HTTPException as exc:
            raise NotificationError(f"Discord test message failed: {exc}") from exc

    def _attachment_limit(self, channel: discord.abc.Messageable) -> int:
        limit = self.config.discord_max_attachment_bytes
        guild = getattr(channel, "guild", None)
        guild_limit = getattr(guild, "filesize_limit", None)
        if guild_limit and guild_limit > 0:
            limit = min(limit, int(guild_limit))
        return limit

    async def _publish_large_file(self, downloaded: DownloadedDocument) -> str | None:
        base_url = self.config.public_file_base_url
        public_dir = self.config.public_dir
        if not base_url or not public_dir:
            return None

        filename = f"{downloaded.content_hash[:12]}_{downloaded.attachment_filename}"
        target = public_dir / filename
        try:
            await asyncio.to_thread(self._write_public_file, target, downloaded.data)
        except OSError:
            logger.exception("Could not copy %s to the public directory", filename)
            return None
        return f"{base_url.rstrip('/')}/{quote(filename)}"

    @staticmethod
    def _write_public_file(path: Path, data: bytes) -> None:
        ensure_directory(path.parent)
        if path.exists():
            return
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
