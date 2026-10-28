# Dockerfile for Review Intelligence MVP SaaS
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and writing .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    PORT=8000

# Git commit hash argument for reproducibility metadata
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Create non-root user and application workspace
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Cache layer: install dependencies first
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Create persistent data directory and grant permissions to non-root user
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Copy application and dashboard source code
COPY app /app/app
COPY dashboard /app/dashboard

# Set ownership to appuser
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

EXPOSE 8000

# Native Python stdlib healthcheck hitting /health (no curl required)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

# Single Uvicorn worker process (SQLite + in-process thread runner)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
