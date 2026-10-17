FROM python:3.12.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SUBSCRIBE_CONFIG=/app/config/subscribe_msg.yaml

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY scripts/init_clickhouse.py ./scripts/init_clickhouse.py
COPY subscribe_msg.yaml ./config/subscribe_msg.yaml

RUN python -m pip install --constraint constraints.txt . \
    && chown -R app:app /app

USER 10001:10001

CMD ["python", "-m", "app.main"]


FROM runtime AS tests

USER root
RUN python -m pip install --constraint constraints.txt pytest==9.1.1
COPY --chown=app:app scripts/run_integration.sh ./scripts/run_integration.sh
COPY --chown=app:app docker-compose.local.yml ./docker-compose.local.yml
COPY --chown=app:app subscribe_msg.yaml ./subscribe_msg.yaml
COPY --chown=app:app docs/fixtures/messages.json ./docs/fixtures/messages.json
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app tests ./tests
USER 10001:10001
