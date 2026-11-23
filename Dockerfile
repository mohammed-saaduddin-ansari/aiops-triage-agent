# syntax=docker/dockerfile:1.7
FROM python:3.15-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.15-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin triage \
 && mkdir -p /var/lib/triage && chown 10001 /var/lib/triage
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY runbooks ./runbooks
COPY evals/scenarios ./evals/scenarios
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TRIAGE_RUNBOOK_DIR=/app/runbooks \
    TRIAGE_AUDIT_LOG_PATH=/var/lib/triage/audit.jsonl
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["triage-agent"]
CMD ["serve"]
