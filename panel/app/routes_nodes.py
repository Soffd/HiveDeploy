import os
import json
import secrets
import logging
import urllib.request

import psutil
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .bootstrap import templates, HOST
from .database import get_db
from .models import User, Instance, ServerNode, SiteConfig, InviteCode
from .auth import get_current_user_from_cookie
from .docker_manager import get_all_instances_status, calc_ports

logger = logging.getLogger(__name__)
router = APIRouter()

from .routes_admin import require_admin


# ════════════════════════════════════════════════════════════
#  节点管理 + 总览
# ════════════════════════════════════════════════════════════
@router.get("/admin/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    nodes     = db.query(ServerNode).order_by(ServerNode.id).all()
    api_token = db.query(SiteConfig).filter_by(key="api_token").first()
    local_url = f"https://{HOST}"
    return templates.TemplateResponse(request, "nodes.html", {"user": user, "nodes": nodes,
        "api_token": api_token.value if api_token else "",
        "local_url": local_url,
    })


@router.post("/admin/nodes")
async def node_add(name: str = Form(...), url: str = Form(...),
                   api_token: str = Form(...),
                   user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    db.add(ServerNode(name=name, url=url.rstrip("/"), api_token=api_token))
    db.commit()
    return RedirectResponse("/admin/nodes?added=1", 302)


@router.post("/admin/nodes/{node_id}/delete")
async def node_delete(node_id: int, user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    n = db.query(ServerNode).filter_by(id=node_id).first()
    if n: db.delete(n); db.commit()
    return RedirectResponse("/admin/nodes", 302)


@router.post("/admin/nodes/regen_token")
async def regen_token(user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if cfg:
        cfg.value = secrets.token_hex(32)
    else:
        db.add(SiteConfig(key="api_token", value=secrets.token_hex(32)))
    db.commit()
    return RedirectResponse("/admin/nodes?regen=1", 302)


@router.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "overview.html",
        {"user": user})


@router.get("/api/aggregate_nodes")
async def aggregate_nodes(user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    results = []

    # 本地
    local_users = db.query(User).count()
    local_inst  = db.query(Instance).count()
    all_stat    = get_all_instances_status()
    instances   = []
    for u in db.query(User).filter(User.instance != None).all():
        if u.instance:
            st = all_stat.get(u.username, {})
            ports = calc_ports(u.id)
            instances.append({
                "user_id":  u.id,
                "username": u.username,
                "bot_type": u.instance.bot_type or "napcat",
                "astrbot":  st.get("astrbot", "unknown"),
                "napcat":   st.get("napcat",  "unknown"),
                "llonebot": st.get("llonebot", "unknown"),
                "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None,
                "ports": {
                    "astrbot_web": ports["astrbot_web"],
                    "napcat_web":  ports["napcat_web"],
                },
            })
    mem  = psutil.virtual_memory()
    cpu  = psutil.cpu_percent(interval=0.3)
    max_users_cfg = db.query(SiteConfig).filter_by(key="max_users").first()
    local_max_users = int(max_users_cfg.value or "0") if max_users_cfg else 0
    reg_open_cfg = db.query(SiteConfig).filter_by(key="registration_open").first()
    local_reg_open = (reg_open_cfg.value if reg_open_cfg else "true") == "true"
    results.append({
        "name": os.environ.get("SITE_NAME", "本服务器"),
        "url":  f"https://{HOST}",
        "user_count": local_users,
        "max_users": local_max_users,
        "registration_open": local_reg_open,
        "instance_count": local_inst,
        "cpu_percent": cpu,
        "mem_percent": mem.percent,
        "instances": instances,
        "error": None,
    })

    # 远程
    nodes = db.query(ServerNode).all()
    for node in nodes:
        try:
            req = urllib.request.Request(
                f"{node.url}/api/v1/status",
                headers={"Authorization": f"Bearer {node.api_token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            results.append({
                "name":              node.name,
                "url":               node.url,
                "user_count":        data.get("user_count", 0),
                "max_users":         data.get("max_users", 0),
                "registration_open": data.get("registration_open", False),
                "instance_count":    data.get("instance_count", 0),
                "cpu_percent":       data.get("cpu_percent", 0),
                "mem_percent":       data.get("mem_percent", 0),
                "instances":         data.get("instances", []),
                "error":             None,
            })
        except Exception as e:
            results.append({
                "name": node.name, "url": node.url,
                "user_count": 0, "max_users": 0, "registration_open": False,
                "instance_count": 0,
                "cpu_percent": 0, "mem_percent": 0,
                "instances": [], "error": str(e),
            })

    return JSONResponse(results)


# ── 对外状态接口 ─────────────────────────────────────────────

@router.get("/api/v1/status")
async def api_v1_status(request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = auth[7:]
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if not cfg or cfg.value != token:
        raise HTTPException(401, "Invalid token")

    users    = db.query(User).count()
    inst_cnt = db.query(Instance).count()
    all_stat = get_all_instances_status()
    instances = []
    for u in db.query(User).all():
        if u.instance:
            st = all_stat.get(u.username, {})
            ports = calc_ports(u.id)
            instances.append({
                "user_id":  u.id,
                "username": u.username,
                "bot_type": u.instance.bot_type or "napcat",
                "astrbot":  st.get("astrbot", "unknown"),
                "napcat":   st.get("napcat",  "unknown"),
                "llonebot": st.get("llonebot", "unknown"),
                "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None,
                "ports": {
                    "astrbot_web": ports["astrbot_web"],
                    "napcat_web":  ports["napcat_web"],
                },
            })
    mem = psutil.virtual_memory()
    max_users_cfg = db.query(SiteConfig).filter_by(key="max_users").first()
    max_users = int(max_users_cfg.value or "0") if max_users_cfg else 0
    reg_open_cfg = db.query(SiteConfig).filter_by(key="registration_open").first()
    reg_open = reg_open_cfg.value if reg_open_cfg else "true"
    hub_url_cfg = db.query(SiteConfig).filter_by(key="hub_url").first()
    active_codes = db.query(InviteCode).filter_by(used=False, hidden=False).count()
    return JSONResponse({
        "server_name":        os.environ.get("SITE_NAME", HOST),
        "user_count":         users,
        "max_users":          max_users,
        "registration_open":  reg_open == "true",
        "instance_count":     inst_cnt,
        "cpu_percent":        psutil.cpu_percent(interval=0.3),
        "mem_percent":        mem.percent,
        "mem_used":           mem.used,
        "mem_total":          mem.total,
        "instances":          instances,
        "invite_codes_active": active_codes,
        "hub_url":            hub_url_cfg.value if hub_url_cfg else "",
    })


@router.get("/status", response_class=HTMLResponse)
async def public_status_page(request: Request, token: str = "",
                              db: Session = Depends(get_db)):
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    valid = vcfg and token == vcfg.value
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    site_name = scfg.value if scfg else "HiveDeploy"
    return templates.TemplateResponse(request, "public_status.html", {
        "valid": valid,
        "token": token,
        "site_name": site_name,
    })


@router.get("/api/public/stats")
async def public_stats_api(token: str = "", db: Session = Depends(get_db)):
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    if not vcfg or token != vcfg.value:
        raise HTTPException(401, "Invalid token")

    results = []
    # 本地数据
    local_users = db.query(User).count()
    local_inst  = db.query(Instance).count()
    all_stat    = get_all_instances_status()
    running_ab  = sum(1 for s in all_stat.values() if s.get("astrbot") == "running")
    running_nc  = sum(1 for s in all_stat.values() if s.get("napcat") == "running")
    running_ll  = sum(1 for s in all_stat.values() if s.get("llonebot") == "running")
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    max_users_cfg2 = db.query(SiteConfig).filter_by(key="max_users").first()
    local_max_users2 = int(max_users_cfg2.value or "0") if max_users_cfg2 else 0
    reg_open_cfg2 = db.query(SiteConfig).filter_by(key="registration_open").first()
    local_reg_open2 = (reg_open_cfg2.value if reg_open_cfg2 else "true") == "true"
    results.append({
        "name":              scfg.value if scfg else os.environ.get("SITE_NAME", "本服务器"),
        "url":               f"https://{HOST}",
        "user_count":        local_users,
        "max_users":         local_max_users2,
        "registration_open": local_reg_open2,
        "instance_count":    local_inst,
        "running_astrbot":   running_ab,
        "running_napcat":    running_nc,
        "running_llonebot":  running_ll,
        "cpu_percent":       psutil.cpu_percent(interval=0.3),
        "mem_percent":       mem.percent,
        "mem_used":          mem.used,
        "mem_total":         mem.total,
        "disk_percent":      disk.percent,
        "disk_used":         disk.used,
        "disk_total":        disk.total,
        "error":             None,
    })

    # 远程节点
    nodes = db.query(ServerNode).all()
    for node in nodes:
        try:
            req = urllib.request.Request(
                f"{node.url}/api/v1/status",
                headers={"Authorization": f"Bearer {node.api_token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            results.append({
                "name":              node.name,
                "url":               node.url,
                "user_count":        data.get("user_count", 0),
                "max_users":         data.get("max_users", 0),
                "registration_open": data.get("registration_open", False),
                "instance_count":    data.get("instance_count", 0),
                "running_astrbot":   sum(1 for i in data.get("instances",[]) if i.get("astrbot")=="running"),
                "running_napcat":    sum(1 for i in data.get("instances",[]) if i.get("napcat")=="running"),
                "running_llonebot":  sum(1 for i in data.get("instances",[]) if i.get("llonebot")=="running"),
                "cpu_percent":       data.get("cpu_percent", 0),
                "mem_percent":       data.get("mem_percent", 0),
                "mem_used":          0, "mem_total":  0,
                "disk_percent":      0, "disk_used":  0, "disk_total": 0,
                "error":             None,
            })
        except Exception as e:
            results.append({
                "name": node.name, "url": node.url,
                "user_count": 0, "max_users": 0, "registration_open": False,
                "instance_count": 0,
                "running_astrbot": 0, "running_napcat": 0, "running_llonebot": 0,
                "cpu_percent": 0, "mem_percent": 0,
                "mem_used": 0, "mem_total": 0,
                "disk_percent": 0, "disk_used": 0, "disk_total": 0,
                "error": str(e),
            })

    total_users = sum(n["user_count"] for n in results)
    total_inst  = sum(n["instance_count"] for n in results)
    total_ab    = sum(n["running_astrbot"] for n in results)
    total_nc    = sum(n["running_napcat"] for n in results)
    total_ll    = sum(n["running_llonebot"] for n in results)

    return JSONResponse({
        "nodes":       results,
        "total_users": total_users,
        "total_inst":  total_inst,
        "total_ab":    total_ab,
        "total_nc":    total_nc,
        "total_ll":    total_ll,
    })


# 管理员设置公开总览
@router.get("/admin/public_status", response_class=HTMLResponse)
async def admin_public_status_page(request: Request, user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    return templates.TemplateResponse(request, "admin_public_status.html", {"user": user,
        "view_token": vcfg.value if vcfg else "",
        "site_name": scfg.value if scfg else "HiveDeploy",
        "host": HOST,
    })


@router.post("/admin/public_status")
async def admin_public_status_save(
    site_name: str = Form("HiveDeploy"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    for key, val in [("site_name", site_name)]:
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        if cfg: cfg.value = val
        else: db.add(SiteConfig(key=key, value=val))
    db.commit()
    return RedirectResponse("/admin/public_status?saved=1", 302)


@router.post("/admin/public_status/regen_token")
async def regen_view_token(user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    cfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    new_token = secrets.token_hex(16)
    if cfg: cfg.value = new_token
    else: db.add(SiteConfig(key="public_view_token", value=new_token))
    db.commit()
    return RedirectResponse("/admin/public_status?regen=1", 302)
