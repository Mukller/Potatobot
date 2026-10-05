#!/bin/bash
# Potatobot production healthcheck - runs every minute from cron.
#
# Three checks, because they fail for different reasons:
#   HTTP  : is the app alive at all (web server + database)
#   beat  : is its event loop still turning (a heartbeat the app itself
#           writes). This replaced a grep for "Run polling" in the logs,
#           which aiogram writes once at startup - so the old check found
#           nothing after five idle minutes and restarted a healthy bot
#           every eight minutes.
#   token : did Telegram reject the bot token (HTTP 401)? Only 401 counts:
#           a timeout or a 5xx says nothing about the token, and restarting
#           the bot cannot fix a network blip.
#
# On FAIL_THRESHOLD consecutive failures: restart the container, log it, and
# alert through the project's own bot. Stays quiet until recovery.
set -u

NAME=potatobot
# The container listens on 8080; compose publishes it as 8082 on the host.
# This script runs on the host, so it must use the published port.
HEALTH_URL="http://localhost:8082/health"
BASE=/home/anton/potatobot
ENV_FILE="$BASE/.env"
LOG="$BASE/healthcheck.log"
STATE=/tmp/potatobot-health.state
FAIL_THRESHOLD=3
HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/potatobot.heartbeat}"
# Three missed writes at a 30s cadence, with slack for a busy minute.
MAX_BEAT_AGE="${MAX_BEAT_AGE:-180}"

# Read the credentials from .env instead of carrying them in this file. An
# earlier version shipped with the literal strings __TG_TOKEN__ and
# __CHAT_ID__, so every alert died with a 404 while the log cheerfully
# recorded "alert sent". A placeholder must never be able to look like
# success, which is why send_tg reports what Telegram answered.
TG_TOKEN=$(grep -m1 '^BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r"'"'"'')
CHAT_ID=$(grep -m1 '^ADMIN_USER_IDS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r"'"'"' ' | cut -d, -f1)

say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# keep our own log bounded (it runs every minute)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -500 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi

send_tg() {
    local code
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" --data-urlencode "text=$1" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
        say "telegram: delivered"
        return 0
    fi
    say "telegram: DELIVERY FAILED http=${code} token_len=${#TG_TOKEN} chat=${CHAT_ID:-<empty>}"
    return 1
}

# --- is the process/container even up -------------------------------------
RUNNING=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo "false")
# Assign, then default only if empty. `curl ... || echo 000` appends a second
# 000 to curl's own 000, which is how the log ended up reading http=000000.
HTTP=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)
[ -n "$HTTP" ] || HTTP="000"

# --- heartbeat age, measured inside the container -------------------------
BEAT_MTIME=$(docker exec "$NAME" stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo "")
if [ -n "$BEAT_MTIME" ]; then
    BEAT_AGE=$(( $(date +%s) - BEAT_MTIME ))
else
    BEAT_AGE="-1"
fi

# --- is the token still accepted by Telegram? 401 only. --------------------
AUTH=$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
    "https://api.telegram.org/bot${TG_TOKEN}/getMe" 2>/dev/null)
[ -n "$AUTH" ] || AUTH="000"

state="ok"
[ -f "$STATE" ] && state=$(cat "$STATE")
reason=""

[ "$RUNNING" != "true" ] && reason="container not running"
[ -z "$reason" ] && [ "$HTTP" != "200" ] && reason="health HTTP ${HTTP}"
if [ -z "$reason" ]; then
    if [ "$BEAT_AGE" = "-1" ]; then
        reason="no heartbeat file (${HEARTBEAT_FILE})"
    elif [ "$BEAT_AGE" -gt "$MAX_BEAT_AGE" ]; then
        reason="heartbeat stale by ${BEAT_AGE}s (max ${MAX_BEAT_AGE}s)"
    fi
fi
[ -z "$reason" ] && [ "$AUTH" = "401" ] && reason="Telegram rejected the bot token (401)"

if [ -n "$reason" ]; then
    fails=$(cat "${STATE}.fails" 2>/dev/null || echo 0)
    fails=$((fails + 1))
    echo "$fails" > "${STATE}.fails"
    say "FAIL ($fails/$FAIL_THRESHOLD): $reason [http=${HTTP} beat=${BEAT_AGE} auth=${AUTH}]"

    # -ge, not -eq: if the restart did not fix it, the fourth consecutive
    # failure has to restart again instead of only logging itself out.
    if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
        say "restarting $NAME"
        docker restart "$NAME" >> "$LOG" 2>&1
        if [ "$state" != "alerted" ]; then
            if send_tg "🔴 Potatobot не отвечает: ${reason}. Контейнер перезапущен."; then
                echo "alerted" > "$STATE"
                say "alert sent, state=alerted"
            else
                say "alert NOT marked as sent - delivery failed, will retry next minute"
            fi
        fi
    fi
    exit 0
fi

# --- healthy --------------------------------------------------------------
rm -f "${STATE}.fails"
if [ "$state" = "alerted" ]; then
    say "recovered after restart"
    # give it a moment, then confirm it really came back
    sleep 20
    HTTP2=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || echo "000")
    BEAT2=$(docker exec "$NAME" stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo "")
    AGE2="-1"
    [ -n "$BEAT2" ] && AGE2=$(( $(date +%s) - BEAT2 ))
    if [ "$HTTP2" = "200" ] && [ "$AGE2" != "-1" ] && [ "$AGE2" -le "$MAX_BEAT_AGE" ]; then
        send_tg "🟢 Potatobot снова работает"
        say "recovery confirmed"
    else
        send_tg "🟡 Potatobot не восстановился после перезапуска (HTTP ${HTTP2}, beat ${AGE2}s)"
        say "recovery NOT confirmed (HTTP ${HTTP2} beat=${AGE2})"
    fi
    echo "ok" > "$STATE"
fi
exit 0