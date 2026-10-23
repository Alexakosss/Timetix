from __future__ import annotations

from datetime import datetime, timezone

from timetable_bot.categories import Category
from timetable_bot.messages import build_announcement
from timetable_bot.scraper import DiscoveredDocument
from timetable_bot.utils import (
    build_attachment_filename,
    classify_category,
    extract_date_from_filename,
    format_greek_datetime,
    normalize_greek,
    normalize_url,
    sanitize_filename,
    validate_pdf_bytes,
)


def test_normalize_greek() -> None:
    assert normalize_greek("Γ΄ Τάξης Ειδικότητας") == "γ ταξησ ειδικοτητασ"
    assert normalize_greek("Γ' ΤΑΞΗΣ ΓΕΝΙΚΗΣ ΠΑΙΔΕΙΑΣ") == "γ ταξησ γενικησ παιδειασ"
    assert normalize_greek("  πολλα   κενα  ") == "πολλα κενα"


def test_classify_only_the_two_gamma_categories() -> None:
    assert classify_category("Τμήματα Γ΄ Τάξης Ειδικότητας") is Category.SPECIALTY
    assert classify_category("Τμήματα Γ΄ Τάξης Γενικής Παιδείας") is Category.GENERAL
    assert classify_category("Τμήματα Γ’ Τάξης Γενικής Παιδείας") is Category.GENERAL
    assert classify_category("Τμήματα Α΄ Τάξης") is None
    assert classify_category("Τμήματα Β΄ Τάξης Γενικής Παιδείας") is None
    assert classify_category("Τμήματα Β΄ Τάξης Τομέα") is None
    assert classify_category("Γενικής Παιδείας") is None


def test_extract_date_from_filename() -> None:
    assert extract_date_from_filename("Γ ειδικοτητα191026.pdf") == "2026-10-19"
    assert extract_date_from_filename("Γ_ταξη_19-10-2026.pdf") == "2026-10-19"
    assert extract_date_from_filename("προγραμμα_2026-10-19.pdf") == "2026-10-19"
    assert extract_date_from_filename("no-date-here.pdf") is None


def test_attachment_filename_is_clear_and_safe() -> None:
    name = build_attachment_filename(Category.SPECIALTY, "Γ ειδικοτητα191026.pdf")
    assert name.startswith("Γ_ΤΑΞΗ_ΕΙΔΙΚΟΤΗΤΑΣ_")
    assert name.endswith(".pdf")
    assert "/" not in name and "\\" not in name
    assert sanitize_filename('bad<>:"|?*name.pdf') == "bad_______name.pdf"


def test_validate_pdf_bytes() -> None:
    assert validate_pdf_bytes(b"%PDF-1.7\n...\n%%EOF")
    assert not validate_pdf_bytes(b"<html><body>error</body></html>")
    assert not validate_pdf_bytes(b"")
    assert not validate_pdf_bytes(b"not a pdf")


def test_normalize_url_encodes_greek_and_keeps_escapes() -> None:
    assert (
        normalize_url("Γ ειδικοτητα191026.pdf", "http://example.test/wp-gen-files/")
        == "http://example.test/wp-gen-files/%CE%93%20%CE%B5%CE%B9%CE%B4%CE%B9%CE%BA%CE%BF%CF%84%CE%B7%CF%84%CE%B1191026.pdf"
    )
    already_encoded = "http://example.test/%CE%93%20%CF%84%CE%B1%CE%BE%CE%B7.pdf"
    assert normalize_url(already_encoded, "http://example.test/") == already_encoded
    assert normalize_url("#anchor", "http://example.test/") is None
    assert normalize_url("mailto:x@example.com", "http://example.test/") is None


def test_format_greek_datetime_uses_athens_time() -> None:
    value = "2026-10-19T17:45:24+00:00"
    formatted = format_greek_datetime(value)
    assert formatted.startswith("19/10/2026")
    # October is UTC+3 in Athens.
    assert "20:45" in formatted


def test_announcement_titles_are_exact() -> None:
    specialty = DiscoveredDocument(
        category=Category.SPECIALTY,
        url="http://example.test/a.pdf",
        filename="a.pdf",
        link_text="link",
        source_page="http://example.test/",
        published_date="2026-10-19",
    )
    general = DiscoveredDocument(
        category=Category.GENERAL,
        url="http://example.test/b.pdf",
        filename="b.pdf",
        link_text="link",
        source_page="http://example.test/",
        published_date=None,
    )
    text_specialty = build_announcement(
        specialty,
        is_update=False,
        detected_at=datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc).isoformat(),
        last_modified=None,
        attachment_filename="Γ_ΤΑΞΗ_ΕΙΔΙΚΟΤΗΤΑΣ_a.pdf",
        timetable_page_url="http://example.test/",
    )
    text_general = build_announcement(
        general,
        is_update=True,
        detected_at=datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc).isoformat(),
        last_modified=None,
        attachment_filename="Γ_ΤΑΞΗ_ΓΕΝΙΚΗΣ_ΠΑΙΔΕΙΑΣ_b.pdf",
        timetable_page_url="http://example.test/",
    )
    assert text_specialty.startswith(
        "📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΕΙΔΙΚΟΤΗΤΑΣ"
    )
    assert text_general.startswith(
        "📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΓΕΝΙΚΗΣ ΠΑΙΔΕΙΑΣ"
    )
    assert "🔄 Είδος: Ενημέρωση υπάρχοντος αρχείου" in text_general
    assert "📅 Εντοπίστηκε:" in text_specialty
