import json
import re
import logging
from datetime import datetime

import docker as docker_lib
from fastapi import APIRouter, Request, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .bootstrap import templates
from .database import get_db, SessionLocal
from .models import User, Instance, SiteConfig
from .auth import get_current_user_from_cookie
from .docker_manager import (
    create_user_instance_async, get_creation_progress,
    stop_user_instance, start_user_instance, restart_user_instance,
    delete_user_instance, get_instance_status, get_container_logs,
    calc_ports, calc_extra_ports,
    pull_and_recreate, pull_and_recreate_single,
    get_pull_progress, get_single_pull_progress,
    create_single_service_async, detect_public_ip,
    check_port_conflicts, get_affected_services, stop_affected_services, recreate_services,
    configure_napcat_astrbot,
)

logger = logging.getLogger(__name__)
router = APIRouter()


VALID_SERVICES = ("astrbot", "napcat", "llonebot")


def _is_vip_user(user: User) -> bool:
    if user.is_admin:
        return True
    return bool(user.vip_expire_at and user.vip_expire_at >= datetime.now())


def _tool_access(db: Session, key: str, default: str = "limited") -> str:
    cfg = db.query(SiteConfig).filter_by(key=key).first()
    value = (cfg.value if cfg else default or "limited").strip().lower()
    if value not in ("free", "limited", "vip"):
        value = default
    return value


def _tool_allowed(access: str, user: User) -> bool:
    return access != "vip" or _is_vip_user(user)


# ════════════════════════════════════════════════════════════
#  实例管理 API
# ════════════════════════════════════════════════════════════
@router.post("/api/instance/create")
async def create_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db),
                          bot_type: str = Body("napcat", embed=True)):
    if db.query(Instance).filter_by(user_id=user.id).first():
        return JSONResponse({"error": "实例已存在"})

    if bot_type not in ("napcat", "llonebot"):
        bot_type = "napcat"

    ports = calc_ports(user.id)

    def _cb(result, error=None):
        _db = SessionLocal()
        try:
            inst = _db.query(Instance).filter_by(user_id=user.id).first()
            if result:
                inst.astrbot_container_id = result["astrbot_container_id"]
                if bot_type == "llonebot":
                    inst.llonebot_container_id = result.get("llonebot_container_id")
                    inst.napcat_container_id = None
                else:
                    inst.napcat_container_id = result.get("napcat_container_id")
                    inst.llonebot_container_id = None
                inst.bot_type = bot_type
                inst.status = "running"
            else:
                inst.status = "error"
            _db.commit()
        finally:
            _db.close()

    inst = Instance(
        user_id=user.id,
        astrbot_port=ports["astrbot_web"],
        napcat_web_port=ports["napcat_web"],
        astrbot_ws_port=ports["astrbot_ws"],
        extra_ports_json="[]",
        bot_type=bot_type,
        status="creating",
    )
    db.add(inst); db.commit()
    create_user_instance_async(user.username, user.id, _cb, bot_type=bot_type)
    return JSONResponse({"ok": True})


@router.get("/api/instance/progress")
async def instance_progress(user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_creation_progress(user.username))


