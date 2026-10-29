# Timetable Discord Bot

A Discord bot that monitors a school's official timetable page and automatically posts new or updated timetables to Discord. 

It watches for **only** the two Grade 3 categories:

| Category | Stored ID | Announcement Title |
| --- | --- | --- |
| Grade 3 - Specialization | `specialty` | `📚 NEW TIMETABLE — GRADE 3 SPECIALIZATION` |
| Grade 3 - General Education | `general` | `📚 NEW TIMETABLE — GRADE 3 GENERAL EDUCATION` |

All other classes and categories on the page are ignored.

Default target page: <http://1epal-aigal.att.sch.gr/sched-stud/>

---

## How It Works

1. Every `CHECK_INTERVAL_MINUTES` (default: **7**), the bot fetches the timetable page.
2. It finds links related to the two monitored Grade 3 categories. While the current site uses direct PDFs, the parser can also follow category subpages (up to depth 2, same host) if the layout changes.
3. Each PDF is downloaded, validated (`%PDF-` magic bytes, rejects HTML error pages), and hashed with **SHA-256**.
4. The hash is compared against the SQLite history:
   - **New URL / new content** → Posted as a new timetable
   - **Same URL, different content** → Posted as an update (schools often overwrite the same file)
   - **Same content** → Never reposted, even if the URL changes
   - **Failed Discord upload** → Retried on the next check
5. Announcements include the exact title, detection time, original school link, and if it fits Discord's attachment limit, the PDF attached directly. Large files fall back to a download link (or a configurable public host) and are never silently dropped.
6. The bot is resilient to network/site errors. Failures are logged, stored in `bot_state`, counted, and retried on the next interval.

All state is stored in SQLite (`timetables`, `versions`, `bot_state`), so restarts won't repost anything.

### First Run / Baseline

With `BASELINE_ON_FIRST_RUN=true` (default), the first check records existing PDFs **without posting them**. After that, only new or modified files are announced. You can do this manually with `/baseline` or `python bot.py --baseline`.

### Dry Run

`DRY_RUN=true` is the default. The bot will do everything (discovery, download, hashing, DB writes, logging) except sending messages to Discord. Set `DRY_RUN=false` only when you're ready to post. Setting `TEST_CHANNEL_ID` will override `CHANNEL_ID` for all posts.

### Multi-Server Support

The bot now supports being added to multiple Discord servers. Each server can be configured independently:

1. Invite the bot to your server
2. Run `/setup #your-channel` as a user with `Manage Server` permissions
3. The bot will post announcements to that channel

Use `/disable` to stop announcements for your server. Global `TEST_CHANNEL_ID` and `DRY_RUN` still take precedence for testing.

---

## Requirements

- Python **3.12** (3.11 also works)
- A Discord application/bot token
- Write access to `data/` and `logs/`

Dependencies: `discord.py`, `aiohttp`, `beautifulsoup4`, `python-dotenv`, `PyMuPDF`, `tzdata` (see `requirements.txt`)

---

## 1. Create a Discord Application

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. Go to **Bot** tab → **Reset Token** → Copy the token (keep it secret)
3. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions:
     - View Channels
     - Send Messages
     - Attach Files
     - Embed Links
     - Read Message History
4. Use the generated URL to invite the bot to your server
5. Enable **Developer Mode** in Discord (User Settings → Advanced), right-click your server → **Copy Server ID**, right-click your channel → **Copy Channel ID**

For instant slash command sync, set `GUILD_ID`. Without it, global commands can take up to an hour to appear.

---

## 2. Configuration

```bash
cp .env.example .env  # Windows: copy .env.example .env
```

Edit `.env`:

```dotenv
DISCORD_TOKEN=...           # Required
GUILD_ID=...               # Optional, for instant command sync
CHANNEL_ID=...             # Default channel (or use /setup per server)
DRY_RUN=true               # Keep true until you're ready
```

