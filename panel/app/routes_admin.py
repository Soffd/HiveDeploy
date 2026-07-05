import os
import json
import uuid
import secrets
import logging
import re
from html import escape
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from .bootstrap import templates, HOST, UPLOAD_DIR
from .database import get_db
from .models import (
    User, Instance, SmtpConfig, SiteConfig, PaymentConfig,
    RenewalRecord, InviteCode, BannedUser, EmailTemplate, Announcement,
    UserMessage,
)
from .auth import (
    get_password_hash, get_current_user_from_cookie,
)
from .docker_manager import (
    stop_user_instance, start_user_instance, restart_user_instance,
    delete_user_instance, get_all_instances_status, update_user_memory_limits,
)
from .email_service import send_email, DEFAULT_EMAIL_TEMPLATES, EMAIL_TEMPLATE_VARIABLES
from .hub_sync import _push_ban_to_hub, _push_unban_to_hub

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_ACCOUNT_DAYS = 36500


def require_admin(user: User = Depends(get_current_user_from_cookie)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="权限不足")
    return user


def _clean_announcement_style(font_size: str, color: str) -> tuple[int, str]:
    try:
        size = int(font_size)
    except (TypeError, ValueError):
        size = 15
    size = max(12, min(size, 24))
    color = (color or "").strip()
    if color and not color.startswith("#"):
        color = ""
    if color and len(color) not in (4, 7):
        color = ""
    return size, color


def _tool_access_value(db: Session, key: str, default: str = "limited") -> str:
    cfg = db.query(SiteConfig).filter_by(key=key).first()
    value = (cfg.value if cfg else default or "limited").strip().lower()
    if value not in ("free", "limited", "vip"):
        value = default
    return value


def _normalize_tool_access(value: Optional[str], default: str = "limited") -> str:
    value = (value or default).strip().lower()
    return value if value in ("free", "limited", "vip") else default


def _site_value(db: Session, key: str, default: str = "") -> str:
    cfg = db.query(SiteConfig).filter_by(key=key).first()
    return cfg.value if cfg and cfg.value is not None else default


def _bounded_int(value: str, default: int, min_value: int = 0, max_value: int = 3650) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


def _memory_mb_value(value: Optional[str], default: Optional[int] = None) -> Optional[int]:
    text_value = "" if value is None else str(value).strip()
    if text_value == "":
        return default
    try:
        mb = int(text_value)
    except (TypeError, ValueError):
        mb = default if default is not None else 0
    return max(0, min(mb or 0, 262144))


def _parse_optional_days(value: Optional[str], field_label: str, max_days: int = MAX_ACCOUNT_DAYS) -> Optional[int]:
    text_value = (value or "").strip()
    if not text_value:
        return None
    if not re.fullmatch(r"\d+", text_value):
        raise ValueError(f"{field_label}必须是正整数")
    days = int(text_value)
    if days <= 0:
        raise ValueError(f"{field_label}必须大于 0")
    if days > max_days:
        raise ValueError(f"{field_label}不能超过 {max_days} 天，如需长期保留请使用留存账号")
    return days


def _save_site_value(db: Session, key: str, value: str):
    cfg = db.query(SiteConfig).filter_by(key=key).first()
    if cfg:
        cfg.value = value
    else:
        db.add(SiteConfig(key=key, value=value))


