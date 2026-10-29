"""Slash commands and status rendering."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

try:
    import pymupdf
except ImportError:  # pragma: no cover - optional runtime dependency
    pymupdf = None

from .categories import CATEGORY_INFO, CATEGORY_ORDER, Category
from .monitor import CheckInProgress, CheckSummary
from .notifier import NotificationError
from .scraper import ScraperError
from .utils import format_greek_datetime, truncate, utc_now

if TYPE_CHECKING:  # pragma: no cover
    from bot import TimetableBot

logger = logging.getLogger(__name__)


def _render_pdf_pages_as_jpegs(pdf_data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Render each PDF page to a readable, size-bounded JPEG attachment."""

    if pymupdf is None:
        raise RuntimeError("Το PyMuPDF δεν είναι εγκατεστημένο.")

    results: list[tuple[str, bytes]] = []
    with pymupdf.open(stream=pdf_data, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("Το PDF είναι κλειδωμένο με κωδικό.")
        if not document.page_count:
            raise ValueError("Το PDF δεν περιέχει σελίδες.")

        stem = Path(filename).stem
        for page_number, page in enumerate(document, start=1):
            # Render at about 144 dpi for ordinary pages, with a hard pixel cap
            # to avoid excessive memory use for unusually large PDF page sizes.
            longest_side = max(page.rect.width, page.rect.height)
            scale = min(2.0, 5000 / longest_side) if longest_side else 1.0
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            jpeg = pixmap.tobytes("jpeg", jpg_quality=85)
            results.append((f"{stem}_selida_{page_number:02}.jpg", jpeg))

    return results


def _uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}ημ")
    if hours:
        parts.append(f"{hours}ω")
    if minutes:
        parts.append(f"{minutes}λ")
    if not parts:
        parts.append(f"{secs}δ")
    return " ".join(parts)


def format_check_summary(summary: CheckSummary, *, dry_run: bool) -> str:
    lines: list[str] = [f"**{summary.headline}**"]
    if summary.baseline:
        lines.append(
            "🗂️ Λειτουργία **baseline**: καταγράφηκαν τα υπάρχοντα αρχεία χωρίς αποστολή."
        )
    lines.append(f"📄 Βρέθηκαν: **{summary.discovered}**")
    lines.append(
        f"🆕 Νέα: **{summary.new}** · 🔄 Ενημερώσεις: **{summary.updated}** · "
        f"✅ Ίδια: **{summary.unchanged}** · ♻️ Διπλότυπα: **{summary.duplicates}**"
    )
    if summary.retried:
        lines.append(f"🔁 Επαναλήψεις αποστολής: **{summary.retried}**")
    if summary.baseline:
        lines.append(f"🗂️ Καταγραφές baseline: **{summary.baseline_recorded}**")
    if dry_run:
        lines.append(
            f"🧪 **Dry run**: θα στέλνονταν **{summary.would_post}** μήνυμα(τα). "
            "Δεν στάλθηκε τίποτα."
        )
    else:
        lines.append(f"📤 Στάλθηκαν: **{summary.posted}**")
    if summary.failed:
        lines.append(f"❌ Αποτυχίες: **{summary.failed}**")
    if summary.duration_seconds:
        lines.append(f"⏱️ Διάρκεια: {summary.duration_seconds:.1f}s")

    if summary.errors:
        lines.append("")
        lines.append("**Σφάλματα:**")
        for error in summary.errors[:5]:
            lines.append(f"• {truncate(error, 300)}")
        if len(summary.errors) > 5:
            lines.append(f"• …και {len(summary.errors) - 5} ακόμη")
    if summary.warnings:
        lines.append("")
        lines.append("**Προειδοποιήσεις:**")
        for warning in summary.warnings[:4]:
            lines.append(f"• {truncate(warning, 300)}")
        if len(summary.warnings) > 4:
            lines.append(f"• …και {len(summary.warnings) - 4} ακόμη")

    if summary.baseline and summary.failed == 0 and not summary.fatal:
        lines.append("")
        lines.append(
            "✅ Το baseline ολοκληρώθηκε. Από τώρα και στο εξής θα ανακοινώνονται "
            "μόνο νέα ή τροποποιημένα αρχεία."
        )
    elif summary.baseline and summary.failed:
        lines.append("")
        lines.append(
            "⚠️ Το baseline δεν ολοκληρώθηκε πλήρως. Εκτελέστε ξανά `/baseline` "
            "όταν αποκατασταθεί το πρόβλημα."
        )

    return truncate("\n".join(lines), 1900)


