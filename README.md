# Ωρολόγιο Πρόγραμμα Bot — 1ο ΕΠΑΛ Αιγάλεω

A Discord bot that monitors the official timetable page of **1ο ΕΠΑΛ Αιγάλεω** and
posts new or updated timetables automatically.

It watches **only** these two categories:

| Category | Stored id | Announcement title |
| --- | --- | --- |
| Τμήματα Γ΄ Τάξης Ειδικότητας | `specialty` | `📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΕΙΔΙΚΟΤΗΤΑΣ` |
| Τμήματα Γ΄ Τάξης Γενικής Παιδείας | `general` | `📚 ΝΕΟ ΩΡΟΛΟΓΙΟ ΠΡΟΓΡΑΜΜΑ — Γ΄ ΤΑΞΗ ΓΕΝΙΚΗΣ ΠΑΙΔΕΙΑΣ` |

Every other class/category on the page is ignored.

Source page: <http://1epal-aigal.att.sch.gr/sched-stud/>

---

## How it works

1. Every `CHECK_INTERVAL_MINUTES` (default **7**) the bot fetches the timetable
   page.
2. It follows the links whose text/context names one of the two monitored Γ΄
   categories. Both links are currently direct PDFs, but the parser also follows
   category sub-pages (up to depth 2, same host) if the school changes the layout.
3. Each PDF is downloaded, validated (`%PDF-` magic bytes, no HTML error pages)
   and hashed with **SHA-256**.
4. The hash is compared with the SQLite history:
   * **new URL / new bytes** → posted as a new timetable;
   * **same URL, different bytes** → posted as an update (the school often
     overwrites the same file);
   * **same bytes** → never posted again, even if the URL changed;
   * **previous Discord upload failed** → retried on the next check.
5. The announcement contains the exact Greek title, detection date, the original
   school link and – when it fits Discord's limit – the PDF itself as an
   attachment. Files that are too large fall back to a download link (or a
   configurable public host), and are never silently dropped.
6. The bot itself never crashes on website/network errors: failures are logged,
   stored in `bot_state`, counted, and retried on the next interval.

State is kept in SQLite (`timetables`, `versions`, `bot_state`), so restarts do
not repost anything.

### First run / baseline

With `BASELINE_ON_FIRST_RUN=true` (default), the first check records the PDFs
that already exist **without posting them**. After that, only genuinely new or
modified files are announced. The same behaviour is available on demand with
`/baseline` or `python bot.py --baseline`.

### Dry run

`DRY_RUN=true` is the default. The bot does everything (discovery, download,
hashing, database writes, logging) except talking to Discord. Set
`DRY_RUN=false` only when you are ready for real announcements. A
`TEST_CHANNEL_ID`, when set, overrides `CHANNEL_ID` for all posts.

---

## Requirements

* Python **3.12** (3.11 works too)
* A Discord application / bot token
* Write access to `data/` and `logs/`

Dependencies are listed in `requirements.txt` (`discord.py`, `aiohttp`,
`beautifulsoup4`, `python-dotenv`, `tzdata`).

---

## 1. Create the Discord application

1. Open <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token. Keep it secret.
3. **Installation** / **OAuth2 → URL Generator**:
   * Scopes: `bot`, `applications.commands`
   * Bot permissions:
     * View Channels
     * Send Messages
     * Attach Files
     * Embed Links
     * Read Message History
4. Open the generated URL and invite the bot to your server.
5. Enable **Developer Mode** in Discord (User Settings → Advanced), then
   right-click the target channel → **Copy Channel ID**. Right-click the server →
   **Copy Server ID** for `GUILD_ID`.

Slash commands are registered in `GUILD_ID` instantly when set; otherwise the
global registration can take up to an hour.

---

## 2. Configure

```bash
cp .env.example .env       # Windows: copy .env.example .env
```

Edit `.env`:

```dotenv
DISCORD_TOKEN=...           # required for normal operation
GUILD_ID=123456789012345678 # optional, instant slash commands
CHANNEL_ID=123456789012345678
DRY_RUN=true                # start in dry run!
```

