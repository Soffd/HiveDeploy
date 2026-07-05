import docker
import os
import json
import logging
import glob
import io
import re
import secrets
import tarfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)

DATA_DIR      = os.environ.get("DATA_DIR", "/data/instances")
BOT_NETWORK   = os.environ.get("BOT_NETWORK", "bot_user_net")
ASTRBOT_IMAGE   = "soulter/astrbot:latest"
NAPCAT_IMAGE    = "mlikiowa/napcat-docker:latest"
LLONEBOT_IMAGE  = "initialencounter/llonebot:latest"
PORT_BASE     = int(os.environ.get("INSTANCE_PORT_BASE", "20000"))
def _clean_platform_host(value: str) -> str:
    raw = (value or "localhost").strip().lstrip("\ufeff")
    raw = raw.replace("\ufffd", "").replace("ï¿½", "").replace("�", "")
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname or raw.split("/")[0]
    host = host.strip().strip("[]")
    numbers = re.findall(r"\d{1,3}", host)
    if len(numbers) == 4 and not re.search(r"[A-Za-z]", host):
        octets = [int(n) for n in numbers]
        if all(0 <= n <= 255 for n in octets):
            return ".".join(str(n) for n in octets)
    host = re.sub(r"[^A-Za-z0-9.\-:]", "", host)
    return host or "localhost"


PANEL_HOST    = _clean_platform_host(os.environ.get("PLATFORM_HOST", "localhost"))
_TZ_ENV = {"TZ": "Asia/Shanghai"}
_TZ_VOL = {"/etc/localtime": {"bind": "/etc/localtime", "mode": "ro"}}

def _astrbot_env() -> dict:
    return {"ASTRBOT_PORT": "6185", "QUART_TRUSTED_HOSTS": "localhost,127.0.0.1"}


def _normalize_memory_mb(value, default: int = 0) -> int:
    try:
        mb = int(value)
    except (TypeError, ValueError):
        mb = default
    return max(0, min(mb, 262144))


def _container_memory_limits(user_id: int) -> Dict[str, int]:
    defaults = {"astrbot": 1024, "napcat": 500, "llonebot": 500}
    db = None
    try:
        from .database import SessionLocal
        from .models import SiteConfig, User
        db = SessionLocal()
        astrbot_cfg = db.query(SiteConfig).filter_by(key="default_astrbot_memory_mb").first()
        bot_cfg = db.query(SiteConfig).filter_by(key="default_bot_memory_mb").first()
        astrbot_default = _normalize_memory_mb(astrbot_cfg.value if astrbot_cfg else None, 1024)
        bot_default = _normalize_memory_mb(bot_cfg.value if bot_cfg else None, 500)
        user = db.query(User).filter_by(id=user_id).first()
        astrbot_limit = _normalize_memory_mb(getattr(user, "astrbot_memory_limit_mb", None), astrbot_default)
        bot_limit = _normalize_memory_mb(getattr(user, "bot_memory_limit_mb", None), bot_default)
        return {"astrbot": astrbot_limit, "napcat": bot_limit, "llonebot": bot_limit}
    except Exception as e:
        logger.warning(f"读取容器内存限制失败，使用默认值: {e}")
        return defaults
    finally:
        if db:
            db.close()


def _container_resource_kwargs(user_id: int, service: str) -> Dict[str, str]:
    mb = _container_memory_limits(user_id).get(service, 0)
    return {"mem_limit": f"{mb}m"} if mb > 0 else {}


def update_user_memory_limits(username: str, user_id: int) -> Dict[str, str]:
    """热更新已存在容器的内存上限；失败不会影响 DB 设置，下次重建会生效。"""
    client = get_client()
    result: Dict[str, str] = {}
    for service in ("astrbot", "napcat", "llonebot"):
        name = f"{service}_{username}"
        try:
            container = client.containers.get(name)
        except docker.errors.NotFound:
            continue
        try:
            limits = _container_memory_limits(user_id)
            mb = limits.get(service, 0)
            if mb > 0:
                container.update(mem_limit=f"{mb}m")
                result[service] = f"{mb}MB"
            else:
                container.update(mem_limit=0)
                result[service] = "unlimited"
        except Exception as e:
            logger.warning(f"更新 {name} 内存限制失败: {e}")
            result[service] = f"error: {e}"
    return result

# 镜像源列表，优先官方，失败后依次尝试加速源
# 格式：None = 官方 docker.io，字符串 = 镜像加速前缀
IMAGE_REGISTRIES = [
    None,                               # 官方源 docker.io
    "docker.1ms.run",                   # 1ms 加速
    "docker.m.daocloud.io",             # DaoCloud
    "docker.kubesre.xyz",               # KubeSRE
    "mirror.aliyuncs.com",              # 阿里云
    "docker.mirrors.ustc.edu.cn",       # 中科大
    "hub-mirror.c.163.com",             # 网易
    "registry.docker-cn.com",           # Docker 官方中国
]

# ── 公网 IP 探测（带缓存） ────────────────────────────────────
_cached_public_ip: Optional[str] = None

def detect_public_ip() -> str:
    """探测服务器真实公网 IP，优先使用环境变量 PLATFORM_HOST（若为 IP/域名），
    否则请求多个外部服务，结果缓存到进程内存。"""
    global _cached_public_ip
    if _cached_public_ip:
        return _cached_public_ip

    # 如果 PLATFORM_HOST 看起来不是 localhost / 127.x，直接用它
    import re as _re
    if PANEL_HOST and PANEL_HOST not in ("localhost", "127.0.0.1", "0.0.0.0"):
        logger.info(f"使用 PLATFORM_HOST 作为公网地址: {PANEL_HOST}")
        _cached_public_ip = PANEL_HOST
        return _cached_public_ip

    probe_urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://ip.sb",
        "https://icanhazip.com",
    ]
    for url in probe_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode().strip()
                # 简单校验：IPv4 或非空字符串
                if ip and _re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip):
                    logger.info(f"探测到公网 IP: {ip} (via {url})")
                    _cached_public_ip = ip
                    return ip
        except Exception as e:
            logger.debug(f"IP 探测失败 {url}: {e}")
            continue

    logger.warning(f"公网 IP 探测全部失败，回退到 PANEL_HOST: {PANEL_HOST}")
    _cached_public_ip = PANEL_HOST
    return PANEL_HOST


def _mirror_image(image: str, registry: Optional[str]) -> str:
    """将镜像名转换为使用指定镜像源的地址"""
    if registry is None:
        return image  # 官方源，不做转换
    # image 格式：user/repo:tag 或 library/repo:tag
    # 转换为 registry/user/repo:tag
    return f"{registry}/{image}"