class TimetableCog(commands.Cog):
    def __init__(self, bot: "TimetableBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /setup
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setup",
        description="Ρύθμιση του bot για αυτόν τον server (διαχειριστές)",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if self.bot.db is None:
            await interaction.response.send_message(
                "Το bot δεν έχει συνδεθεί με τη βάση δεδομένων.", ephemeral=True
            )
            return

        self.bot.db.upsert_guild_setting(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"✅ Ρυθμίστηκε! Τα ωρολόγια θα ανακοινώνονται στο {channel.mention}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /disable
    # ------------------------------------------------------------------
    @app_commands.command(
        name="disable",
        description="Απενεργοποίηση ανακοινώσεων για αυτόν τον server (διαχειριστές)",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        if self.bot.db is None:
            await interaction.response.send_message(
                "Το bot δεν έχει συνδεθεί με τη βάση δεδομένων.", ephemeral=True
            )
            return
        self.bot.db.remove_guild_setting(interaction.guild_id)
        await interaction.response.send_message(
            "✅ Οι ανακοινώσεις απενεργοποιήθηκαν για αυτόν τον server.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /check_timetables
    # ------------------------------------------------------------------
    @app_commands.command(
        name="check_timetables",
        description="Έλεγχος τώρα για νέα ή ενημερωμένα ωρολόγια προγράμματα",
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id, i.user.id))
    async def check_timetables(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self.bot.monitor.run_check(
                reason=f"manual:{interaction.user.id}"
            )
        except CheckInProgress:
            await interaction.followup.send(
                "⏳ Έλεγχος βρίσκεται ήδη σε εξέλιξη. Δοκιμάστε ξανά σε λίγο.",
                ephemeral=True,
            )
            return

        text = format_check_summary(summary, dry_run=self.bot.config.dry_run)
        await interaction.followup.send(text, ephemeral=True)

    # ------------------------------------------------------------------
    # /send_timetable
    # ------------------------------------------------------------------
    @app_commands.command(
        name="send_timetable",
        description="Στέλνει το τρέχον πρόγραμμα του σχολείου ως PDF ή JPG",
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 20.0, key=lambda i: (i.guild_id, i.user.id))
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Και οι δύο κατηγορίες", value="both"),
            app_commands.Choice(name="Γ΄ Τάξης Ειδικότητας", value="specialty"),
            app_commands.Choice(name="Γ΄ Τάξης Γενικής Παιδείας", value="general"),
        ],
        output_format=[
            app_commands.Choice(name="PDF", value="pdf"),
            app_commands.Choice(name="JPG", value="jpg"),
            app_commands.Choice(name="PDF και JPG", value="both"),
        ],
    )
    async def send_timetable(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        output_format: app_commands.Choice[str],
    ) -> None:
        """Fetch and post the selected current school timetables in-channel."""

        await interaction.response.defer(thinking=True)
        try:
            discovery = await self.bot.scraper.discover()
        except ScraperError as exc:
            await interaction.followup.send(
                f"❌ Δεν μπόρεσα να βρω τα προγράμματα στον ιστότοπο του σχολείου: {truncate(str(exc), 1200)}"
            )
            return

        if category.value == "both":
            selected_categories = set(CATEGORY_ORDER)
        else:
            selected_categories = {Category(category.value)}
        documents = [
            document
            for document in discovery.documents
            if document.category in selected_categories
        ]
        if not documents:
            await interaction.followup.send(
                "Δεν βρέθηκε πρόγραμμα για την επιλεγμένη κατηγορία."
            )
            return

        include_pdf = output_format.value in {"pdf", "both"}
        include_jpg = output_format.value in {"jpg", "both"}
        attachment_limit = self.bot.config.discord_max_attachment_bytes
        attachments: list[tuple[str, bytes]] = []
        details: list[str] = []
        failures: list[str] = []

        for document in documents:
            label = CATEGORY_INFO[document.category].label
            try:
                downloaded = await self.bot.scraper.download(document)
            except ScraperError as exc:
                failures.append(f"{label}: {truncate(str(exc), 250)}")
                continue

            details.append(
                f"• **{label}** — `{downloaded.attachment_filename}` (<{document.url}>)"
            )
            if include_pdf:
                if downloaded.size <= attachment_limit:
                    attachments.append((downloaded.attachment_filename, downloaded.data))
                else:
                    details.append(
                        f"  Το PDF ξεπερνά το όριο αποστολής ({attachment_limit // (1024 * 1024)} MB): {document.url}"
                    )

            if include_jpg:
                if pymupdf is None:
                    failures.append(
                        f"{label}: η μετατροπή σε JPG δεν είναι διαθέσιμη (λείπει το PyMuPDF)."
                    )
                else:
                    try:
                        page_images = await asyncio.to_thread(
                            _render_pdf_pages_as_jpegs,
                            downloaded.data,
                            downloaded.attachment_filename,
                        )
                        for image_name, image_data in page_images:
                            if len(image_data) <= attachment_limit:
                                attachments.append((image_name, image_data))
                            else:
                                failures.append(
                                    f"{label}: η εικόνα {image_name} ξεπερνά το όριο αποστολής."
                                )
                    except Exception as exc:  # noqa: BLE001 - don't let one PDF break the command
                        failures.append(
                            f"{label}: δεν έγινε μετατροπή σε JPG ({truncate(str(exc), 200)})."
                        )

        content_parts = ["📚 **Τρέχον πρόγραμμα σχολείου**", *details]
        if failures:
            content_parts.extend(["", "**Προβλήματα:**", *[f"• {item}" for item in failures]])
        content = truncate("\n".join(content_parts), 1900)

        # Discord accepts at most ten attachments per message.
        if not attachments:
            await interaction.followup.send(content)
            return
        for batch_index in range(0, len(attachments), 10):
            batch = attachments[batch_index : batch_index + 10]
            files = [
                discord.File(io.BytesIO(data), filename=filename)
                for filename, data in batch
            ]
            await interaction.followup.send(
                content if batch_index == 0 else "Συνέχεια αρχείων προγράμματος σχολείου.",
                files=files,
            )

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------
    @app_commands.command(
        name="status",
        description="Κατάσταση του bot και τελευταίος έλεγχος",
    )
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        snapshot = self.bot.monitor.status_snapshot()
        config = self.bot.config

        if snapshot["is_running"]:
            state = "🔄 Έλεγχος σε εξέλιξη"
            color = discord.Color.blue()
        elif snapshot["last_error"] and snapshot["consecutive_failures"] > 0:
            state = "❌ Τελευταίος έλεγχος απέτυχε"
            color = discord.Color.red()
        elif snapshot["last_warning"] or snapshot["baseline_pending"]:
            state = "⚠️ Λειτουργεί με προειδοποιήσεις"
            color = discord.Color.orange()
        elif self.bot.is_ready() and not self.bot.is_closed():
            state = "✅ Σε λειτουργία"
            color = discord.Color.green()
        else:
            state = "⚠️ Μη συνδεδεμένο"
            color = discord.Color.orange()

        embed = discord.Embed(
            title="📊 Κατάσταση Bot Ωρολογίου",
            description="1ο ΕΠΑΛ Αιγάλεω · Γ΄ Τάξης Ειδικότητας & Γενικής Παιδείας",
            color=color,
            timestamp=utc_now(),
        )
        embed.add_field(name="Κατάσταση", value=state, inline=False)
        embed.add_field(
            name="Τελευταίος έλεγχος",
            value=format_greek_datetime(snapshot["last_check_started_at"]),
            inline=True,
        )
        embed.add_field(
            name="Τελευταία επιτυχία",
            value=format_greek_datetime(snapshot["last_success_at"]),
            inline=True,
        )
        embed.add_field(
            name="Επόμενος έλεγχος",
            value=self._next_check_text(),
            inline=True,
        )
        embed.add_field(
            name="Διάστημα",
            value=f"Κάθε {config.check_interval_minutes} λεπτά",
            inline=True,
        )
        if config.dry_run:
            mode = "🧪 Dry run (δεν στέλνονται μηνύματα)"
        else:
            mode = "📤 Κανονική αποστολή"
        embed.add_field(name="Λειτουργία", value=mode, inline=True)

        channel_id = config.destination_channel_id
        channel_text = f"<#{channel_id}>" if channel_id else "— (δεν έχει οριστεί)"
        if config.test_channel_id:
            channel_text += "\n🧪 TEST_CHANNEL_ID ενεργό"
        embed.add_field(name="Κανάλι", value=channel_text, inline=True)

        if not config.baseline_on_first_run:
            baseline_text = "— (απενεργοποιημένο)"
        elif snapshot["baseline_done"]:
            baseline_text = (
                "✅ Ολοκληρώθηκε\n"
                f"({format_greek_datetime(snapshot['baseline_at'])})"
            )
        else:
            baseline_text = "⏳ Εκκρεμεί"
        embed.add_field(name="Baseline", value=baseline_text, inline=True)

        counts_lines: list[str] = []
        for category in CATEGORY_ORDER:
            info = CATEGORY_INFO[category]
            data = snapshot["counts"].get(category.value, {})
            counts_lines.append(
                f"**{info.label}**\n"
                f"{data.get('total', 0)} αρχείο(α) · "
                f"{data.get('pending', 0)} εκκρεμούν · "
                f"{data.get('posted_current', 0)} δημοσιευμένα"
            )
        embed.add_field(
            name="Καταγραφές",
            value="\n".join(counts_lines) or "—",
            inline=False,
        )

        summary = snapshot["last_check_summary"]
        if summary:
            summary_text = (
                f"Βρέθηκαν {summary.get('discovered', 0)} · "
                f"νέα {summary.get('new', 0)} · "
                f"ενημερώσεις {summary.get('updated', 0)} · "
                f"δημοσιεύσεις {summary.get('posted', 0)} · "
                f"dry-run {summary.get('would_post', 0)} · "
                f"αποτυχίες {summary.get('failed', 0)}"
            )
            if summary.get("baseline"):
                summary_text = "Baseline: " + summary_text
            embed.add_field(name="Τελευταία σύνοψη", value=truncate(summary_text, 1024), inline=False)

        if snapshot["last_warning"]:
            embed.add_field(
                name="Τελευταία προειδοποίηση",
                value=truncate(snapshot["last_warning"], 900),
                inline=False,
            )
        if snapshot["last_error"]:
            embed.add_field(
                name="Τελευταίο σφάλμα",
                value=truncate(snapshot["last_error"], 900),
                inline=False,
            )
        if snapshot["consecutive_failures"]:
            embed.add_field(
                name="Συνεχόμενες αποτυχίες",
                value=str(snapshot["consecutive_failures"]),
                inline=True,
            )

        embed.add_field(
            name="Latency",
            value=f"{self.bot.latency * 1000:.0f} ms",
            inline=True,
        )
        embed.add_field(
            name="Uptime",
            value=_uptime(self.bot.uptime_seconds),
            inline=True,
        )
        embed.add_field(
            name="Βάση / εκδόσεις",
            value=(
                f"{snapshot['versions']} αρχεία-εκδόσεις\n"
                f"{snapshot['posted_versions']} δημοσιευμένες"
            ),
            inline=True,
        )
        embed.set_footer(text="Ωρολόγιο Πρόγραμμα Bot · 1ο ΕΠΑΛ Αιγάλεω")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /baseline
    # ------------------------------------------------------------------
    @app_commands.command(
        name="baseline",
        description="Καταγραφή των υπαρχόντων ωρολογίων χωρίς αποστολή (διαχειριστές)",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id, i.user.id))
    async def baseline(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self.bot.monitor.run_check(
                reason=f"manual-baseline:{interaction.user.id}",
                baseline=True,
            )
        except CheckInProgress:
            await interaction.followup.send(
                "⏳ Έλεγχος βρίσκεται ήδη σε εξέλιξη.", ephemeral=True
            )
            return
        text = format_check_summary(summary, dry_run=self.bot.config.dry_run)
        await interaction.followup.send(text, ephemeral=True)

    # ------------------------------------------------------------------
    # /test_post
    # ------------------------------------------------------------------
    @app_commands.command(
        name="test_post",
        description="Δοκιμαστικό μήνυμα στο κανάλι (διαχειριστές, απαιτεί DRY_RUN=false)",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def test_post(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.bot.notifier.send_test_message()
        except NotificationError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Το δοκιμαστικό μήνυμα στάλθηκε: {message.jump_url}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Helpers / error handling
    # ------------------------------------------------------------------
    def _next_check_text(self) -> str:
        next_iteration: datetime | None = getattr(self.bot.check_loop, "next_iteration", None)
        if next_iteration is None:
            return "—"
        if next_iteration.tzinfo is None:
            next_iteration = next_iteration.replace(tzinfo=timezone.utc)
        return format_greek_datetime(next_iteration)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ Περίμενε {error.retry_after:.0f} δευτερόλεπτα και δοκίμασε ξανά."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "🔒 Χρειάζεται το δικαίωμα «Διαχείριση Server»."
        else:
            logger.error("Slash command failed", exc_info=error)
            message = "❌ Παρουσιάστηκε σφάλμα. Δες τα logs του bot."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not send command error to Discord")
