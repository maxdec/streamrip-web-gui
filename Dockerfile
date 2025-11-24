FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Create a non-root user
RUN groupadd -g 1000 appuser && \
    useradd -r -u 1000 -g appuser appuser

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml .python-version ./

# Create virtual environment and install dependencies
RUN uv venv && uv pip install -e .

# Copy application files
COPY app.py /app/
COPY templates /app/templates/
COPY static /app/static/

# Create necessary directories with proper ownership
RUN mkdir -p /downloads /logs /config/streamrip && \
    chown -R 1000:1000 /downloads /logs /config /app

# Switch to non-root user
USER 1000:1000

# Expose port
EXPOSE 5000

# Set environment to use the venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Run with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "2", "--timeout", "60", "app:app"]
