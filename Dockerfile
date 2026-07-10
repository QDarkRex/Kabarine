FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /bot

# ffmpeg/ffprobe: compress oversized videos. gosu: drop root after fixing volume perms.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

# install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Container starts as root ONLY to make the mounted ./data writable, then immediately
# drops to the unprivileged 'appuser' for the actual bot process.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /bot/data/downloads; chown -R appuser:appuser /bot/data 2>/dev/null || true; exec gosu appuser \"$@\"", "sh"]
CMD ["python", "bot.py"]
