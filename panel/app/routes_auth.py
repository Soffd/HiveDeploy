import re
import logging
import random
import string
import hashlib
import secrets
import math
import struct
import zlib
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from .bootstrap import templates, HOST
from .database import get_db
from .models import User, SmtpConfig, SiteConfig, VerificationCode, InviteCode, BannedUser, EmailCaptchaChallenge
from .auth import (
    get_password_hash, authenticate_user, create_access_token,
    get_current_user_from_cookie, verify_password, SECRET_KEY,
)
from .email_service import send_verification_code
from .hub_sync import _push_code_usage_to_hub

logger = logging.getLogger(__name__)
router = APIRouter()
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CAPTCHA_TTL_MINUTES = 5
EMAIL_CODE_COOLDOWN_SECONDS = 60
_captcha_rng = random.SystemRandom()
_FONT_5X7 = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _valid_captcha_id(captcha_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,64}", captcha_id or ""))


def _captcha_hash(captcha_id: str, code: str) -> str:
    raw = f"{captcha_id}:{(code or '').strip().upper()}:{SECRET_KEY}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cleanup_expired_email_captchas(db: Session):
    try:
        db.query(EmailCaptchaChallenge).filter(
            EmailCaptchaChallenge.expires_at < datetime.now()
        ).delete()
        db.commit()
    except Exception:
        db.rollback()


def _draw_line(pixels: bytearray, width: int, height: int, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]):
    dx = abs(x2 - x1)
    dy = -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx + dy
    x, y = x1, y1
    while True:
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 3
            pixels[idx:idx + 3] = bytes(color)
        if x == x2 and y == y2:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def _fill_rect(pixels: bytearray, width: int, height: int, x: int, y: int, w: int, h: int, color: tuple[int, int, int]):
    for yy in range(max(0, y), min(height, y + h)):
        row = yy * width * 3
        for xx in range(max(0, x), min(width, x + w)):
            idx = row + xx * 3
            pixels[idx:idx + 3] = bytes(color)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) +
        chunk_type +
        data +
        struct.pack(">I", zlib.crc32(chunk_type + data) & 0xffffffff)
    )


def _captcha_png(code: str) -> bytes:
    width, height = 168, 54
    pixels = bytearray([246, 248, 250] * width * height)

    for _ in range(900):
        x, y = _captcha_rng.randrange(width), _captcha_rng.randrange(height)
        color = _captcha_rng.choice([(208, 215, 222), (139, 148, 158), (88, 166, 255), (63, 185, 80), (210, 153, 34)])
        idx = (y * width + x) * 3
        pixels[idx:idx + 3] = bytes(color)

    for _ in range(8):
        color = _captcha_rng.choice([(88, 166, 255), (63, 185, 80), (210, 153, 34), (248, 81, 73), (139, 148, 158)])
        x1, y1 = _captcha_rng.randrange(width), _captcha_rng.randrange(height)
        x2, y2 = _captcha_rng.randrange(width), _captcha_rng.randrange(height)
        _draw_line(pixels, width, height, x1, y1, x2, y2, color)
        _draw_line(pixels, width, height, x1, min(height - 1, y1 + 1), x2, min(height - 1, y2 + 1), color)

    scale = 5
    spacing = 7
    x = 12
    for idx, ch in enumerate(code):
        pattern = _FONT_5X7.get(ch, _FONT_5X7["A"])
        y_base = 8 + _captcha_rng.randint(-2, 2)
        x_jitter = x + _captcha_rng.randint(-2, 2)
        color = _captcha_rng.choice([(13, 17, 23), (31, 35, 40), (9, 105, 218), (17, 99, 41)])
        for row_idx, row in enumerate(pattern):
            for col_idx, bit in enumerate(row):
                if bit != "1":
                    continue
                if _captcha_rng.random() < 0.04:
                    continue
                _fill_rect(
                    pixels,
                    width,
                    height,
                    x_jitter + col_idx * scale,
                    y_base + row_idx * scale,
                    scale - 1,
                    scale - 1,
                    color,
                )
        x += 5 * scale + spacing

    for _ in range(5):
        color = _captcha_rng.choice([(35, 134, 54), (9, 105, 218), (130, 80, 223), (191, 80, 16)])
        _draw_line(
            pixels,
            width,
            height,
            _captcha_rng.randrange(0, 20),
            _captcha_rng.randrange(8, height - 8),
            _captcha_rng.randrange(width - 20, width),
            _captcha_rng.randrange(8, height - 8),
            color,
        )

    raw = b"".join(
        b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3])
        for y in range(height)
    )
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += _png_chunk(b"IEND", b"")
    return png


