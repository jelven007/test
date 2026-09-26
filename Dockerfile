FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 banxia \
    && useradd --system --uid 10001 --gid banxia --create-home banxia

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
ARG PIP_INDEX_URL
RUN python -m pip install --no-cache-dir ".[production]"

COPY config ./config

RUN mkdir -p /app/data/wal /app/reports /app/scheduled_reports \
    && chown -R banxia:banxia /app

USER banxia

ENTRYPOINT ["banxia-service"]
