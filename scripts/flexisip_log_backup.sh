#!/bin/bash
# Flexisip Proxy Log Backup Script
# Incrementally backs up only new lines from the last run.
# Tracks the last backed-up line number in a state file (backups/.last_line).
# The actual log file inside the container is left untouched.
#
# Usage:
#   ./scripts/flexisip_log_backup.sh             # backup stg2b (default)
#   ./scripts/flexisip_log_backup.sh stg2b       # explicit server name
#
# Output file: backups/stg2b_flexisip-proxy_YYYY-MM-DD_HH-MM-SS.log

set -euo pipefail

SERVER="${1:-stg2b}"
CONTAINER="proxy"
CONTAINER_LOG="/usr/local/var/log/flexisip/flexisip-proxy.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/../backups"
STATE_FILE="$BACKUP_DIR/.${SERVER}_last_line"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
DEST="$BACKUP_DIR/${SERVER}_flexisip-proxy_${TIMESTAMP}.log"

mkdir -p "$BACKUP_DIR"

# Read last backed-up line (0 if first run)
LAST_LINE=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

# Get total lines currently in the container log
TOTAL_LINES=$(ssh "$SERVER" "sudo podman exec ${CONTAINER} wc -l ${CONTAINER_LOG} | awk '{print \$1}'")
NEW_LINES=$((TOTAL_LINES - LAST_LINE))

echo "[${TIMESTAMP}] Last backed-up line: ${LAST_LINE} | Current total: ${TOTAL_LINES} | New lines: ${NEW_LINES}"

if [ "$NEW_LINES" -le 0 ]; then
    echo "[${TIMESTAMP}] Nothing new to backup. Exiting."
    exit 0
fi

# Pull only the new lines from the container
ssh "$SERVER" "sudo podman exec ${CONTAINER} tail -n +$((LAST_LINE + 1)) ${CONTAINER_LOG}" \
    > "$DEST"

SIZE=$(du -sh "$DEST" | cut -f1)
echo "[${TIMESTAMP}] Backup saved locally: $DEST (${SIZE})"

# Update state file to current total line count
echo "$TOTAL_LINES" > "$STATE_FILE"
echo "[${TIMESTAMP}] State updated: next backup will start from line $((TOTAL_LINES + 1))."
