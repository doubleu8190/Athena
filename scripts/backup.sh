#!/bin/sh
# Daily SQLite backup using WAL-compatible .backup command.
# Usage: ./scripts/backup.sh [backup_dir]

set -e

DB_PATH="${SQLITE_DB_PATH:-/data/athena.db}"
BACKUP_DIR="${1:-/data/backups}"
BACKUP_FILE="${BACKUP_DIR}/athena-$(date +%Y%m%d-%H%M%S).db"

mkdir -p "$BACKUP_DIR"

echo "Backing up $DB_PATH to $BACKUP_FILE..."
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

echo "Backup complete: $BACKUP_FILE"

# Keep only the last 7 backups
ls -1t "$BACKUP_DIR"/athena-*.db 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null || true
