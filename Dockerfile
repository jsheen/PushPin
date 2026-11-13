# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /uvx /usr/local/bin/

# Non-root user
ARG USER=app
ARG UID=1000
RUN useradd -m -u ${UID} ${USER} \
    && mkdir -p /opt/venv /app \
    && chown -R ${USER}:${USER} /opt/venv /app
USER ${USER}
WORKDIR /app

# Install dependencies first (cached layer), then the project itself
COPY --chown=${USER}:${USER} pyproject.toml uv.lock* README.md ./
RUN --mount=type=cache,target=/home/${USER}/.cache/uv,uid=${UID} \
    uv sync --no-install-project --extra train --extra api --extra dev

COPY --chown=${USER}:${USER} src ./src
COPY --chown=${USER}:${USER} configs ./configs
RUN --mount=type=cache,target=/home/${USER}/.cache/uv,uid=${UID} \
    uv sync --extra train --extra api --extra dev

ENTRYPOINT ["/bin/bash", "-c"]
CMD ["pushpin-train --config configs/train.yml"]
