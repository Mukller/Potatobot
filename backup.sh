#!/bin/bash
# Daily PostgreSQL backup for potatobot with restore verification.
# Runs Sun 03:30 local (server TZ) via cron; keeps 14 days on disk.
set -u

BACKUP_DIR=/home/anton/potatobot/backups
KEEP_DAYS=14
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/potatobot-$STAMP.sql.gz"
LOG=/home/anton/potatobot/backup.log

mkdir -p "$BACKUP_DIR"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 1) dump
if docker exec potatobot-db pg_dump -U potato -d potatobot 2>>"$LOG" | gzip -9 > "$OUT"; then
    SIZE=$(du -h "$OUT" | cut -f1)
    log "backup ok: $OUT ($SIZE)"
else
    log "backup FAILED"
    rm -f "$OUT"
    exit 1
fi

# 2) verify the dump actually restores into a throwaway database
VERIFY_DB="potatobot_verify_$$"
if docker exec potatobot-db psql -U potato -d postgres -c "CREATE DATABASE $VERIFY_DB" >>"$LOG" 2>&1 \
   && gunzip -c "$OUT" | docker exec -i potatobot-db psql -U potato -d "$VERIFY_DB" >>"$LOG" 2>&1; then
    TABLES=$(docker exec potatobot-db psql -U potato -d "$VERIFY_DB" -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)
    log "restore verified: $TABLES tables in $VERIFY_DB"
else
    log "restore verification FAILED for $OUT"
fi
docker exec potatobot-db psql -U potato -d postgres -c "DROP DATABASE IF EXISTS $VERIFY_DB" >>"$LOG" 2>&1

# 3) prune
find "$BACKUP_DIR" -name 'potatobot-*.sql.gz' -mtime "+$KEEP_DAYS" -delete 2>/dev/null
COUNT=$(ls -1 "$BACKUP_DIR"/potatobot-*.sql.gz 2>/dev/null | wc -l)
log "retention: $COUNT dumps kept"