def _verify_email_captcha(db: Session, captcha_id: str, captcha_code: str, purpose: str) -> str:
    if purpose not in ("register", "reset_password", "change_password"):
        return "无效的验证目的"
    if not _valid_captcha_id(captcha_id):
        return "请刷新图形验证码后重试"
    challenge = db.query(EmailCaptchaChallenge).filter_by(id=captcha_id).first()
    if not challenge or challenge.used or challenge.purpose != purpose:
        return "图形验证码已失效，请刷新后重试"
    if challenge.expires_at < datetime.now():
        challenge.used = True
        db.commit()
        return "图形验证码已过期，请刷新后重试"
    challenge.attempts = (challenge.attempts or 0) + 1
    if challenge.attempts > 5:
        challenge.used = True
        db.commit()
        return "图形验证码错误次数过多，请刷新后重试"
    if not secrets.compare_digest(challenge.code_hash, _captcha_hash(captcha_id, captcha_code)):
        db.commit()
        return "图形验证码错误"
    challenge.used = True
    db.commit()
    return ""


def _email_code_retry_after(db: Session, email: str, purpose: str) -> int:
    latest = db.query(VerificationCode).filter(
        VerificationCode.email == email,
        VerificationCode.purpose == purpose,
    ).order_by(VerificationCode.created_at.desc()).first()
    if not latest or not latest.created_at:
        return 0
    elapsed = (datetime.now() - latest.created_at).total_seconds()
    return max(0, math.ceil(EMAIL_CODE_COOLDOWN_SECONDS - elapsed))


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


@router.get("/api/email/captcha/{captcha_id}")
async def email_captcha_image(captcha_id: str, purpose: str = "register", db: Session = Depends(get_db)):
    if purpose not in ("register", "reset_password", "change_password") or not _valid_captcha_id(captcha_id):
        raise HTTPException(status_code=400, detail="invalid captcha")
    _cleanup_expired_email_captchas(db)
    code = "".join(_captcha_rng.choice(CAPTCHA_ALPHABET) for _ in range(5))
    old = db.query(EmailCaptchaChallenge).filter_by(id=captcha_id).first()
    if old:
        old.purpose = purpose
        old.code_hash = _captcha_hash(captcha_id, code)
        old.created_at = datetime.now()
        old.expires_at = datetime.now() + timedelta(minutes=CAPTCHA_TTL_MINUTES)
        old.used = False
        old.attempts = 0
    else:
        db.add(EmailCaptchaChallenge(
            id=captcha_id,
            purpose=purpose,
            code_hash=_captcha_hash(captcha_id, code),
            expires_at=datetime.now() + timedelta(minutes=CAPTCHA_TTL_MINUTES),
        ))
    db.commit()
    return Response(
        content=_captcha_png(code),
        media_type="image/png",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.post("/api/email/send-code")
async def send_verification_code_api(
    request: Request,
    email: str = Form(...),
    purpose: str = Form(...),
    captcha_id: str = Form(""),
    captcha_code: str = Form(""),
    db: Session = Depends(get_db),
):
    email = _normalize_email(email)
    if purpose not in ("register", "reset_password", "change_password"):
        return JSONResponse({"ok": False, "error": "无效的验证目的"}, status_code=400)
    if not email:
        return JSONResponse({"ok": False, "error": "请填写邮箱地址"}, status_code=400)

    if purpose == "change_password":
        try:
            current_user = get_current_user_from_cookie(request, db)
        except HTTPException:
            return JSONResponse({"ok": False, "error": "请先登录后再发送修改密码验证码"}, status_code=403)
        if _normalize_email(current_user.email) != email:
            return JSONResponse({"ok": False, "error": "只能向当前账号绑定邮箱发送验证码"}, status_code=403)

    captcha_error = _verify_email_captcha(db, captcha_id, captcha_code, purpose)
    if captcha_error:
        return JSONResponse({"ok": False, "error": captcha_error}, status_code=400)

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

    retry_after = _email_code_retry_after(db, email, purpose)
    if retry_after > 0:
        return JSONResponse({
            "ok": False,
            "error": f"发送过于频繁，请 {retry_after} 秒后再试",
            "retry_after": retry_after,
        }, status_code=429)

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
    captcha_id = ""
    captcha_code = ""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
            email = (data.get("email") or "").strip()
            captcha_id = (data.get("captcha_id") or "").strip()
            captcha_code = (data.get("captcha_code") or "").strip()
        else:
            form = await request.form()
            email = (form.get("email") or "").strip()
            captcha_id = (form.get("captcha_id") or "").strip()
            captcha_code = (form.get("captcha_code") or "").strip()
    except Exception:
        email = ""

    if not email:
        return JSONResponse({"ok": False, "error": "请填写邮箱地址"}, status_code=400)

    return await send_verification_code_api(
        request=request,
        email=email,
        purpose="reset_password",
        captcha_id=captcha_id,
        captcha_code=captcha_code,
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