def _send_user_message_email(db: Session, recipient: User, title: str, content: str, msg_type: str) -> bool:
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg or not cfg.enabled or not recipient.email:
        return False
    type_label = {"notice": "通知", "warning": "警告", "tip": "提示"}.get(msg_type, "通知")
    safe_title = escape(title.strip() or type_label)
    safe_content = escape(content or "")
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.7;color:#1f2937;padding:24px;">
      <div style="max-width:560px;margin:0 auto;border:1px solid #e5e7eb;border-radius:12px;padding:24px;background:#ffffff;">
        <div style="font-weight:700;color:#2563eb;font-size:20px;margin-bottom:12px;">HiveDeploy</div>
        <div style="display:inline-block;background:#eff6ff;color:#1d4ed8;border-radius:999px;padding:4px 10px;font-size:12px;margin-bottom:12px;">{type_label}</div>
        <h2 style="font-size:18px;margin:0 0 14px;">{safe_title}</h2>
        <div style="white-space:pre-wrap;color:#374151;">{safe_content}</div>
        <p style="margin-top:22px;color:#9ca3af;font-size:12px;">此邮件是站内信副本，请登录面板查看完整通知。</p>
      </div>
    </div>
    """
    return send_email(recipient.email, f"[HiveDeploy] {title.strip() or type_label}", html, cfg)


# ════════════════════════════════════════════════════════════
#  管理后台
# ════════════════════════════════════════════════════════════
@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    users    = db.query(User).order_by(User.id).all()
    all_stat = get_all_instances_status()
    instance_data = []
    for u in users:
        if u.instance:
            instance_data.append({
                "username": u.username,
                "instance": u.instance,
                "status":   all_stat.get(u.username, {}),
            })
    astrbot_running = sum(1 for d in instance_data if d["status"].get("astrbot") == "running")
    napcat_running  = sum(1 for d in instance_data if d["status"].get("napcat") == "running")
    llonebot_running = sum(1 for d in instance_data if d["status"].get("llonebot") == "running")
    max_users_cfg = db.query(SiteConfig).filter_by(key="max_users").first()
    reg_open_cfg = db.query(SiteConfig).filter_by(key="registration_open").first()
    allowed_domains_cfg = db.query(SiteConfig).filter_by(key="allowed_email_domains").first()
    hub_url_cfg = db.query(SiteConfig).filter_by(key="hub_url").first()
    require_invite_cfg = db.query(SiteConfig).filter_by(key="require_invite_code").first()
    invite_min_days_cfg = db.query(SiteConfig).filter_by(key="invite_min_days").first()
    invite_monthly_limit_cfg = db.query(SiteConfig).filter_by(key="invite_monthly_limit").first()
    invite_active_limit_cfg = db.query(SiteConfig).filter_by(key="invite_active_limit").first()
    invite_code_bytes_cfg = db.query(SiteConfig).filter_by(key="invite_code_bytes").first()
    auto_delete_expired_days_cfg = db.query(SiteConfig).filter_by(key="auto_delete_expired_days").first()
    default_astrbot_memory_cfg = db.query(SiteConfig).filter_by(key="default_astrbot_memory_mb").first()
    default_bot_memory_cfg = db.query(SiteConfig).filter_by(key="default_bot_memory_mb").first()
    max_users = int(max_users_cfg.value or "0") if max_users_cfg else 0
    reg_open = reg_open_cfg.value if reg_open_cfg else "true"
    allowed_domains = allowed_domains_cfg.value if allowed_domains_cfg else ""
    hub_url = hub_url_cfg.value if hub_url_cfg else ""
    require_invite = require_invite_cfg.value if require_invite_cfg else "true"
    invite_min_days = invite_min_days_cfg.value if invite_min_days_cfg else "90"
    invite_monthly_limit = invite_monthly_limit_cfg.value if invite_monthly_limit_cfg else "5"
    invite_active_limit = invite_active_limit_cfg.value if invite_active_limit_cfg else "10"
    invite_code_bytes = invite_code_bytes_cfg.value if invite_code_bytes_cfg else "8"
    auto_delete_expired_days = auto_delete_expired_days_cfg.value if auto_delete_expired_days_cfg else "7"
    default_astrbot_memory_mb = _memory_mb_value(default_astrbot_memory_cfg.value if default_astrbot_memory_cfg else None, 1024)
    default_bot_memory_mb = _memory_mb_value(default_bot_memory_cfg.value if default_bot_memory_cfg else None, 500)
    current_users = db.query(User).count()
    invite_codes = db.query(InviteCode).filter_by(hidden=False).order_by(InviteCode.created_at.desc()).limit(200).all()
    total_active = db.query(InviteCode).filter_by(used=False, hidden=False).count()

    creator_stats = db.query(
        InviteCode.creator_id,
        func.count(InviteCode.id).label("total"),
        func.sum(InviteCode.used).label("used_count"),
    ).filter(InviteCode.hidden == False).group_by(InviteCode.creator_id).all()
    invite_stats = []
    for cs in creator_stats:
        u = db.query(User).filter_by(id=cs.creator_id).first()
        total = cs.total
        used = cs.used_count or 0
        rate = f"{used / total * 100:.0f}%" if total > 0 else "0%"
        invite_stats.append({
            "username": u.username if u else ("节点-" + str(cs.creator_id)),
            "total": total,
            "used_count": used,
            "rate": rate,
        })
    invite_stats.sort(key=lambda x: x["used_count"], reverse=True)

    return templates.TemplateResponse(request, "admin.html", {"user": user,
        "users": users, "instance_data": instance_data, "host": HOST,
        "astrbot_running": astrbot_running, "napcat_running": napcat_running,
        "llonebot_running": llonebot_running,
        "max_users": max_users, "current_users": current_users, "reg_open": reg_open,
        "allowed_domains": allowed_domains, "hub_url": hub_url,
        "require_invite_code": require_invite,
        "invite_min_days": invite_min_days,
        "invite_monthly_limit": invite_monthly_limit,
        "invite_active_limit": invite_active_limit,
        "invite_code_bytes": invite_code_bytes,
        "auto_delete_expired_days": auto_delete_expired_days,
        "default_astrbot_memory_mb": default_astrbot_memory_mb,
        "default_bot_memory_mb": default_bot_memory_mb,
        "max_account_days": MAX_ACCOUNT_DAYS,
        "invite_codes": invite_codes, "total_active_codes": total_active,
        "invite_stats": invite_stats,
    })


@router.post("/admin/user/create")
async def admin_create_user(
    username: str = Form(...), email: str = Form(...),
    password: str = Form(...),
    is_admin: Optional[str] = Form(None),
    expire_days: Optional[str] = Form(None),
    vip_days: Optional[str] = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(User).filter_by(username=username).first():
        return RedirectResponse("/admin?error=用户名已存在", 302)
    _is_admin = is_admin in ("on", "true", "1", "yes")
    try:
        _expire_days = _parse_optional_days(expire_days, "有效天数")
        _vip_days = _parse_optional_days(vip_days, "VIP天数")
    except ValueError as exc:
        return RedirectResponse(f"/admin?error={quote(str(exc))}", 302)
    expire_at = datetime.now() + timedelta(days=_expire_days) if _expire_days else None
    vip_expire_at = datetime.now() + timedelta(days=_vip_days) if _vip_days else None
    db.add(User(
        username=username, email=email,
        hashed_password=get_password_hash(password),
        is_admin=_is_admin, expire_at=expire_at, vip_expire_at=vip_expire_at,
    ))
    db.commit()
    return RedirectResponse("/admin?created=1", 302)


@router.post("/admin/user/{user_id}/toggle")
async def admin_toggle_user(user_id: int, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        u.is_active = not u.is_active
        if not u.is_active:
            if not db.query(BannedUser).filter_by(username=u.username).first():
                db.add(BannedUser(username=u.username, email=u.email, source_node="local"))
            db.commit()
            _push_ban_to_hub(db, u.username, u.email)
        else:
            bu = db.query(BannedUser).filter_by(username=u.username).first()
            if bu:
                db.delete(bu)
            db.commit()
            _push_unban_to_hub(db, u.username, u.email)
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{user_id}/reset_password")
async def admin_reset_password(user_id: int, user: User = Depends(require_admin),
                               db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u:
        u.hashed_password = get_password_hash("123456"); db.commit()
    return RedirectResponse("/admin?reset=1", 302)


@router.post("/admin/user/{user_id}/toggle_retained")
async def admin_toggle_retained_user(user_id: int, user: User = Depends(require_admin),
                                     db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        u.retained_account = not bool(getattr(u, "retained_account", False))
        db.commit()
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{user_id}/delete")
async def admin_delete_user(user_id: int, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        delete_user_instance(u.username)
        if u.instance: db.delete(u.instance)
        db.query(UserMessage).filter_by(user_id=u.id).delete(synchronize_session=False)
        bu = db.query(BannedUser).filter_by(username=u.username).first()
        if bu:
            db.delete(bu)
            _push_unban_to_hub(db, u.username, u.email)
        db.delete(u); db.commit()
    return RedirectResponse("/admin", 302)


@router.post("/admin/user/{user_id}/set_expire")
async def admin_set_expire(user_id: int, request: Request,
                           user: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    body = await request.json()
    u = db.query(User).filter_by(id=user_id).first()
    if not u: raise HTTPException(404)

    was_expired = u.expire_at is not None and u.expire_at < datetime.now()

    action = body.get("action")
    if action == "clear":
        u.expire_at = None
    elif action == "set":
        date_str = body.get("date")
        u.expire_at = datetime.strptime(date_str + " 23:59:59", "%Y-%m-%d %H:%M:%S") if date_str else None
    elif action in ("add30", "add90", "add365"):
        days = int(action[3:])
        if u.expire_at is None:
            return JSONResponse({"ok": False, "error": "该用户为永久时长，无需增加天数"}, status_code=400)
        base = max(u.expire_at, datetime.now())
        u.expire_at = base + timedelta(days=days)
    db.commit()

    now_expired = u.expire_at is not None and u.expire_at < datetime.now()
    if was_expired and not now_expired:
        try:
            start_user_instance(u.username)
            logger.info(f"续期后重启容器: {u.username}")
        except Exception as e:
            logger.error(f"续期重启容器失败 {u.username}: {e}")
    elif not was_expired and now_expired:
        try:
            stop_user_instance(u.username)
            logger.info(f"设置到期后停止容器: {u.username}")
        except Exception as e:
            logger.error(f"设置到期停止容器失败 {u.username}: {e}")

    return JSONResponse({"ok": True, "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None})


@router.post("/admin/user/{user_id}/set_vip")
async def admin_set_vip(user_id: int, request: Request,
                        user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    body = await request.json()
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        raise HTTPException(404)

    action = body.get("action")
    if action == "clear":
        u.vip_expire_at = None
    elif action == "set":
        date_str = body.get("date")
        u.vip_expire_at = datetime.strptime(date_str + " 23:59:59", "%Y-%m-%d %H:%M:%S") if date_str else None
    elif action in ("add30", "add90", "add365"):
        days = int(action[3:])
        base = max(u.vip_expire_at or datetime.now(), datetime.now())
        u.vip_expire_at = base + timedelta(days=days)
    else:
        return JSONResponse({"ok": False, "error": "未知操作"}, status_code=400)

    db.commit()
    return JSONResponse({"ok": True, "vip_expire_at": u.vip_expire_at.strftime("%Y-%m-%d") if u.vip_expire_at else None})


@router.post("/admin/user/{user_id}/set_memory_limits")
async def admin_set_memory_limits(user_id: int, request: Request,
                                  user: User = Depends(require_admin),
                                  db: Session = Depends(get_db)):
    body = await request.json()
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        raise HTTPException(404)
    u.astrbot_memory_limit_mb = _memory_mb_value(body.get("astrbot_memory_limit_mb"), None)
    u.bot_memory_limit_mb = _memory_mb_value(body.get("bot_memory_limit_mb"), None)
    db.commit()
    update_result = {}
    try:
        update_result = update_user_memory_limits(u.username, u.id)
    except Exception as e:
        logger.warning(f"更新运行中容器内存限制失败 {u.username}: {e}")
        update_result = {"error": str(e)}
    return JSONResponse({
        "ok": True,
        "astrbot_memory_limit_mb": u.astrbot_memory_limit_mb,
        "bot_memory_limit_mb": u.bot_memory_limit_mb,
        "update_result": update_result,
    })


@router.post("/admin/tools/settings")
async def admin_save_tool_settings(
    reset_astrbot_password_access: str = Form("limited"),
    auto_config_access: str = Form("limited"),
    reset_astrbot_password_badge: str = Form(""),
    auto_config_badge: str = Form("Beta"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    values = {
        "quick_tool_reset_astrbot_password_access": _normalize_tool_access(reset_astrbot_password_access),
        "quick_tool_auto_config_access": _normalize_tool_access(auto_config_access),
        "quick_tool_reset_astrbot_password_badge": reset_astrbot_password_badge.strip()[:24],
        "quick_tool_auto_config_badge": auto_config_badge.strip()[:24],
        "quick_tool_reset_astrbot_password_vip_only": "true" if reset_astrbot_password_access == "vip" else "false",
    }
    for key, value in values.items():
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        if cfg:
            cfg.value = value
        else:
            db.add(SiteConfig(key=key, value=value))
    db.commit()
    return RedirectResponse("/admin/tools?saved=1", 302)


@router.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools_page(request: Request, user: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    reset_tool_vip_cfg = db.query(SiteConfig).filter_by(key="quick_tool_reset_astrbot_password_vip_only").first()
    reset_access = _tool_access_value(db, "quick_tool_reset_astrbot_password_access", "limited")
    if reset_tool_vip_cfg and reset_tool_vip_cfg.value == "true":
        reset_access = "vip"
    return templates.TemplateResponse(request, "admin_tools.html", {
        "user": user,
        "reset_astrbot_password_access": reset_access,
        "auto_config_access": _tool_access_value(db, "quick_tool_auto_config_access", "limited"),
        "reset_astrbot_password_badge": _site_value(db, "quick_tool_reset_astrbot_password_badge", ""),
        "auto_config_badge": _site_value(db, "quick_tool_auto_config_badge", "Beta"),
    })


@router.post("/admin/run_expiry_check")
async def admin_run_expiry_check(user: User = Depends(require_admin),
                                  db: Session = Depends(get_db)):
    from .email_service import check_and_enforce_expiry
    now = datetime.now()
    users = db.query(User).filter(User.expire_at != None).all()
    report = []
    for u in users:
        delta = u.expire_at - now
        report.append({
            "username": u.username,
            "expire_at": u.expire_at.strftime("%Y-%m-%d %H:%M"),
            "days_left": delta.days,
            "expired": delta.days < 0,
        })
    check_and_enforce_expiry(db)
    return JSONResponse({"users": report, "check_triggered": True})


@router.post("/admin/settings")
async def admin_save_settings(
    max_users: str = Form("0"),
    registration_open: str = Form("false"),
    allowed_email_domains: str = Form(""),
    hub_url: str = Form(""),
    require_invite_code: str = Form("false"),
    invite_min_days: str = Form("90"),
    invite_monthly_limit: str = Form("5"),
    invite_active_limit: str = Form("10"),
    invite_code_bytes: str = Form("8"),
    auto_delete_expired_days: str = Form("7"),
    default_astrbot_memory_mb: str = Form("1024"),
    default_bot_memory_mb: str = Form("500"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    for key, val in [("max_users", max_users), ("registration_open", registration_open),
                     ("allowed_email_domains", allowed_email_domains.strip()),
                     ("hub_url", hub_url.strip().rstrip("/")),
                     ("require_invite_code", require_invite_code),
                     ("invite_min_days", invite_min_days),
                     ("invite_monthly_limit", invite_monthly_limit),
                     ("invite_active_limit", invite_active_limit),
                     ("invite_code_bytes", invite_code_bytes),
                     ("auto_delete_expired_days", str(_bounded_int(auto_delete_expired_days, 7))),
                     ("default_astrbot_memory_mb", str(_memory_mb_value(default_astrbot_memory_mb, 1024))),
                     ("default_bot_memory_mb", str(_memory_mb_value(default_bot_memory_mb, 500)))]:
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        if cfg:
            cfg.value = val
        else:
            db.add(SiteConfig(key=key, value=val))
    db.commit()
    return RedirectResponse("/admin?settings_saved=1", 302)


# ════════════════════════════════════════════════════════════
#  站内信管理
# ════════════════════════════════════════════════════════════
@router.get("/admin/messages", response_class=HTMLResponse)
async def admin_messages_page(request: Request, user: User = Depends(require_admin),
                              db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.username).all()
    messages = db.query(UserMessage).order_by(UserMessage.created_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "messages.html", {
        "user": user,
        "users": users,
        "messages": messages,
        "message_email_copy_default": _site_value(db, "message_email_copy_default", "false"),
        "smtp_enabled": bool(db.query(SmtpConfig).filter_by(id=1, enabled=True).first()),
    })


@router.post("/admin/messages/settings")
async def admin_messages_settings(message_email_copy_default: str = Form("false"),
                                  user: User = Depends(require_admin),
                                  db: Session = Depends(get_db)):
    _save_site_value(db, "message_email_copy_default", "true" if message_email_copy_default == "true" else "false")
    db.commit()
    return RedirectResponse("/admin/messages?saved=1", 302)


@router.post("/admin/messages/create")
async def admin_message_create(
    user_id: int = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    type: str = Form("notice"),
    send_email_copy: Optional[str] = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    recipient = db.query(User).filter_by(id=user_id).first()
    if not recipient:
        raise HTTPException(404, "用户不存在")
    msg_type = type if type in ("notice", "warning", "tip") else "notice"
    email_sent = False
    if send_email_copy == "true":
        email_sent = _send_user_message_email(db, recipient, title, content, msg_type)
    db.add(UserMessage(
        user_id=recipient.id,
        title=title.strip()[:128] or "站内信",
        content=content.strip(),
        type=msg_type,
        email_sent=email_sent,
    ))
    db.commit()
    return RedirectResponse("/admin/messages?saved=1", 302)


@router.post("/admin/messages/{message_id}/delete")
async def admin_message_delete(message_id: int, user: User = Depends(require_admin),
                               db: Session = Depends(get_db)):
    msg = db.query(UserMessage).filter_by(id=message_id).first()
    if msg:
        db.delete(msg)
        db.commit()
    return RedirectResponse("/admin/messages?deleted=1", 302)


# ════════════════════════════════════════════════════════════
#  公告管理
# ════════════════════════════════════════════════════════════
@router.get("/admin/announcements", response_class=HTMLResponse)
async def admin_announcements_page(request: Request, user: User = Depends(require_admin),
                                   db: Session = Depends(get_db)):
    announcements = db.query(Announcement).order_by(
        Announcement.pinned.desc(),
        Announcement.updated_at.desc(),
        Announcement.id.desc(),
    ).all()
    return templates.TemplateResponse(request, "announcements.html", {
        "user": user,
        "announcements": announcements,
    })


@router.post("/admin/announcements/create")
async def admin_announcement_create(
    title: str = Form(...),
    content: str = Form(...),
    type: str = Form("info"),
    level: str = Form("normal"),
    enabled: Optional[str] = Form(None),
    pinned: Optional[str] = Form(None),
    bold: Optional[str] = Form(None),
    font_size: str = Form("15"),
    color: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if type not in ("info", "price", "migration", "warning", "ban"):
        type = "info"
    if level not in ("normal", "required"):
        level = "normal"
    size, color = _clean_announcement_style(font_size, color)
    db.add(Announcement(
        title=title.strip() or "系统公告",
        content=content.strip(),
        type=type,
        level=level,
        enabled=enabled == "true",
        pinned=pinned == "true",
        bold=bold == "true",
        font_size=size,
        color=color,
    ))
    db.commit()
    return RedirectResponse("/admin/announcements?saved=1", 302)


@router.post("/admin/announcements/{announcement_id}/update")
async def admin_announcement_update(
    announcement_id: int,
    title: str = Form(...),
    content: str = Form(...),
    type: str = Form("info"),
    level: str = Form("normal"),
    enabled: Optional[str] = Form(None),
    pinned: Optional[str] = Form(None),
    bold: Optional[str] = Form(None),
    font_size: str = Form("15"),
    color: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    item = db.query(Announcement).filter_by(id=announcement_id).first()
    if not item:
        raise HTTPException(404)
    if type not in ("info", "price", "migration", "warning", "ban"):
        type = "info"
    if level not in ("normal", "required"):
        level = "normal"
    size, color = _clean_announcement_style(font_size, color)
    item.title = title.strip() or "系统公告"
    item.content = content.strip()
    item.type = type
    item.level = level
    item.enabled = enabled == "true"
    item.pinned = pinned == "true"
    item.bold = bold == "true"
    item.font_size = size
    item.color = color
    item.updated_at = datetime.now()
    db.commit()
    return RedirectResponse("/admin/announcements?saved=1", 302)


@router.post("/admin/announcements/{announcement_id}/delete")
async def admin_announcement_delete(announcement_id: int, user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    item = db.query(Announcement).filter_by(id=announcement_id).first()
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse("/admin/announcements?deleted=1", 302)


@router.post("/admin/instance/{user_id}/restart")
async def admin_restart_instance(user_id: int, user: User = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and u.instance: restart_user_instance(u.username)
    return RedirectResponse("/admin", 302)


# ════════════════════════════════════════════════════════════
#  SMTP 设置
# ════════════════════════════════════════════════════════════
@router.get("/admin/smtp", response_class=HTMLResponse)
async def smtp_page(request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg:
        cfg = SmtpConfig(id=1); db.add(cfg); db.commit()
    return templates.TemplateResponse(request, "smtp.html",
        {"user": user, "cfg": cfg})


@router.post("/admin/smtp")
async def smtp_save(
    host: str = Form(""), port: int = Form(465),
    username: str = Form(""), password: str = Form(""),
    from_email: str = Form(""), from_name: str = Form("HiveDeploy"),
    use_tls: bool = Form(False), enabled: bool = Form(False),
    renewal_notify_email: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg:
        cfg = SmtpConfig(id=1); db.add(cfg)
    cfg.host=host; cfg.port=port; cfg.username=username
    cfg.password=password; cfg.from_email=from_email
    cfg.from_name=from_name; cfg.use_tls=use_tls; cfg.enabled=enabled
    cfg.renewal_notify_email = renewal_notify_email.strip()
    db.commit()
    return RedirectResponse("/admin/smtp?saved=1", 302)


@router.post("/admin/smtp/test")
async def smtp_test(to_email: str = Form(...),
                    user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg or not cfg.enabled:
        return JSONResponse({"error": "SMTP 未启用"})
    ok = send_email(to_email, "HiveDeploy 测试邮件",
        "<h2>✅ SMTP 配置正常</h2><p>如果你收到这封邮件，说明 SMTP 配置已成功。</p>", cfg)
    return JSONResponse({"ok": ok, "error": "" if ok else "发送失败，请检查配置"})


@router.get("/admin/email-templates", response_class=HTMLResponse)
async def admin_email_templates_page(request: Request,
                                     user: User = Depends(require_admin),
                                     db: Session = Depends(get_db)):
    rows = {t.key: t for t in db.query(EmailTemplate).all()}
    templates_data = []
    for key, default in DEFAULT_EMAIL_TEMPLATES.items():
        row = rows.get(key)
        templates_data.append({
            "key": key,
            "name": row.name if row else default["name"],
            "subject": row.subject if row else default["subject"],
            "body_html": row.body_html if row else default["body_html"],
            "default_subject": default["subject"],
            "default_body_html": default["body_html"],
        })
    return templates.TemplateResponse(request, "email_templates.html", {
        "user": user,
        "templates": templates_data,
        "variables": EMAIL_TEMPLATE_VARIABLES,
    })


@router.post("/admin/email-templates/{template_key}")
async def admin_save_email_template(template_key: str,
                                    subject: str = Form(""),
                                    body_html: str = Form(""),
                                    user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    if template_key not in DEFAULT_EMAIL_TEMPLATES:
        raise HTTPException(404, "模板不存在")
    default = DEFAULT_EMAIL_TEMPLATES[template_key]
    row = db.query(EmailTemplate).filter_by(key=template_key).first()
    if row:
        row.subject = subject.strip() or default["subject"]
        row.body_html = body_html.strip() or default["body_html"]
        row.name = default["name"]
    else:
        db.add(EmailTemplate(
            key=template_key,
            name=default["name"],
            subject=subject.strip() or default["subject"],
            body_html=body_html.strip() or default["body_html"],
        ))
    db.commit()
    return RedirectResponse(f"/admin/email-templates?saved={template_key}", 302)


@router.post("/admin/email-templates/{template_key}/reset")
async def admin_reset_email_template(template_key: str,
                                     user: User = Depends(require_admin),
                                     db: Session = Depends(get_db)):
    if template_key not in DEFAULT_EMAIL_TEMPLATES:
        raise HTTPException(404, "模板不存在")
    default = DEFAULT_EMAIL_TEMPLATES[template_key]
    row = db.query(EmailTemplate).filter_by(key=template_key).first()
    if row:
        row.subject = default["subject"]
        row.body_html = default["body_html"]
        row.name = default["name"]
    else:
        db.add(EmailTemplate(
            key=template_key,
            name=default["name"],
            subject=default["subject"],
            body_html=default["body_html"],
        ))
    db.commit()
    return RedirectResponse(f"/admin/email-templates?reset={template_key}", 302)


# ════════════════════════════════════════════════════════════
#  文件上传 / 静态资源
# ════════════════════════════════════════════════════════════
@router.post("/admin/upload")
async def admin_upload_file(user: User = Depends(require_admin),
                      file: UploadFile = File(...)):
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
    if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        raise HTTPException(400, "仅支持图片格式")
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    data = await file.read()
    with open(file_path, "wb") as f:
        f.write(data)
    return JSONResponse({"ok": True, "filename": safe_name})


@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(404)
    return FileResponse(file_path)


# ════════════════════════════════════════════════════════════
#  支付配置（管理后台）
# ════════════════════════════════════════════════════════════
@router.get("/admin/payment", response_class=HTMLResponse)
async def admin_payment_page(request: Request, user: User = Depends(require_admin),
                              db: Session = Depends(get_db)):
    cfg = db.query(PaymentConfig).filter_by(id=1).first()
    if not cfg:
        cfg = PaymentConfig(id=1)
        db.add(cfg)
        db.commit()
    return templates.TemplateResponse(request, "payment_config.html",
        {"user": user, "cfg": cfg})


@router.post("/admin/payment")
async def admin_payment_save(
    price_text: str = Form(""),
    instructions: str = Form(""),
    social_qq: str = Form(""),
    social_wechat: str = Form(""),
    social_telegram: str = Form(""),
    social_discord: str = Form(""),
    renewal_enabled: bool = Form(False),
    wechat_qr_file: Optional[UploadFile] = File(None),
    alipay_qr_file: Optional[UploadFile] = File(None),
    clear_wechat_qr: str = Form(""),
    clear_alipay_qr: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = db.query(PaymentConfig).filter_by(id=1).first()
    if not cfg:
        cfg = PaymentConfig(id=1)
        db.add(cfg)

    cfg.price_text = price_text
    cfg.instructions = instructions
    cfg.social_qq = social_qq
    cfg.social_wechat = social_wechat
    cfg.social_telegram = social_telegram
    cfg.social_discord = social_discord
    cfg.renewal_enabled = renewal_enabled

    if clear_wechat_qr == "1":
        if cfg.wechat_qr:
            old = os.path.join(UPLOAD_DIR, cfg.wechat_qr)
            if os.path.exists(old):
                os.remove(old)
        cfg.wechat_qr = ""
    if wechat_qr_file and wechat_qr_file.filename:
        ext = wechat_qr_file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            ext = "png"
        safe_name = f"wechat_{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(UPLOAD_DIR, safe_name)
        data = await wechat_qr_file.read()
        with open(file_path, "wb") as f:
            f.write(data)
        if cfg.wechat_qr:
            old = os.path.join(UPLOAD_DIR, cfg.wechat_qr)
            if os.path.exists(old):
                os.remove(old)
        cfg.wechat_qr = safe_name

    if clear_alipay_qr == "1":
        if cfg.alipay_qr:
            old = os.path.join(UPLOAD_DIR, cfg.alipay_qr)
            if os.path.exists(old):
                os.remove(old)
        cfg.alipay_qr = ""
    if alipay_qr_file and alipay_qr_file.filename:
        ext = alipay_qr_file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            ext = "png"
        safe_name = f"alipay_{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(UPLOAD_DIR, safe_name)
        data = await alipay_qr_file.read()
        with open(file_path, "wb") as f:
            f.write(data)
        if cfg.alipay_qr:
            old = os.path.join(UPLOAD_DIR, cfg.alipay_qr)
            if os.path.exists(old):
                os.remove(old)
        cfg.alipay_qr = safe_name

    db.commit()
    return RedirectResponse("/admin/payment?saved=1", 302)


# ════════════════════════════════════════════════════════════
#  续期记录（管理后台查看）
# ════════════════════════════════════════════════════════════
@router.get("/admin/renewals", response_class=HTMLResponse)
async def admin_renewals_page(request: Request, user: User = Depends(require_admin),
                               db: Session = Depends(get_db)):
    records = db.query(RenewalRecord).order_by(RenewalRecord.created_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "admin_renewals.html",
        {"user": user, "records": records})
