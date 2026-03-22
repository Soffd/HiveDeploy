import docker
import os
import json
import logging
import threading
import time
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)

DATA_DIR      = os.environ.get("DATA_DIR", "/data/instances")
BOT_NETWORK   = os.environ.get("BOT_NETWORK", "bot_user_net")
ASTRBOT_IMAGE = "soulter/astrbot:latest"
NAPCAT_IMAGE  = "mlikiowa/napcat-docker:latest"
PORT_BASE     = int(os.environ.get("INSTANCE_PORT_BASE", "20000"))
PANEL_HOST    = os.environ.get("PLATFORM_HOST", "localhost")

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
            for line in client.api.pull(mirror_img, stream=True, decode=True):
                st   = line.get("status", "")
                prog = line.get("progressDetail", {})
                cur, tot = prog.get("current", 0), prog.get("total", 0)
                if tot and cur:
                    detail = f"{st} {int(cur/tot*100)}%  ({cur//1024//1024}MB/{tot//1024//1024}MB)"
                else:
                    detail = st
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
    offset = (user_id - 1) * 10
    return {
        "astrbot_web": PORT_BASE + offset,
        "napcat_web":  PORT_BASE + offset + 1,
        "napcat_ws":   PORT_BASE + offset + 2,
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


def write_napcat_config(napcat_dir: str, username: str):
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
            "url": f"ws://astrbot_{username}:6199",
            "heartInterval": 30000, "reconnectInterval": 5000, "token": ""
        }],
        "debug": False, "localFile2Url": True
    }
    with open(os.path.join(config_dir, "napcat.json"), "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _build_extra_port_bindings(extra_ports: List[Dict]) -> Dict:
    """将弹性端口配置转成 docker SDK ports 字典"""
    bindings = {}
    for ep in extra_ports:
        container_port = f"{ep['container_port']}/tcp"
        bindings[container_port] = ep["host_port"]
    return bindings


def _create_instance_background(username: str, user_id: int, extra_ports: List[Dict], callback):
    client = get_client()
    try:
        ensure_user_network()
        ports = calc_ports(user_id)

        _set_progress(username, "准备数据目录...")
        user_data_dir = os.path.join(DATA_DIR, username)
        astrbot_dir = os.path.join(user_data_dir, "astrbot")
        napcat_dir  = os.path.join(user_data_dir, "napcat")
        os.makedirs(astrbot_dir, exist_ok=True)
        os.makedirs(napcat_dir,  exist_ok=True)
        write_napcat_config(napcat_dir, username)
        write_astrbot_config(astrbot_dir, username)

        for name in [f"napcat_{username}", f"astrbot_{username}"]:
            try:
                old = client.containers.get(name)
                old.stop(timeout=5); old.remove()
            except docker.errors.NotFound:
                pass

        # 拉取镜像（自动多源重试）
        for image, label in [(NAPCAT_IMAGE, "NapCat"), (ASTRBOT_IMAGE, "AstrBot")]:
            _set_progress(username, f"正在拉取 {label} 镜像...", "这可能需要几分钟")
            pull_with_fallback(
                client, image,
                lambda step, detail, _l=label: _set_progress(username, step or f"正在拉取 {_l} 镜像...", detail)
            )

        # AstrBot 弹性端口
        ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
        nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}

        _set_progress(username, "正在启动 NapCat 容器...")
        napcat_ports = {"6099/tcp": ports["napcat_web"], "3001/tcp": ports["napcat_ws"]}
        napcat_ports.update(nc_extra)
        napcat_c = client.containers.run(
            NAPCAT_IMAGE, name=f"napcat_{username}",
            network=BOT_NETWORK, hostname=f"napcat_{username}",
            ports=napcat_ports,
            volumes={napcat_dir: {"bind": "/root/.config/QQ", "mode": "rw"}},
            environment={"NAPCAT_WS_PORT": "3001", "WEBUI_PORT": "6099"},
            labels=_traefik_labels(username, "napcat", 6099),
            detach=True, restart_policy={"Name": "unless-stopped"},
        )

        _set_progress(username, "正在启动 AstrBot 容器...")
        astrbot_ports = {"6185/tcp": ports["astrbot_web"]}
        astrbot_ports.update(ab_extra)
        astrbot_c = client.containers.run(
            ASTRBOT_IMAGE, name=f"astrbot_{username}",
            network=BOT_NETWORK, hostname=f"astrbot_{username}",
            ports=astrbot_ports,
            volumes={astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}},
            environment={"ASTRBOT_PORT": "6185"},
            labels=_traefik_labels(username, "astrbot", 6185),
            detach=True, restart_policy={"Name": "unless-stopped"},
        )

        _set_progress(username, "实例创建完成！", done=True)
        callback({"napcat_container_id": napcat_c.id, "astrbot_container_id": astrbot_c.id, "ports": ports})

    except Exception as e:
        logger.exception(f"创建实例失败: {username}")
        _set_progress(username, "创建失败", error=str(e))
        callback(None, str(e))


