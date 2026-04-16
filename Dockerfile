# ── find-a-home Docker image ───────────────────────────────────────────────────
# Works on linux/amd64 and linux/arm64 (Apple Silicon, Raspberry Pi).
# Build: docker build -t find-a-home .
# Run:   docker run --env-file .env -v $(pwd)/data:/app/data find-a-home

FROM python:3.11-slim

# System deps required by Playwright's Chromium build
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached on unchanged requirements)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser
RUN playwright install chromium

# Copy application code
COPY . .

# Persistent data volume (seen listings, etc.)
RUN mkdir -p data
VOLUME ["/app/data"]

# Default command: run all enabled profiles
CMD ["python", "main.py", "run"]