@router.post("/api/instance/start")
async def start_instance(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    start_user_instance(user.username)
    inst.status = "running"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/stop")
async def stop_instance(user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    stop_user_instance(user.username)
    inst.status = "stopped"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/restart")
async def restart_instance(user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    restart_user_instance(user.username)
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/start/{service}")
async def start_single(service: str,
                       user: User = Depends(get_current_user_from_cookie),
                       db: Session = Depends(get_db)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    start_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/stop/{service}")
async def stop_single(service: str,
                      user: User = Depends(get_current_user_from_cookie),
                      db: Session = Depends(get_db)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    stop_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/restart/{service}")
async def restart_single(service: str,
                         user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    restart_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/create/{service}")
async def create_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in VALID_SERVICES: raise HTTPException(400)

    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst:
        ports = calc_ports(user.id)
        inst = Instance(
            user_id=user.id,
            astrbot_port=ports["astrbot_web"],
            napcat_web_port=ports["napcat_web"],
            astrbot_ws_port=ports["astrbot_ws"],
            extra_ports_json="[]",
            bot_type="llonebot" if service == "llonebot" else "napcat",
            status="creating",
        )
        db.add(inst); db.commit()
    else:
        # 更新 bot_type
        if service in ("napcat", "llonebot"):
            inst.bot_type = service
            extra_ports = json.loads(inst.extra_ports_json or "[]")
            extra_ports = [ep for ep in extra_ports if ep.get("service") in ("astrbot", service)]
            inst.extra_ports_json = json.dumps(extra_ports)
            db.commit()

    extra_ports = json.loads(inst.extra_ports_json or "[]")
    create_single_service_async(user.username, user.id, service, extra_ports)
    return JSONResponse({"ok": True})


@router.post("/api/instance/delete/{service}")
async def delete_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    delete_user_instance(user.username, service)

    # 删除单个容器后，检查是否所有容器均已不存在
    status = get_instance_status(user.username)
    all_gone = all(status.get(k, "not_found") == "not_found"
                   for k in ("astrbot", "napcat", "llonebot"))
    if all_gone:
        db.delete(inst); db.commit()
    else:
        # 更新对应的 container_id 为 None
        if service == "astrbot":
            inst.astrbot_container_id = None
        elif service == "napcat":
            inst.napcat_container_id = None
        elif service == "llonebot":
            inst.llonebot_container_id = None
        db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/delete")
async def delete_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    delete_user_instance(user.username)
    db.delete(inst); db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/update")
async def update_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"error": "实例不存在"})
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    pull_and_recreate(user.username, user.id, extra_ports,
                      bot_type=inst.bot_type or "napcat")
    return JSONResponse({"ok": True})


@router.get("/api/instance/update_progress")
async def update_progress(user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_pull_progress(user.username))


@router.post("/api/instance/update/{service}")
async def update_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, "无效服务")
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"error": "实例不存在"})
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    if service in ("napcat", "llonebot"):
        inst.bot_type = service
        inst.napcat_container_id = None
        inst.llonebot_container_id = None
        extra_ports = [ep for ep in extra_ports if ep.get("service") in ("astrbot", service)]
        inst.extra_ports_json = json.dumps(extra_ports)
        db.commit()
    pull_and_recreate_single(user.username, user.id, service, extra_ports)
    return JSONResponse({"ok": True})


@router.get("/api/instance/update_progress/{service}")
async def update_single_progress(service: str,
                                  user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_single_pull_progress(user.username, service))


@router.post("/api/instance/auto_config")
async def auto_config_instance(user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db)):
    access = _tool_access(db, "quick_tool_auto_config_access", "limited")
    if not _tool_allowed(access, user):
        return JSONResponse({"ok": False, "error": "该快捷工具仅限 VIP 用户使用"}, status_code=403)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst:
        return JSONResponse({"ok": False, "error": "实例不存在"})
    if (inst.bot_type or "napcat") != "napcat":
        return JSONResponse({"ok": False, "error": "一键配置仅支持 NapCat，LLOneBot 暂不支持"})

    ws_port = inst.astrbot_ws_port
    try:
        extra_ports = json.loads(inst.extra_ports_json or "[]")
    except Exception:
        extra_ports = []
    for ep in extra_ports:
        if ep.get("service") == "astrbot" and ep.get("container_port") == 6199:
            ws_port = ep.get("host_port", ws_port)

    public_host = (detect_public_ip() or "").replace("https://", "").replace("http://", "").split("/")[0]
    ws_url = f"ws://{public_host}:{ws_port}/ws"
    return JSONResponse(configure_napcat_astrbot(user.username, ws_url))


