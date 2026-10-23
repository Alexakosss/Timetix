from __future__ import annotations

from pathlib import Path

from timetable_bot.categories import Category
from timetable_bot.storage import Database

NOW = "2026-10-23T08:00:00+00:00"


def register(
    db: Database,
    *,
    content_hash: str,
    url: str = "http://example.test/a.pdf",
    category: Category = Category.SPECIALTY,
    baseline: bool = False,
):
    return db.register_document(
        category=category.value,
        url=url,
        filename="test.pdf",
        content_hash=content_hash,
        detected_at=NOW,
        last_modified="2026-10-18T17:45:24+00:00",
        published_date="2026-10-19",
        baseline=baseline,
    )


def test_new_post_unchanged_update_and_revert(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        first = register(db, content_hash="a" * 64)
        assert first.status == "new"
        assert first.should_post is True
        assert first.is_update is False

        # Not marked posted yet -> retry rather than a second "new".
        retry = register(db, content_hash="a" * 64)
        assert retry.status == "retry"
        assert retry.should_post is True

        db.mark_posted(
            category=Category.SPECIALTY.value,
            url="http://example.test/a.pdf",
            content_hash="a" * 64,
            posted_at=NOW,
        )
        unchanged = register(db, content_hash="a" * 64)
        assert unchanged.status == "unchanged"
        assert unchanged.should_post is False

        updated = register(db, content_hash="b" * 64)
        assert updated.status == "updated"
        assert updated.should_post is True
        assert updated.is_update is True

        db.mark_posted(
            category=Category.SPECIALTY.value,
            url="http://example.test/a.pdf",
            content_hash="b" * 64,
            posted_at=NOW,
        )
        revert = register(db, content_hash="a" * 64)
        assert revert.status == "duplicate"
        assert revert.should_post is False
        current = db.get_timetable(Category.SPECIALTY.value, "http://example.test/a.pdf")
        assert current is not None
        assert current.posted_version_hash == "a" * 64
    finally:
        db.close()


def test_identical_content_under_a_new_url_is_not_reposted(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        register(db, content_hash="c" * 64, url="http://example.test/old.pdf")
        db.mark_posted(
            category=Category.SPECIALTY.value,
            url="http://example.test/old.pdf",
            content_hash="c" * 64,
            posted_at=NOW,
        )
        duplicate = register(db, content_hash="c" * 64, url="http://example.test/new.pdf")
        assert duplicate.status == "duplicate"
        assert duplicate.should_post is False
    finally:
        db.close()


def test_same_content_can_be_posted_once_per_category(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        register(db, content_hash="d" * 64, category=Category.SPECIALTY)
        db.mark_posted(
            category=Category.SPECIALTY.value,
            url="http://example.test/a.pdf",
            content_hash="d" * 64,
            posted_at=NOW,
        )
        other_category = register(
            db,
            content_hash="d" * 64,
            url="http://example.test/a-general.pdf",
            category=Category.GENERAL,
        )
        assert other_category.status == "new"
        assert other_category.should_post is True
    finally:
        db.close()


def test_baseline_records_without_posting_and_can_be_superseded(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        baseline = register(db, content_hash="e" * 64, baseline=True)
        assert baseline.status == "baseline"
        assert baseline.should_post is False
        assert db.get_state_bool("baseline_done") is False  # monitor sets this

        repeated = register(db, content_hash="e" * 64, baseline=True)
        assert repeated.should_post is False

        changed = register(db, content_hash="f" * 64, baseline=False)
        assert changed.status == "updated"
        assert changed.should_post is True
    finally:
        db.close()


def test_baseline_marks_an_unposted_version_as_known(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        register(db, content_hash="1" * 64, baseline=False)  # pretend upload failed
        baseline = register(db, content_hash="1" * 64, baseline=True)
        assert baseline.should_post is False

        later = register(db, content_hash="1" * 64, baseline=False)
        assert later.should_post is False
        assert later.status == "unchanged"
    finally:
        db.close()


def test_state_and_status_helpers(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        db.set_state("last_check_started_at", NOW)
        assert db.get_state("last_check_started_at") == NOW
        assert db.increment_state("consecutive_failures") == 1
        assert db.increment_state("consecutive_failures") == 2

        register(db, content_hash="2" * 64)
        counts = db.get_status_counts()
        assert counts[Category.SPECIALTY.value]["total"] == 1
        assert counts[Category.SPECIALTY.value]["pending"] == 1
        assert db.get_versions_count() == 1
        assert db.get_posted_versions_count() == 0
    finally:
        db.close()
