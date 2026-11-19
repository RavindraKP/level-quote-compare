# Level: one Streamlit process with its background worker thread.
# Run exactly one container/process (PLAN §5): each process starts its own worker.
FROM python:3.12-slim

# uv from PyPI (pinned), so the build only needs PyPI and the Python base image.
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# The virtualenv's python and streamlit come first on PATH: no `uv run` at start-up.
# Mount the host's persistent disk at /var/level so the database and uploads survive restarts.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LEVEL_DB_PATH=/var/level/level.db \
    LEVEL_UPLOAD_DIR=/var/level/uploads \
    PORT=8501
VOLUME ["/var/level"]
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT','8501'), timeout=4)"

CMD ["sh", "deploy/start.sh"]
