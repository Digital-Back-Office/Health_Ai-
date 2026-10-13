FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && rm -rf app build *.egg-info

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# /data holds the ElevenLabs agent state written by setup-agent (a volume in docker-compose.yml).
RUN useradd --system --uid 1001 --home /app appuser && mkdir -p /data && chown appuser /app /data
ENV ELEVENLABS_STATE_FILE=/data/elevenlabs-state.json
USER appuser

EXPOSE 8000
# --proxy-headers: the real client IP (per-IP rate limit) comes from nginx's X-Forwarded-For.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
