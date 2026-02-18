#!/bin/bash
set -e

# Load config from .env
if [ -f .env ]; then
  export $(grep -E '^(VPS_HOST|VPS_PATH)=' .env | xargs)
fi

if [ -z "$VPS_HOST" ] || [ -z "$VPS_PATH" ]; then
  echo "Error: VPS_HOST and VPS_PATH must be set in .env"
  exit 1
fi

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

# Sync .env.production separately (contains IB Gateway credentials)
rsync -avz .env.production "$VPS_HOST:$VPS_PATH/.env.production"

# Sync OpenClaw skills
rsync -avz --delete \
  openclaw-skills/gold-analyst/ "$VPS_HOST:/root/.openclaw/skills/gold-analyst/"
rsync -avz --delete \
  openclaw-skills/gold-trader/ "$VPS_HOST:/root/.openclaw/skills/gold-trader/"
rsync -avz --delete \
  openclaw-skills/claude-cli/ "$VPS_HOST:/root/.openclaw/skills/claude-cli/"
rsync -avz \
  openclaw-skills/SOUL.md "$VPS_HOST:/root/.openclaw/agents/main/SOUL.md"

# Ensure IB Gateway is running via Docker
echo "Starting IB Gateway..."
ssh "$VPS_HOST" "cd $VPS_PATH && docker compose up -d ib-gateway"

# Wait for IB Gateway to be ready
echo "Waiting for IB Gateway to connect (this may trigger 2FA on your phone)..."
sleep 15

# Restart trading bot on VPS
ssh "$VPS_HOST" "cd $VPS_PATH && source .venv/bin/activate && pkill -f 'uvicorn app.main:app' || true && sleep 1 && nohup uvicorn app.main:app --host 127.0.0.1 --port 8001 > /var/log/gold-trader.log 2>&1 &"

echo "Deployed. Checking health..."
sleep 3
ssh "$VPS_HOST" "curl -sf http://localhost:8001/health" && echo "" && echo "Bot is running." || echo "Warning: Bot not responding yet (may still be starting)."
echo ""
echo "Check IB Gateway logs: ssh $VPS_HOST 'docker logs ib-gateway --tail 50'"