def pull_with_fallback(client, image: str, progress_cb: Callable[[str, str], None]) -> str:
    """
    尝试从多个镜像源拉取镜像，返回成功拉取的镜像名（可能是加速源地址）。
    progress_cb(step, detail) 用于汇报进度。
    """
    last_error = None
    for i, registry in enumerate(IMAGE_REGISTRIES):
        mirror_img = _mirror_image(image, registry)
        source_name = registry or "官方源 (docker.io)"
        try:
            progress_cb(f"正在从 {source_name} 拉取镜像...", "")
            logger.info(f"尝试拉取 {mirror_img} (源: {source_name})")
            _layers = {}  # layer_id -> "状态 百分比 (当前/总计)"
            for line in client.api.pull(mirror_img, stream=True, decode=True):
                st   = line.get("status", "")
                prog = line.get("progressDetail", {})
                cur, tot = prog.get("current", 0), prog.get("total", 0)
                lid  = line.get("id", "")

                if lid:
                    if st in ("Pull complete", "Already exists", "Download complete"):
                        _layers.pop(lid, None)
                    else:
                        if tot and cur:
                            _layers[lid] = f"{st} {int(cur/tot*100)}% ({cur//1024//1024}MB/{tot//1024//1024}MB)"
                        else:
                            _layers[lid] = st

                if _layers:
                    detail = "\n".join(_layers.values())
                elif lid:
                    detail = st
                else:
                    detail = st if st else ""

                progress_cb(f"正在从 {source_name} 拉取镜像...", detail)
                # 检测明显的网络错误，提前放弃
                err_msg = line.get("error", "")
                if err_msg and any(k in err_msg.lower() for k in
                                   ["timeout", "connection refused", "no route", "dial tcp",
                                    "i/o timeout", "network", "tls", "certificate"]):
                    raise Exception(err_msg)

            # 如果用了加速源，给本地打原始 tag 方便容器引用
            if registry is not None:
                progress_cb(f"重新标记镜像...", "")
                try:
                    client.api.tag(mirror_img, image)
                except Exception:
                    pass  # tag 失败不影响使用

            logger.info(f"拉取成功: {mirror_img}")
            return mirror_img

        except Exception as e:
            last_error = str(e)
            logger.warning(f"从 {source_name} 拉取失败: {e}")
            if i < len(IMAGE_REGISTRIES) - 1:
                progress_cb(f"源 {source_name} 失败，切换下一个源...", str(e)[:80])
                time.sleep(1)  # 短暂等待后重试

    raise Exception(f"所有镜像源均拉取失败，最后错误: {last_error}")

PANEL_NETWORK = os.environ.get("PANEL_NETWORK", "bot_panel_net")

_creation_progress: Dict[str, Dict] = {}
_pull_progress: Dict[str, Dict] = {}

try:
    _client = docker.from_env()
except Exception as e:
    logger.error(f"无法连接到 Docker: {e}")
    _client = None


def get_client():
    if _client is None:
        raise RuntimeError("Docker 客户端未初始化")
    return _client


def _traefik_labels(username: str, service: str, container_port: int) -> dict:
    """返回容器基础 labels（不启用 Traefik 路由，用端口直接访问）"""
    return {
        "bot_platform": "true",
        "platform_user": username,
        "platform_service": service,
    }


