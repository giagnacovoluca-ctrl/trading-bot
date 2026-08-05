FROM python:3.11-slim

LABEL maintainer="BountyBrain"
LABEL description="Expert system to maximize EPHH on bounty platforms"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ src/
COPY config/ config/
COPY pyproject.toml .

# Install package in editable mode
RUN pip install -e .

# Create storage directories
RUN mkdir -p storage/datasets storage/logs storage/db storage/workspaces storage/models

# Non-root user for security
RUN useradd -m -u 1000 bountybrain && chown -R bountybrain:bountybrain /app
USER bountybrain

# Dashboard port
EXPOSE 8080

# Default: run the pipeline once
CMD ["python", "-m", "bountybrain.main", "run"]
