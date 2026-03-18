# 🤖 Bot Platform — 多租户机器人托管平台

基于 **AstrBot + NapCat + Docker** 的共享机器人框架服务器。
支持多用户独立实例、一键创建、WebUI 管理、文件管理、实时日志。

---

## 📋 前置要求

- Linux 服务器（推荐 Ubuntu 22.04+）
- Docker 24+（[安装文档](https://docs.docker.com/engine/install/)）
- Docker Compose v2
- 开放防火墙端口：`80`、`8080`、`20000~29999`

---

## 🚀 快速部署（HTTP）
```bash
# 1. 解压
tar xzf shared-bot-platform-release.tar.gz
cd shared-bot-platform

# 2. 运行部署向导
bash scripts/setup.sh
```

向导会引导你填入：
- 服务器 IP 或域名
- 管理员密码

完成后访问 `http://你的IP/` 即可登录。

---

## 🌐 配置自定义域名

### 方法一：直接用 IP + 域名解析（最简单）

1. 在域名服务商添加 A 记录，指向服务器 IP
2. 修改 `.env` 中的 `PLATFORM_HOST`：
```bash
nano .env
# 修改为：
PLATFORM_HOST=你的域名.com
```

3. 重启面板：
```bash
docker compose restart panel
```

### 方法二：通过 Traefik 做域名路由（推荐）

修改 `docker-compose.yml` 中 panel 的 labels：
```yaml
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.panel.rule=Host(`你的域名.com`)"
      - "traefik.http.routers.panel.entrypoints=web"
      - "traefik.http.services.panel.loadbalancer.server.port=3000"
```

重启生效：
```bash
docker compose down && docker compose up -d
```

---

## 🔒 配置 HTTPS（使用自有 SSL 证书）

### 第一步：放置证书文件

将证书文件复制到 `traefik/certs/` 目录：
```bash
# 证书文件（包含完整证书链）
cp 你的证书.pem traefik/certs/cert.pem
# 或
cp 你的证书.crt traefik/certs/cert.pem

# 私钥文件
cp 你的私钥.key traefik/certs/key.pem
# 或
cp 你的私钥.pem traefik/certs/key.pem
```

> 宝塔面板证书通常在：
> `/www/server/panel/vhost/cert/你的域名/` 目录下
> 证书文件为 `fullchain.pem`，私钥为 `privkey.pem`

### 第二步：启用 tls.yml

编辑 `traefik/tls.yml`，取消注释：
```yaml
tls:
  certificates:
    - certFile: /certs/cert.pem
      keyFile: /certs/key.pem
```

### 第三步：替换 docker-compose.yml

将 `docker-compose.yml` 替换为以下内容（修改域名部分）：
```yaml
services:
  panel:
    build: ./panel
    container_name: bot_panel
    restart: unless-stopped
    environment:
      - SECRET_KEY=${SECRET_KEY:-change_this_secret_key_in_production}
      - ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD:-admin123}
      - DATA_DIR=/data/instances
      - DOCKER_SOCKET=/var/run/docker.sock
      - PLATFORM_HOST=${PLATFORM_HOST:-localhost}
      - BOT_NETWORK=bot_user_net
      - INSTANCE_PORT_BASE=${INSTANCE_PORT_BASE:-20000}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - bot_panel_db:/app/db
      - bot_instances_data:/data/instances
    networks:
      - bot_panel_net
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.panel.rule=Host(`你的域名.com`)"
      - "traefik.http.routers.panel.entrypoints=websecure"
      - "traefik.http.routers.panel.tls=true"
      - "traefik.http.services.panel.loadbalancer.server.port=3000"

  traefik:
    image: traefik:v3.0
    container_name: bot_traefik
    restart: unless-stopped
    command:
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--providers.docker.network=bot_panel_net"
      - "--providers.file.filename=/etc/traefik/tls.yml"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.web.http.redirections.entrypoint.to=websecure"
      - "--entrypoints.web.http.redirections.entrypoint.scheme=https"
      - "--entrypoints.websecure.address=:443"
      - "--entrypoints.traefik.address=:8080"
      - "--log.level=INFO"
    ports:
      - "80:80"
      - "443:443"
      - "${TRAEFIK_DASHBOARD_PORT:-8080}:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik/tls.yml:/etc/traefik/tls.yml:ro
      - ./traefik/certs:/certs:ro
    networks:
      - bot_panel_net

volumes:
  bot_panel_db:
  bot_instances_data:

networks:
  bot_panel_net:
    name: bot_panel_net
    external: true
  bot_user_net:
    name: bot_user_net
    external: true
```

### 第四步：重启
```bash
docker compose down && docker compose up -d
docker logs bot_traefik --tail=15
```

日志无 `ERR` 即成功，访问 `https://你的域名.com`。

---

## ⚙️ 环境变量说明（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SECRET_KEY` | — | JWT 签名密钥，**务必修改为随机长字符串** |
| `ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | `admin123` | 管理员密码，**务必修改** |
| `PLATFORM_HOST` | `localhost` | 服务器 IP 或域名（用于展示实例访问地址）|
| `PANEL_PORT` | `80` | 面板监听端口 |
| `INSTANCE_PORT_BASE` | `20000` | 用户实例端口起始值 |

---

## 📖 使用流程

1. 管理员登录，在 **管理后台** 创建用户账号
2. 用户登录后点击 **「创建我的实例」**（首次拉取镜像约 1~3 分钟）
3. 实例创建后，用户在控制台可以：
   - 打开 **NapCat WebUI** 扫码登录 QQ
   - 打开 **AstrBot WebUI** 配置 AI / 插件
   - 查看实时日志
   - 在线编辑配置文件
   - 拉取最新镜像更新实例

---

## 📡 端口分配规则

| 用户 ID | AstrBot WebUI | NapCat WebUI | NapCat WS |
|---------|--------------|--------------|-----------|
| 1 | 20000 | 20001 | 20002 |
| 2 | 20010 | 20011 | 20012 |
| N | 20000+(N-1)×10 | +1 | +2 |

防火墙需开放 `20000~29999` 端口段。

---

## 🛠️ 常用运维命令
```bash
# 查看面板日志
docker compose logs -f panel

# 重启面板（不丢失数据）
docker compose restart panel

# 重启 traefik
docker compose restart traefik

# 停止所有服务
docker compose down

# 备份所有实例数据
bash scripts/backup.sh

# 备份指定用户
bash scripts/backup.sh 用户名
```

---

## ❓ 常见问题

**Q: 创建实例卡在「正在拉取镜像」很久？**
A: 首次需下载 AstrBot 和 NapCat 镜像，共约 500MB~1GB，取决于网络速度，耐心等待即可。

**Q: 端口被占用？**
A: 修改 `.env` 中的 `INSTANCE_PORT_BASE`，例如改为 `30000`，并确保防火墙对应开放。

**Q: 忘记管理员密码？**
A: 直接修改 `.env` 中的 `ADMIN_PASSWORD`，然后执行：
```bash
docker compose restart panel
```
面板重启时会用新密码重建管理员账号（如账号已存在则不覆盖，需手动在容器内重置）。

**Q: 如何彻底重置某用户实例？**
A: 在管理后台删除用户，数据目录保留在 `data/instances/用户名/`，重新创建用户后再创建实例即可恢复数据。

**Q: 证书更新后如何生效？**
A: 替换 `traefik/certs/` 下的证书文件后，重启 traefik：
```bash
docker compose restart traefik
```

---

## 🔑 管理员忘记密码

在服务器上执行重置脚本：
```bash
bash scripts/reset_admin.sh
```

脚本会列出所有管理员账号，输入用户名和新密码即可重置，无需停止服务。