def ensure_user_network():
    client = get_client()
    try:
        client.networks.get(BOT_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(BOT_NETWORK, driver="bridge")


def calc_ports(user_id: int) -> Dict[str, int]:
    """
    每用户固定分配 3 个端口（stride=10，其余 7 个留给弹性端口池）：
      +0  astrbot_web  — AstrBot 管理面板 (6185)
      +1  napcat_web   — NapCat WebUI    (6099)
      +2  astrbot_ws   — AstrBot WS 服务对外端口 (容器内 6199)
    """
    offset = (user_id - 1) * 10
    return {
        "astrbot_web": PORT_BASE + offset,
        "napcat_web":  PORT_BASE + offset + 1,
        "astrbot_ws":  PORT_BASE + offset + 2,  # AstrBot 6199 对外映射端口
    }


def calc_extra_ports(user_id: int) -> List[int]:
    """返回该用户可用的弹性端口列表（base+3 ~ base+9，共7个）"""
    base = PORT_BASE + (user_id - 1) * 10
    return list(range(base + 3, base + 10))


def get_creation_progress(username: str) -> Dict:
    return _creation_progress.get(username, {})


def _set_progress(username: str, step: str, detail: str = "", done: bool = False, error: str = ""):
    _creation_progress[username] = {"step": step, "detail": detail, "done": done, "error": error}
    logger.info(f"[{username}] {step} {detail}")


def get_pull_progress(username: str) -> dict:
    return _pull_progress.get(f"{username}:both", {})


def get_single_pull_progress(username: str, service: str) -> dict:
    return _pull_progress.get(f"{username}:{service}", {})


def _set_pull_progress(key: str, step: str, detail: str = "", done: bool = False, error: str = ""):
    _pull_progress[key] = {"step": step, "detail": detail, "done": done, "error": error}
    logger.info(f"[pull:{key}] {step} {detail}")


def write_astrbot_config(astrbot_dir: str, username: str):
    """AstrBot 监听 6199 等待 NapCat 反向连接"""
    config_dir = os.path.join(astrbot_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    config = {
        "platform_settings": [{
            "name": f"napcat_{username}",
            "type": "aiocqhttp",
            "enable": True,
            "config": {"ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199}
        }]
    }
    with open(os.path.join(config_dir, "platform.json"), "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    ensure_astrbot_dashboard_config(astrbot_dir)


def ensure_astrbot_dashboard_config(astrbot_dir: str):
    cmd_config_path = os.path.join(astrbot_dir, "cmd_config.json")
    cmd_config = {}
    if os.path.exists(cmd_config_path):
        try:
            with open(cmd_config_path, "r", encoding="utf-8") as f:
                cmd_config = json.load(f)
        except Exception:
            cmd_config = {}
    if not isinstance(cmd_config, dict):
        cmd_config = {}
    cmd_config.setdefault("config_version", 2)
    dashboard = cmd_config.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        dashboard = {}
        cmd_config["dashboard"] = dashboard
    dashboard["host"] = "0.0.0.0"
    with open(cmd_config_path, "w", encoding="utf-8") as f:
        json.dump(cmd_config, f, ensure_ascii=False, indent=2)


def ensure_astrbot_config_for_user(username: str):
    astrbot_dir = os.path.join(DATA_DIR, username, "astrbot")
    os.makedirs(astrbot_dir, exist_ok=True)
    ensure_astrbot_dashboard_config(astrbot_dir)


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _save_json_file(path: str, data: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_container_json(container, path: str) -> Any:
    errors = []
    quoted = sh_quote(path)
    for cmd in (
        ["sh", "-lc", f"test -f {quoted} && cat {quoted}"],
        ["bash", "-lc", f"test -f {quoted} && cat {quoted}"],
    ):
        try:
            result = container.exec_run(cmd)
            if result.exit_code == 0:
                raw = result.output.decode("utf-8", errors="replace")
                return json.loads(raw.lstrip("\ufeff"))
            errors.append(f"{cmd[0]} exit={result.exit_code}: {result.output.decode('utf-8', errors='replace')[:200]}")
        except Exception as exc:
            errors.append(f"{cmd[0]} error: {exc}")

    script = (
        "import json,sys;"
        "p=sys.argv[1];"
        "print(json.dumps(json.load(open(p,encoding='utf-8-sig')),ensure_ascii=False))"
    )
    for binary in ("python", "python3", "python3.11"):
        try:
            result = container.exec_run([binary, "-c", script, path])
            if result.exit_code == 0:
                return json.loads(result.output.decode("utf-8", errors="replace"))
            errors.append(f"{binary} exit={result.exit_code}: {result.output.decode('utf-8', errors='replace')[:200]}")
        except Exception as exc:
            errors.append(f"{binary} error: {exc}")

    try:
        bits, _ = container.get_archive(path)
        archive = b"".join(bits)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.getmembers()[0]
            extracted = tar.extractfile(member)
            if extracted:
                return json.loads(extracted.read().decode("utf-8-sig"))
        errors.append("archive extracted no file")
    except Exception as exc:
        errors.append(f"archive error: {exc}")

    raise RuntimeError("; ".join(errors) or f"file not readable: {path}")


def _write_container_json(container, path: str, data: Any):
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    archive_io = io.BytesIO()
    with tarfile.open(fileobj=archive_io, mode="w") as tar:
        info = tarfile.TarInfo(filename)
        info.size = len(content)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(content))
    archive_io.seek(0)
    if not container.put_archive(directory, archive_io.getvalue()):
        raise RuntimeError(f"无法写入容器文件: {path}")


def _find_container_file(container, command: str) -> str | None:
    try:
        result = container.exec_run(["sh", "-lc", command])
        if result.exit_code != 0:
            return None
        output = result.output.decode("utf-8", errors="ignore").strip()
        return output.splitlines()[0].strip() if output else None
    except Exception:
        return None


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _find_napcat_onebot_config(napcat_dir: str) -> str | None:
    patterns = [
        os.path.join(napcat_dir, "config", "onebot11_*.json"),
        os.path.join(napcat_dir, "**", "onebot11_*.json"),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[0]
    return None


def _container_bind_source(container_name: str, container_dest: str) -> str | None:
    client = get_client()
    try:
        container = client.containers.get(container_name)
        mounts = container.attrs.get("Mounts", [])
        dest = container_dest.rstrip("/")
        for mount in mounts:
            if (mount.get("Destination") or "").rstrip("/") == dest:
                source = mount.get("Source")
                if source:
                    return source
    except Exception:
        return None
    return None


def _service_data_dir(username: str, service: str, container_dest: str) -> str:
    container_name = f"{service}_{username}"
    mounted = _container_bind_source(container_name, container_dest)
    if mounted:
        return mounted
    return os.path.join(DATA_DIR, username, service)


def configure_napcat_astrbot(username: str, ws_url: str) -> Dict[str, Any]:
    status = get_instance_status(username)
    if status.get("astrbot") != "running" or status.get("napcat") != "running":
        return {"ok": False, "error": "AstrBot 和 NapCat 容器需要同时处于 running 状态"}
    if status.get("llonebot") == "running":
        return {"ok": False, "error": "LLOneBot 不支持一键配置，请切换为 NapCat"}

    client = get_client()
    try:
        astrbot_container = client.containers.get(f"astrbot_{username}")
        napcat_container = client.containers.get(f"napcat_{username}")
    except Exception:
        return {"ok": False, "error": "无法读取 AstrBot 或 NapCat 容器"}

    astrbot_config_path = _find_container_file(astrbot_container, r'''
for p in /AstrBot/data/cmd_config.json /app/data/cmd_config.json /data/cmd_config.json /AstrBot/cmd_config.json; do
  [ -f "$p" ] && echo "$p" && exit 0
done
find /AstrBot /app /data /root -name 'cmd_config.json' -type f 2>/dev/null | sed -n '1p'
''')
    napcat_config_path = _find_container_file(napcat_container, r'''
for p in /app/napcat/config/onebot11_*.json /root/.config/QQ/config/onebot11_*.json /root/.config/QQ/*/onebot11_*.json; do
  [ -f "$p" ] && echo "$p" && exit 0
done
find /app/napcat /root/.config/QQ -name 'onebot11_*.json' -type f 2>/dev/null | sed -n '1p'
''')
    if not astrbot_config_path:
        return {"ok": False, "error": "未在 AstrBot 容器内找到 cmd_config.json，请先启动一次 AstrBot"}
    if not napcat_config_path:
        return {"ok": False, "error": "未找到 NapCat onebot11 配置，请先完成扫码登录"}

    token = secrets.token_hex(16)

    try:
        astrbot_config = _read_container_json(astrbot_container, astrbot_config_path)
    except FileNotFoundError:
        return {"ok": False, "error": f"无法读取 AstrBot 配置文件：{astrbot_config_path}"}
    except Exception as exc:
        return {"ok": False, "error": f"无法读取 AstrBot 配置文件：{astrbot_config_path}；{exc}"}
    if not isinstance(astrbot_config, dict):
        return {"ok": False, "error": "AstrBot cmd_config.json 格式无效"}
    dashboard = astrbot_config.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        dashboard = {}
        astrbot_config["dashboard"] = dashboard
    dashboard["host"] = "0.0.0.0"
    astrbot_config["platform"] = [{
        "id": "default",
        "type": "aiocqhttp",
        "enable": True,
        "ws_reverse_host": "0.0.0.0",
        "ws_reverse_port": 6199,
        "ws_reverse_token": token,
    }]
    _write_container_json(astrbot_container, astrbot_config_path, astrbot_config)

    try:
        napcat_config = _read_container_json(napcat_container, napcat_config_path)
    except FileNotFoundError:
        return {"ok": False, "error": "无法读取 NapCat onebot11 配置"}
    except Exception as exc:
        return {"ok": False, "error": f"无法读取 NapCat onebot11 配置：{exc}"}
    if not isinstance(napcat_config, dict):
        return {"ok": False, "error": "NapCat onebot11 配置格式无效"}
    network = napcat_config.setdefault("network", {})
    if not isinstance(network, dict):
        network = {}
        napcat_config["network"] = network
    network["websocketClients"] = [{
        "enable": True,
        "name": "rws",
        "url": ws_url,
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": token,
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
        "verifyCertificate": True,
    }]
    _write_container_json(napcat_container, napcat_config_path, napcat_config)

    restart_user_instance(username, "astrbot")
    restart_user_instance(username, "napcat")
    return {
        "ok": True,
        "ws_url": ws_url,
        "astrbot_config": astrbot_config_path,
        "napcat_config": napcat_config_path,
    }


def _find_astrbot_cmd_config(container) -> str | None:
    return _find_container_file(container, r'''
for p in /AstrBot/data/cmd_config.json /app/data/cmd_config.json /data/cmd_config.json /AstrBot/cmd_config.json; do
  [ -f "$p" ] && echo "$p" && exit 0
done
find /AstrBot /app /data /root -name 'cmd_config.json' -type f 2>/dev/null | sed -n '1p'
''')


def reset_astrbot_dashboard_password(username: str) -> Dict[str, Any]:
    status = get_instance_status(username)
    if status.get("astrbot") != "running":
        return {"ok": False, "error": "AstrBot 容器需要处于 running 状态"}

    client = get_client()
    try:
        astrbot_container = client.containers.get(f"astrbot_{username}")
    except Exception:
        return {"ok": False, "error": "无法读取 AstrBot 容器"}

    config_path = _find_astrbot_cmd_config(astrbot_container)
    if not config_path:
        return {"ok": False, "error": "未在 AstrBot 容器内找到 cmd_config.json，请先启动一次 AstrBot"}

    try:
        config = _read_container_json(astrbot_container, config_path)
    except Exception as exc:
        return {"ok": False, "error": f"无法读取 AstrBot 配置文件：{config_path}；{exc}"}
    if not isinstance(config, dict):
        return {"ok": False, "error": "AstrBot cmd_config.json 格式无效"}

    config["dashboard"] = {
        "enable": True,
        "host": "0.0.0.0",
        "port": 6185,
        "disable_access_log": True,
        "ssl": {
            "enable": False,
            "cert_file": "",
            "key_file": "",
            "ca_certs": "",
        },
    }

    try:
        _write_container_json(astrbot_container, config_path, config)
    except Exception as exc:
        return {"ok": False, "error": f"无法写入 AstrBot 配置文件：{config_path}；{exc}"}

    restart_user_instance(username, "astrbot")
    return {"ok": True, "config_path": config_path}


def write_napcat_config(napcat_dir: str, username: str, astrbot_ws_port: int):
    """
    NapCat 通过公网 IP + 对外端口连接到 AstrBot，完全绕过容器内部 hostname 解析。
    astrbot_ws_port 为宿主机上 AstrBot 6199 的对外映射端口。
    """
    public_ip = detect_public_ip()
    ws_url = f"ws://{public_ip}:{astrbot_ws_port}"
    logger.info(f"[{username}] NapCat → AstrBot WS 地址: {ws_url}")

    config_dir = os.path.join(napcat_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    config = {
        "httpServers": [],
        "wsServers": [{
            "name": "default_ws", "enable": True,
            "port": 3001, "host": "0.0.0.0",
            "heartInterval": 30000, "token": "",
            "messagePostFormat": "array", "debug": False
        }],
        "wsReverseServers": [{
            "name": f"astrbot_{username}", "enable": True,
            "url": ws_url,
            "heartInterval": 30000, "reconnectInterval": 5000, "token": ""
        }],
        "debug": False, "localFile2Url": True
    }
    with open(os.path.join(config_dir, "napcat.json"), "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def write_llonebot_config(llonebot_dir: str, username: str, astrbot_ws_port: int):
    """
    LLOneBot 通过公网 IP + 对外端口连接到 AstrBot，与 NapCat 功能一致。
    astrbot_ws_port 为宿主机上 AstrBot 6199 的对外映射端口。
    """
    public_ip = detect_public_ip()
    ws_url = f"ws://{public_ip}:{astrbot_ws_port}"
    logger.info(f"[{username}] LLOneBot → AstrBot WS 地址: {ws_url}")

    config_dir = os.path.join(llonebot_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    config = {
        "wsServers": [{
            "name": "default_ws", "enable": True,
            "port": 3001, "host": "0.0.0.0",
            "heartInterval": 30000, "token": "",
            "messagePostFormat": "array", "debug": False
        }],
        "wsReverseServers": [{
            "name": f"astrbot_{username}", "enable": True,
            "url": ws_url,
            "heartInterval": 30000, "reconnectInterval": 5000, "token": ""
        }],
        "debug": False, "localFile2Url": True
    }
    with open(os.path.join(config_dir, "llonebot.json"), "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _build_extra_port_bindings(extra_ports: List[Dict]) -> Dict:
    """将弹性端口配置转成 docker SDK ports 字典"""
    bindings = {}
    for ep in extra_ports:
        container_port = f"{ep['container_port']}/tcp"
        bindings[container_port] = ep["host_port"]
    return bindings


def _create_instance_background(username: str, user_id: int, extra_ports: List[Dict], bot_type: str, callback):
    client = get_client()
    try:
        ensure_user_network()
        ports = calc_ports(user_id)
        effective_ws = _effective_ws_port(extra_ports, ports["astrbot_ws"])
        bot_label = "LLOneBot" if bot_type == "llonebot" else "NapCat"
        bot_image = LLONEBOT_IMAGE if bot_type == "llonebot" else NAPCAT_IMAGE

        _set_progress(username, "准备数据目录...")
        user_data_dir = os.path.join(DATA_DIR, username)
        astrbot_dir = os.path.join(user_data_dir, "astrbot")
        bot_dir     = os.path.join(user_data_dir, bot_type)
        os.makedirs(astrbot_dir, exist_ok=True)
        os.makedirs(bot_dir,    exist_ok=True)

        # 写入对应 bot 的配置文件
        if bot_type == "llonebot":
            write_llonebot_config(bot_dir, username, effective_ws)
        else:
            write_napcat_config(bot_dir, username, effective_ws)
        write_astrbot_config(astrbot_dir, username)

        # 清理旧容器（包含互斥 bot）
        old_names = [f"astrbot_{username}", f"napcat_{username}", f"llonebot_{username}"]
        for name in old_names:
            try:
                old = client.containers.get(name)
                old.stop(timeout=5); old.remove()
            except docker.errors.NotFound:
                pass

        # 拉取镜像
        for image, label in [(bot_image, bot_label), (ASTRBOT_IMAGE, "AstrBot")]:
            _set_progress(username, f"正在拉取 {label} 镜像...", "这可能需要几分钟")
            pull_with_fallback(
                client, image,
                lambda step, detail, _l=label: _set_progress(username, step or f"正在拉取 {_l} 镜像...", detail)
            )

        # 弹性端口分组
        ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
        bt_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == bot_type}

        _set_progress(username, f"正在启动 {bot_label} 容器...")
        if bot_type == "llonebot":
            bot_c = _run_llonebot(client, username, user_id, ports, user_data_dir, bt_extra)
        else:
            bot_c = _run_napcat(client, username, user_id, ports, user_data_dir, bt_extra)

        _set_progress(username, "正在启动 AstrBot 容器...")
        astrbot_ports = {
            "6185/tcp": ports["astrbot_web"],
            "6199/tcp": ports["astrbot_ws"],
        }
        astrbot_ports.update(ab_extra)
        astrbot_c = client.containers.run(
            ASTRBOT_IMAGE, name=f"astrbot_{username}",
            network=BOT_NETWORK, hostname=f"astrbot_{username}",
            ports=astrbot_ports,
            volumes={**{astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}}, **_TZ_VOL},
            environment={**_astrbot_env(), **_TZ_ENV},
            labels=_traefik_labels(username, "astrbot", 6185),
            detach=True, restart_policy={"Name": "unless-stopped"},
            **_container_resource_kwargs(user_id, "astrbot"),
        )

        _set_progress(username, "实例创建完成！", done=True)
        result = {"astrbot_container_id": astrbot_c.id, "ports": ports}
        if bot_type == "llonebot":
            result["llonebot_container_id"] = bot_c.id
        else:
            result["napcat_container_id"] = bot_c.id
        callback(result)

    except Exception as e:
        logger.exception(f"创建实例失败: {username}")
        _set_progress(username, "创建失败", error=str(e))
        callback(None, str(e))


def create_user_instance_async(username: str, user_id: int, callback, extra_ports: List[Dict] = None, bot_type: str = "napcat"):
    _set_progress(username, "初始化中...")
    t = threading.Thread(
        target=_create_instance_background,
        args=(username, user_id, extra_ports or [], bot_type, callback),
        daemon=True,
    )
    t.start()


def _force_remove(client, name: str):
    """停止并强制删除容器，等待端口释放，容器不存在时静默忽略"""
    try:
        c = client.containers.get(name)
        c.stop(timeout=10)
        c.remove(force=True)
        # 等待 Docker 完全释放容器占用的端口
        for _ in range(20):
            try:
                client.containers.get(name)
                time.sleep(0.3)
            except docker.errors.NotFound:
                return
    except docker.errors.NotFound:
        pass


def _run_napcat(client, username: str, user_id: int, ports: Dict, data_dir: str, nc_extra: Dict):
    """创建并启动 NapCat 容器（端口冲突前请先调用 _force_remove 清理旧容器）"""
    napcat_dir = os.path.join(data_dir, "napcat")
    napcat_ports = {"6099/tcp": ports["napcat_web"]}
    napcat_ports.update(nc_extra)
    return client.containers.run(
        NAPCAT_IMAGE, name=f"napcat_{username}",
        network=BOT_NETWORK, hostname=f"napcat_{username}",
        ports=napcat_ports,
        volumes={**{napcat_dir: {"bind": "/root/.config/QQ", "mode": "rw"}}, **_TZ_VOL},
        environment={**{"WEBUI_PORT": "6099"}, **_TZ_ENV},
        labels=_traefik_labels(username, "napcat", 6099),
        detach=True, restart_policy={"Name": "unless-stopped"},
        **_container_resource_kwargs(user_id, "napcat"),
    )


def _run_llonebot(client, username: str, user_id: int, ports: Dict, data_dir: str, ll_extra: Dict):
    """创建并启动 LLOneBot 容器（端口冲突前请先调用 _force_remove 清理旧容器）"""
    llonebot_dir = os.path.join(data_dir, "llonebot")
    llonebot_ports = {"3080/tcp": ports["napcat_web"]}
    llonebot_ports.update(ll_extra)
    return client.containers.run(
        LLONEBOT_IMAGE, name=f"llonebot_{username}",
        network=BOT_NETWORK, hostname=f"llonebot_{username}",
        ports=llonebot_ports,
        volumes={**{llonebot_dir: {"bind": "/root/.config/QQ", "mode": "rw"}}, **_TZ_VOL},
        environment={**{"WEBUI_PORT": "3080"}, **_TZ_ENV},
        labels=_traefik_labels(username, "llonebot", 3080),
        detach=True, restart_policy={"Name": "unless-stopped"},
        **_container_resource_kwargs(user_id, "llonebot"),
    )


def get_occupied_host_ports(client, exclude_containers: List[str] = None) -> Dict[int, str]:
    """
    返回宿主机上正在运行的容器所占用的端口映射 {host_port: container_name}。
    exclude_containers: 即将被删除重建的容器，其端口不算冲突。
    """
    exclude = set(exclude_containers or [])
    occupied: Dict[int, str] = {}
    try:
        for c in client.containers.list(all=False):  # 只看运行中
            if c.name in exclude:
                continue
            for bindings in (c.ports or {}).values():
                if not bindings:
                    continue
                for b in bindings:
                    try:
                        occupied[int(b["HostPort"])] = c.name
                    except (KeyError, ValueError, TypeError):
                        pass
    except Exception as e:
        logger.warning(f"获取宿主机端口占用失败: {e}")
    return occupied


def check_port_conflicts(new_host_ports: List[int], exclude_containers: List[str] = None) -> Dict[int, str]:
    """
    检查 new_host_ports 中是否有端口被其他容器占用（排除即将被删除的容器）。
    返回冲突字典 {port: 占用它的容器名}，为空则无冲突。
    """
    client = get_client()
    occupied = get_occupied_host_ports(client, exclude_containers)
    return {p: occupied[p] for p in new_host_ports if p in occupied}


def get_affected_services(old_mappings: List[Dict], new_mappings: List[Dict]) -> List[str]:
    """
    比较新旧弹性端口配置，返回实际变更的服务列表（只有端口真正变化才重建）。
    """
    def _key(mappings, svc):
        return sorted(
            (int(m["container_port"]), int(m["host_port"]))
            for m in mappings if m.get("service") == svc and m.get("container_port")
        )
    return [svc for svc in ("astrbot", "napcat", "llonebot")
            if _key(old_mappings, svc) != _key(new_mappings, svc)]


def _effective_ws_port(extra_ports: List[Dict], default_ws_port: int) -> int:
    """若弹性端口覆盖了 astrbot 的 6199 容器端口，返回自定义宿主端口，否则返回默认值。"""
    for ep in extra_ports:
        if ep.get("service") == "astrbot" and ep.get("container_port") == 6199:
            return ep["host_port"]
    return default_ws_port


def stop_affected_services(username: str, services: List[str]):
    """同步停止受影响的容器（释放端口），供上层在 DB 提交前调用。"""
    client = get_client()
    for svc in services:
        _force_remove(client, f"{svc}_{username}")
        if svc == "napcat":
            _force_remove(client, f"llonebot_{username}")
        elif svc == "llonebot":
            _force_remove(client, f"napcat_{username}")
    # 确保 Docker 已完全释放端口再返回，避免后续容器启动时端口仍被占用
    time.sleep(1.0)


def recreate_services(username: str, user_id: int, services: List[str],
                      extra_ports: List[Dict] = None, old_extra_ports: List[Dict] = None):
    """
    启动指定服务容器（不拉镜像，不停止旧容器——调用方需先调 stop_affected_services）。
    services: ["astrbot"] / ["napcat"] / ["llonebot"] / ["astrbot","napcat"] 等组合
    old_extra_ports: 失败时用于回退的旧端口配置。
    进度通过 get_pull_progress(username) 查询。
    """
    key = f"{username}:both"
    extra_ports = extra_ports or []
    old_extra_ports = old_extra_ports or []

    def _build_extras(eps):
        return (
            {f"{ep['container_port']}/tcp": ep["host_port"] for ep in eps if ep.get("service") == "astrbot"},
            {f"{ep['container_port']}/tcp": ep["host_port"] for ep in eps if ep.get("service") == "napcat"},
            {f"{ep['container_port']}/tcp": ep["host_port"] for ep in eps if ep.get("service") == "llonebot"},
        )

    def _start_one(client, ports, svc, label, ab_ex, nc_ex, ll_ex, ws_port):
        """启动单个容器，端口冲突时自动释放并重试（最多 3 次）"""
        container_name = f"{svc}_{username}"
        data_dir = os.path.join(DATA_DIR, username)
        for attempt in range(3):
            try:
                if svc == "astrbot":
                    astrbot_ports = {
                        "6185/tcp": ports["astrbot_web"],
                        "6199/tcp": ports["astrbot_ws"],
                    }
                    astrbot_ports.update(ab_ex)
                    return client.containers.run(
                        ASTRBOT_IMAGE, name=container_name,
                        network=BOT_NETWORK, hostname=f"astrbot_{username}",
                        ports=astrbot_ports,
                        volumes={**{os.path.join(data_dir, "astrbot"): {"bind": "/AstrBot/data", "mode": "rw"}}, **_TZ_VOL},
                        environment={**_astrbot_env(), **_TZ_ENV},
                        labels=_traefik_labels(username, "astrbot", 6185),
                        detach=True, restart_policy={"Name": "unless-stopped"},
                        **_container_resource_kwargs(user_id, "astrbot"),
                    )
                elif svc == "llonebot":
                    llonebot_dir = os.path.join(data_dir, "llonebot")
                    os.makedirs(llonebot_dir, exist_ok=True)
                    write_llonebot_config(llonebot_dir, username, ws_port)
                    return _run_llonebot(client, username, user_id, ports, data_dir, ll_ex)
                else:  # napcat
                    napcat_dir = os.path.join(data_dir, "napcat")
                    write_napcat_config(napcat_dir, username, ws_port)
                    return _run_napcat(client, username, user_id, ports, data_dir, nc_ex)
            except Exception as e:
                err = str(e)
                if "port is already allocated" in err and attempt < 2:
                    logger.warning(f"端口冲突，清理 {container_name} 后重试 ({attempt+2}/3)")
                    _force_remove(client, container_name)
                    _set_pull_progress(key, f"{label} 端口冲突，等待释放后重试...")
                    time.sleep(2.0)
                    continue
                raise

    def _start_services(client, ports, eps, label_prefix=""):
        ab_ex, nc_ex, ll_ex = _build_extras(eps)
        ws_port = _effective_ws_port(eps, ports["astrbot_ws"])
        for svc in services:
            label = {"astrbot": "AstrBot", "napcat": "NapCat", "llonebot": "LLOneBot"}.get(svc, svc)
            _set_pull_progress(key, f"{label_prefix}启动新 {label} 容器...")
            _start_one(client, ports, svc, label, ab_ex, nc_ex, ll_ex, ws_port)

    def _rollback_db():
        try:
            from .database import SessionLocal
            from .models import Instance
            db2 = SessionLocal()
            inst = db2.query(Instance).filter_by(user_id=user_id).first()
            if inst:
                inst.extra_ports_json = json.dumps(old_extra_ports)
                db2.commit()
            db2.close()
        except Exception as dbe:
            logger.exception(f"回退 DB 失败: {username}")

    def _run():
        client = get_client()
        try:
            ports = calc_ports(user_id)
            _start_services(client, ports, extra_ports)
            _set_pull_progress(key, "重建完成！", done=True)
        except Exception as e:
            logger.exception(f"recreate_services 失败: {username}")
            if old_extra_ports:
                logger.warning(f"尝试用旧配置回退 {username}")
                _set_pull_progress(key, "新配置启动失败，正在回退到旧端口配置...")
                try:
                    for svc in services:
                        _force_remove(client, f"{svc}_{username}")
                    time.sleep(1.0)
                    _start_services(client, calc_ports(user_id), old_extra_ports)
                    _rollback_db()
                    _set_pull_progress(key, "新配置启动失败，已自动回退到旧配置", done=True)
                    return
                except Exception as re:
                    logger.exception(f"回退也失败: {username}")
            _set_pull_progress(key, "重建失败", error=str(e))

    _set_pull_progress(key, f"初始化启动 {'+'.join(services)}...")
    threading.Thread(target=_run, daemon=True).start()


def create_single_service_async(username: str, user_id: int, service: str, extra_ports: List[Dict] = None):
    """单独创建 astrbot / napcat / llonebot 容器。napcat 与 llonebot 互斥。"""
    extra_ports = extra_ports or []
    labels = {"astrbot": "AstrBot", "napcat": "NapCat", "llonebot": "LLOneBot"}
    label = labels.get(service, service)

    def _run():
        client = get_client()
        try:
            ensure_user_network()
            ports = calc_ports(user_id)
            effective_ws = _effective_ws_port(extra_ports, ports["astrbot_ws"])
            data_dir = os.path.join(DATA_DIR, username)
            ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
            nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}
            ll_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "llonebot"}

            if service == "astrbot":
                # 仅创建 AstrBot：只需删除自身旧容器，不再重建其他 bot
                _set_progress(username, "释放端口：停止旧容器...")
                _force_remove(client, f"astrbot_{username}")

                astrbot_dir = os.path.join(data_dir, "astrbot")
                os.makedirs(astrbot_dir, exist_ok=True)
                write_astrbot_config(astrbot_dir, username)

                _set_progress(username, "正在拉取 AstrBot 镜像...")
                pull_with_fallback(
                    client, ASTRBOT_IMAGE,
                    lambda step, detail: _set_progress(username, step or "正在拉取 AstrBot 镜像...", detail)
                )

                _set_progress(username, "正在启动 AstrBot 容器...")
                astrbot_ports = {
                    "6185/tcp": ports["astrbot_web"],
                    "6199/tcp": ports["astrbot_ws"],
                }
                astrbot_ports.update(ab_extra)
                client.containers.run(
                    ASTRBOT_IMAGE, name=f"astrbot_{username}",
                    network=BOT_NETWORK, hostname=f"astrbot_{username}",
                    ports=astrbot_ports,
                    volumes={**{astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}}, **_TZ_VOL},
                    environment={**_astrbot_env(), **_TZ_ENV},
                    labels=_traefik_labels(username, "astrbot", 6185),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                    **_container_resource_kwargs(user_id, "astrbot"),
                )

            elif service == "napcat":
                # NapCat 与 LLOneBot 互斥：先清除 llonebot
                _force_remove(client, f"llonebot_{username}")
                _force_remove(client, f"napcat_{username}")
                napcat_dir = os.path.join(data_dir, "napcat")
                os.makedirs(napcat_dir, exist_ok=True)
                write_napcat_config(napcat_dir, username, effective_ws)
                _set_progress(username, "正在拉取 NapCat 镜像...")
                pull_with_fallback(
                    client, NAPCAT_IMAGE,
                    lambda step, detail: _set_progress(username, step or "正在拉取 NapCat 镜像...", detail)
                )
                _run_napcat(client, username, user_id, ports, data_dir, nc_extra)

            elif service == "llonebot":
                # LLOneBot 与 NapCat 互斥：先清除 napcat
                _force_remove(client, f"napcat_{username}")
                _force_remove(client, f"llonebot_{username}")
                llonebot_dir = os.path.join(data_dir, "llonebot")
                os.makedirs(llonebot_dir, exist_ok=True)
                write_llonebot_config(llonebot_dir, username, effective_ws)
                _set_progress(username, "正在拉取 LLOneBot 镜像...")
                pull_with_fallback(
                    client, LLONEBOT_IMAGE,
                    lambda step, detail: _set_progress(username, step or "正在拉取 LLOneBot 镜像...", detail)
                )
                _run_llonebot(client, username, user_id, ports, data_dir, ll_extra)

            _set_progress(username, f"{label} 创建完成！", done=True)

        except Exception as e:
            logger.exception(f"单服务创建失败: {username}/{service}")
            _set_progress(username, f"{label} 创建失败", error=str(e))

    _set_progress(username, f"初始化 {label}...")
    threading.Thread(target=_run, daemon=True).start()


def _recreate_containers(client, username: str, user_id: int, extra_ports: List[Dict], bot_type: str = "napcat"):
    """
    重建两个容器（使用当前镜像，保留数据）。
    顺序：先停旧容器释放端口 → 建 AstrBot → 建 NapCat/LLOneBot。
    """
    ports = calc_ports(user_id)
    effective_ws = _effective_ws_port(extra_ports, ports["astrbot_ws"])
    data_dir = os.path.join(DATA_DIR, username)
    astrbot_dir = os.path.join(data_dir, "astrbot")
    os.makedirs(astrbot_dir, exist_ok=True)
    write_astrbot_config(astrbot_dir, username)

    # 先全部清理，保证端口释放（幂等，不存在时静默）
    _force_remove(client, f"napcat_{username}")
    _force_remove(client, f"llonebot_{username}")
    _force_remove(client, f"astrbot_{username}")

    ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
    nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}
    ll_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "llonebot"}

    # 先建 AstrBot（占 astrbot_ws 端口）
    astrbot_ports = {
        "6185/tcp": ports["astrbot_web"],
        "6199/tcp": ports["astrbot_ws"],
    }
    astrbot_ports.update(ab_extra)
    client.containers.run(
        ASTRBOT_IMAGE, name=f"astrbot_{username}",
        network=BOT_NETWORK, hostname=f"astrbot_{username}",
        ports=astrbot_ports,
        volumes={**{astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}}, **_TZ_VOL},
        environment={**_astrbot_env(), **_TZ_ENV},
        labels=_traefik_labels(username, "astrbot", 6185),
        detach=True, restart_policy={"Name": "unless-stopped"},
        **_container_resource_kwargs(user_id, "astrbot"),
    )

    # 再建 bot 容器
    if bot_type == "llonebot":
        llonebot_dir = os.path.join(data_dir, "llonebot")
        os.makedirs(llonebot_dir, exist_ok=True)
        write_llonebot_config(llonebot_dir, username, effective_ws)
        _run_llonebot(client, username, user_id, ports, data_dir, ll_extra)
    else:
        napcat_dir = os.path.join(data_dir, "napcat")
        write_napcat_config(napcat_dir, username, effective_ws)
        _run_napcat(client, username, user_id, ports, data_dir, nc_extra)


def pull_and_recreate(username: str, user_id: int, extra_ports: List[Dict] = None, bot_type: str = "napcat"):
    """拉取两个最新镜像并重建容器"""
    key = f"{username}:both"
    extra_ports = extra_ports or []
    bot_label = "LLOneBot" if bot_type == "llonebot" else "NapCat"
    bot_image = LLONEBOT_IMAGE if bot_type == "llonebot" else NAPCAT_IMAGE

    def _run():
        client = get_client()
        try:
            for image, label in [(bot_image, bot_label), (ASTRBOT_IMAGE, "AstrBot")]:
                _set_pull_progress(key, f"正在拉取 {label} 最新镜像...")
                pull_with_fallback(
                    client, image,
                    lambda step, detail, _k=key, _l=label: _set_pull_progress(_k, step or f"正在拉取 {_l} 最新镜像...", detail)
                )

            _set_pull_progress(key, "用新镜像重建容器（停止旧容器 + 启动）...")
            _recreate_containers(client, username, user_id, extra_ports, bot_type)
            _set_pull_progress(key, "更新完成！", done=True)

        except Exception as e:
            logger.exception(f"全量更新失败: {username}")
            _set_pull_progress(key, "更新失败", error=str(e))

    _set_pull_progress(key, "初始化更新...")
    threading.Thread(target=_run, daemon=True).start()


def recreate_only(username: str, user_id: int, extra_ports: List[Dict] = None, bot_type: str = "napcat"):
    """
    不拉新镜像，直接用现有镜像重建容器（用于弹性端口配置变更等场景）。
    进度通过 get_pull_progress(username) 查询（key = "{username}:both"）。
    """
    key = f"{username}:both"
    extra_ports = extra_ports or []

    def _run():
        client = get_client()
        try:
            _set_pull_progress(key, "停止旧容器，释放端口...")
            _force_remove(client, f"napcat_{username}")
            _force_remove(client, f"llonebot_{username}")
            _force_remove(client, f"astrbot_{username}")
            _set_pull_progress(key, "启动容器中...")
            _recreate_containers(client, username, user_id, extra_ports, bot_type)
            _set_pull_progress(key, "重建完成！", done=True)
        except Exception as e:
            logger.exception(f"重建容器失败: {username}")
            _set_pull_progress(key, "重建失败", error=str(e))

    _set_pull_progress(key, "初始化重建...")
    threading.Thread(target=_run, daemon=True).start()


def pull_and_recreate_single(username: str, user_id: int, service: str, extra_ports: List[Dict] = None):
    """仅拉取并重建指定服务容器。napcat 与 llonebot 互斥。"""
    key = f"{username}:{service}"
    extra_ports = extra_ports or []
    image_map = {"astrbot": ASTRBOT_IMAGE, "napcat": NAPCAT_IMAGE, "llonebot": LLONEBOT_IMAGE}
    label_map = {"astrbot": "AstrBot", "napcat": "NapCat", "llonebot": "LLOneBot"}
    image = image_map.get(service, NAPCAT_IMAGE)
    label = label_map.get(service, service)

    def _run():
        client = get_client()
        try:
            ports = calc_ports(user_id)
            effective_ws = _effective_ws_port(extra_ports, ports["astrbot_ws"])
            data_dir = os.path.join(DATA_DIR, username)
            ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
            nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}
            ll_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "llonebot"}

            _set_pull_progress(key, f"正在拉取 {label} 最新镜像...")
            pull_with_fallback(
                client, image,
                lambda step, detail, _k=key, _l=label: _set_pull_progress(_k, step or f"正在拉取 {_l} 最新镜像...", detail)
            )

            if service == "astrbot":
                _set_pull_progress(key, "停止并删除旧 AstrBot 容器...")
                _force_remove(client, f"astrbot_{username}")
                _set_pull_progress(key, "用新镜像重建 AstrBot 容器...")
                astrbot_dir = os.path.join(data_dir, "astrbot")
                os.makedirs(astrbot_dir, exist_ok=True)
                write_astrbot_config(astrbot_dir, username)
                astrbot_ports = {
                    "6185/tcp": ports["astrbot_web"],
                    "6199/tcp": ports["astrbot_ws"],
                }
                astrbot_ports.update(ab_extra)
                client.containers.run(
                    ASTRBOT_IMAGE, name=f"astrbot_{username}",
                    network=BOT_NETWORK, hostname=f"astrbot_{username}",
                    ports=astrbot_ports,
                    volumes={**{astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}}, **_TZ_VOL},
                    environment={**_astrbot_env(), **_TZ_ENV},
                    labels=_traefik_labels(username, "astrbot", 6185),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                    **_container_resource_kwargs(user_id, "astrbot"),
                )

            elif service == "napcat":
                _force_remove(client, f"llonebot_{username}")
                _set_pull_progress(key, "停止并删除旧 NapCat 容器...")
                _force_remove(client, f"napcat_{username}")
                napcat_dir = os.path.join(data_dir, "napcat")
                write_napcat_config(napcat_dir, username, effective_ws)
                _set_pull_progress(key, "用新镜像重建 NapCat 容器...")
                _run_napcat(client, username, user_id, ports, data_dir, nc_extra)

            elif service == "llonebot":
                _force_remove(client, f"napcat_{username}")
                _set_pull_progress(key, "停止并删除旧 LLOneBot 容器...")
                _force_remove(client, f"llonebot_{username}")
                llonebot_dir = os.path.join(data_dir, "llonebot")
                os.makedirs(llonebot_dir, exist_ok=True)
                write_llonebot_config(llonebot_dir, username, effective_ws)
                _set_pull_progress(key, "用新镜像重建 LLOneBot 容器...")
                _run_llonebot(client, username, user_id, ports, data_dir, ll_extra)

            _set_pull_progress(key, f"{label} 更新完成！", done=True)

        except Exception as e:
            logger.exception(f"单服务更新失败: {username}/{service}")
            _set_pull_progress(key, "更新失败", error=str(e))

    _set_pull_progress(key, "初始化更新...")
    threading.Thread(target=_run, daemon=True).start()


