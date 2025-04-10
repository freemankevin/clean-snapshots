# Use official Python runtime as base image
FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y curl && \
    rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    NEXUS_URL=http://nexus:8081 \
    NEXUS_USER=admin \
    NEXUS_PASS=admin123 \
    REPOSITORY_NAME=maven-snapshots \
    RETAIN_COUNT=3 \
    DRY_RUN=false \
    LOG_LEVEL=INFO \
    SCHEDULE_TIME=03:00 \
    HEALTHCHECK_PORT=8000

# Create and set working directory
WORKDIR /app


# Copy  file
COPY clean_nexus_snapshots.py requirements.txt .

# Install required packages
RUN pip install --no-cache-dir  -r requirements.txt


# Create log directory
RUN mkdir -p /var/log && \
    touch /var/log/nexus_cleanup.log && \
    chmod 666 /var/log/nexus_cleanup.log

# Health check configuration
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Set entrypoint
ENTRYPOINT ["python", "-u", "clean_nexus_snapshots.py"]