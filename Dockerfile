# ---- 构建阶段:从锁文件创建独立 venv(与运行环境隔离) ----
FROM ghcr.io/astral-sh/uv:0.12.1 AS uv

FROM python:3.14-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Optional regional mirror for locked installs. When set, dependencies are
# exported from uv.lock with hashes and installed through the configured index;
# the default path remains uv sync --locked for CI and non-mirror builds.
ARG PYPI_INDEX_URL=""
ENV PYPI_INDEX_URL=${PYPI_INDEX_URL}

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN if [ -n "$PYPI_INDEX_URL" ]; then \
      uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/requirements.txt \
      && uv venv "$UV_PROJECT_ENVIRONMENT" --python /usr/local/bin/python \
      && uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" \
           --index-url "$PYPI_INDEX_URL" --require-hashes -r /tmp/requirements.txt \
      && uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" \
           --index-url "$PYPI_INDEX_URL" --no-deps .; \
    else \
      uv sync --locked --no-dev --no-editable; \
    fi

# ---- 运行阶段:仅保留 venv 产物 + 运行所需源码 ----
FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 仅复制运行产物和维护脚本(tests/文档等不进镜像)
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations

# 非 root 运行；data/ 仅供显式维护任务使用，业务状态保存在 PostgreSQL/RustFS。
RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
STOPSIGNAL SIGTERM

ENV PYTHONPATH="/app:/app/src"

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz', timeout=3)" || exit 1

CMD ["uvicorn", "agent_runtime.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