def stop_user_instance(username: str, service: str = None):
    """停止容器。service 为 None 时停止全部，否则只停止指定服务"""
    client = get_client()
    all_containers = [f"napcat_{username}", f"llonebot_{username}", f"astrbot_{username}"]
    names = [f"{service}_{username}"] if service else all_containers
    for name in names:
        try:
            client.containers.get(name).stop(timeout=10)
        except docker.errors.NotFound:
            pass


def start_user_instance(username: str, service: str = None):
    """启动容器。service 为 None 时启动全部，否则只启动指定服务"""
    client = get_client()
    all_containers = [f"astrbot_{username}", f"napcat_{username}", f"llonebot_{username}"]
    names = [f"{service}_{username}"] if service else all_containers
    if service in (None, "astrbot"):
        ensure_astrbot_config_for_user(username)
    for name in names:
        try:
            client.containers.get(name).start()
        except docker.errors.NotFound:
            pass


def restart_user_instance(username: str, service: str = None):
    """重启容器。service 为 None 时重启全部，否则只重启指定服务"""
    client = get_client()
    all_containers = [f"napcat_{username}", f"llonebot_{username}", f"astrbot_{username}"]
    names = [f"{service}_{username}"] if service else all_containers
    if service in (None, "astrbot"):
        ensure_astrbot_config_for_user(username)
    for name in names:
        try:
            client.containers.get(name).restart(timeout=10)
        except docker.errors.NotFound:
            pass


