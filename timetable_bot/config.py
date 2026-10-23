"""Environment-based configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMETABLE_URL = "http://1epal-aigal.att.sch.gr/sched-stud/"
_PLACEHOLDER_TOKENS = {
    "",
    "your_discord_bot_token_here",
    "replace_me",
    "changeme",
    "paste_token_here",
}


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    guild_id: int | None
    channel_id: int | None
    test_channel_id: int | None
    timetable_url: str
    check_interval_minutes: int
    dry_run: bool
    baseline_on_first_run: bool
    database_path: Path
    download_dir: Path
    log_dir: Path
    log_level: str
    max_download_mb: int
    discord_max_attachment_mb: int
    request_timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    user_agent: str
    public_file_base_url: str | None
    public_dir: Path | None
    verify_ssl: bool

    @property
    def destination_channel_id(self) -> int | None:
        """A test channel, when configured, always wins."""

        return self.test_channel_id or self.channel_id

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    @property
    def discord_max_attachment_bytes(self) -> int:
        return self.discord_max_attachment_mb * 1024 * 1024


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _parse_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.casefold()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be true/false (got {raw!r})")


def _parse_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got {raw!r})") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {value})")
    return value


def _parse_optional_int(name: str) -> int | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a Discord ID (got {raw!r})") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive Discord ID")
    return value


def _parse_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _parse_path(name: str, default: str) -> Path:
    raw = _get(name, default) or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_config(*, require_discord: bool = True) -> Config:
    """Load and validate ``.env``/environment variables."""

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    token = (_get("DISCORD_TOKEN", "") or "").strip()
    if require_discord and token.casefold() in _PLACEHOLDER_TOKENS:
        raise ConfigError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token."
        )
    if token and token.casefold() in _PLACEHOLDER_TOKENS:
        token = ""

    guild_id = _parse_optional_int("GUILD_ID")
    channel_id = _parse_optional_int("CHANNEL_ID")
    test_channel_id = _parse_optional_int("TEST_CHANNEL_ID")
    dry_run = _parse_bool("DRY_RUN", True)

    if require_discord and not dry_run and channel_id is None and test_channel_id is None:
        raise ConfigError(
            "CHANNEL_ID (or TEST_CHANNEL_ID) is required when DRY_RUN=false."
        )

    interval = _parse_int("CHECK_INTERVAL_MINUTES", 7, minimum=1, maximum=1440)
    max_download_mb = _parse_int("MAX_DOWNLOAD_MB", 50, minimum=1, maximum=500)
    discord_attachment_mb = _parse_int("DISCORD_MAX_ATTACHMENT_MB", 10, minimum=1, maximum=500)

    public_base = _get("PUBLIC_FILE_BASE_URL")
    public_dir_raw = _get("PUBLIC_DIR")
    public_dir: Path | None = None
    if public_base:
        public_base = public_base.rstrip("/")
        public_dir = _parse_path("PUBLIC_DIR", "public/timetables")
    elif public_dir_raw:
        # A directory without a public URL is still useful for manual hosting,
        # but the bot cannot build a link from it.
        public_dir = _parse_path("PUBLIC_DIR", "public/timetables")

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ConfigError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

    timetable_url = _get("TIMETABLE_URL", DEFAULT_TIMETABLE_URL) or DEFAULT_TIMETABLE_URL

    return Config(
        discord_token=token,
        guild_id=guild_id,
        channel_id=channel_id,
        test_channel_id=test_channel_id,
        timetable_url=timetable_url,
        check_interval_minutes=interval,
        dry_run=dry_run,
        baseline_on_first_run=_parse_bool("BASELINE_ON_FIRST_RUN", True),
        database_path=_parse_path("DATABASE_PATH", "data/timetables.sqlite3"),
        download_dir=_parse_path("DOWNLOAD_DIR", "data/downloads"),
        log_dir=_parse_path("LOG_DIR", "logs"),
        log_level=log_level,
        max_download_mb=max_download_mb,
        discord_max_attachment_mb=discord_attachment_mb,
        request_timeout_seconds=_parse_int(
            "REQUEST_TIMEOUT_SECONDS", 30, minimum=5, maximum=300
        ),
        max_retries=_parse_int("MAX_RETRIES", 3, minimum=1, maximum=10),
        retry_backoff_seconds=_parse_float(
            "RETRY_BACKOFF_SECONDS", 2.0, minimum=0.0
        ),
        user_agent=_get(
            "USER_AGENT",
            "SchoolTimetableBot/1.0 (+https://github.com/; school timetable monitor)",
        )
        or "SchoolTimetableBot/1.0",
        public_file_base_url=public_base,
        public_dir=public_dir,
        verify_ssl=_parse_bool("VERIFY_SSL", True),
    )
