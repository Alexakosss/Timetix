"""Pure message-building functions (easy to unit test)."""

from __future__ import annotations

from .categories import CATEGORY_INFO
from .scraper import DiscoveredDocument
from .utils import format_greek_date, format_greek_datetime


def build_announcement(
    document: DiscoveredDocument,
    *,
    is_update: bool,
    detected_at: str,
    last_modified: str | None,
    attachment_filename: str,
    timetable_page_url: str,
    link_only: bool = False,
    link_only_url: str | None = None,
    file_size: str | None = None,
    attachment_limit: str | None = None,
) -> str:
    """Build the Greek Discord message for one timetable PDF."""

    info = CATEGORY_INFO[document.category]
    lines: list[str] = [
        info.title,
        "",
        "🔄 Είδος: Ενημέρωση υπάρχοντος αρχείου"
        if is_update
        else "🆕 Είδος: Νέο αρχείο",
        f"📅 Εντοπίστηκε: {format_greek_datetime(detected_at)} (ώρα Ελλάδας)",
    ]

    if last_modified:
        lines.append(
            f"🕓 Τελευταία ενημέρωση στο site: {format_greek_datetime(last_modified)} "
            "(ώρα Ελλάδας)"
        )
    if document.published_date:
        lines.append(f"🗓️ Ημερομηνία αρχείου: {format_greek_date(document.published_date)}")

    lines.extend(
        [
            f"🔗 Σελίδα Ωρολογίου: {timetable_page_url}",
            f"📄 Άμεσος σύνδεσμος PDF: {document.url}",
            f"📎 Αρχείο: `{attachment_filename}`",
        ]
    )

    if link_only:
        lines.extend(
            [
                "",
                "⚠️ Το αρχείο δεν επισυνάπτεται επειδή υπερβαίνει το όριο "
                f"συνημμένου του Discord"
                + (f" ({file_size} > {attachment_limit})" if file_size and attachment_limit else "")
                + ".",
                f"📥 Κατεβάστε το PDF: {link_only_url or document.url}",
            ]
        )

    return "\n".join(lines)
