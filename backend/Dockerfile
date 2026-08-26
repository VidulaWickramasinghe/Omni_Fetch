# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG DENO_IMAGE=denoland/deno:bin-2.9.5

FROM ${DENO_IMAGE} AS deno

FROM ${PYTHON_IMAGE} AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    OMNIFETCH_DOWNLOAD_DIR=/data/downloads \
    OMNIFETCH_FRONTEND_DIR=/app/frontend

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 omnifetch \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin omnifetch

WORKDIR /app

# yt-dlp requires an external JavaScript runtime for full YouTube support.
COPY --from=deno /deno /usr/local/bin/deno

COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY --chown=10001:10001 backend/app ./app
COPY --chown=10001:10001 frontend ./frontend
RUN mkdir -p /data/downloads \
    && chown -R 10001:10001 /data

USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
