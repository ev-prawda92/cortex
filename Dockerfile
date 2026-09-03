FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -r cortex && chown -R cortex:cortex /app
USER cortex

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:3000/health/ready || exit 1

EXPOSE 3000

# Run controlled migrations before startup. Cortex currently owns its scheduler
# in-process, so use one worker until scheduler leadership is externalized.
CMD ["sh", "-c", "python migrate.py && exec uvicorn cortex:app --host 0.0.0.0 --port 3000 --workers 1 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
