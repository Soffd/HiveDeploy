import os
import json
import logging
import urllib.request
from datetime import datetime

from fastapi import Request
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import SiteConfig, InviteCode, BannedUser

logger = logging.getLogger(__name__)

HOST = os.environ.get("PLATFORM_HOST", "localhost")


def _verify_api_token(request: Request, db: Session) -> SiteConfig | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if not cfg or cfg.value != token:
        return None
    return cfg


def _push_code_to_hub(db: Session, code: str, source_node: str):
    _push_codes_to_hub(db, [code], source_node)


def _push_codes_to_hub(db: Session, codes: list[str], source_node: str):
    codes = [code for code in codes if code]
    if not codes:
        return
    hub_url = db.query(SiteConfig).filter_by(key="hub_url").first()
    if not hub_url or not hub_url.value:
        return
    try:
        node_name = os.environ.get("SITE_NAME", HOST)
        data = json.dumps({
            "node_name": node_name,
            "codes": [{"code": code, "source_node": node_name} for code in codes],
        }).encode()
        req = urllib.request.Request(
            f"{hub_url.value}/api/invite-codes/receive",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                db.query(InviteCode).filter(InviteCode.code.in_(codes)).update(
                    {InviteCode.source_node: node_name},
                    synchronize_session=False,
                )
                db.commit()
    except Exception as e:
        logger.warning(f"推送邀请码到 Hub 失败: {e}")


def _push_codes_to_hub_bg(codes: list[str], source_node: str):
    db = SessionLocal()
    try:
        _push_codes_to_hub(db, codes, source_node)
    finally:
        db.close()


def _push_code_to_hub_bg(code: str, source_node: str):
    _push_codes_to_hub_bg([code], source_node)


def _push_code_usage_to_hub(db: Session, code: str, used_at: datetime):
    hub_url = db.query(SiteConfig).filter_by(key="hub_url").first()
    if not hub_url or not hub_url.value:
        return
    try:
        node_name = os.environ.get("SITE_NAME", HOST)
        data = json.dumps({
            "node_name": node_name,
            "codes": [{
                "code": code,
                "source_node": node_name,
                "used": True,
                "used_at": used_at.strftime("%Y-%m-%d %H:%M:%S"),
            }],
        }).encode()
        req = urllib.request.Request(
            f"{hub_url.value}/api/invite-codes/receive",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                ic = db.query(InviteCode).filter_by(code=code).first()
                if ic:
                    ic.usage_synced = True
                    db.commit()
    except Exception as e:
        logger.warning(f"推送邀请码使用状态到 Hub 失败: {e}")


def _push_code_usage_to_hub_bg(code: str, used_at: datetime):
    db = SessionLocal()
    try:
        _push_code_usage_to_hub(db, code, used_at)
    finally:
        db.close()


def _push_ban_to_hub(db: Session, username: str, email: str):
    hub_url = db.query(SiteConfig).filter_by(key="hub_url").first()
    if not hub_url or not hub_url.value:
        return
    try:
        node_name = os.environ.get("SITE_NAME", HOST)
        data = json.dumps({
            "node_name": node_name,
            "bans": [{"username": username, "email": email, "action": "ban"}],
        }).encode()
        req = urllib.request.Request(
            f"{hub_url.value}/api/banned-users/receive",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            json.loads(r.read())
    except Exception as e:
        logger.warning(f"推送封禁用户到 Hub 失败: {e}")


def _push_unban_to_hub(db: Session, username: str, email: str):
    hub_url = db.query(SiteConfig).filter_by(key="hub_url").first()
    if not hub_url or not hub_url.value:
        return
    try:
        node_name = os.environ.get("SITE_NAME", HOST)
        data = json.dumps({
            "node_name": node_name,
            "bans": [{"username": username, "email": email, "action": "unban"}],
        }).encode()
        req = urllib.request.Request(
            f"{hub_url.value}/api/banned-users/receive",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            json.loads(r.read())
    except Exception as e:
        logger.warning(f"推送解封用户到 Hub 失败: {e}")
