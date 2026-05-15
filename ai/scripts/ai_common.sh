#!/bin/bash
# ai_common.sh — Shared helpers for AI orchestrator scripts.
# Source this from each ai_*.sh: source "$(dirname "$0")/ai_common.sh"

set -euo pipefail

# ------------------------------------------------------------------
# Path resolution
# ------------------------------------------------------------------
AI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$(cd "$AI_DIR/.." && pwd)"
STATE_DIR="$AI_DIR/state"
PROMPTS_DIR="$AI_DIR/prompts"
PINE_DIR="$AI_DIR/pine"
MCP_CONFIG="$AI_DIR/mcp-trading.json"
AI_LOG_DIR="$PROJECT_DIR/journal/ai"
AI_LOG_FILE="$AI_LOG_DIR/ai.log"
REGIME_FILE="$STATE_DIR/regime.json"
INSIGHTS_FILE="$STATE_DIR/insights.json"

mkdir -p "$STATE_DIR" "$AI_LOG_DIR" "$STATE_DIR/trade_reasoning" "$STATE_DIR/sessions"

# ------------------------------------------------------------------
# Env / API config — load from gold-trader .env
# ------------------------------------------------------------------
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    API_KEY=$(grep -E '^API_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- || echo "")
    TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || echo "")
    TG_CHAT_ID=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2- || echo "")
    AI_PAPER_MODE=$(grep -E '^AI_PAPER_MODE=' "$ENV_FILE" | cut -d= -f2- | tr '[:upper:]' '[:lower:]' || echo "false")
    AI_LIVE_INSTRUMENTS=$(grep -E '^AI_LIVE_INSTRUMENTS=' "$ENV_FILE" | cut -d= -f2- || echo "")
else
    echo "ERROR: $ENV_FILE not found" >&2
    exit 1
fi

BOT_URL="${BOT_URL:-http://localhost:8001}"

if [ -z "$API_KEY" ]; then
    echo "ERROR: API_SECRET_KEY missing in $ENV_FILE" >&2
    exit 1
fi

# ------------------------------------------------------------------
# Locking — prevent concurrent AI script runs of the same kind
# ------------------------------------------------------------------
LOCK_DIR="/tmp/krabbe-ai-locks"
mkdir -p "$LOCK_DIR"

acquire_lock() {
    local name="$1"
    local lockfile="$LOCK_DIR/${name}.lock"
    local max_wait=5
    for i in $(seq 1 $max_wait); do
        if mkdir "$lockfile" 2>/dev/null; then
            echo $$ > "$lockfile/pid"
            trap "rm -rf '$lockfile'" EXIT
            return 0
        fi
        if [ -f "$lockfile/pid" ]; then
            local pid
            pid=$(cat "$lockfile/pid" 2>/dev/null || echo "0")
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -rf "$lockfile"
                continue
            fi
        fi
        sleep 1
    done
    log "Could not acquire lock '$name' — another instance running"
    return 1
}

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [${SCRIPT_NAME:-ai}] $*" | tee -a "$AI_LOG_FILE" >&2
}

# ------------------------------------------------------------------
# API helpers (call gold-trader Docker container)
# ------------------------------------------------------------------
api_get() {
    local endpoint="$1"
    curl -sf -H "x-api-key: $API_KEY" "${BOT_URL}${endpoint}" 2>/dev/null
}

api_post() {
    local endpoint="$1"
    local data="$2"
    local http_code body tmpfile
    tmpfile=$(mktemp)
    http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' -X POST \
        -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
        -d "$data" "${BOT_URL}${endpoint}" 2>/dev/null) || true
    body=$(cat "$tmpfile" 2>/dev/null)
    rm -f "$tmpfile"
    if [ "$http_code" -ge 200 ] 2>/dev/null && [ "$http_code" -lt 300 ] 2>/dev/null; then
        echo "$body"
    elif [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
        echo "{\"status\":\"error\",\"http_code\":$http_code,\"body\":$(echo "$body" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}"
    fi
}

