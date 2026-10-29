"""Entry point for the 1ο ΕΠΑΛ Αιγάλεω timetable Discord bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from contextlib import suppress
from logging.handlers import RotatingFileHandler

import aiohttp
import discord
from discord.ext import commands, tasks

from timetable_bot.categories import CATEGORY_INFO, CATEGORY_ORDER
from timetable_bot.config import Config, ConfigError, load_config
from timetable_bot.discord_ui import TimetableCog
from timetable_bot.monitor import CheckInProgress, Monitor
from timetable_bot.notifier import Notifier
from timetable_bot.scraper import TimetableScraper
from timetable_bot.storage import Database
from timetable_bot.utils import format_bytes

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Logging / helpers
# ----------------------------------------------------------------------
def setup_logging(config: Config, *, console_only: bool = False) -> None:
    level = getattr(logging, config.log_level, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        with suppress(Exception):
            handler.close()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if not console_only:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_dir / "timetable-bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # The bot's own messages remain informative without third-party noise.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def create_session(config: Config) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        ssl=config.verify_ssl,
        limit=10,
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            "Accept-Language": "el-GR,el;q=0.9,en;q=0.5",
        },
        timeout=aiohttp.ClientTimeout(total=config.request_timeout_seconds + 15),
    )


def log_startup_banner(config: Config) -> None:
    logger.info("=" * 72)
    logger.info("1ο ΕΠΑΛ Αιγάλεω — Bot Ωρολογίου Προγράμματος")
    logger.info("Timetable page : %s", config.timetable_url)
    logger.info(
        "Monitoring     : %s",
        ", ".join(CATEGORY_INFO[category].label for category in CATEGORY_ORDER),
    )
    logger.info("Interval       : every %d minute(s)", config.check_interval_minutes)
    logger.info("Dry run        : %s", config.dry_run)
    logger.info("Baseline first : %s", config.baseline_on_first_run)
    logger.info(
        "Destination    : %s",
        config.destination_channel_id or "not configured",
    )
    logger.info("Database       : %s", config.database_path)
    logger.info("Downloads      : %s", config.download_dir)
    logger.info("Logs           : %s", config.log_dir)
    logger.info("=" * 72)
    if not 5 <= config.check_interval_minutes <= 10:
        logger.warning(
            "CHECK_INTERVAL_MINUTES=%d is outside the recommended 5–10 minute range.",
            config.check_interval_minutes,
        )


# ----------------------------------------------------------------------
# Discord bot
# ----------------------------------------------------------------------
class TimetableBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.db = Database(config.database_path)
        self.session: aiohttp.ClientSession | None = None
        self.scraper: TimetableScraper | None = None
        self.notifier: Notifier | None = None
        self.monitor: Monitor | None = None
        self.start_time = time.monotonic()
        self._startup_done = False
        self._startup_task: asyncio.Task | None = None
        self.check_loop = tasks.loop(minutes=config.check_interval_minutes)(
            self._scheduled_check
        )

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.start_time

    async def setup_hook(self) -> None:
        self.session = create_session(self.config)
        self.scraper = TimetableScraper(self.config, self.session)
        self.notifier = Notifier(self.config, self, self.db)
        self.monitor = Monitor(self.config, self.db, self.scraper, self.notifier)

        await self.add_cog(TimetableCog(self))

        if self.config.guild_id:
            # A guild sync only uploads commands registered for that guild, so the
            # global commands must be copied over first. Without this the sync
            # would send an empty payload and Discord would keep 0 commands.
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                logger.info(
                    "Synced %d slash command(s) to guild %s",
                    len(synced),
                    self.config.guild_id,
                )
            except discord.HTTPException:
                logger.exception(
                    "Guild slash-command sync failed; trying a global sync instead."
                )
                try:
                    synced = await self.tree.sync()
                    logger.info("Synced %d global slash command(s)", len(synced))
                except discord.HTTPException:
                    logger.exception("Global slash-command sync also failed.")
        else:
            try:
                synced = await self.tree.sync()
                logger.info(
                    "Synced %d global slash command(s). Global commands can take "
                    "up to an hour to appear; set GUILD_ID for instant sync.",
                    len(synced),
                )
            except discord.HTTPException:
                logger.exception("Global slash-command sync failed.")

    async def on_ready(self) -> None:
        if self._startup_done:
            return
        self._startup_done = True
        logger.info(
            "Logged in as %s (id=%s), in %d guild(s)",
            self.user,
            getattr(self.user, "id", "?"),
            len(self.guilds),
        )
        self._startup_task = asyncio.create_task(self._startup_sequence())

    async def _startup_sequence(self) -> None:
        if self.monitor is None:
            logger.error("Monitor was not initialised; cannot start checks.")
            return
        try:
            logger.info("Running startup timetable check...")
            summary = await self.monitor.run_check(reason="startup")
            logger.info(
                "Startup check finished: new=%d updated=%d posted=%d would_post=%d "
                "failed=%d baseline=%s",
                summary.new,
                summary.updated,
                summary.posted,
                summary.would_post,
                summary.failed,
                summary.baseline,
            )
        except CheckInProgress:
            logger.warning("Startup check skipped: another check is already running.")
        except Exception:  # noqa: BLE001 - startup must not take the bot down
            logger.exception("Startup check crashed; the periodic loop will retry.")
        finally:
            if not self.check_loop.is_running():
                self.check_loop.start()
                logger.info(
                    "Periodic timetable checks started (every %d minute(s)).",
                    self.config.check_interval_minutes,
                )

    async def _scheduled_check(self) -> None:
        if self.monitor is None:
            return
        try:
            summary = await self.monitor.run_check(reason="scheduled")
            logger.info(
                "Scheduled check: discovered=%d new=%d updated=%d posted=%d "
                "would_post=%d failed=%d",
                summary.discovered,
                summary.new,
                summary.updated,
                summary.posted,
                summary.would_post,
                summary.failed,
            )
        except CheckInProgress:
            logger.warning("Scheduled check skipped: another check is already running.")
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Scheduled check crashed; will retry next interval.")

    async def close(self) -> None:
        if self.check_loop.is_running():
            self.check_loop.cancel()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._startup_task
        if self.session is not None and not self.session.closed:
            await self.session.close()
        with suppress(Exception):
            self.db.close()
        await super().close()


# ----------------------------------------------------------------------
# CLI commands (no Discord connection required)
# ----------------------------------------------------------------------
async def _check_only(config: Config) -> int:
    async with create_session(config) as session:
        scraper = TimetableScraper(config, session)
        try:
            discovery = await scraper.discover()
        except Exception as exc:  # noqa: BLE001
            logger.error("Discovery failed: %s", exc, exc_info=True)
            return 1

        results: list[dict] = []
        had_error = False
        for document in discovery.documents:
            info = CATEGORY_INFO[document.category]
            entry: dict = {
                "category": info.label,
                "link_text": document.link_text,
                "url": document.url,
                "filename": document.filename,
                "published_date": document.published_date,
            }
            try:
                downloaded = await scraper.download(document)
                entry.update(
                    {
                        "ok": True,
                        "size": downloaded.size,
                        "size_human": format_bytes(downloaded.size),
                        "sha256": downloaded.content_hash,
                        "content_type": downloaded.content_type,
                        "resolved_url": downloaded.resolved_url,
                        "last_modified": downloaded.last_modified,
                        "attachment_filename": downloaded.attachment_filename,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                had_error = True
                entry.update({"ok": False, "error": str(exc)})
            results.append(entry)

        payload = {
            "source_url": discovery.source_url,
            "fetched_at": discovery.fetched_at,
            "warnings": discovery.warnings,
            "documents": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if had_error else 0


async def _baseline_cli(config: Config) -> int:
    db = Database(config.database_path)
    async with create_session(config) as session:
        scraper = TimetableScraper(config, session)
        # A baseline never posts, so no Discord client is needed.
        monitor = Monitor(config, db, scraper, Notifier(config, None))
        try:
            summary = await monitor.run_check(reason="cli-baseline", baseline=True)
        finally:
            db.close()
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 and not summary.fatal else 1


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timetable-bot",
        description=(
            "Discord bot that monitors the 1ο ΕΠΑΛ Αιγάλεω timetable page for "
            "Γ΄ Τάξης Ειδικότητας and Γ΄ Τάξης Γενικής Παιδείας PDFs."
        ),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Fetch the page and PDFs, print what was found, write nothing and exit.",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Record the current PDFs as the baseline (no Discord posts) and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = _build_parser().parse_args(argv)

    try:
        if args.check_only:
            config = load_config(require_discord=False)
            setup_logging(config, console_only=True)
            return asyncio.run(_check_only(config))

        if args.baseline:
            config = load_config(require_discord=False)
            setup_logging(config)
            log_startup_banner(config)
            logger.info("CLI baseline mode: nothing will be posted to Discord.")
            return asyncio.run(_baseline_cli(config))

        config = load_config(require_discord=True)
        setup_logging(config)
        log_startup_banner(config)

        bot = TimetableBot(config)
        try:
            bot.run(config.discord_token, log_handler=None)
        except discord.LoginFailure:
            logger.error(
                "Discord login failed. Check DISCORD_TOKEN and that the bot is invited."
            )
            return 1
        except KeyboardInterrupt:
            logger.info("Shutdown requested.")
            return 0
        return 0
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
