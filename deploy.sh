#!/bin/bash
set -e

VPS_HOST="root@77.37.125.65"
VPS_PATH="/opt/gold-trader"

echo "Deploying gold-trader to VPS..."

# Sync app code (excludes .env, .venv, __pycache__, .git, tests, db)
rsync -avz --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude '.env.production' \
  --exclude '.env.example' \
  --exclude 'trades.db' \
  --exclude '.pytest_cache' \
  --exclude 'gold_trader.egg-info' \
  --exclude 'tests' \
  ./ "$VPS_HOST:$VPS_PATH/"

# Sync OpenClaw skills
rsync -avz --delete \
  openclaw-skills/gold-analyst/ "$VPS_HOST:/root/.openclaw/skills/gold-analyst/"
rsync -avz --delete \
  openclaw-skills/gold-trader/ "$VPS_HOST:/root/.openclaw/skills/gold-trader/"
rsync -avz --delete \
  openclaw-skills/claude-cli/ "$VPS_HOST:/root/.openclaw/skills/claude-cli/"
rsync -avz \
  openclaw-skills/SOUL.md "$VPS_HOST:/root/.openclaw/agents/main/SOUL.md"

# Restart trading bot on VPS
ssh "$VPS_HOST" "cd $VPS_PATH && source .venv/bin/activate && pkill -f 'uvicorn app.main:app' || true && sleep 1 && nohup uvicorn app.main:app --host 127.0.0.1 --port 8001 > /var/log/gold-trader.log 2>&1 &"

echo "Deployed. Checking health..."
sleep 2
ssh "$VPS_HOST" "curl -sf http://localhost:8001/health" && echo "" && echo "Bot is running." || echo "Warning: Bot not responding yet (may still be starting)."
