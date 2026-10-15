#!/usr/bin/env bash
# Ежедневная копия базы ОМР № 4 (docs/deploy.md, шаг 10). Запускается таймером systemd
# omr4-backup.timer от пользователя postgres; хранит копии BACKUP_DAYS дней.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/omr4}"
BACKUP_DAYS="${BACKUP_DAYS:-30}"
DB_NAME="${DB_NAME:-omr4}"

mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/omr4-$(date +%F_%H%M).dump"
# Сначала во временный файл: оборванная копия не выглядит готовой.
pg_dump --format=custom --file="$target.part" "$DB_NAME"
mv "$target.part" "$target"
chmod 600 "$target"
find "$BACKUP_DIR" -name 'omr4-*.dump' -mtime +"$BACKUP_DAYS" -delete
echo "Копия: $target ($(du -h "$target" | cut -f1))"