def create_user_instance_async(username: str, user_id: int, callback, extra_ports: List[Dict] = None):
    _set_progress(username, "初始化中...")
    t = threading.Thread(
        target=_create_instance_background,
        args=(username, user_id, extra_ports or [], callback),
        daemon=True,
    )
    t.start()


def create_single_service_async(username: str, user_id: int, service: str, extra_ports: List[Dict] = None):
    """单独创建 astrbot 或 napcat 容器"""
    key = f"{username}:{service}"
    extra_ports = extra_ports or []
    label = "AstrBot" if service == "astrbot" else "NapCat"

    def _run():
        client = get_client()
        try:
            ensure_user_network()
            ports = calc_ports(user_id)
            data_dir = os.path.join(DATA_DIR, username)

            # 停止并删除旧容器（如有）
            try:
                old = client.containers.get(f"{service}_{username}")
                old.stop(timeout=5); old.remove()
            except docker.errors.NotFound:
                pass

            _set_progress(username, f"正在拉取 {label} 镜像...")

            if service == "napcat":
                image = NAPCAT_IMAGE
                napcat_dir = os.path.join(data_dir, "napcat")
                os.makedirs(napcat_dir, exist_ok=True)
                write_napcat_config(napcat_dir, username)
                pull_with_fallback(
                    client, image,
                    lambda step, detail, _l=label: _set_progress(username, step or f"正在拉取 {_l} 镜像...", detail)
                )
                nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}
                napcat_ports = {"6099/tcp": ports["napcat_web"], "3001/tcp": ports["napcat_ws"]}
                napcat_ports.update(nc_extra)
                client.containers.run(
                    NAPCAT_IMAGE, name=f"napcat_{username}",
                    network=BOT_NETWORK, hostname=f"napcat_{username}",
                    ports=napcat_ports,
                    volumes={napcat_dir: {"bind": "/root/.config/QQ", "mode": "rw"}},
                    environment={"NAPCAT_WS_PORT": "3001", "WEBUI_PORT": "6099"},
                    labels=_traefik_labels(username, "napcat", 6099),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                )
            else:
                image = ASTRBOT_IMAGE
                astrbot_dir = os.path.join(data_dir, "astrbot")
                os.makedirs(astrbot_dir, exist_ok=True)
                write_astrbot_config(astrbot_dir, username)
                pull_with_fallback(
                    client, image,
                    lambda step, detail, _l=label: _set_progress(username, step or f"正在拉取 {_l} 镜像...", detail)
                )
                ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
                astrbot_ports = {"6185/tcp": ports["astrbot_web"]}
                astrbot_ports.update(ab_extra)
                client.containers.run(
                    ASTRBOT_IMAGE, name=f"astrbot_{username}",
                    network=BOT_NETWORK, hostname=f"astrbot_{username}",
                    ports=astrbot_ports,
                    volumes={astrbot_dir: {"bind": "/AstrBot/data", "mode": "rw"}},
                    environment={"ASTRBOT_PORT": "6185"},
                    labels=_traefik_labels(username, "astrbot", 6185),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                )

            _set_progress(username, f"{label} 创建完成！", done=True)

        except Exception as e:
            logger.exception(f"单服务创建失败: {username}/{service}")
            _set_progress(username, f"{label} 创建失败", error=str(e))

    _set_progress(username, f"初始化 {label}...")
    threading.Thread(target=_run, daemon=True).start()


