# Use a multi-architecture compatible base image
FROM --platform=$BUILDPLATFORM python:3.9-slim

# Set Shanghai timezone and install system dependencies
ENV TZ=Asia/Shanghai
RUN apt-get update && \
    apt-get install -y tzdata curl && \
    ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
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

# Create working directory
WORKDIR /app

# Copy application files
COPY clean_nexus_snapshots.py requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create log directory and set permissions
RUN mkdir -p /var/log && \
    touch /var/log/nexus_cleanup.log && \
    chmod 666 /var/log/nexus_cleanup.log

# Configure health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Set entrypoint
ENTRYPOINT ["python", "-u", "clean_nexus_snapshots.py"]