# ------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------
send_telegram() {
    local msg="$1"
    [ -z "$TG_TOKEN" ] && return 0
    # Honor AI_TG_NOTIFICATIONS_ENABLED kill-switch (defaults to enabled)
    local enabled
    enabled=$(grep -E '^AI_TG_NOTIFICATIONS_ENABLED=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    [ "$enabled" = "false" ] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d chat_id="$TG_CHAT_ID" \
        -d parse_mode="Markdown" \
        -d text="$msg" \
        -d disable_web_page_preview=true > /dev/null 2>&1 || true
}

# ------------------------------------------------------------------
# Claude CLI wrapper
#
# Usage:
#   result=$(call_claude MODEL PROMPT_FILE USER_MESSAGE)
#
# MODEL: "sonnet" | "opus" | "haiku"
# PROMPT_FILE: path to system prompt markdown file
# USER_MESSAGE: the user turn (can include context JSON)
# ------------------------------------------------------------------
call_claude() {
    local model="$1"
    local prompt_file="$2"
    local user_message="$3"

    if [ ! -f "$prompt_file" ]; then
        log "ERROR: prompt file not found: $prompt_file"
        echo '{"error":"prompt file missing"}'
        return 1
    fi

    local system_prompt
    system_prompt=$(cat "$prompt_file")

    # Call claude CLI non-interactively. Allow MCP TradingView tools + Bash for pine script ops.
    # Use --permission-mode bypassPermissions + --dangerously-skip-permissions to avoid any prompts.
    local result
    result=$(claude -p "$user_message" \
        --model "$model" \
        --append-system-prompt "$system_prompt" \
        --mcp-config "$MCP_CONFIG" \
        --allowedTools "mcp__tradingview" \
        --strict-mcp-config \
        --output-format json \
        --no-session-persistence \
        --dangerously-skip-permissions \
        2>>"$AI_LOG_FILE")

    if [ -z "$result" ]; then
        log "ERROR: claude returned empty output"
        echo '{"error":"empty claude output"}'
        return 1
    fi

    # Extract the "result" field from claude's wrapper JSON, then find the JSON object inside.
    echo "$result" | python3 -c "
import sys, json, re
try:
    d = json.loads(sys.stdin.read())
    result = d.get('result', '').strip()

    # Strip markdown fences
    if result.startswith('\`\`\`'):
        lines = result.split('\n')
        if lines[0].startswith('\`\`\`'):
            lines = lines[1:]
        if lines and lines[-1].startswith('\`\`\`'):
            lines = lines[:-1]
        result = '\n'.join(lines)

    # Claude sometimes adds prose before the JSON. Extract the largest {...} or [...] block.
    # Find the first { or [ and match brackets to find the end.
    def extract_json(s):
        for i, c in enumerate(s):
            if c in '{[':
                open_c = c
                close_c = '}' if c == '{' else ']'
                depth = 0
                in_str = False
                esc = False
                for j in range(i, len(s)):
                    ch = s[j]
                    if esc:
                        esc = False
                        continue
                    if ch == '\\\\':
                        esc = True
                        continue
                    if ch == '\"':
                        in_str = not in_str
                        continue
                    if in_str:
                        continue
                    if ch == open_c:
                        depth += 1
                    elif ch == close_c:
                        depth -= 1
                        if depth == 0:
                            return s[i:j+1]
        return None

    extracted = extract_json(result)
    if extracted:
        # Validate it parses
        json.loads(extracted)
        print(extracted)
    else:
        print(result)  # fall back to raw
except Exception as e:
    print(json.dumps({'error': f'parse error: {e}'}), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null
}

# ------------------------------------------------------------------
# State helpers
# ------------------------------------------------------------------
read_regime() {
    if [ -f "$REGIME_FILE" ]; then
        cat "$REGIME_FILE"
    else
        echo '{"instruments_in_play":[],"instruments_avoid":[],"summary":"no regime yet"}'
    fi
}

read_insights() {
    if [ -f "$INSIGHTS_FILE" ]; then
        cat "$INSIGHTS_FILE"
    else
        echo '{"insights":[]}'
    fi
}

write_regime() {
    local content="$1"
    echo "$content" > "$REGIME_FILE.tmp" && mv "$REGIME_FILE.tmp" "$REGIME_FILE"
}

write_insights() {
    local content="$1"
    echo "$content" > "$INSIGHTS_FILE.tmp" && mv "$INSIGHTS_FILE.tmp" "$INSIGHTS_FILE"
}

# ------------------------------------------------------------------
# Safety: check if paper mode (per-instrument)
#
# Usage:
#   is_paper_mode              → global check (true if AI_PAPER_MODE=true)
#   is_paper_instrument XAUUSD → per-instrument (false if in AI_LIVE_INSTRUMENTS)
#
# AI_LIVE_INSTRUMENTS=XAUUSD means ONLY XAUUSD trades live.
# Everything else stays paper even if AI_PAPER_MODE=false.
# ------------------------------------------------------------------
is_paper_mode() {
    [ "$AI_PAPER_MODE" = "true" ] && return 0 || return 1
}

is_paper_instrument() {
    local inst="$1"
    # If global paper mode is on, everything is paper
    [ "$AI_PAPER_MODE" = "true" ] && return 0
    # Empty AI_LIVE_INSTRUMENTS = nothing live (safer default)
    [ -z "$AI_LIVE_INSTRUMENTS" ] && return 0
    # Only instruments in the list trade live
    echo ",$AI_LIVE_INSTRUMENTS," | grep -qi ",$inst," && return 1 || return 0
}

# ------------------------------------------------------------------
# Market hours gate — London (07-16 UTC) + NY (13-21 UTC) = 07-21 UTC Mon-Fri
# Call at top of each AI script. Exits 0 if outside hours.
# Pass --btc to allow BTC-only runs 24/7 (for scripts that filter BTC).
# ------------------------------------------------------------------
is_market_open() {
    local utc_hour utc_dow
    utc_hour=$(date -u '+%H' | sed 's/^0//')
    utc_dow=$(date -u '+%u')  # 1=Mon ... 7=Sun

    # Weekend: Saturday (6) or Sunday (7) — FX/gold closed, BTC still trades
    if [ "$utc_dow" -ge 6 ]; then
        return 1
    fi
    # Weekday: London+NY combined = 07-21 UTC
    if [ "${utc_hour:-0}" -ge 7 ] && [ "${utc_hour:-0}" -lt 21 ]; then
        return 0
    fi
    return 1
}

gate_market_hours() {
    if ! is_market_open; then
        log "Outside market hours (07-21 UTC Mon-Fri) — skipping"
        exit 0
    fi
}

# ------------------------------------------------------------------
# JSON extract helper (small wrapper for clarity)
# ------------------------------------------------------------------
json_get() {
    local json="$1"
    local path="$2"
    # path example: ".action" or ".setups[0].direction"
    echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
path = '$path'.lstrip('.')
try:
    for part in path.replace('[', '.').replace(']', '').split('.'):
        if part == '':
            continue
        if part.isdigit():
            d = d[int(part)]
        else:
            d = d[part]
    if isinstance(d, (dict, list)):
        print(json.dumps(d))
    else:
        print(d)
except Exception:
    print('')
" 2>/dev/null
}