def _recreate_containers(client, username: str, user_id: int, extra_ports: List[Dict]):
    """重建两个容器（使用当前镜像，保留数据）"""
    ports = calc_ports(user_id)
    data_dir = os.path.join(DATA_DIR, username)

    ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
    nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}

    napcat_ports = {"6099/tcp": ports["napcat_web"], "3001/tcp": ports["napcat_ws"]}
    napcat_ports.update(nc_extra)
    client.containers.run(
        NAPCAT_IMAGE, name=f"napcat_{username}",
        network=BOT_NETWORK, hostname=f"napcat_{username}",
        ports=napcat_ports,
        volumes={os.path.join(data_dir, "napcat"): {"bind": "/root/.config/QQ", "mode": "rw"}},
        environment={"NAPCAT_WS_PORT": "3001", "WEBUI_PORT": "6099"},
        labels=_traefik_labels(username, "napcat", 6099),
        detach=True, restart_policy={"Name": "unless-stopped"},
    )

    astrbot_ports = {"6185/tcp": ports["astrbot_web"]}
    astrbot_ports.update(ab_extra)
    client.containers.run(
        ASTRBOT_IMAGE, name=f"astrbot_{username}",
        network=BOT_NETWORK, hostname=f"astrbot_{username}",
        ports=astrbot_ports,
        volumes={os.path.join(data_dir, "astrbot"): {"bind": "/AstrBot/data", "mode": "rw"}},
        environment={"ASTRBOT_PORT": "6185"},
        labels=_traefik_labels(username, "astrbot", 6185),
        detach=True, restart_policy={"Name": "unless-stopped"},
    )


def pull_and_recreate(username: str, user_id: int, extra_ports: List[Dict] = None):
    """拉取两个最新镜像并重建容器"""
    key = f"{username}:both"
    extra_ports = extra_ports or []

    def _run():
        client = get_client()
        try:
            for image, label in [(NAPCAT_IMAGE, "NapCat"), (ASTRBOT_IMAGE, "AstrBot")]:
                _set_pull_progress(key, f"正在拉取 {label} 最新镜像...")
                pull_with_fallback(
                    client, image,
                    lambda step, detail, _k=key, _l=label: _set_pull_progress(_k, step or f"正在拉取 {_l} 最新镜像...", detail)
                )

            _set_pull_progress(key, "停止并删除旧容器...")
            stop_user_instance(username)
            for name in [f"napcat_{username}", f"astrbot_{username}"]:
                try:
                    client.containers.get(name).remove(force=True)
                except docker.errors.NotFound:
                    pass

            _set_pull_progress(key, "用新镜像重建容器...")
            _recreate_containers(client, username, user_id, extra_ports)
            _set_pull_progress(key, "更新完成！", done=True)

        except Exception as e:
            logger.exception(f"全量更新失败: {username}")
            _set_pull_progress(key, "更新失败", error=str(e))

    _set_pull_progress(key, "初始化更新...")
    threading.Thread(target=_run, daemon=True).start()


