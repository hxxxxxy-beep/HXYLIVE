# syntax=docker/dockerfile:1
FROM python:3.11-slim

ARG APP_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/data \
    PORT=8080 \
    HXYLIVE_DNS_CACHE=false \
    APP_VERSION=${APP_VERSION}

# Install ffmpeg, optional local DNS cache support, and build dependencies for native packages (psutil on arm64)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates dnsmasq-base gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies and the Chromium runtime used by protected providers.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium && \
    apt-get purge -y --auto-remove gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy source
COPY app ./app
COPY static ./static
COPY README.md ./
COPY docker/entrypoint.sh /usr/local/bin/hxylive-entrypoint
RUN chmod +x /usr/local/bin/hxylive-entrypoint && \
    test -x /usr/local/bin/hxylive-entrypoint && \
    HXYLIVE_ENTRYPOINT_TESTING=1 /usr/local/bin/hxylive-entrypoint

# Create data volume for recordings
VOLUME ["/data"]

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/hxylive-entrypoint"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
