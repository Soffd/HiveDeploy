#!/usr/bin/env bash
# Bot Platform — 实例数据备份脚本
# 用法: bash scripts/backup.sh [username]  （不填则备份所有）

set -e
BACKUP_DIR="./backups/$(date +%Y%m%d_%H%M%S)"
DATA_ROOT=${DATA_DIR:-./data/instances}

mkdir -p "$BACKUP_DIR"

if [ -n "$1" ]; then
  echo "备份用户: $1"
  if [ -d "$DATA_ROOT/$1" ]; then
    tar czf "$BACKUP_DIR/${1}.tar.gz" -C "$DATA_ROOT" "$1"
    echo "✅ 已备份到 $BACKUP_DIR/${1}.tar.gz"
  else
    echo "❌ 用户目录不存在: $DATA_ROOT/$1"
    exit 1
  fi
else
  echo "备份所有实例数据..."
  tar czf "$BACKUP_DIR/all_instances.tar.gz" -C "$(dirname $DATA_ROOT)" "$(basename $DATA_ROOT)"
  echo "✅ 已备份到 $BACKUP_DIR/all_instances.tar.gz"
fi
