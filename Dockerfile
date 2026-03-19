FROM python:3.12-slim

WORKDIR /app

# System deps for Twisted (ctrader-open-api) and cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir . && pip install --no-cache-dir service-identity

# Copy app code
COPY app/ app/
COPY cron/ cron/

# Journal directories
RUN mkdir -p journal/{intraday/scans,swing/scans,scalp/scans,daily/scans,bb_bounce/scans,ny_orb/scans,monitors,summaries}

# Make cron scripts executable
RUN chmod +x cron/*.sh

# Entrypoint: start cron + uvicorn
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8001

ENTRYPOINT ["/entrypoint.sh"]