def delete_user_instance(username: str, service: str = None):
    client = get_client()
    all_containers = [f"napcat_{username}", f"llonebot_{username}", f"astrbot_{username}"]
    names = [f"{service}_{username}"] if service else all_containers
    for name in names:
        try:
            c = client.containers.get(name)
            c.stop(timeout=5); c.remove(force=True)
        except docker.errors.NotFound:
            pass


def get_instance_status(username: str) -> Dict[str, str]:
    client = get_client()
    status = {}
    for key, name in [("astrbot", f"astrbot_{username}"),
                       ("napcat", f"napcat_{username}"),
                       ("llonebot", f"llonebot_{username}")]:
        try:
            c = client.containers.get(name)
            c.reload()
            status[key] = c.status
        except docker.errors.NotFound:
            status[key] = "not_found"
        except Exception:
            status[key] = "error"
    return status


def get_container_logs(username: str, service: str, lines: int = 200) -> str:
    client = get_client()
    try:
        return client.containers.get(f"{service}_{username}").logs(
            tail=lines, timestamps=True
        ).decode("utf-8", errors="replace")
    except docker.errors.NotFound:
        return "容器不存在"
    except Exception as e:
        return f"获取日志失败: {e}"


def get_all_instances_status() -> Dict[str, Dict[str, str]]:
    client = get_client()
    result = {}
    try:
        for c in client.containers.list(all=True):
            name = c.name
            username = (c.labels or {}).get("platform_user", "")
            service  = (c.labels or {}).get("platform_service", "")

            # Fallback: detect by container name pattern {service}_{username}
            if not username or not service:
                for prefix in ("astrbot_", "napcat_", "llonebot_"):
                    if name.startswith(prefix):
                        service = prefix[:-1]
                        username = name[len(prefix):]
                        break

            if username and service:
                if username not in result:
                    result[username] = {}
                result[username][service] = c.status
    except Exception as e:
        logger.error(f"获取实例状态失败: {e}")
    return result


