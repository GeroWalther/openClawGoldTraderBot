#!/bin/bash
set -e

# Load config from .env
if [ -f .env ]; then
  export $(grep -E '^(VPS_HOST|VPS_PATH|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|API_SECRET_KEY)=' .env | xargs)
fi

if [ -z "$VPS_HOST" ] || [ -z "$VPS_PATH" ]; then
  echo "Error: VPS_HOST and VPS_PATH must be set in .env"
  exit 1
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$API_SECRET_KEY" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN and API_SECRET_KEY must be set in .env"
  exit 1
fi

echo "=== Gold Trader – Switch IBKR Account ==="
echo ""
echo "This will update credentials on the VPS and restart IB Gateway + bot."
echo ""

# Choose mode
# gnzsnz/ib-gateway internal ports: 4003=live, 4004=paper
echo "Trading mode:"
echo "  1) paper (host 4002 -> container 4004)"
echo "  2) live  (host 4001 -> container 4003)"
read -p "Select [1/2]: " MODE_CHOICE

if [ "$MODE_CHOICE" = "1" ]; then
  TRADING_MODE="paper"
  HOST_PORT="4002"
  CONTAINER_PORT="4004"
elif [ "$MODE_CHOICE" = "2" ]; then
  TRADING_MODE="live"
  HOST_PORT="4001"
  CONTAINER_PORT="4003"
else
  echo "Invalid choice."
  exit 1
fi

# Prompt for credentials
read -p "IBKR Username: " TWS_USERID
read -s -p "IBKR Password: " TWS_PASSWORD
echo ""

if [ -z "$TWS_USERID" ] || [ -z "$TWS_PASSWORD" ]; then
  echo "Error: username and password are required."
  exit 1
fi

echo ""
echo "Mode:     $TRADING_MODE"
echo "User:     $TWS_USERID"
echo "Port:     $HOST_PORT (host) -> $CONTAINER_PORT (container)"
read -p "Continue? [y/N]: " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

# Update .env.production locally
cat > .env.production <<EOF
# IB Gateway container settings (ghcr.io/gnzsnz/ib-gateway)
TWS_USERID=$TWS_USERID
TWS_PASSWORD=$TWS_PASSWORD
TRADING_MODE=$TRADING_MODE
TWS_ACCEPT_INCOMING=accept
READ_ONLY_API=no
TWOFA_TIMEOUT_ACTION=restart

# Telegram
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-7524185386}

# App
API_SECRET_KEY=$API_SECRET_KEY
EOF

# Update IBKR_PORT in local .env to match
sed -i '' "s/^IBKR_PORT=.*/IBKR_PORT=$HOST_PORT/" .env

# Update docker-compose port mapping
sed -i '' "s|127.0.0.1:[0-9]*:[0-9]*|127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}|" docker-compose.yml

echo ""
echo "Local files updated. Deploying to VPS..."

# Sync updated files to VPS
rsync -avz .env.production "$VPS_HOST:$VPS_PATH/.env.production"
rsync -avz docker-compose.yml "$VPS_HOST:$VPS_PATH/docker-compose.yml"

# Stop bot on VPS (use fuser to kill by port, avoids pkill killing SSH session)
echo "Stopping bot..."
ssh "$VPS_HOST" "fuser -k 8001/tcp 2>/dev/null || true"
sleep 2

# Stop IB Gateway
echo "Stopping IB Gateway..."
ssh "$VPS_HOST" "cd $VPS_PATH && docker compose down"

# Update IBKR_PORT in VPS .env
ssh "$VPS_HOST" "cd $VPS_PATH && sed -i 's/^IBKR_PORT=.*/IBKR_PORT=$HOST_PORT/' .env"

# Start IB Gateway with new config
echo "Starting IB Gateway ($TRADING_MODE mode)..."
ssh "$VPS_HOST" "cd $VPS_PATH && docker compose up -d ib-gateway"

echo "Waiting for IB Gateway to connect (check your phone for 2FA)..."
sleep 20

# Start bot
echo "Starting bot..."
ssh "$VPS_HOST" "cd $VPS_PATH && source .venv/bin/activate && nohup uvicorn app.main:app --host 127.0.0.1 --port 8001 > /var/log/gold-trader.log 2>&1 &"

echo "Checking health..."
sleep 5
ssh "$VPS_HOST" "curl -sf http://localhost:8001/health" && echo "" && echo "Bot is running in $TRADING_MODE mode." || echo "Warning: Bot not responding yet (may still be starting)."

# Show account info
echo ""
echo "Account info:"
ssh "$VPS_HOST" "curl -s -H 'X-API-Key: $API_SECRET_KEY' http://localhost:8001/api/v1/positions/account"
echo ""
