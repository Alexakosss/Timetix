from __future__ import annotations

from pathlib import Path

from timetable_bot.categories import Category
from timetable_bot.scraper import parse_timetable_html

FIXTURE = Path(__file__).parent / "fixtures" / "timetable_page.html"


def test_parse_realistic_page_only_two_gamma_categories() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_timetable_html(html, "http://example.test/sched-stud/")

    assert len(parsed.documents) == 4
    by_category = {
        category: [doc for doc in parsed.documents if doc.category is category]
        for category in Category
    }
    assert len(by_category[Category.SPECIALTY]) == 2
    assert len(by_category[Category.GENERAL]) == 2

    urls = {doc.url for doc in parsed.documents}
    assert not any("%CE%91%20" in url or "%CE%92%20" in url for url in urls)
    assert any("%CE%93%20%CE%B5%CE%B9%CE%B4%CE%B9%CE%BA%CE%BF%CF%84%CE%B7%CF%84%CE%B1191026.pdf" in url for url in urls)

    # Commented-out and footer links must not be treated as live documents.
    assert not any("παλιο" in url for url in urls)
    assert not any("footer" in url for url in urls)
    # An unrelated "εδώ" link with no category context is ignored.
    assert not any("%CE%9B%CE%99%CE%A3%CE%A4%CE%91" in url for url in urls)

    general = by_category[Category.GENERAL][0]
    assert general.published_date == "2026-10-19"
    assert general.filename.startswith("Γ_ταξη")

    subpage_urls = {candidate.url for candidate in parsed.subpages}
    assert "http://example.test/category/specialty/" in subpage_urls


def test_context_and_inherited_category() -> None:
    html = """
    <html><body><article>
      <p>Τμήματα Γ΄ Τάξης Ειδικότητας.
        <a href="/files/context.pdf">εδώ</a>
      </p>
      <div>
        <a href="/files/inherited.pdf">εδώ</a>
        <a href="/files/explicit.pdf">Τμήματα Γ΄ Τάξης Γενικής Παιδείας</a>
      </div>
      <p>Τμήματα Γ΄ Τάξης Ειδικότητας:
        <a href="/files/unsupported.docx">αρχείο Word</a>
      </p>
    </article></body></html>
    """
    parsed = parse_timetable_html(
        html,
        "http://example.test/",
        inherited_category=Category.SPECIALTY,
    )
    by_url = {document.url: document for document in parsed.documents}

    assert by_url["http://example.test/files/context.pdf"].category is Category.SPECIALTY
    assert by_url["http://example.test/files/inherited.pdf"].category is Category.SPECIALTY
    assert by_url["http://example.test/files/explicit.pdf"].category is Category.GENERAL
    assert len(parsed.unsupported) == 1
    assert parsed.unsupported[0].url == "http://example.test/files/unsupported.docx"
    assert parsed.warnings


def test_multiple_pdf_links_for_same_category_are_all_kept() -> None:
    html = """
    <html><body><article>
      <ul>
        <li><a href="/a.pdf">Τμήματα Γ΄ Τάξης Γενικής Παιδείας</a></li>
        <li><a href="/b.pdf">Τμήματα Γ΄ Τάξης Γενικής Παιδείας</a></li>
      </ul>
    </article></body></html>
    """
    parsed = parse_timetable_html(html, "http://example.test/")
    assert {doc.url for doc in parsed.documents} == {
        "http://example.test/a.pdf",
        "http://example.test/b.pdf",
    }
