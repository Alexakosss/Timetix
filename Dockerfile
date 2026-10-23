FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Athens

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./
COPY timetable_bot ./timetable_bot

# Drop privileges; data/logs are mounted as volumes and must be writable.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs /app/public \
    && chown -R bot:bot /app
USER bot

VOLUME ["/app/data", "/app/logs"]
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "bot.py"]
