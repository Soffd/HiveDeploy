import re
import logging
import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .bootstrap import templates, HOST
from .database import get_db
from .models import User, SmtpConfig, SiteConfig, VerificationCode, InviteCode, BannedUser
from .auth import (
    get_password_hash, authenticate_user, create_access_token,
    get_current_user_from_cookie, verify_password,
)
from .email_service import send_verification_code
from .hub_sync import _push_code_usage_to_hub

logger = logging.getLogger(__name__)
router = APIRouter()


# ════════════════════════════════════════════════════════════
#  认证
# ════════════════════════════════════════════════════════════
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                db: Session = Depends(get_db)):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse(request, "login.html",
            {"error": "用户名或密码错误"}, status_code=401)
    if not user.is_active:
        return templates.TemplateResponse(request, "login.html",
            {"error": "账号已被冻结或封禁，如有疑问请联系管理员"}, status_code=403)
    if user.expire_at and user.expire_at < datetime.now():
        from .docker_manager import stop_user_instance
        try:
            stop_user_instance(user.username)
        except Exception:
            pass
    token = create_access_token({"sub": user.username})
    resp  = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, max_age=86400)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


@router.post("/api/email/send-code")
async def send_verification_code_api(
    email: str = Form(...),
    purpose: str = Form(...),
    db: Session = Depends(get_db),
):
    if purpose not in ("register", "reset_password", "change_password"):
        return JSONResponse({"ok": False, "error": "无效的验证目的"}, status_code=400)

    if purpose == "register" and db.query(User).filter_by(email=email).first():
        return JSONResponse({"ok": False, "error": "该邮箱已被注册"}, status_code=400)

    if purpose == "register":
        allowed_domains_cfg = db.query(SiteConfig).filter_by(key="allowed_email_domains").first()
        allowed_domains_str = allowed_domains_cfg.value.strip() if allowed_domains_cfg else ""
        if allowed_domains_str:
            allowed = [d.strip().lower() for d in allowed_domains_str.split(",") if d.strip()]
            email_domain = email.split("@")[-1].lower() if "@" in email else ""
            if allowed and email_domain not in allowed:
                return JSONResponse({"ok": False, "error": "该邮箱域名不允许注册，请使用允许的邮箱域名"}, status_code=400)

    if purpose == "reset_password":
        user = db.query(User).filter_by(email=email).first()
        if not user:
            return JSONResponse({"ok": False, "error": "该邮箱未注册"}, status_code=400)

    smtp_cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not smtp_cfg or not smtp_cfg.enabled:
        return JSONResponse({"ok": False, "error": "SMTP 未配置，无法发送验证码"}, status_code=400)

    code = "".join(random.choices(string.digits, k=6))

    db.query(VerificationCode).filter(
        VerificationCode.email == email,
        VerificationCode.purpose == purpose,
    ).delete()

    vc = VerificationCode(
        email=email,
        code=code,
        purpose=purpose,
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(vc)
    db.commit()

    user_for_template = db.query(User).filter_by(email=email).first()
    if send_verification_code(email, code, smtp_cfg, db=db, purpose=purpose, user=user_for_template):
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"ok": False, "error": "验证码发送失败，请检查 SMTP 配置"}, status_code=500)


@router.post("/api/send-reset-code")
async def send_reset_code_legacy(request: Request, db: Session = Depends(get_db)):
    """Backward-compatible endpoint for cached reset-password pages."""
    email = ""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
            email = (data.get("email") or "").strip()
        else:
            form = await request.form()
            email = (form.get("email") or "").strip()
    except Exception:
        email = ""

    if not email:
        return JSONResponse({"ok": False, "error": "请填写邮箱地址"}, status_code=400)

    return await send_verification_code_api(
        email=email,
        purpose="reset_password",
        db=db,
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Session = Depends(get_db)):
    reg_open = db.query(SiteConfig).filter_by(key="registration_open").first()
    if reg_open and reg_open.value != "true":
        return templates.TemplateResponse(request, "register.html",
            {"errors": ["注册功能暂未开放"], "username": "", "email": "", "closed": True})
    max_users = int(db.query(SiteConfig).filter_by(key="max_users").first().value or "0")
    current = db.query(User).count()
    slots = max_users - current if max_users > 0 else 999
    if max_users > 0 and slots <= 0:
        return templates.TemplateResponse(request, "register.html",
            {"errors": ["当前服务器注册人数已满，请选择其他节点"], "username": "", "email": "", "closed": True})
    smtp_enabled = db.query(SmtpConfig).filter_by(id=1).first()
    smtp_ready = smtp_enabled is not None and smtp_enabled.enabled
    require_invite_cfg = db.query(SiteConfig).filter_by(key="require_invite_code").first()
    require_invite = require_invite_cfg.value == "true" if require_invite_cfg else True
    return templates.TemplateResponse(request, "register.html",
        {"errors": [], "username": "", "email": "", "max_users": max_users,
         "current": current, "slots": slots, "smtp_ready": smtp_ready,
         "require_invite_code": require_invite, "invite_code": ""})


