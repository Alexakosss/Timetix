"""Small, dependency-free helpers shared by the bot modules."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .categories import CATEGORY_INFO, Category

try:
    from zoneinfo import ZoneInfo

    ATHENS_TZ = ZoneInfo("Europe/Athens")
except Exception:  # pragma: no cover - only used if tzdata is missing
    from datetime import timedelta

    ATHENS_TZ = timezone(timedelta(hours=2), name="Europe/Athens")


_APOSTROPHE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u00b4": "'",
        "\u0384": "'",
        "\u0374": "'",
    }
)


def clean_text(value: str | None) -> str:
    """Collapse HTML whitespace without changing the actual words."""

    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_greek(value: str | None) -> str:
    """Return a comparison-friendly Greek string.

    Accents/tonos, apostrophes, punctuation, case and final sigma are removed so
    that ``Γ΄ Τάξης`` and ``Γ' ΤΑΞΗΣ`` compare identically.
    """

    if not value:
        return ""
    text = clean_text(value).translate(_APOSTROPHE_TRANSLATION)
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("ς", "σ")
    text = re.sub(r"[^0-9a-zα-ω]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify_category(value: str | None) -> Category | None:
    """Identify only the two Γ΄ categories requested by the user."""

    normalized = normalize_greek(value)
    if not normalized:
        return None

    # The class marker must be the standalone Greek letter gamma, not the
    # beginning of words such as «γενικής».
    if not re.search(r"(?:^|\s)γ(?=\s|$)", normalized):
        return None
    if not re.search(r"(?:^|\s)γ\s+ταξ", normalized):
        return None

    general_phrase = "γ ταξησ γενικησ παιδειασ" in normalized or (
        "γενικ" in normalized and "παιδει" in normalized
    )
    specialty_phrase = "γ ταξησ ειδικοτητασ" in normalized or "ειδικοτητ" in normalized

    if general_phrase and not specialty_phrase:
        return Category.GENERAL
    if specialty_phrase and not general_phrase:
        return Category.SPECIALTY

    # If a context block contains both words, prefer the explicit full phrase.
    if "γενικησ παιδειασ" in normalized:
        return Category.GENERAL
    if "ειδικοτητασ" in normalized:
        return Category.SPECIALTY
    return None


_DATE_WITH_SEPARATORS = re.compile(
    r"(?<!\d)(?P<a>\d{1,4})[-/_. ](?P<b>\d{1,2})[-/_. ](?P<c>\d{2,4})(?!\d)"
)
_SIX_DIGITS = re.compile(r"(?<!\d)(?P<dmy>\d{6})(?!\d)")
_EIGHT_DIGITS = re.compile(r"(?<!\d)(?P<ymd>\d{8})(?!\d)")


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year < 80 else 1900 + year


def extract_date_from_filename(filename: str | None) -> str | None:
    """Find a date in a filename such as ``Γ ειδικοτητα191026.pdf``.

    Returns an ISO ``YYYY-MM-DD`` string or ``None``. This is best-effort: the
    authoritative "updated" date is the HTTP ``Last-Modified`` header.
    """

    if not filename:
        return None
    name = unquote(Path(filename).name)

    for match in _DATE_WITH_SEPARATORS.finditer(name):
        raw_a, raw_b, raw_c = match.group("a"), match.group("b"), match.group("c")
        a, b, c = int(raw_a), int(raw_b), int(raw_c)

        if len(raw_a) == 4:
            parsed = _make_date(a, b, c)
        elif len(raw_c) == 4:
            parsed = _make_date(c, b, a)
        else:
            parsed = _make_date(_expand_year(c), b, a)
        if parsed:
            return parsed.isoformat()

    for match in _EIGHT_DIGITS.finditer(name):
        raw = match.group("ymd")
        parsed = _make_date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        if parsed:
            return parsed.isoformat()

    for match in _SIX_DIGITS.finditer(name):
        raw = match.group("dmy")
        parsed = _make_date(_expand_year(int(raw[4:6])), int(raw[2:4]), int(raw[0:2]))
        if parsed:
            return parsed.isoformat()

    return None


_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(value: str | None, fallback: str = "timetable.pdf") -> str:
    """Make a filename safe for Windows, Linux and Discord attachments."""

    name = clean_text(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")
    if not name:
        name = fallback

    stem, suffix = os.path.splitext(name)
    if stem.casefold() in _RESERVED_WINDOWS_NAMES:
        stem = f"_{stem}"
    if len(stem) > 120:
        stem = stem[:120]
    if len(suffix) > 12:
        suffix = suffix[:12]
    result = f"{stem}{suffix}"
    return result or fallback


def filename_from_url(url: str) -> str:
    path = urlsplit(url).path
    last = unquote(path.rsplit("/", 1)[-1])
    return sanitize_filename(last or "timetable.pdf")


def build_attachment_filename(category: Category, original: str | None) -> str:
    """Prefix the school's filename with a clear Greek category name."""

    info = CATEGORY_INFO[category]
    base = sanitize_filename(original or "timetable.pdf")
    stem, suffix = os.path.splitext(base)
    if suffix.casefold() != ".pdf":
        suffix = ".pdf"
    result = f"{info.filename_prefix}_{stem}{suffix}"
    if len(result) > 180:
        result = f"{result[:170]}{suffix}"
    return result


def validate_pdf_bytes(data: bytes) -> bool:
    """Reject HTML error pages and anything that is not actually a PDF."""

    if not data or len(data) < 5:
        return False
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"%PDF-"):
        return False
    if b"<html" in data[:512].lower():
        return False
    return True


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_http_date(value: str | None) -> str | None:
    """Convert an HTTP date header into an ISO timestamp (UTC)."""

    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def format_greek_datetime(value: datetime | str | None, *, include_seconds: bool = False) -> str:
    """Format a timestamp in Greek local time, e.g. ``23/10/2026 08:46``."""

    parsed = _as_datetime(value)
    if parsed is None:
        return "—"
    local = parsed.astimezone(ATHENS_TZ)
    fmt = "%d/%m/%Y %H:%M:%S" if include_seconds else "%d/%m/%Y %H:%M"
    return local.strftime(fmt)


def format_greek_date(value: date | datetime | str | None) -> str:
    """Format an ISO date (or datetime) as ``19/10/2026``."""

    if value is None:
        return "—"
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    return parsed.strftime("%d/%m/%Y")


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def truncate(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def is_same_host(first: str, second: str) -> bool:
    def host(url: str) -> str:
        netloc = urlsplit(url).netloc.lower()
        hostname = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
        return hostname.removeprefix("www.")

    try:
        return bool(host(first)) and host(first) == host(second)
    except ValueError:
        return False


def normalize_url(href: str | None, base_url: str) -> str | None:
    """Resolve a link and percent-encode Greek/space characters."""

    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    try:
        joined = urlsplit(href).geturl()
        from urllib.parse import urljoin, urlunsplit

        joined = urljoin(base_url, joined)
        parts = urlsplit(joined)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    path = quote(parts.path or "/", safe="/%:@&+$,-_.!~*'()")
    query = quote(parts.query, safe="=&?/:;+%@!~*'()")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}" + (
        f"?{query}" if query else ""
    )


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
