import json
import asyncio
import logging
from calendar import monthrange
from datetime import datetime, timedelta

import psutil
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .bootstrap import templates, HOST
from .database import get_db
from .models import User, Instance, PaymentConfig, RenewalRecord
from .auth import get_current_user_from_cookie, verify_password, get_password_hash
from .docker_manager import (
    get_instance_status, calc_extra_ports, detect_public_ip,
    start_user_instance,
)
from .models import VerificationCode
from .email_service import send_renewal_notification

logger = logging.getLogger(__name__)
router = APIRouter()


def add_calendar_month(dt: datetime) -> datetime:
    """Add one natural month and clamp to the last day of the target month."""
    year = dt.year + (1 if dt.month == 12 else 0)
    month = 1 if dt.month == 12 else dt.month + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# ════════════════════════════════════════════════════════════
#  Dashboard
# ════════════════════════════════════════════════════════════
@router.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(get_current_user_from_cookie),
                    db: Session = Depends(get_db)):
    instance = db.query(Instance).filter_by(user_id=user.id).first()
    container_status = get_instance_status(user.username) if instance else {}
    bot_type = instance.bot_type if instance else "napcat"
    extra_ports = []
    all_ports = []
    if instance and instance.extra_ports_json:
        try:
            all_ports = json.loads(instance.extra_ports_json)
            extra_ports = [ep for ep in all_ports if ep.get("service") in ("astrbot", bot_type)]
        except Exception:
            pass
    available_extra = calc_extra_ports(user.id) if instance else []

    # 计算弹性端口覆盖后的有效端口（用于连接配置展示）
    effective_astrbot_ws_port = instance.astrbot_ws_port if instance else 0
    effective_astrbot_web_port = instance.astrbot_port if instance else 0
    effective_bot_web_port = instance.napcat_web_port if instance else 0
    for ep in all_ports:
        svc = ep.get("service", "")
        cp = ep.get("container_port")
        if svc == "astrbot":
            if cp == 6199:
                effective_astrbot_ws_port = ep["host_port"]
            elif cp == 6185:
                effective_astrbot_web_port = ep["host_port"]
        elif svc == "napcat" and cp == 6099:
            effective_bot_web_port = ep["host_port"]
        elif svc == "llonebot" and cp == 3080:
            effective_bot_web_port = ep["host_port"]

    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "instance": instance,
        "container_status": container_status, "host": HOST,
        "extra_ports": extra_ports, "available_extra_ports": available_extra,
        "public_ip": detect_public_ip(),
        "effective_astrbot_ws_port": effective_astrbot_ws_port,
        "effective_astrbot_web_port": effective_astrbot_web_port,
        "effective_bot_web_port": effective_bot_web_port,
    })


# ════════════════════════════════════════════════════════════
#  Profile
# ════════════════════════════════════════════════════════════
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: User = Depends(get_current_user_from_cookie)):
    return templates.TemplateResponse(request, "profile.html",
        {"user": user, "errors": []})


