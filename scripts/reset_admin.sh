#!/usr/bin/env bash
# Bot Platform — 管理员密码重置脚本
# 用法: bash scripts/reset_admin.sh

set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; NC='\033[0m'; BOLD='\033[1m'

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║      管理员密码重置工具              ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# 检查容器是否运行
if ! docker ps --format '{{.Names}}' | grep -q "^bot_panel$"; then
  echo -e "${RED}[ERR]${NC} bot_panel 容器未运行，请先执行 docker compose up -d"
  exit 1
fi

# 列出所有管理员账号
echo -e "${CYAN}[INFO]${NC} 当前管理员账号列表："
docker exec bot_panel python3 -c "
from app.database import SessionLocal
from app.models import User
db = SessionLocal()
admins = db.query(User).filter(User.is_admin == True).all()
if not admins:
    print('  （无管理员账号）')
for u in admins:
    print(f'  ID:{u.id}  用户名:{u.username}  邮箱:{u.email}  状态:{\"启用\" if u.is_active else \"禁用\"}')
db.close()
"

echo ""

# 输入要重置的用户名
read -rp "  请输入要重置密码的管理员用户名: " TARGET_USER
if [ -z "$TARGET_USER" ]; then
  echo -e "${RED}[ERR]${NC} 用户名不能为空"
  exit 1
fi

# 输入新密码
while true; do
  read -rsp "  请输入新密码（至少6位）: " NEW_PW
  echo ""
  if [ ${#NEW_PW} -lt 6 ]; then
    echo -e "${YELLOW}[WARN]${NC} 密码至少 6 位，请重新输入"
  else
    break
  fi
done

# 执行重置
RESULT=$(docker exec bot_panel python3 -c "
from app.database import SessionLocal
from app.models import User
from app.auth import get_password_hash
db = SessionLocal()
u = db.query(User).filter(User.username == '${TARGET_USER}').first()
if not u:
    print('NOT_FOUND')
elif not u.is_admin:
    print('NOT_ADMIN')
else:
    u.hashed_password = get_password_hash('${NEW_PW}')
    u.is_active = True
    db.commit()
    print('OK')
db.close()
")

case "$RESULT" in
  OK)
    echo -e "${GREEN}[OK]${NC}   密码重置成功！"
    echo -e "  用户名: ${BOLD}${TARGET_USER}${NC}"
    echo -e "  新密码: ${BOLD}（您刚才输入的密码）${NC}"
    ;;
  NOT_FOUND)
    echo -e "${RED}[ERR]${NC} 用户 '${TARGET_USER}' 不存在"
    exit 1
    ;;
  NOT_ADMIN)
    echo -e "${RED}[ERR]${NC} 用户 '${TARGET_USER}' 不是管理员"
    echo -e "  如需强制重置，请使用管理后台或联系数据库管理员"
    exit 1
    ;;
  *)
    echo -e "${RED}[ERR]${NC} 重置失败: $RESULT"
    exit 1
    ;;
esac