Key options (see `.env.example` for full details):

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token |
| `GUILD_ID` | — | Server ID for instant slash command registration |
| `CHANNEL_ID` | — | Default announcement channel |
| `TEST_CHANNEL_ID` | — | Overrides all channels (great for testing) |
| `DRY_RUN` | `true` | If true, no messages are sent to Discord |
| `BASELINE_ON_FIRST_RUN` | `true` | Record existing files silently on first check |
| `TIMETABLE_URL` | School page | Override the monitored page |
| `CHECK_INTERVAL_MINUTES` | `7` | Recommended: 5-10 |
| `REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout |
| `MAX_RETRIES` | `3` | Retries for 5xx/429/network errors |
| `RETRY_BACKOFF_SECONDS` | `2.0` | Exponential backoff base |
| `MAX_DOWNLOAD_MB` | `50` | Max PDF size to download |
| `DISCORD_MAX_ATTACHMENT_MB` | `10` | Upload limit (also respects guild limit) |
| `PUBLIC_FILE_BASE_URL` / `PUBLIC_DIR` | — | Optional hosting for large files |
| `DATABASE_PATH` | `data/timetables.sqlite3` | SQLite database |
| `DOWNLOAD_DIR` | `data/downloads` | Local audit copies |
| `LOG_DIR` | `logs` | Log files |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `VERIFY_SSL` | `true` | Disable only for broken TLS certificates |

---

## 3. Install & Run Locally

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt

python bot.py --check-only   # Inspect site, no DB/Discord
python bot.py --baseline     # Record current files, no posting
python bot.py                # Run the bot
```

`--check-only` prints JSON with discovered files, sizes, and SHA-256 hashes. It never writes to the database or contacts Discord.

---

## 4. Slash Commands

| Command | Permissions | Description |
| --- | --- | --- |
| `/setup <channel>` | Manage Server | Configure which channel receives announcements for this server |
| `/disable` | Manage Server | Disable announcements for this server |
| `/check_timetables` | Everyone | Manually check for new/updated timetables |
| `/status` | Everyone | View bot status, last check, next check, counts, errors, uptime |
| `/send_timetable <category> <format>` | Everyone | Send current timetables on-demand (PDF, JPG, or both) |
| `/baseline` | Manage Server | Record current PDFs without posting |
| `/test_post` | Manage Server | Send a test message (requires `DRY_RUN=false`) |

Command responses are ephemeral. Manual checks have cooldowns.

---

## 5. Deployment

### Option A: Docker (Recommended)

```bash
mkdir -p data logs
cp .env.example .env
# Edit .env, keep DRY_RUN=true for first run

docker compose up -d --build
docker compose logs -f
```

Data persists in `./data` and `./logs`. The container auto-restarts and stops gracefully.

### Option B: systemd on Linux VPS

```bash
sudo useradd --system --home /opt/timetable-bot timetable
sudo mkdir -p /opt/timetable-bot
sudo chown -R timetable:timetable /opt/timetable-bot

# Copy files to /opt/timetable-bot
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

### Hosting Options

| Host | Free | Notes |
| --- | --- | --- |
| [Railway](https://railway.app/) | Yes | Easiest, GitHub integration, great for Discord bots |
| [Render](https://render.com/) | Yes | Simple deployment, free tier available |
| [Oracle Cloud Always Free](https://www.oracle.com/cloud/free/) | Yes (forever) | Best long-term, full VPS control |
| [Hetzner](https://www.hetzner.com/) | No (~€4/month) | Cheap, reliable, great performance |

---

## 6. Logging, Recovery & Maintenance

- **Logs:** Console + `logs/timetable-bot.log` (5 × 5MB rotation)
- **Audit trail:** Every downloaded PDF saved to `data/downloads/<category>/`
- **Resilient:** Network failures retried with backoff, shown in `/status`
- **Restart-safe:** Hashes and post history in SQLite, nothing gets reposted
- **Validation:** HTML error pages and invalid files are rejected and logged
- **Large files:** Falls back to link if over Discord limit (or use public hosting)
- **Backup:** Stop bot and copy `data/` folder
- **Reset history:** Stop bot, delete `data/timetables.sqlite3`, restart (will baseline silently)

**Note:** Filenames, `Last-Modified`, and ETag are treated as hints only. SHA-256 of the actual file bytes is the source of truth since schools often overwrite files in place.

---

## 7. Development & Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests cover category matching, URL/filename/date utilities, Greek messages, SQLite change detection (baseline, cross-URL duplicates, failed post retries), and HTML discovery against fixtures.

### Project Structure

```
bot.py                  Entry point, Discord lifecycle, scheduler, CLI
timetable_bot/
  config.py             Environment config & validation
  categories.py         Monitored categories & announcement titles
  scraper.py            HTML discovery, HTTP retries, PDF validation
  storage.py            SQLite schema & change detection
  monitor.py            Check orchestration, baseline, summaries
  notifier.py           Discord posting, attachments, fallbacks
  messages.py           Announcement text
  discord_ui.py         Slash commands & status embeds
  utils.py              Greek normalization, hashing, dates, filenames
tests/                  Pytest suite + fixtures
deploy/                 systemd unit
```

### Manual Verification

```bash
python bot.py --check-only
```

For the current site, this should find exactly 2 PDFs (one per category), both `application/pdf`, valid `%PDF-`, with different SHA-256 hashes. Exits non-zero if any monitored PDF can't be downloaded/validated.