def pull_and_recreate_single(username: str, user_id: int, service: str, extra_ports: List[Dict] = None):
    """仅拉取并重建指定服务容器"""
    key = f"{username}:{service}"
    extra_ports = extra_ports or []
    image = ASTRBOT_IMAGE if service == "astrbot" else NAPCAT_IMAGE
    label = "AstrBot" if service == "astrbot" else "NapCat"

    def _run():
        client = get_client()
        try:
            _set_pull_progress(key, f"正在拉取 {label} 最新镜像...")
            pull_with_fallback(
                client, image,
                lambda step, detail, _k=key, _l=label: _set_pull_progress(_k, step or f"正在拉取 {_l} 最新镜像...", detail)
            )

            _set_pull_progress(key, f"停止并删除旧 {label} 容器...")
            container_name = f"{service}_{username}"
            try:
                c = client.containers.get(container_name)
                c.stop(timeout=10)
                c.remove(force=True)
            except docker.errors.NotFound:
                pass

            _set_pull_progress(key, f"用新镜像重建 {label} 容器...")
            ports = calc_ports(user_id)
            data_dir = os.path.join(DATA_DIR, username)

            if service == "napcat":
                nc_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "napcat"}
                napcat_ports = {"6099/tcp": ports["napcat_web"], "3001/tcp": ports["napcat_ws"]}
                napcat_ports.update(nc_extra)
                client.containers.run(
                    NAPCAT_IMAGE, name=f"napcat_{username}",
                    network=BOT_NETWORK, hostname=f"napcat_{username}",
                    ports=napcat_ports,
                    volumes={os.path.join(data_dir, "napcat"): {"bind": "/root/.config/QQ", "mode": "rw"}},
                    environment={"NAPCAT_WS_PORT": "3001", "WEBUI_PORT": "6099"},
                    labels=_traefik_labels(username, "napcat", 6099),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                )
            else:
                ab_extra = {f"{ep['container_port']}/tcp": ep["host_port"] for ep in extra_ports if ep.get("service") == "astrbot"}
                astrbot_ports = {"6185/tcp": ports["astrbot_web"]}
                astrbot_ports.update(ab_extra)
                client.containers.run(
                    ASTRBOT_IMAGE, name=f"astrbot_{username}",
                    network=BOT_NETWORK, hostname=f"astrbot_{username}",
                    ports=astrbot_ports,
                    volumes={os.path.join(data_dir, "astrbot"): {"bind": "/AstrBot/data", "mode": "rw"}},
                    environment={"ASTRBOT_PORT": "6185"},
                    labels=_traefik_labels(username, "astrbot", 6185),
                    detach=True, restart_policy={"Name": "unless-stopped"},
                )

            _set_pull_progress(key, f"{label} 更新完成！", done=True)

        except Exception as e:
            logger.exception(f"单服务更新失败: {username}/{service}")
            _set_pull_progress(key, "更新失败", error=str(e))

    _set_pull_progress(key, "初始化更新...")
    threading.Thread(target=_run, daemon=True).start()


def stop_user_instance(username: str, service: str = None):
    """停止容器。service 为 None 时停止全部，否则只停止指定服务"""
    client = get_client()
    names = [f"{service}_{username}"] if service else [f"napcat_{username}", f"astrbot_{username}"]
    for name in names:
        try:
            client.containers.get(name).stop(timeout=10)
        except docker.errors.NotFound:
            pass


def start_user_instance(username: str, service: str = None):
    """启动容器。service 为 None 时启动全部，否则只启动指定服务"""
    client = get_client()
    names = [f"{service}_{username}"] if service else [f"astrbot_{username}", f"napcat_{username}"]
    for name in names:
        try:
            client.containers.get(name).start()
        except docker.errors.NotFound:
            pass


def restart_user_instance(username: str, service: str = None):
    """重启容器。service 为 None 时重启全部，否则只重启指定服务"""
    client = get_client()
    names = [f"{service}_{username}"] if service else [f"napcat_{username}", f"astrbot_{username}"]
    for name in names:
        try:
            client.containers.get(name).restart(timeout=10)
        except docker.errors.NotFound:
            pass


def delete_user_instance(username: str, service: str = None):
    client = get_client()
    names = [f"{service}_{username}"] if service else [f"napcat_{username}", f"astrbot_{username}"]
    for name in names:
        try:
            c = client.containers.get(name)
            c.stop(timeout=5); c.remove(force=True)
        except docker.errors.NotFound:
            pass


def get_instance_status(username: str) -> Dict[str, str]:
    client = get_client()
    status = {}
    for key, name in [("astrbot", f"astrbot_{username}"), ("napcat", f"napcat_{username}")]:
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
        for c in client.containers.list(all=True, filters={"label": "bot_platform=true"}):
            username = c.labels.get("platform_user", "unknown")
            service  = c.labels.get("platform_service", "unknown")
            if username not in result:
                result[username] = {}
            result[username][service] = c.status
    except Exception as e:
        logger.error(f"获取实例状态失败: {e}")
    return result