# 弹性端口
@router.get("/api/instance/extra_ports")
async def get_extra_ports(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"mappings": [], "available": []})
    mappings = json.loads(inst.extra_ports_json or "[]")
    used = {m["host_port"] for m in mappings}
    available = [p for p in calc_extra_ports(user.id) if p not in used]
    return JSONResponse({"mappings": mappings, "available": available})


@router.post("/api/instance/extra_ports")
async def save_extra_ports(request: Request,
                           user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst:
        return JSONResponse({"error": "实例不存在"})

    body = await request.json()
    new_mappings = body.get("mappings", [])
    allowed = set(calc_extra_ports(user.id))

    for m in new_mappings:
        if m.get("host_port") not in allowed:
            return JSONResponse({"error": f"端口 {m.get('host_port')} 不在允许范围内"})

    try:
        old_mappings = json.loads(inst.extra_ports_json or "[]")
    except Exception:
        old_mappings = []

    affected = get_affected_services(old_mappings, new_mappings)

    if not affected:
        inst.extra_ports_json = json.dumps(new_mappings)
        db.commit()
        return JSONResponse({"ok": True, "rebuilt": False,
                             "message": "配置未变化，已保存（无需重建容器）"})

    # 检查新端口是否被非重建容器占用（排除即将重建的容器）
    new_host_ports = [m["host_port"] for m in new_mappings
                      if m.get("service") in affected]
    exclude_containers = [f"{svc}_{user.username}" for svc in affected]
    conflicts = check_port_conflicts(new_host_ports, exclude_containers)
    if conflicts:
        detail = "；".join(f"端口 {p} 被容器 {c} 占用" for p, c in conflicts.items())
        return JSONResponse({"error": f"端口冲突，无法重建：{detail}"})

    # 先同步停止受影响容器（释放端口），再提交 DB，最后异步启动
    # 顺序保证：端口释放 → 配置持久化 → 新容器启动，避免交叉占用导致冲突
    try:
        stop_affected_services(user.username, affected)
    except Exception as e:
        return JSONResponse({"error": f"停止旧容器失败：{e}"})

    inst.extra_ports_json = json.dumps(new_mappings)
    db.commit()
    recreate_services(user.username, user.id, affected, new_mappings, old_mappings)
    return JSONResponse({"ok": True, "rebuilt": True, "affected": affected})


# ════════════════════════════════════════════════════════════
#  日志
# ════════════════════════════════════════════════════════════
@router.get("/api/instance/napcat_token")
async def get_napcat_token(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    bot_type = inst.bot_type if inst else "napcat"
    container_name = f"llonebot_{user.username}" if bot_type == "llonebot" else f"napcat_{user.username}"
    try:
        client = docker_lib.from_env()
        container = client.containers.get(container_name)
        logs = container.logs(stream=False, timestamps=False)
        text = logs.decode("utf-8", errors="replace")
        matches = re.findall(r'WebUi Token:\s*([a-f0-9]+)', text, re.IGNORECASE)
        if matches:
            return JSONResponse({"token": matches[-1]})
    except Exception as e:
        logger.error(f"获取 {bot_type} token 失败: {e}")
    return JSONResponse({"token": None})


@router.get("/logs/{service}", response_class=HTMLResponse)
async def view_logs(service: str, request: Request,
                    user: User = Depends(get_current_user_from_cookie)):
    if service not in VALID_SERVICES:
        raise HTTPException(404)
    logs = get_container_logs(user.username, service, 200)
    return templates.TemplateResponse(request, "logs.html",
        {"user": user, "service": service, "logs": logs})


@router.get("/api/logs/{service}")
async def api_logs(service: str, lines: int = 200,
                   user: User = Depends(get_current_user_from_cookie)):
    if service not in VALID_SERVICES:
        raise HTTPException(404)
    lines = max(20, min(lines, 1000))
    return JSONResponse({"ok": True, "logs": get_container_logs(user.username, service, lines)})
