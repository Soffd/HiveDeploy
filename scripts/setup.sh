#!/usr/bin/env bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║      Bot Platform 部署向导           ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── 依赖检查 ───────────────────────────────────────────────────
info "检查依赖..."
command -v docker >/dev/null 2>&1 || error "未找到 docker，请先安装 Docker"
docker compose version >/dev/null 2>&1 || \
  command -v docker-compose >/dev/null 2>&1 || \
  error "未找到 docker compose，请先安装 Docker Compose v2"
ok "Docker 已就绪"

# Docker 版本检查
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "0.0.0")
DOCKER_MAJOR=$(echo $DOCKER_VERSION | cut -d. -f1)
if [ "$DOCKER_MAJOR" -lt 20 ]; then
  warn "Docker 版本过低（当前: ${DOCKER_VERSION}），建议升级到 20.10+ 以获得最佳兼容性"
  warn "升级命令: curl -fsSL https://get.docker.com | sh"
  read -rp "  是否继续安装？[y/N]: " CONTINUE
  [ "${CONTINUE,,}" = "y" ] || error "请升级 Docker 后重试"
else
  ok "Docker 版本: ${DOCKER_VERSION}"
fi

# ── 生成 .env ─────────────────────────────────────────────────
if [ ! -f .env ]; then
  info "生成 .env 配置文件..."
  cp .env.example .env

  # 生成随机 SECRET_KEY
  SECRET=$(tr -dc 'A-Za-z0-9!@#%^&*' </dev/urandom | head -c 48 2>/dev/null || echo "fallback_key_$(date +%s)")
  sed -i "s/change_this_to_a_long_random_string_in_production/${SECRET}/" .env

  # 询问服务器 IP/域名
  echo ""
  read -rp "  请输入服务器 IP 或域名 [默认: localhost]: " HOST
  HOST=${HOST:-localhost}
  sed -i "s/your.server.ip.or.domain/${HOST}/" .env

  # 询问管理员用户名
  read -rp "  请设置管理员用户名 [默认: admin]: " ADMINUSER
  ADMINUSER=${ADMINUSER:-admin}
  sed -i "s/^ADMIN_USERNAME=.*/ADMIN_USERNAME=${ADMINUSER}/" .env

  # 询问管理员密码
  while true; do
    read -rsp "  请设置管理员密码（至少6位）: " ADMINPW
    echo ""
    if [ ${#ADMINPW} -lt 6 ]; then
      warn "密码至少 6 位，请重新输入"
    else
      break
    fi
  done
  sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=${ADMINPW}/" .env

  ok ".env 配置完成"
else
  warn ".env 已存在，跳过生成（如需重新配置请删除 .env 后重新运行）"
  # 从已有 .env 读取变量供后续显示
  HOST=$(grep PLATFORM_HOST .env | cut -d= -f2)
  ADMINUSER=$(grep ADMIN_USERNAME .env | cut -d= -f2)
fi

# ── 创建 Docker 网络 ──────────────────────────────────────────
info "准备 Docker 网络..."
docker network create bot_user_net  2>/dev/null && ok "创建网络 bot_user_net"  || warn "网络 bot_user_net 已存在"
docker network create bot_panel_net 2>/dev/null && ok "创建网络 bot_panel_net" || warn "网络 bot_panel_net 已存在"

# ── 构建并启动 ────────────────────────────────────────────────
info "构建面板镜像（首次需要几分钟）..."
docker compose build --no-cache panel

info "启动服务..."
docker compose up -d

echo ""
ok "部署完成！"
echo ""

# 读取最终配置显示（只从 .env 读，不显示密码）
FINAL_HOST=$(grep PLATFORM_HOST .env | cut -d= -f2)
FINAL_USER=$(grep ADMIN_USERNAME .env | cut -d= -f2)
PANEL_PORT=$(grep PANEL_PORT .env | cut -d= -f2)
PANEL_PORT=${PANEL_PORT:-80}

echo -e "  ${BOLD}访问地址:${NC}"
if [ "$PANEL_PORT" = "80" ]; then
  echo -e "  📦 管理面板:  ${CYAN}http://${FINAL_HOST}/${NC}"
else
  echo -e "  📦 管理面板:  ${CYAN}http://${FINAL_HOST}:${PANEL_PORT}/${NC}"
fi
echo -e "  🔧 Traefik:   ${CYAN}http://${FINAL_HOST}:8080/${NC}"
echo ""
echo -e "  ${BOLD}管理员账号:${NC}"
echo -e "  用户名: ${FINAL_USER}"
echo -e "  密码:   ${BOLD}（您在向导中设置的密码）${NC}"
echo ""
echo -e "  ${YELLOW}⚠️  提示：${NC}"
echo -e "  • 用户实例端口范围 20000~29999，请确保防火墙已开放"
echo -e "  • 如需 HTTPS，请参考 README.md 中的 SSL 配置说明"
echo -e "  • 账号由管理员在后台创建，普通用户无法自行注册"
echo ""
