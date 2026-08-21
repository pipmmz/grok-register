# Lightweight image for headless Linux:
# WebUI/CLI + Xvfb + Chromium (headed under virtual display).
# Base is slim; no desktop stack.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    GROK_REGISTER_HOST=0.0.0.0 \
    GROK_REGISTER_PORT=8092 \
    # DrissionPage / Chromium
    BROWSER_PATH=/usr/bin/chromium \
    CHROME_BIN=/usr/bin/chromium \
    CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"

WORKDIR /app

# Minimal runtime for Chromium under Xvfb.
# chromium pulls its own deps; keep recommends off for size.
# chromium-sandbox not needed: we pass --no-sandbox for containers.
# Skip heavy CJK font packs to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-liberation \
        ca-certificates \
        curl \
        xvfb \
        procps \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Python deps (web stack included so one image covers CLI + WebUI).
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# App source (filtered by .dockerignore).
COPY . .

RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /data /data/cpa_auths /data/screenshots \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /app /data

# Chromium in containers needs these; also help DrissionPage find the binary.
ENV PATH="/usr/bin:${PATH}"

USER app
VOLUME ["/data"]
EXPOSE 8092

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${GROK_REGISTER_PORT}/api/config" >/dev/null || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
