#!/bin/bash
# setup.sh — Host-side installation for Krabbe AI Trading System.
# Run once on your Mac to install TradingView MCP, configure Claude CLI, install crontab.

set -euo pipefail

AI_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$AI_DIR/.." && pwd)"
MCP_REPO_DIR="${MCP_REPO_DIR:-$HOME/tradingview-mcp-jackson}"

echo ""
echo "================================================="
echo "  Krabbe AI Trading System — Host Setup"
echo "================================================="
echo ""
echo "Project dir: $PROJECT_DIR"
echo "AI dir:      $AI_DIR"
echo "MCP target:  $MCP_REPO_DIR"
echo ""

# ------------------------------------------------------------------
# 1. Check prerequisites
# ------------------------------------------------------------------
echo "[1/6] Checking prerequisites..."

command -v claude >/dev/null 2>&1 || { echo "ERROR: 'claude' CLI not found. Install via: https://claude.com/claude-code"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "ERROR: 'node' not found. Install Node.js 18+ first"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "ERROR: 'docker' not found"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "ERROR: 'git' not found"; exit 1; }

CLAUDE_VER=$(claude --version 2>&1 | head -1 || echo "unknown")
NODE_VER=$(node --version 2>&1 || echo "unknown")
echo "  claude: $CLAUDE_VER"
echo "  node:   $NODE_VER"
echo "  docker: ok"
echo ""

# ------------------------------------------------------------------
# 2. Install TradingView MCP
# ------------------------------------------------------------------
echo "[2/6] Installing TradingView MCP..."

if [ -d "$MCP_REPO_DIR" ]; then
    echo "  Repo exists at $MCP_REPO_DIR — pulling latest..."
    (cd "$MCP_REPO_DIR" && git pull --rebase 2>&1 | sed 's/^/    /') || true
else
    echo "  Cloning tradingview-mcp-jackson..."
    git clone https://github.com/LewisWJackson/tradingview-mcp-jackson.git "$MCP_REPO_DIR"
fi

echo "  Installing npm dependencies..."
(cd "$MCP_REPO_DIR" && npm install 2>&1 | tail -3 | sed 's/^/    /')
echo ""

# ------------------------------------------------------------------
# 3. Configure mcp-trading.json with absolute path
# ------------------------------------------------------------------
echo "[3/6] Configuring MCP config..."

# Read entrypoint from package.json "main" field (authoritative)
MCP_ENTRY="$MCP_REPO_DIR/$(node -e "console.log(require('$MCP_REPO_DIR/package.json').main)" 2>/dev/null)"
if [ ! -f "$MCP_ENTRY" ]; then
    MCP_ENTRY="$MCP_REPO_DIR/src/server.js"
fi

if [ -z "$MCP_ENTRY" ] || [ ! -f "$MCP_ENTRY" ]; then
    echo "ERROR: could not find TradingView MCP entrypoint. Check $MCP_REPO_DIR manually."
    exit 1
fi

echo "  MCP entrypoint: $MCP_ENTRY"

cat > "$AI_DIR/mcp-trading.json" <<EOF
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["$MCP_ENTRY"],
      "env": {
        "TV_DEBUG_PORT": "9222"
      }
    }
  }
}
EOF

echo "  Wrote $AI_DIR/mcp-trading.json"
echo ""

# ------------------------------------------------------------------
# 4. Ensure state directories + log file
# ------------------------------------------------------------------
echo "[4/6] Creating state directories..."
mkdir -p "$AI_DIR/state/trade_reasoning" "$AI_DIR/state/sessions" "$PROJECT_DIR/journal/ai"
touch "$PROJECT_DIR/journal/ai/ai.log"
chmod +x "$AI_DIR/scripts"/*.sh
echo "  Done."
echo ""

# ------------------------------------------------------------------
# 5. Print crontab snippet (user installs manually)
# ------------------------------------------------------------------
echo "[5/6] Crontab configuration"
echo ""
echo "To enable AI cron jobs, run:  crontab -e"
echo "Then paste the following (adjust TZ if needed):"
echo ""
cat <<CRON
# ============ Krabbe AI Trading System ============
# All times UTC. Adjust hours 07-21 for your timezone if needed.
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin

# Regime scan — every 30min, Mon-Fri 07-21 UTC
*/30 7-21 * * 1-5  $AI_DIR/scripts/ai_regime.sh

# Setup scan — every 15min, Mon-Fri 07-21 UTC (offset to avoid racing regime)
5-50/15 7-21 * * 1-5  $AI_DIR/scripts/ai_scan.sh

# Position management — every 15min at :07, :22, :37, :52 (offset from scan)
7,22,37,52 7-21 * * 1-5  $AI_DIR/scripts/ai_manage.sh

# Daily review — 21:05 UTC, Mon-Fri
5 21 * * 1-5  $AI_DIR/scripts/ai_review.sh

# Friday EOD close — 20:55 UTC
55 20 * * 5  $AI_DIR/scripts/ai_eod.sh
CRON
echo ""

# ------------------------------------------------------------------
# 6. Verification checklist
# ------------------------------------------------------------------
echo "[6/6] Next steps:"
echo ""
echo "  1. Open TradingView Desktop with remote debugging enabled:"
echo "     macOS: /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222"
echo "     (create a shell alias: alias tv='open -na TradingView --args --remote-debugging-port=9222')"
echo ""
echo "  2. Set TRADING_MODE=ai in $PROJECT_DIR/.env"
echo ""
echo "  3. Restart gold-trader:  docker compose up -d --force-recreate"
echo ""
echo "  4. Test MCP connection:"
echo "     claude -p 'What symbol is TradingView showing?' --mcp-config $AI_DIR/mcp-trading.json --allowedTools 'mcp__tradingview' --dangerously-skip-permissions"
echo ""
echo "  5. Test one script manually:"
echo "     bash $AI_DIR/scripts/ai_regime.sh"
echo "     cat $AI_DIR/state/regime.json"
echo ""
echo "  6. Install crontab from snippet above."
echo ""
echo "Setup complete."
