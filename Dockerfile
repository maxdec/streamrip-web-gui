FROM python:3.14-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
  ffmpeg \
  git \
  curl \
  && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Create a non-root user with home directory
RUN groupadd -g 1000 appuser && \
  useradd -r -u 1000 -g appuser -m -d /home/appuser appuser

# Create necessary directories with proper ownership
RUN mkdir -p /downloads /logs /config/streamrip /app && \
  chown -R 1000:1000 /downloads /logs /config /app

# Set working directory
WORKDIR /app

# Copy dependency files with correct ownership
COPY --chown=1000:1000 pyproject.toml .python-version ./

# Switch to non-root user for installation
USER 1000:1000

# Create virtual environment and install dependencies
# UV will use /home/appuser/.cache which now exists and is owned by appuser
RUN uv venv && uv pip install -e .

# Copy application files with correct ownership
COPY --chown=1000:1000 app.py /app/
COPY --chown=1000:1000 templates /app/templates/
COPY --chown=1000:1000 static /app/static/

# Expose port
EXPOSE 5000

# Set environment to use the venv
ENV PATH="/app/.venv/bin:$PATH" \
  PYTHONUNBUFFERED=1

# Run with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "2", "--timeout", "60", "app:app"]