def get_container_stats(username: str, service: str) -> dict:
    """获取容器的 CPU、内存、网络、磁盘 IO 统计"""
    try:
        client = get_client()
        container = client.containers.get(f"{service}_{username}")
        if container.status != "running":
            return {"error": "容器未运行"}

        # stats(stream=False) 取一次快照
        raw = container.stats(stream=False)

        # CPU 使用率
        cpu_delta   = raw["cpu_stats"]["cpu_usage"]["total_usage"] - \
                      raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"]["system_cpu_usage"] - \
                       raw["precpu_stats"]["system_cpu_usage"]
        num_cpus    = raw["cpu_stats"].get("online_cpus") or \
                      len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
        cpu_pct = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        # 内存
        mem_usage = raw["memory_stats"].get("usage", 0)
        mem_cache = raw["memory_stats"].get("stats", {}).get("cache", 0)
        mem_rss   = mem_usage - mem_cache   # 实际 RSS（去掉 page cache）
        mem_limit = raw["memory_stats"].get("limit", 0)

        # 网络 IO（累计）
        net_rx, net_tx = 0, 0
        for iface in raw.get("networks", {}).values():
            net_rx += iface.get("rx_bytes", 0)
            net_tx += iface.get("tx_bytes", 0)

        # 磁盘 IO（累计）
        blk_read, blk_write = 0, 0
        for entry in raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []:
            if entry.get("op") == "read":
                blk_read += entry.get("value", 0)
            elif entry.get("op") == "write":
                blk_write += entry.get("value", 0)

        return {
            "cpu_pct":    round(cpu_pct, 2),
            "mem_rss":    mem_rss,
            "mem_limit":  mem_limit,
            "mem_pct":    round(mem_rss / mem_limit * 100, 1) if mem_limit else 0,
            "net_rx":     net_rx,
            "net_tx":     net_tx,
            "blk_read":   blk_read,
            "blk_write":  blk_write,
            "error":      None,
        }
    except docker.errors.NotFound:
        return {"error": "容器不存在"}
    except Exception as e:
        return {"error": str(e)}


def get_data_dir_size(username: str, service: str) -> int:
    """获取用户数据目录大小（字节），读取宿主机目录"""
    import subprocess
    path = os.path.join(DATA_DIR, username, service)
    if not os.path.isdir(path):
        return 0
    try:
        result = subprocess.run(
            ["du", "-sb", path], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except Exception:
        pass
    return 0

