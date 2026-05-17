import os
import re
import json
import secrets
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
CST = ZoneInfo("Asia/Shanghai")
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import engine, SessionLocal, get_db
from .models import User, SmtpConfig, SiteConfig, Announcement
from .auth import get_password_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── DB 初始化 ────────────────────────────────────────────────
def _run_migrations():
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        insp = inspect(engine)

        users_cols = [c["name"] for c in insp.get_columns("users")]
        if "expire_at" not in users_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN expire_at DATETIME"))
            conn.commit()

        if insp.has_table("instances"):
            inst_cols = [c["name"] for c in insp.get_columns("instances")]
            if "extra_ports_json" not in inst_cols:
                conn.execute(text("ALTER TABLE instances ADD COLUMN extra_ports_json TEXT DEFAULT '[]'"))
                conn.commit()
            if "llonebot_container_id" not in inst_cols:
                conn.execute(text("ALTER TABLE instances ADD COLUMN llonebot_container_id VARCHAR(64)"))
                conn.commit()
            if "bot_type" not in inst_cols:
                conn.execute(text("ALTER TABLE instances ADD COLUMN bot_type VARCHAR(16) DEFAULT 'napcat'"))
                conn.commit()

        if not insp.has_table("email_log"):
            conn.execute(text("""
                CREATE TABLE email_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    email_type VARCHAR(32) NOT NULL,
                    sent_date VARCHAR(10) NOT NULL,
                    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_email_log_user_id ON email_log (user_id)"))
            conn.commit()

        if not insp.has_table("email_templates"):
            conn.execute(text("""
                CREATE TABLE email_templates (
                    key VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    subject VARCHAR(256) DEFAULT '',
                    body_html TEXT DEFAULT '',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.commit()

        if not insp.has_table("payment_config"):
            conn.execute(text("""
                CREATE TABLE payment_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    wechat_qr TEXT DEFAULT '',
                    alipay_qr TEXT DEFAULT '',
                    price_text VARCHAR(256) DEFAULT '',
                    instructions TEXT DEFAULT '',
                    social_qq VARCHAR(128) DEFAULT '',
                    social_wechat VARCHAR(128) DEFAULT '',
                    social_telegram VARCHAR(256) DEFAULT '',
                    social_discord VARCHAR(256) DEFAULT '',
                    renewal_enabled BOOLEAN DEFAULT 0
                )
            """))
            conn.execute(text("INSERT INTO payment_config (id) VALUES (1)"))
            conn.commit()

        if not insp.has_table("renewal_records"):
            conn.execute(text("""
                CREATE TABLE renewal_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username VARCHAR(32) NOT NULL,
                    days_added INTEGER NOT NULL,
                    previous_expire_at DATETIME,
                    new_expire_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_renewal_records_user_id ON renewal_records (user_id)"))
            conn.commit()

        if not insp.has_table("invite_codes"):
            conn.execute(text("""
                CREATE TABLE invite_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(32) NOT NULL UNIQUE,
                    creator_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    used BOOLEAN DEFAULT 0,
                    used_by INTEGER,
                    used_at DATETIME,
                    hidden BOOLEAN DEFAULT 0,
                    source_node VARCHAR(64) DEFAULT 'local'
                )
            """))
            conn.execute(text("CREATE UNIQUE INDEX ix_invite_codes_code ON invite_codes (code)"))
            conn.commit()

        if insp.has_table("invite_codes"):
            cols = [c["name"] for c in insp.get_columns("invite_codes")]
            if "hidden" not in cols:
                conn.execute(text("ALTER TABLE invite_codes ADD COLUMN hidden BOOLEAN DEFAULT 0"))
                conn.commit()
            if "usage_synced" not in cols:
                conn.execute(text("ALTER TABLE invite_codes ADD COLUMN usage_synced BOOLEAN DEFAULT 1"))
                conn.commit()
                conn.execute(text("UPDATE invite_codes SET usage_synced = 0 WHERE used = 1"))
                conn.commit()

        if insp.has_table("smtp_config"):
            smtp_cols = [c["name"] for c in insp.get_columns("smtp_config")]
            if "renewal_notify_email" not in smtp_cols:
                conn.execute(text("ALTER TABLE smtp_config ADD COLUMN renewal_notify_email TEXT DEFAULT ''"))
                conn.commit()

        if not insp.has_table("banned_users"):
            conn.execute(text("""
                CREATE TABLE banned_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(32) NOT NULL,
                    email VARCHAR(128) NOT NULL,
                    banned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source_node VARCHAR(64) DEFAULT 'local'
                )
            """))
            conn.execute(text("CREATE INDEX ix_banned_users_username ON banned_users (username)"))
            conn.execute(text("CREATE INDEX ix_banned_users_email ON banned_users (email)"))
            conn.commit()

        if not insp.has_table("announcements"):
            conn.execute(text("""
                CREATE TABLE announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title VARCHAR(128) NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    type VARCHAR(32) NOT NULL DEFAULT 'info',
                    level VARCHAR(32) NOT NULL DEFAULT 'normal',
                    enabled BOOLEAN DEFAULT 1,
                    pinned BOOLEAN DEFAULT 0,
                    font_size INTEGER DEFAULT 15,
                    color VARCHAR(16) DEFAULT '',
                    bold BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_announcements_enabled ON announcements (enabled)"))
            conn.execute(text("CREATE INDEX ix_announcements_pinned ON announcements (pinned)"))
            conn.commit()

        if insp.has_table("announcements") and insp.has_table("site_config"):
            existing = conn.execute(text("SELECT COUNT(*) FROM announcements")).scalar() or 0
            if existing == 0:
                legacy_content = conn.execute(text("SELECT value FROM site_config WHERE key='announcement_content'")).scalar()
                if legacy_content and legacy_content.strip():
                    legacy_title = conn.execute(text("SELECT value FROM site_config WHERE key='announcement_title'")).scalar() or "系统公告"
                    legacy_type = conn.execute(text("SELECT value FROM site_config WHERE key='announcement_type'")).scalar() or "info"
                    legacy_enabled = conn.execute(text("SELECT value FROM site_config WHERE key='announcement_enabled'")).scalar() or "false"
                    conn.execute(text("""
                        INSERT INTO announcements (title, content, type, level, enabled, pinned, font_size, color, bold)
                        VALUES (:title, :content, :type, 'normal', :enabled, 0, 15, '', 0)
                    """), {
                        "title": legacy_title,
                        "content": legacy_content,
                        "type": legacy_type,
                        "enabled": 1 if legacy_enabled == "true" else 0,
                    })
                    conn.commit()


from .models import Base, VerificationCode, SiteConfig as SC2
Base.metadata.create_all(bind=engine)
_run_migrations()


def _ensure_site_config(key: str, value: str):
    db = SessionLocal()
    try:
        cfg = db.query(SC2).filter_by(key=key).first()
        if not cfg:
            db.add(SC2(key=key, value=value))
            db.commit()
    finally:
        db.close()


_ensure_site_config("max_users", "0")
_ensure_site_config("registration_open", "true")
_ensure_site_config("allowed_email_domains", "")
_ensure_site_config("hub_url", "")
_ensure_site_config("require_invite_code", "true")
_ensure_site_config("invite_min_days", "90")
_ensure_site_config("invite_monthly_limit", "5")
_ensure_site_config("invite_active_limit", "10")
_ensure_site_config("invite_code_bytes", "8")
_ensure_site_config("announcement_enabled", "false")
_ensure_site_config("announcement_type", "info")
_ensure_site_config("announcement_title", "")
_ensure_site_config("announcement_content", "")
_ensure_site_config("announcement_version", "")

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/db/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="HiveDeploy")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
templates = Jinja2Templates(directory="/app/templates")

HOST = os.environ.get("PLATFORM_HOST", "localhost")

# ── 模板全局工具 ─────────────────────────────────────────────
def status_badge(s: str) -> str:
    return {"running": "success", "exited": "secondary", "not_found": "dark",
            "error": "danger", "creating": "warning"}.get(s, "secondary")

def user_expired(user) -> bool:
    if user.expire_at is None:
        return False
    return datetime.now() > user.expire_at

def days_until_expire(user) -> Optional[int]:
    if user.expire_at is None:
        return None
    delta = user.expire_at - datetime.now()
    return delta.days

def regex_match(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value, re.IGNORECASE))

def get_site_announcement() -> dict:
    db = SessionLocal()
    try:
        rows = db.query(Announcement).filter_by(enabled=True).order_by(
            Announcement.pinned.desc(),
            Announcement.updated_at.desc(),
            Announcement.id.desc(),
        ).all()
        items = []
        version_parts = []
        for row in rows:
            content = (row.content or "").strip()
            if not content:
                continue
            updated = row.updated_at or row.created_at or datetime.now()
            version_parts.append(f"{row.id}:{updated.isoformat(timespec='seconds')}")
            items.append({
                "id": row.id,
                "title": row.title or "系统公告",
                "content": content,
                "type": row.type or "info",
                "level": row.level or "normal",
                "pinned": bool(row.pinned),
                "font_size": row.font_size or 15,
                "color": row.color or "",
                "bold": bool(row.bold),
                "updated_at": updated.strftime("%Y-%m-%d %H:%M"),
            })
        return {
            "enabled": bool(items),
            "items": items,
            "version": "|".join(version_parts),
        }
    finally:
        db.close()

templates.env.globals["status_badge"]       = status_badge
templates.env.globals["user_expired"]       = user_expired
templates.env.globals["days_until_expire"]  = days_until_expire
templates.env.globals["get_site_announcement"] = get_site_announcement
templates.env.filters["regex_match"]        = regex_match
templates.env.filters["tojson"]             = json.dumps

# ── 初始化管理员 + SiteConfig ────────────────────────────────
def _bootstrap():
    db = SessionLocal()
    try:
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
        if not db.query(User).filter_by(username=admin_username).first():
            db.add(User(
                username=admin_username, email=f"{admin_username}@platform.local",
                hashed_password=get_password_hash(admin_password),
                is_admin=True, is_active=True,
            ))
            db.commit()
            logger.info(f"管理员账号 '{admin_username}' 已创建")

        cfg = db.query(SiteConfig).filter_by(key="api_token").first()
        if not cfg:
            db.add(SiteConfig(key="api_token", value=secrets.token_hex(32)))
            db.commit()
        vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
        if not vcfg:
            db.add(SiteConfig(key="public_view_token", value=secrets.token_hex(16)))
            db.commit()
        scfg = db.query(SiteConfig).filter_by(key="site_name").first()
        if not scfg:
            db.add(SiteConfig(key="site_name", value="HiveDeploy"))
            db.commit()
        smtp = db.query(SmtpConfig).filter_by(id=1).first()
        if not smtp:
            db.add(SmtpConfig(id=1))
            db.commit()
    finally:
        db.close()

_bootstrap()

from .email_service import start_expiry_scheduler
start_expiry_scheduler(get_db)
