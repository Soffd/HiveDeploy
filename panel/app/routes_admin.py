import os
import json
import uuid
import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from .bootstrap import templates, HOST, UPLOAD_DIR
from .database import get_db
from .models import (
    User, Instance, SmtpConfig, SiteConfig, PaymentConfig,
    RenewalRecord, InviteCode, BannedUser, EmailTemplate, Announcement,
)
from .auth import (
    get_password_hash, get_current_user_from_cookie,
)
from .docker_manager import (
    stop_user_instance, start_user_instance, restart_user_instance,
    delete_user_instance, get_all_instances_status,
)
from .email_service import send_email, DEFAULT_EMAIL_TEMPLATES, EMAIL_TEMPLATE_VARIABLES
from .hub_sync import _push_ban_to_hub, _push_unban_to_hub

logger = logging.getLogger(__name__)
router = APIRouter()


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
    max_users = int(max_users_cfg.value or "0") if max_users_cfg else 0
    reg_open = reg_open_cfg.value if reg_open_cfg else "true"
    allowed_domains = allowed_domains_cfg.value if allowed_domains_cfg else ""
    hub_url = hub_url_cfg.value if hub_url_cfg else ""
    require_invite = require_invite_cfg.value if require_invite_cfg else "true"
    invite_min_days = invite_min_days_cfg.value if invite_min_days_cfg else "90"
    invite_monthly_limit = invite_monthly_limit_cfg.value if invite_monthly_limit_cfg else "5"
    invite_active_limit = invite_active_limit_cfg.value if invite_active_limit_cfg else "10"
    invite_code_bytes = invite_code_bytes_cfg.value if invite_code_bytes_cfg else "8"
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
        "invite_codes": invite_codes, "total_active_codes": total_active,
        "invite_stats": invite_stats,
    })


@router.post("/admin/user/create")
async def admin_create_user(
    username: str = Form(...), email: str = Form(...),
    password: str = Form(...),
    is_admin: Optional[str] = Form(None),
    expire_days: Optional[str] = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(User).filter_by(username=username).first():
        return RedirectResponse("/admin?error=用户名已存在", 302)
    _is_admin = is_admin in ("on", "true", "1", "yes")
    _expire_days = int(expire_days) if expire_days and expire_days.strip().isdigit() else None
    expire_at = datetime.now() + timedelta(days=_expire_days) if _expire_days else None
    db.add(User(
        username=username, email=email,
        hashed_password=get_password_hash(password),
        is_admin=_is_admin, expire_at=expire_at,
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


@router.post("/admin/user/{user_id}/delete")
async def admin_delete_user(user_id: int, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        delete_user_instance(u.username)
        if u.instance: db.delete(u.instance)
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
                     ("invite_code_bytes", invite_code_bytes)]:
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        if cfg:
            cfg.value = val
        else:
            db.add(SiteConfig(key=key, value=val))
    db.commit()
    return RedirectResponse("/admin?settings_saved=1", 302)


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
