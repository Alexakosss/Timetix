"""The main monitoring loop: discover, download, compare, notify."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field

from .categories import CATEGORY_INFO
from .config import Config
from .notifier import Notifier
from .scraper import TimetableScraper
from .storage import Database
from .utils import format_bytes, truncate, utc_now_iso

logger = logging.getLogger(__name__)


class CheckInProgress(RuntimeError):
    """Raised when a manual check overlaps the scheduled check."""


@dataclass(slots=True)
class CheckSummary:
    started_at: str
    finished_at: str | None
    reason: str
    baseline: bool
    discovered: int = 0
    downloaded: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    retried: int = 0
    baseline_recorded: int = 0
    posted: int = 0
    would_post: int = 0
    failed: int = 0
    fatal: bool = False
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return not self.fatal and self.failed == 0

    @property
    def headline(self) -> str:
        if self.fatal:
            return "Ο έλεγχος απέτυχε"
        if self.failed:
            return "Ο έλεγχος ολοκληρώθηκε με προβλήματα"
        return "Ο έλεγχος ολοκληρώθηκε"


class Monitor:
    def __init__(
        self,
        config: Config,
        database: Database,
        scraper: TimetableScraper,
        notifier: Notifier | None,
    ) -> None:
        self.config = config
        self.db = database
        self.scraper = scraper
        self.notifier = notifier
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    @property
    def baseline_pending(self) -> bool:
        return self.config.baseline_on_first_run and not self.db.get_state_bool(
            "baseline_done"
        )

    async def run_check(self, *, reason: str, baseline: bool = False) -> CheckSummary:
        if self._lock.locked():
            raise CheckInProgress("A timetable check is already running.")
        async with self._lock:
            return await self._run_check(reason=reason, baseline=baseline)

    async def _run_check(self, *, reason: str, baseline: bool) -> CheckSummary:
        started = utc_now_iso()
        started_monotonic = time.monotonic()
        effective_baseline = baseline or self.baseline_pending
        summary = CheckSummary(
            started_at=started,
            finished_at=None,
            reason=reason,
            baseline=effective_baseline,
        )

        self.db.set_state("last_check_started_at", started)
        self.db.set_state("last_check_reason", reason)
        logger.info(
            "Starting timetable check (reason=%s, baseline=%s, dry_run=%s)",
            reason,
            effective_baseline,
            self.config.dry_run,
        )

        try:
            discovery = await self.scraper.discover()
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.exception("Timetable discovery failed")
            summary.fatal = True
            summary.errors.append(f"Αποτυχία ανάκτησης σελίδας: {exc}")
            self._finish_failed(summary, started_monotonic, fatal=True)
            return summary

        summary.discovered = len(discovery.documents)
        summary.warnings.extend(discovery.warnings)
        self.db.set_state("consecutive_failures", "0")

        processed_keys: set[tuple[str, str]] = set()
        for document in discovery.documents:
            label = CATEGORY_INFO[document.category].label
            key = (document.category.value, document.url)
            if key in processed_keys:
                continue
            processed_keys.add(key)

            try:
                downloaded = await self.scraper.download(document)
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the check
                logger.warning("Download failed for %s: %s", document.url, exc)
                summary.failed += 1
                summary.errors.append(f"{label} / {document.filename}: {exc}")
                continue

            summary.downloaded += 1
            try:
                saved_path = await self.scraper.save_download(downloaded)
                logger.info(
                    "Downloaded %s (%s, sha256=%s) -> %s",
                    downloaded.attachment_filename,
                    format_bytes(downloaded.size),
                    downloaded.content_hash[:12],
                    saved_path,
                )
            except Exception as exc:  # noqa: BLE001 - audit copy is best-effort
                logger.exception("Could not save a local copy of %s", document.url)
                summary.warnings.append(
                    f"Αποτυχία τοπικής αποθήκευσης {document.filename}: {exc}"
                )

            try:
                registration = self.db.register_document(
                    category=document.category.value,
                    url=document.url,
                    filename=downloaded.attachment_filename,
                    content_hash=downloaded.content_hash,
                    detected_at=downloaded.detected_at,
                    last_modified=downloaded.last_modified,
                    published_date=document.published_date,
                    baseline=effective_baseline,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Database registration failed for %s", document.url)
                summary.failed += 1
                summary.errors.append(f"{label} / {document.filename}: DB error: {exc}")
                continue

            logger.info(
                "Decision for [%s] %s: %s (post=%s, update=%s)",
                label,
                downloaded.attachment_filename,
                registration.status,
                registration.should_post,
                registration.is_update,
            )

            if registration.status == "unchanged":
                summary.unchanged += 1
                continue
            if registration.status == "duplicate":
                summary.duplicates += 1
                continue
            if registration.status == "baseline":
                summary.baseline_recorded += 1
                continue
            if registration.status == "new":
                summary.new += 1
            elif registration.status == "updated":
                summary.updated += 1
            elif registration.status == "retry":
                summary.retried += 1
            else:  # pragma: no cover - defensive
                summary.warnings.append(
                    f"Άγνωστη κατάσταση εγγραφής: {registration.status}"
                )

            if not registration.should_post:
                continue

            if effective_baseline:
                # Should be impossible: baseline registration never asks for a
                # post. Refuse to post during baseline even if logic changes.
                summary.warnings.append(
                    f"Παράλειψη αποστολής σε λειτουργία baseline: {document.url}"
                )
                continue

            if self.config.dry_run or self.notifier is None:
                summary.would_post += 1
                logger.info(
                    "[DRY RUN] Would post [%s] %s (%s)",
                    label,
                    downloaded.attachment_filename,
                    downloaded.content_hash[:12],
                )
                continue

            try:
                result = await self.notifier.post(
                    downloaded, is_update=registration.is_update
                )
            except Exception as exc:  # noqa: BLE001 - retry on next check
                logger.exception("Discord notification failed for %s", document.url)
                summary.failed += 1
                summary.errors.append(f"{label} / {document.filename}: Discord: {exc}")
                continue

            if result.sent:
                self.db.mark_posted(
                    category=document.category.value,
                    url=document.url,
                    content_hash=downloaded.content_hash,
                    posted_at=utc_now_iso(),
                )
                summary.posted += 1
            elif result.reason == "dry-run":
                summary.would_post += 1
            else:
                summary.failed += 1
                summary.errors.append(
                    f"{label} / {document.filename}: μη αναμενόμενο αποτέλεσμα αποστολής"
                )

            # Be polite to Discord's rate limits.
            await asyncio.sleep(0.75)

        if effective_baseline and summary.failed == 0:
            finished = utc_now_iso()
            self.db.set_state("baseline_done", "1")
            self.db.set_state("baseline_at", finished)
            logger.info(
                "Baseline complete: %d document(s) recorded, nothing posted.",
                summary.baseline_recorded,
            )

        self._finish_success(summary, started_monotonic)
        return summary

    def _finish_success(self, summary: CheckSummary, started_monotonic: float) -> None:
        finished = utc_now_iso()
        summary.finished_at = finished
        summary.duration_seconds = round(time.monotonic() - started_monotonic, 2)

        self.db.set_state("last_check_finished_at", finished)
        self.db.set_state("last_success_at", finished)
        self.db.set_state("last_error", "")
        warning_text = "; ".join([*summary.warnings, *summary.errors])
        self.db.set_state("last_warning", truncate(warning_text, 2000))
        self.db.set_state(
            "last_check_summary",
            json.dumps(summary.to_dict(), ensure_ascii=False),
        )
        logger.info(
            "Check finished in %.2fs: discovered=%d downloaded=%d new=%d updated=%d "
            "unchanged=%d duplicates=%d baseline=%d posted=%d would_post=%d failed=%d",
            summary.duration_seconds,
            summary.discovered,
            summary.downloaded,
            summary.new,
            summary.updated,
            summary.unchanged,
            summary.duplicates,
            summary.baseline_recorded,
            summary.posted,
            summary.would_post,
            summary.failed,
        )

    def _finish_failed(
        self, summary: CheckSummary, started_monotonic: float, *, fatal: bool
    ) -> None:
        finished = utc_now_iso()
        summary.finished_at = finished
        summary.fatal = fatal
        summary.duration_seconds = round(time.monotonic() - started_monotonic, 2)
        error_text = truncate("; ".join(summary.errors) or "Άγνωστο σφάλμα", 2000)
        self.db.set_state("last_check_finished_at", finished)
        self.db.set_state("last_error", error_text)
        self.db.set_state(
            "last_check_summary",
            json.dumps(summary.to_dict(), ensure_ascii=False),
        )
        self.db.increment_state("consecutive_failures")
        logger.error("Check failed: %s", error_text)

    def status_snapshot(self) -> dict:
        raw_summary = self.db.get_state("last_check_summary")
        last_summary: dict | None = None
        if raw_summary:
            try:
                last_summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                last_summary = None

        try:
            consecutive_failures = int(self.db.get_state("consecutive_failures") or "0")
        except ValueError:
            consecutive_failures = 0

        return {
            "last_check_started_at": self.db.get_state("last_check_started_at"),
            "last_check_finished_at": self.db.get_state("last_check_finished_at"),
            "last_success_at": self.db.get_state("last_success_at"),
            "last_error": self.db.get_state("last_error"),
            "last_warning": self.db.get_state("last_warning"),
            "last_check_reason": self.db.get_state("last_check_reason"),
            "last_check_summary": last_summary,
            "baseline_done": self.db.get_state_bool("baseline_done"),
            "baseline_pending": self.baseline_pending,
            "baseline_at": self.db.get_state("baseline_at"),
            "consecutive_failures": consecutive_failures,
            "is_running": self.is_running,
            "counts": self.db.get_status_counts(),
            "versions": self.db.get_versions_count(),
            "posted_versions": self.db.get_posted_versions_count(),
        }
