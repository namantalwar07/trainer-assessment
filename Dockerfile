FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[live]"
COPY config ./config
COPY prompts ./prompts
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures
RUN useradd -r -u 10001 appuser && mkdir -p /srv/data && chown -R appuser /srv/data
ENV DATA_DIR=/srv/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1
CMD ["sh", "/srv/app/scripts/docker-entrypoint.sh"]