@router.post("/register")
async def register(request: Request,
                   username: str = Form(...), email: str = Form(...),
                   password: str = Form(...), password2: str = Form(...),
                   code: str = Form(""),
                   invite_code: str = Form(""),
                   db: Session = Depends(get_db)):
    errors = []

    reg_open_cfg = db.query(SiteConfig).filter_by(key="registration_open").first()
    if reg_open_cfg and reg_open_cfg.value != "true":
        errors.append("注册功能暂未开放")
    max_users_cfg = db.query(SiteConfig).filter_by(key="max_users").first()
    max_users = int(max_users_cfg.value or "0") if max_users_cfg else 0
    current = db.query(User).count()
    slots = max_users - current if max_users > 0 else 999
    if max_users > 0 and slots <= 0:
        errors.append("当前服务器注册人数已满，请选择其他节点")

    if not re.match(r'^[a-zA-Z0-9]{3,32}$', username):
        errors.append("用户名须为 3-32 位字母/数字")
    if password != password2:
        errors.append("两次密码不一致")
    if len(password) < 6:
        errors.append("密码至少 6 位")
    if db.query(User).filter_by(username=username).first():
        errors.append("用户名已被占用")
    if db.query(User).filter_by(email=email).first():
        errors.append("邮箱已被注册")
    if db.query(BannedUser).filter_by(username=username).first():
        errors.append("该用户名已被全局封禁，无法注册")
    if db.query(BannedUser).filter_by(email=email).first():
        errors.append("该邮箱已被全局封禁，无法注册")

    allowed_domains_cfg = db.query(SiteConfig).filter_by(key="allowed_email_domains").first()
    allowed_domains_str = allowed_domains_cfg.value.strip() if allowed_domains_cfg else ""
    if allowed_domains_str:
        allowed = [d.strip().lower() for d in allowed_domains_str.split(",") if d.strip()]
        email_domain = email.split("@")[-1].lower() if "@" in email else ""
        if allowed and email_domain not in allowed:
            errors.append("该邮箱域名不允许注册，请使用允许的邮箱域名")

    if not code.strip():
        errors.append("请输入邮箱验证码")
    else:
        vc = db.query(VerificationCode).filter(
            VerificationCode.email == email,
            VerificationCode.purpose == "register",
            VerificationCode.used == False,
        ).order_by(VerificationCode.created_at.desc()).first()
        if not vc:
            errors.append("请先获取邮箱验证码")
        elif vc.code != code.strip():
            errors.append("验证码错误")
        elif vc.expires_at < datetime.now():
            errors.append("验证码已过期，请重新获取")

    require_invite_cfg = db.query(SiteConfig).filter_by(key="require_invite_code").first()
    require_invite = require_invite_cfg.value == "true" if require_invite_cfg else True
    invite_code_obj = None
    if not errors:
        ic_str = invite_code.strip()
        if require_invite and not ic_str:
            errors.append("请输入邀请码")
        elif ic_str:
            invite_code_obj = db.query(InviteCode).filter(
                InviteCode.code == ic_str,
                InviteCode.used == False,
                InviteCode.hidden == False,
            ).first()
            if not invite_code_obj:
                errors.append("邀请码无效或已被使用")

    if errors:
        smtp_enabled = db.query(SmtpConfig).filter_by(id=1).first()
        smtp_ready = smtp_enabled is not None and smtp_enabled.enabled
        return templates.TemplateResponse(request, "register.html",
            {"errors": errors, "username": username, "email": email,
             "max_users": max_users, "current": current, "slots": slots,
             "smtp_ready": smtp_ready, "require_invite_code": require_invite,
             "invite_code": invite_code, "code": code})

    vc.used = True
    if invite_code_obj:
        invite_code_obj.used = True
        invite_code_obj.used_at = datetime.now()
        invite_code_obj.usage_synced = False

    new_user = User(username=username, email=email,
                     hashed_password=get_password_hash(password),
                     expire_at=datetime.now() + timedelta(days=3))
    db.add(new_user)
    db.flush()
    if invite_code_obj:
        invite_code_obj.used_by = new_user.id
    db.commit()

    if invite_code_obj:
        _push_code_usage_to_hub(db, invite_code_obj.code, invite_code_obj.used_at)

    return RedirectResponse("/login?registered=1", status_code=302)


# ════════════════════════════════════════════════════════════
#  密码重置（忘记密码）
# ════════════════════════════════════════════════════════════
@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, db: Session = Depends(get_db)):
    smtp_enabled = db.query(SmtpConfig).filter_by(id=1).first()
    smtp_ready = smtp_enabled is not None and smtp_enabled.enabled
    return templates.TemplateResponse(request, "reset_password.html",
        {"errors": [], "email": "", "smtp_ready": smtp_ready})


@router.post("/reset-password")
async def reset_password(request: Request,
                         email: str = Form(...),
                         code: str = Form(""),
                         new_password: str = Form(""),
                         new_password2: str = Form(""),
                         db: Session = Depends(get_db)):
    errors = []
    if not code.strip():
        errors.append("请输入邮箱验证码")
    if not new_password:
        errors.append("请输入新密码")
    if new_password != new_password2:
        errors.append("两次密码不一致")
    if len(new_password) < 6 and new_password:
        errors.append("密码至少 6 位")

    user = db.query(User).filter_by(email=email).first()
    if not user:
        errors.append("该邮箱未注册")

    if code.strip():
        vc = db.query(VerificationCode).filter(
            VerificationCode.email == email,
            VerificationCode.purpose == "reset_password",
            VerificationCode.used == False,
        ).order_by(VerificationCode.created_at.desc()).first()
        if not vc:
            errors.append("请先获取邮箱验证码")
        elif vc.code != code.strip():
            errors.append("验证码错误")
        elif vc.expires_at < datetime.now():
            errors.append("验证码已过期，请重新获取")

    if errors:
        smtp_enabled = db.query(SmtpConfig).filter_by(id=1).first()
        smtp_ready = smtp_enabled is not None and smtp_enabled.enabled
        return templates.TemplateResponse(request, "reset_password.html",
            {"errors": errors, "email": email, "smtp_ready": smtp_ready})

    vc.used = True
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    return RedirectResponse("/login?pw_reset=1", status_code=302)
