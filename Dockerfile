# syntax=docker/dockerfile:1

FROM python:3.12-slim

LABEL org.opencontainers.image.title="Bangumi-syncer"
LABEL org.opencontainers.image.description="Sync fnOS (飞牛影视) anime watch progress to Bangumi"
LABEL org.opencontainers.image.source="https://github.com/wwb051221/Bangumi-syncer"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.version="0.1.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Shanghai

WORKDIR /app

# The package has zero runtime dependencies, so the source tree *is* the app.
# Copying it keeps the image small and avoids a network install at build time.
COPY src/ ./src/
COPY config.example.toml README.md ./

RUN mkdir -p /data/state /data/snapshot /config

EXPOSE 8099
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3).read()" || exit 1

# Runs as root on purpose: the fnOS trim.media database is root:root 0700.
# Mount it read-only, and see README "安全" for running unprivileged instead.
ENTRYPOINT ["python", "-m", "bangumi_syncer"]
CMD ["run"]