@router.post("/profile/password")
async def change_password(request: Request,
                          old_password: str = Form(...),
                          new_password: str = Form(...),
                          new_password2: str = Form(...),
                          code: str = Form(""),
                          user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    errors = []
    if not verify_password(old_password, user.hashed_password):
        errors.append("当前密码错误")
    if new_password != new_password2:
        errors.append("两次新密码不一致")
    if len(new_password) < 6:
        errors.append("新密码至少 6 位")

    if not code.strip():
        errors.append("请输入邮箱验证码")
    else:
        vc = db.query(VerificationCode).filter(
            VerificationCode.email == user.email,
            VerificationCode.purpose == "change_password",
            VerificationCode.used == False,
        ).order_by(VerificationCode.created_at.desc()).first()
        if not vc:
            errors.append("请先获取邮箱验证码（点击发送验证码按钮）")
        elif vc.code != code.strip():
            errors.append("验证码错误")
        elif vc.expires_at < datetime.now():
            errors.append("验证码已过期，请重新获取")

    if errors:
        return templates.TemplateResponse(request, "profile.html",
            {"user": user, "errors": errors})

    vc.used = True
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    resp = RedirectResponse("/login?pw_changed=1", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ════════════════════════════════════════════════════════════
#  实例统计
# ════════════════════════════════════════════════════════════
@router.get("/api/instance/stats")
async def instance_stats(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    from .docker_manager import get_container_stats, get_data_dir_size

    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst:
        return JSONResponse({"error": "无实例"})

    bot_type = inst.bot_type or "napcat"

    loop = asyncio.get_event_loop()
    ab_stats, bt_stats = await asyncio.gather(
        loop.run_in_executor(None, get_container_stats, user.username, "astrbot"),
        loop.run_in_executor(None, get_container_stats, user.username, bot_type),
    )
    ab_size, bt_size = await asyncio.gather(
        loop.run_in_executor(None, get_data_dir_size, user.username, "astrbot"),
        loop.run_in_executor(None, get_data_dir_size, user.username, bot_type),
    )
    ab_stats["data_size"] = ab_size
    bt_stats["data_size"] = bt_size

    result = {"astrbot": ab_stats}
    if bot_type == "llonebot":
        result["llonebot"] = bt_stats
        result["napcat"] = {"error": "未部署"}
    else:
        result["napcat"] = bt_stats
        result["llonebot"] = {"error": "未部署"}
    return JSONResponse(result)


@router.get("/api/server/stats")
async def server_stats(user: User = Depends(get_current_user_from_cookie)):
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return JSONResponse({
        "cpu_percent":  psutil.cpu_percent(interval=0.5),
        "mem_total":    mem.total,
        "mem_used":     mem.used,
        "mem_percent":  mem.percent,
        "disk_total":   disk.total,
        "disk_used":    disk.used,
        "disk_percent": disk.percent,
    })


# ════════════════════════════════════════════════════════════
#  用户自助续期
# ════════════════════════════════════════════════════════════
@router.get("/renew", response_class=HTMLResponse)
async def renew_page(request: Request, user: User = Depends(get_current_user_from_cookie),
                      db: Session = Depends(get_db)):
    cfg = db.query(PaymentConfig).filter_by(id=1).first()
    if not cfg:
        cfg = PaymentConfig(id=1)
    return templates.TemplateResponse(request, "renew.html",
        {"user": user, "cfg": cfg, "host": HOST, "now": datetime.now()})


@router.post("/api/renew")
async def do_renew(request: Request,
                    user: User = Depends(get_current_user_from_cookie),
                    db: Session = Depends(get_db)):
    cfg = db.query(PaymentConfig).filter_by(id=1).first()
    if not cfg or not cfg.renewal_enabled:
        return JSONResponse({"ok": False, "error": "自助续期功能暂未开放"}, status_code=400)

    body = await request.json()
    days = body.get("days", 0)
    try:
        days = int(days)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "无效的天数"}, status_code=400)

    if days < 1 or days > 30:
        return JSONResponse({"ok": False, "error": "单次续期天数必须在 1~30 天之间"}, status_code=400)

    if user.expire_at is None:
        return JSONResponse({"ok": False, "error": "您已拥有永久时长，无需续期"}, status_code=400)

    now = datetime.now()
    previous_expire = user.expire_at
    if user.expire_at and user.expire_at > now:
        base = user.expire_at
    else:
        base = now
    if days == 30:
        new_expire = add_calendar_month(base)
        duration_label = "1个月"
    else:
        new_expire = base + timedelta(days=days)
        duration_label = f"{days}天"

    user.expire_at = new_expire

    record = RenewalRecord(
        user_id=user.id,
        username=user.username,
        days_added=days,
        previous_expire_at=previous_expire,
        new_expire_at=new_expire,
    )
    db.add(record)
    db.commit()

    # 发送续期通知给管理员配置的邮箱列表
    try:
        send_renewal_notification(db, user.username, days, previous_expire, new_expire, now)
    except Exception as e:
        logger.error(f"发送续期通知失败: {e}")

    if previous_expire and previous_expire < now:
        try:
            start_user_instance(user.username)
            logger.info(f"自助续期后重启容器: {user.username}")
        except Exception as e:
            logger.error(f"自助续期重启容器失败 {user.username}: {e}")

    return JSONResponse({
        "ok": True,
        "new_expire_at": new_expire.strftime("%Y-%m-%d %H:%M"),
        "days_added": days,
        "duration_label": duration_label,
    })
