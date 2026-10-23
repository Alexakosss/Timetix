"""Timetable page discovery and PDF downloading."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .categories import CATEGORY_INFO, CATEGORY_ORDER, Category
from .config import Config
from .utils import (
    build_attachment_filename,
    classify_category,
    clean_text,
    ensure_directory,
    extract_date_from_filename,
    filename_from_url,
    is_same_host,
    normalize_url,
    parse_http_date,
    sanitize_filename,
    sha256_hex,
    utc_now_iso,
    validate_pdf_bytes,
)

logger = logging.getLogger(__name__)

_HTML_MAX_BYTES = 5 * 1024 * 1024
_MAX_SUBPAGES = 10
_MAX_DEPTH = 2
_OFFICE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
}
_HTML_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cgi"}
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)


class ScraperError(RuntimeError):
    """A recoverable website/network/parsing error."""


class DiscoveryError(ScraperError):
    """The timetable page could not produce any monitored links."""


class InvalidDocumentError(ScraperError):
    """The downloaded bytes are not a real PDF."""


class DownloadTooLargeError(ScraperError):
    """A file exceeded the configured download limit."""


class _TransientHttpError(ScraperError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class DiscoveredDocument:
    category: Category
    url: str
    filename: str
    link_text: str
    source_page: str
    published_date: str | None


@dataclass(frozen=True, slots=True)
class SubpageCandidate:
    category: Category
    url: str
    link_text: str


@dataclass(frozen=True, slots=True)
class UnsupportedLink:
    category: Category
    url: str
    link_text: str
    reason: str


@dataclass(slots=True)
class ParsedPage:
    documents: list[DiscoveredDocument] = field(default_factory=list)
    subpages: list[SubpageCandidate] = field(default_factory=list)
    unsupported: list[UnsupportedLink] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    source_url: str
    fetched_at: str
    documents: list[DiscoveredDocument]
    warnings: list[str]
    unsupported: list[UnsupportedLink]


@dataclass(frozen=True, slots=True)
class DownloadedDocument:
    document: DiscoveredDocument
    data: bytes
    content_hash: str
    content_type: str
    resolved_url: str
    last_modified: str | None
    detected_at: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def attachment_filename(self) -> str:
        return build_attachment_filename(self.document.category, self.document.filename)


def _is_pdf_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path.casefold().endswith(".pdf") or ".pdf" in parts.query.casefold()


def _file_suffix(url: str) -> str:
    return Path(urlsplit(url).path).suffix.casefold()


def _context_text(anchor) -> str:
    """Text around a link, used when the anchor itself is just «εδώ»."""

    parts: list[str] = []
    parent = anchor.parent
    for _ in range(4):
        if parent is None or not getattr(parent, "name", None):
            break
        if parent.name in {
            "li",
            "p",
            "td",
            "th",
            "dd",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            text = clean_text(parent.get_text(" ", strip=True))
            if text and len(text) <= 700:
                parts.append(text)
        parent = parent.parent

    # Only real headings may lend their category to a bare «εδώ» link. Bold text
    # is often just the label of an unrelated, earlier link in the same list, so
    # it must not leak a category into the following paragraph.
    heading = anchor.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading is not None:
        text = clean_text(heading.get_text(" ", strip=True))
        if text and len(text) <= 250:
            parts.append(text)

    return " ".join(parts)


def parse_timetable_html(
    html: str,
    base_url: str,
    *,
    inherited_category: Category | None = None,
) -> ParsedPage:
    """Extract monitored PDF links from a timetable page.

    This function is deliberately network-free so it can be unit tested.
    """

    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.find("main") or soup
    parsed = ParsedPage()
    seen_documents: set[tuple[Category, str]] = set()
    seen_subpages: set[tuple[Category, str]] = set()
    seen_unsupported: set[tuple[Category, str]] = set()

    for anchor in root.find_all("a", href=True):
        href = anchor.get("href")
        if isinstance(href, list):
            href = href[0] if href else None
        absolute = normalize_url(href, base_url)
        if not absolute:
            continue

        text = clean_text(anchor.get_text(" ", strip=True))
        title = clean_text(anchor.get("title") if isinstance(anchor.get("title"), str) else "")
        explicit = classify_category(text) or classify_category(title)
        category = explicit or inherited_category or classify_category(_context_text(anchor))
        if category is None:
            continue

        suffix = _file_suffix(absolute)
        if _is_pdf_url(absolute):
            if (category, absolute) in seen_documents:
                continue
            seen_documents.add((category, absolute))
            filename = filename_from_url(absolute)
            parsed.documents.append(
                DiscoveredDocument(
                    category=category,
                    url=absolute,
                    filename=filename,
                    link_text=text or filename,
                    source_page=base_url,
                    published_date=extract_date_from_filename(filename),
                )
            )
            continue

        if suffix in _OFFICE_EXTENSIONS:
            if (category, absolute) not in seen_unsupported:
                seen_unsupported.add((category, absolute))
                parsed.unsupported.append(
                    UnsupportedLink(
                        category=category,
                        url=absolute,
                        link_text=text or absolute,
                        reason=f"unsupported document type: {suffix}",
                    )
                )
            continue

        if suffix in _HTML_EXTENSIONS:
            if (category, absolute) not in seen_subpages:
                seen_subpages.add((category, absolute))
                parsed.subpages.append(
                    SubpageCandidate(
                        category=category,
                        url=absolute,
                        link_text=text or absolute,
                    )
                )

    for item in parsed.unsupported:
        parsed.warnings.append(
            f"Μη υποστηριζόμενη μορφή αρχείου ({item.reason}) για "
            f"{CATEGORY_INFO[item.category].label}: {item.url}"
        )
    return parsed


def decode_html(data: bytes, content_type: str | None = None) -> str:
    """Decode the school page, which declares UTF-8 but may fall back to 1253."""

    charset: str | None = None
    match = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        meta = _META_CHARSET.search(data[:4096])
        if meta:
            charset = meta.group(1).decode("ascii", errors="ignore")

    for encoding in (charset, "utf-8", "windows-1253", "iso-8859-7"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


class TimetableScraper:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    async def discover(self) -> DiscoveryResult:
        main_data, main_final_url, main_content_type, _ = await self._fetch(
            self.config.timetable_url,
            max_bytes=min(_HTML_MAX_BYTES, self.config.max_download_bytes),
            purpose="timetable page",
        )
        if validate_pdf_bytes(main_data) or (main_content_type or "").startswith(
            "application/pdf"
        ):
            raise DiscoveryError(
                "The configured TIMETABLE_URL returned a PDF instead of an HTML page."
            )

        html = decode_html(main_data, main_content_type)
        first_page = parse_timetable_html(html, main_final_url)
        documents = list(first_page.documents)
        warnings = list(first_page.warnings)
        unsupported = list(first_page.unsupported)

        queue: deque[tuple[Category, str, str, int]] = deque(
            (candidate.category, candidate.url, candidate.link_text, 1)
            for candidate in first_page.subpages
        )
        visited: set[str] = {main_final_url, normalize_url(self.config.timetable_url, main_final_url) or main_final_url}
        followed_pages = 0

        while queue and followed_pages < _MAX_SUBPAGES:
            category, url, link_text, depth = queue.popleft()
            if url in visited or not is_same_host(url, main_final_url):
                continue
            visited.add(url)
            try:
                data, final_url, content_type, _ = await self._fetch(
                    url,
                    max_bytes=min(_HTML_MAX_BYTES, self.config.max_download_bytes),
                    purpose=f"category page for {CATEGORY_INFO[category].label}",
                )
            except ScraperError as exc:
                warnings.append(f"Δεν ήταν δυνατή η ανάκτηση υποσελίδας: {url} ({exc})")
                continue

            followed_pages += 1
            if validate_pdf_bytes(data):
                filename = filename_from_url(final_url)
                documents.append(
                    DiscoveredDocument(
                        category=category,
                        url=final_url,
                        filename=filename,
                        link_text=link_text or filename,
                        source_page=main_final_url,
                        published_date=extract_date_from_filename(filename),
                    )
                )
                continue

            if (content_type or "").startswith("application/pdf"):
                warnings.append(f"Μη έγκυρο PDF από υποσελίδα: {url}")
                continue

            sub_html = decode_html(data, content_type)
            sub_page = parse_timetable_html(
                sub_html, final_url, inherited_category=category
            )
            documents.extend(sub_page.documents)
            warnings.extend(sub_page.warnings)
            unsupported.extend(sub_page.unsupported)

            if depth < _MAX_DEPTH:
                for candidate in sub_page.subpages:
                    if candidate.url not in visited:
                        queue.append(
                            (candidate.category, candidate.url, candidate.link_text, depth + 1)
                        )
            await asyncio.sleep(0.25)

        # Deduplicate and sort deterministically.
        unique: dict[tuple[Category, str], DiscoveredDocument] = {}
        for document in documents:
            unique.setdefault((document.category, document.url), document)
        ordered = sorted(
            unique.values(),
            key=lambda doc: (doc.category.value, doc.filename.casefold(), doc.url),
        )

        found_categories = {document.category for document in ordered}
        for category in CATEGORY_ORDER:
            if category not in found_categories:
                warnings.append(
                    "Δεν βρέθηκε σύνδεσμος για την κατηγορία "
                    f"«{CATEGORY_INFO[category].label}»."
                )

        if not ordered:
            detail = f" Warnings: {'; '.join(warnings)}" if warnings else ""
            raise DiscoveryError(
                "No PDF links matched the Γ΄ Τάξης Ειδικότητας or "
                f"Γ΄ Τάξης Γενικής Παιδείας categories.{detail}"
            )

        for document in ordered:
            logger.info(
                "Discovered [%s] %s -> %s",
                CATEGORY_INFO[document.category].label,
                document.link_text,
                document.url,
            )
        for warning in warnings:
            logger.warning("Discovery warning: %s", warning)

        return DiscoveryResult(
            source_url=main_final_url,
            fetched_at=utc_now_iso(),
            documents=ordered,
            warnings=warnings,
            unsupported=unsupported,
        )

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------
    async def download(self, document: DiscoveredDocument) -> DownloadedDocument:
        data, final_url, content_type, headers = await self._fetch(
            document.url,
            max_bytes=self.config.max_download_bytes,
            purpose=f"PDF for {CATEGORY_INFO[document.category].label}",
        )
        if not validate_pdf_bytes(data):
            preview = data[:120].decode("utf-8", errors="replace").replace("\n", " ")
            raise InvalidDocumentError(
                f"Refusing to use non-PDF response for {document.url} "
                f"(content-type={content_type!r}, first bytes={preview!r})"
            )

        return DownloadedDocument(
            document=document,
            data=data,
            content_hash=sha256_hex(data),
            content_type=content_type,
            resolved_url=final_url,
            last_modified=parse_http_date(headers.get("Last-Modified")),
            detected_at=utc_now_iso(),
        )

    async def save_download(self, downloaded: DownloadedDocument) -> Path:
        category_info = CATEGORY_INFO[downloaded.document.category]
        directory = ensure_directory(self.config.download_dir / category_info.slug)
        filename = downloaded.attachment_filename
        path = directory / f"{downloaded.content_hash[:16]}_{filename}"
        if not path.exists():
            await asyncio.to_thread(self._atomic_write, path, downloaded.data)
        return path

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    # ------------------------------------------------------------------
    # HTTP with retries
    # ------------------------------------------------------------------
    async def _fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        purpose: str,
    ) -> tuple[bytes, str, str, dict[str, str]]:
        attempts = self.config.max_retries
        last_error: Exception | None = None
        timeout = aiohttp.ClientTimeout(
            total=self.config.request_timeout_seconds,
            connect=min(10, self.config.request_timeout_seconds),
            sock_read=self.config.request_timeout_seconds,
        )

        for attempt in range(1, attempts + 1):
            try:
                async with self.session.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    max_redirects=10,
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        retry_after = _retry_after_seconds(
                            response.headers.get("Retry-After")
                        )
                        raise _TransientHttpError(
                            f"HTTP {response.status} for {url}", retry_after=retry_after
                        )
                    if response.status >= 400:
                        raise ScraperError(f"HTTP {response.status} for {url}")

                    content_type = response.headers.get("Content-Type", "")
                    declared_length = response.headers.get("Content-Length")
                    if declared_length:
                        try:
                            if int(declared_length) > max_bytes:
                                raise DownloadTooLargeError(
                                    f"{url} is larger than the configured limit "
                                    f"({max_bytes} bytes)"
                                )
                        except ValueError:
                            pass

                    data = await self._read_limited(response, max_bytes)
                    return (
                        data,
                        str(response.url),
                        content_type,
                        dict(response.headers),
                    )
            except DownloadTooLargeError:
                raise
            except _TransientHttpError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = exc.retry_after
                if delay is None:
                    delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                delay = max(0.0, min(delay, 30.0))
                logger.warning(
                    "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                    purpose,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                    purpose,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise ScraperError(
            f"{purpose} failed after {attempts} attempt(s): {url}: {last_error}"
        )

    @staticmethod
    async def _read_limited(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise DownloadTooLargeError(
                    f"Response exceeded configured limit of {max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# Re-exported for convenience in tests and callers.
__all__ = [
    "DiscoveryError",
    "DiscoveryResult",
    "DiscoveredDocument",
    "DownloadedDocument",
    "DownloadTooLargeError",
    "InvalidDocumentError",
    "ParsedPage",
    "ScraperError",
    "SubpageCandidate",
    "TimetableScraper",
    "UnsupportedLink",
    "decode_html",
    "parse_timetable_html",
    "sanitize_filename",
]