Useful options (full list and comments in `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (never hardcoded) |
| `GUILD_ID` | — | Server for instant command sync |
| `CHANNEL_ID` | — | Announcement channel |
| `TEST_CHANNEL_ID` | — | Overrides `CHANNEL_ID` |
| `DRY_RUN` | `true` | `false` = really post |
| `BASELINE_ON_FIRST_RUN` | `true` | Record existing files silently |
| `TIMETABLE_URL` | school page | Override the monitored page |
| `CHECK_INTERVAL_MINUTES` | `7` | 5–10 recommended |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per HTTP request |
| `MAX_RETRIES` | `3` | Retries for 5xx/429/network errors |
| `RETRY_BACKOFF_SECONDS` | `2.0` | Exponential backoff base |
| `MAX_DOWNLOAD_MB` | `50` | Refuse larger PDFs |
| `DISCORD_MAX_ATTACHMENT_MB` | `10` | Upload cap (guild limit also respected) |
| `PUBLIC_FILE_BASE_URL` / `PUBLIC_DIR` | — | Optional hosting for large files |
| `DATABASE_PATH` | `data/timetables.sqlite3` | SQLite file |
| `DOWNLOAD_DIR` | `data/downloads` | Local audit copy of each PDF |
| `LOG_DIR` | `logs` | Rotating log files |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `VERIFY_SSL` | `true` | Only disable for a broken school certificate |

---

## 3. Install and run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

python bot.py --check-only   # inspect the site, write nothing, no Discord
python bot.py --baseline     # record existing PDFs, post nothing
python bot.py                # run the bot
```

`--check-only` prints JSON with the discovered categories, URLs, sizes and
SHA-256 hashes; it never writes the database and never contacts Discord.

---

## 4. Slash commands

| Command | Who | What it does |
| --- | --- | --- |
| `/check_timetables` | everyone | Runs a check immediately; shows a Greek summary (respects `DRY_RUN`) |
| `/status` | everyone | Current state, last check, next check, counts, errors, uptime |
| `/baseline` | Manage Server | Records current PDFs without posting |
| `/test_post` | Manage Server | Sends a test message; requires `DRY_RUN=false` |

Command responses are ephemeral. Manual checks are rate-limited with a cooldown.

---

## 5. Deployment

### Option A — Docker (recommended)

```bash
mkdir -p data logs
cp .env.example .env
# edit .env; keep DRY_RUN=true for the first run

docker compose up -d --build
docker compose logs -f
```

The compose file mounts `./data` and `./logs`, restarts automatically
(`unless-stopped`), uses `Europe/Athens`, and stops gracefully on SIGTERM.
SQLite and downloaded PDFs survive container rebuilds.

To enable large-file hosting, set `PUBLIC_FILE_BASE_URL` in `.env` and
uncomment the `./public` volume in `docker-compose.yml`, then serve `public/`
with nginx/Caddy.

### Option B — systemd on a VPS (Debian/Ubuntu)

```bash
sudo useradd --system --home /opt/timetable-bot timetable
sudo mkdir -p /opt/timetable-bot
sudo chown -R timetable:timetable /opt/timetable-bot

# copy bot.py, timetable_bot/, requirements.txt into /opt/timetable-bot
cd /opt/timetable-bot
sudo -u timetable python3 -m venv .venv
sudo -u timetable .venv/bin/pip install -r requirements.txt

sudo -u timetable cp .env.example .env
sudo -u timetable nano .env

sudo cp deploy/timetable-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timetable-bot
sudo journalctl -u timetable-bot -f
```

The unit restarts on failure (`Restart=always`), runs unprivileged, and only
allows writes to `data/`, `logs/` and `public/`.

If your paths differ, adjust `WorkingDirectory`, `EnvironmentFile`,
`ExecStart` and `ReadWritePaths` in the unit file.

---

## 6. Logging, recovery and maintenance

* **Logs:** console + `logs/timetable-bot.log` (5 × 5 MB rotation).
* **State:** `data/timetables.sqlite3`; every downloaded PDF is also kept under
  `data/downloads/<category>/` for auditing.
* **Network/site failures:** retried up to `MAX_RETRIES` with exponential
  backoff; persistent failures are shown by `/status` and retried next interval.
* **Discord failures:** the version stays unposted and is retried on the next
  check, so a temporary outage does not lose an announcement.
* **Restart-safe:** hashes and posted flags live in SQLite; nothing is reposted
  after a restart.
* **Invalid files:** HTML error pages, truncated downloads and non-PDF responses
  are rejected and logged; they are never posted.
* **Attachment too large:** the bot checks both `DISCORD_MAX_ATTACHMENT_MB` and
  the guild's limit, then falls back to a public URL (if configured) or the
  original school link, with an explicit note in the message.
* **Backup:** stop the bot and copy `data/` (or at least the `.sqlite3` file),
  then restart.
* **Reset history:** stop the bot, delete `data/timetables.sqlite3`, start again
  (`BASELINE_ON_FIRST_RUN` will re-baseline silently).

### Why not trust filenames or ETags?

The school publishes files such as `Γ ειδικοτητα191026.pdf` and may overwrite
them in place. Filenames, `Last-Modified` and ETag are treated as hints only;
the SHA-256 of the actual bytes is the source of truth.

---

## 7. Development and tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests cover category matching, URL/filename/date helpers, Greek message
building, SQLite change-detection (including baseline, duplicate content across
URLs and failed-post retries) and HTML discovery against a realistic fixture.

```
bot.py                  entry point, Discord lifecycle, scheduler, CLI
timetable_bot/
  config.py             environment configuration and validation
  categories.py         the two monitored categories and exact titles
  scraper.py            HTML discovery, HTTP retries, PDF validation
  storage.py            SQLite schema and change decisions
  monitor.py            check orchestration, baseline, summaries
  notifier.py           Discord posting, attachment limits, large-file fallback
  messages.py           Greek announcement text
  discord_ui.py         slash commands and status embed
  utils.py              Greek normalisation, hashing, dates, filenames
tests/                  offline pytest suite + HTML fixture
deploy/                 systemd unit
```

### Manual verification without Discord

```bash
python bot.py --check-only
```

Expected for the current site: exactly two PDFs (one per category), both
`application/pdf`, valid `%PDF-`, with distinct SHA-256 hashes. The command
exits non-zero if any monitored PDF cannot be downloaded or validated.
