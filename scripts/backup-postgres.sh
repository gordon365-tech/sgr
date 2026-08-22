#!/bin/bash
# Database backup script for PostgreSQL
# Scheduled via cron or Kubernetes CronJob

set -e

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-sgr}"
DB_NAME="${DB_NAME:-sgr}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
S3_BUCKET="${S3_BUCKET:-}"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup filename
BACKUP_FILE="$BACKUP_DIR/sgr_backup_${BACKUP_DATE}.sql"
BACKUP_COMPRESSED="${BACKUP_FILE}.gz"

echo "[$(date)] Starting PostgreSQL backup..."

# Perform backup
PGPASSWORD="$DB_PASSWORD" pg_dump \
  -h "$DB_HOST" \
  -p "$DB_PORT" \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  --verbose \
  --no-password \
  > "$BACKUP_FILE"

if [ $? -ne 0 ]; then
  echo "[$(date)] ERROR: Backup failed!"
  exit 1
fi

# Compress backup
gzip "$BACKUP_FILE"

echo "[$(date)] Backup completed: $BACKUP_COMPRESSED"
ls -lh "$BACKUP_COMPRESSED"

# Upload to S3 if configured
if [ -n "$S3_BUCKET" ]; then
  echo "[$(date)] Uploading to S3..."
  aws s3 cp "$BACKUP_COMPRESSED" "s3://${S3_BUCKET}/postgres-backups/$(basename $BACKUP_COMPRESSED)"
  
  if [ $? -eq 0 ]; then
    echo "[$(date)] S3 upload successful"
  else
    echo "[$(date)] WARNING: S3 upload failed"
  fi
fi

# Clean up old backups (local)
echo "[$(date)] Cleaning old backups (retention: ${RETENTION_DAYS} days)..."
find "$BACKUP_DIR" -name "sgr_backup_*.sql.gz" -mtime +${RETENTION_DAYS} -delete

# Clean up old backups (S3)
if [ -n "$S3_BUCKET" ]; then
  aws s3 ls "s3://${S3_BUCKET}/postgres-backups/" | awk '{print $4}' | while read backup_file; do
    backup_date=$(echo "$backup_file" | grep -oP '\d{8}' | head -1)
    if [ ! -z "$backup_date" ]; then
      backup_epoch=$(date -d "${backup_date:0:4}-${backup_date:4:2}-${backup_date:6:2}" +%s)
      current_epoch=$(date +%s)
      age_days=$(( ($current_epoch - $backup_epoch) / 86400 ))
      
      if [ $age_days -gt $RETENTION_DAYS ]; then
        echo "[$(date)] Deleting old S3 backup: $backup_file"
        aws s3 rm "s3://${S3_BUCKET}/postgres-backups/$backup_file"
      fi
    fi
  done
fi

echo "[$(date)] Backup process completed successfully"
