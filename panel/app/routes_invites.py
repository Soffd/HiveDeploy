import secrets
import logging
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from .database import get_db
from .models import User, InviteCode, BannedUser, SiteConfig
from .auth import get_current_user_from_cookie
from .hub_sync import (
    _push_code_to_hub_bg, _push_codes_to_hub_bg, _push_code_usage_to_hub_bg,
    _verify_api_token,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 邀请码生成辅助函数 ─────────────────────────────────────────

def _get_invite_config(db: Session):
    """读取邀请码生成规则配置，返回 (min_days, monthly_limit, active_limit)"""
    def _cfg(key, default):
        r = db.query(SiteConfig).filter_by(key=key).first()
        try:
            return int(r.value) if r and r.value else default
        except (ValueError, TypeError):
            return default
    return _cfg("invite_min_days", 90), _cfg("invite_monthly_limit", 5), _cfg("invite_active_limit", 10)


def _get_invite_code_bytes(db: Session) -> int:
    """读取邀请码字节长度配置，默认8字节=16位hex"""
    cfg = db.query(SiteConfig).filter_by(key="invite_code_bytes").first()
    try:
        v = int(cfg.value) if cfg and cfg.value else 8
        return max(4, min(v, 32))
    except (ValueError, TypeError):
        return 8


def _generate_unique_invite_code(db: Session, code_bytes: int | None = None) -> str | None:
    """生成唯一邀请码。"""
    base_bytes = code_bytes or _get_invite_code_bytes(db)
    for level in range(3):
        nbytes = base_bytes + level * 4
        for _ in range(50):
            trial = secrets.token_hex(nbytes)
            if not db.query(InviteCode).filter_by(code=trial).first():
                return trial
    return None


def _generate_unique_invite_codes(db: Session, count: int) -> list[str]:
    """批量生成唯一邀请码，避免每个码重复读取配置和查库。"""
    base_bytes = _get_invite_code_bytes(db)
    codes: list[str] = []
    seen: set[str] = set()

    for level in range(3):
        nbytes = base_bytes + level * 4
        batch_size = max((count - len(codes)) * 2, 16)
        candidates = set()
        while len(candidates) < batch_size:
            trial = secrets.token_hex(nbytes)
            if trial not in seen:
                candidates.add(trial)
                seen.add(trial)

        existing = {
            row[0]
            for row in db.query(InviteCode.code)
            .filter(InviteCode.code.in_(candidates))
            .all()
        }
        for code in candidates:
            if code not in existing:
                codes.append(code)
                if len(codes) >= count:
                    return codes
    return codes


def _can_generate_invite(user: User, db: Session) -> tuple:
    """返回 (can_generate: bool, reason: str)"""
    min_days, monthly_limit, active_limit = _get_invite_config(db)

    age = (datetime.now() - user.created_at).days
    if age < min_days:
        return False, f"账号注册不足{min_days}天（当前{age}天），需满{min_days}天可生成邀请码"

    now = datetime.now()
    this_month = db.query(InviteCode).filter(
        InviteCode.creator_id == user.id,
        InviteCode.created_at >= datetime(now.year, now.month, 1),
    ).count()
    if this_month >= monthly_limit:
        return False, f"本月已生成 {this_month}/{monthly_limit} 个邀请码，已达上限"

    active = db.query(InviteCode).filter(
        InviteCode.creator_id == user.id,
        InviteCode.used == False,
        InviteCode.hidden == False,
    ).count()
    if active >= active_limit:
        return False, f"未使用的邀请码已达 {active}/{active_limit} 个上限，请等待部分被使用后重试"

    return True, ""


# ════════════════════════════════════════════════════════════
#  用户邀请码 API
# ════════════════════════════════════════════════════════════
@router.post("/api/invite_codes/generate")
async def user_generate_invite_code(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if user.is_admin:
        return JSONResponse({"ok": False, "error": "管理员请使用后台生成"}, status_code=400)

    can, reason = _can_generate_invite(user, db)
    if not can:
        return JSONResponse({"ok": False, "error": reason}, status_code=400)

    code = _generate_unique_invite_code(db)
    if not code:
        return JSONResponse({"ok": False, "error": "邀请码生成失败，请重试"}, status_code=500)

    db.add(InviteCode(code=code, creator_id=user.id, source_node="local"))
    db.commit()

    background_tasks.add_task(_push_code_to_hub_bg, code, "local")

    active = db.query(InviteCode).filter_by(creator_id=user.id, used=False).count()
    return JSONResponse({"ok": True, "code": code, "active_count": active})


@router.get("/api/invite_codes/my")
async def user_my_invite_codes(
    user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    min_days, monthly_limit, active_limit = _get_invite_config(db)
    is_veteran = (datetime.now() - user.created_at).days >= min_days

    codes = db.query(InviteCode).filter_by(creator_id=user.id, hidden=False).order_by(
        InviteCode.created_at.desc()).all()
    unused = [c for c in codes if not c.used]

    now = datetime.now()
    monthly = db.query(InviteCode).filter(
        InviteCode.creator_id == user.id,
        InviteCode.created_at >= datetime(now.year, now.month, 1),
    ).count()

    return JSONResponse({
        "is_veteran": is_veteran,
        "is_admin": user.is_admin,
        "codes": [{"id": c.id, "code": c.code, "used": c.used,
                    "created_at": c.created_at.strftime("%Y-%m-%d %H:%M")} for c in codes],
        "active_count": len(unused),
        "monthly_count": monthly,
        "monthly_limit": monthly_limit,
        "active_limit": active_limit,
        "min_days": min_days,
    })


@router.post("/api/invite_codes/{code_id}/hide")
async def hide_invite_code(
    code_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    ic = db.query(InviteCode).filter_by(id=code_id).first()
    if not ic:
        raise HTTPException(404, "邀请码不存在")
    if ic.creator_id != user.id and not user.is_admin:
        raise HTTPException(403, "无权操作")
    if not ic.used:
        ic.used = True
        ic.used_at = datetime.now()
        ic.usage_synced = False
        ic.hidden = True
        db.commit()
        background_tasks.add_task(_push_code_usage_to_hub_bg, ic.code, ic.used_at)
    else:
        ic.hidden = True
        db.commit()
    return JSONResponse({"ok": True})


# ════════════════════════════════════════════════════════════
#  管理员邀请码管理
# ════════════════════════════════════════════════════════════
from .routes_admin import require_admin


@router.post("/admin/invite_codes/generate")
async def admin_generate_invite_codes(
    request: Request,
    background_tasks: BackgroundTasks,
    count: str = Form("1"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        n = max(1, min(int(count), 100))
    except ValueError:
        n = 1

    codes = _generate_unique_invite_codes(db, n)
    invite_rows = [InviteCode(code=code, creator_id=user.id, source_node="local") for code in codes]
    db.add_all(invite_rows)
    db.commit()
    for row in invite_rows:
        db.refresh(row)

    background_tasks.add_task(_push_codes_to_hub_bg, codes, "local")

    wants_json = (
        request.headers.get("x-requested-with") == "fetch"
        or "application/json" in request.headers.get("accept", "")
    )
    if wants_json:
        return JSONResponse({
            "ok": True,
            "generated": len(codes),
            "codes": [{
                "id": row.id,
                "code": row.code,
                "source_node": row.source_node,
                "used": row.used,
                "used_by": row.used_by,
                "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            } for row in invite_rows],
        })

    return RedirectResponse(f"/admin?invite_generated={len(codes)}", 302)


@router.post("/admin/invite_codes/{code_id}/delete")
async def admin_delete_invite_code(
    code_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ic = db.query(InviteCode).filter_by(id=code_id).first()
    if ic:
        code = ic.code
        if not ic.used:
            ic.used = True
            ic.used_at = datetime.now()
            ic.usage_synced = False
            db.commit()
            background_tasks.add_task(_push_code_usage_to_hub_bg, code, ic.used_at)
            ic = db.query(InviteCode).filter_by(id=code_id).first()
        if ic:
            db.delete(ic)
            db.commit()
    return RedirectResponse("/admin", 302)


@router.post("/admin/invite_codes/purge_used")
async def admin_purge_used_invite_codes(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    used_codes = db.query(InviteCode).filter(
        InviteCode.used == True,
        InviteCode.hidden == False,
    ).all()
    count = 0
    for ic in used_codes:
        ic.hidden = True
        count += 1
    db.commit()
    return JSONResponse({"ok": True, "removed": count})


@router.get("/admin/invite_codes/stats")
async def admin_invite_stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    creator_stats = db.query(
        InviteCode.creator_id,
        func.count(InviteCode.id).label("total"),
        func.sum(InviteCode.used).label("used_count"),
    ).filter(InviteCode.hidden == False).group_by(InviteCode.creator_id).all()

    stats = []
    for cs in creator_stats:
        u = db.query(User).filter_by(id=cs.creator_id).first()
        total = cs.total
        used = cs.used_count or 0
        rate = f"{used / total * 100:.0f}%" if total > 0 else "0%"
        stats.append({
            "creator_id": cs.creator_id,
            "username": u.username if u else ("节点-" + str(cs.creator_id)),
            "total": total,
            "used_count": used,
            "rate": rate,
        })
    stats.sort(key=lambda x: x["used_count"], reverse=True)

    total_generated = sum(s["total"] for s in stats)
    total_used = sum(s["used_count"] for s in stats)

    return JSONResponse({
        "ok": True,
        "stats": stats,
        "total_generated": total_generated,
        "total_used": total_used,
    })


# ════════════════════════════════════════════════════════════
#  邀请码跨节点同步（对外 API）
# ════════════════════════════════════════════════════════════

@router.post("/api/v1/invite_codes/receive")
async def api_receive_invite_codes(request: Request, db: Session = Depends(get_db)):
    if not _verify_api_token(request, db):
        raise HTTPException(401, "Unauthorized")
    body = await request.json()
    incoming = body.get("codes", [])
    added, skipped, used_updated, deleted = 0, 0, 0, 0
    for item in incoming:
        c = item.get("code", "").strip()
        if not c:
            continue
        existing = db.query(InviteCode).filter_by(code=c).first()

        if item.get("deleted"):
            if existing:
                db.delete(existing)
                deleted += 1
            continue

        if existing:
            if item.get("used") and not existing.used:
                existing.used = True
                existing.used_at = datetime.now()
                existing.used_by = 0
                used_updated += 1
            else:
                skipped += 1
            continue
        db.add(InviteCode(
            code=c,
            creator_id=0,
            source_node=item.get("source_node", "remote"),
            used=item.get("used", False),
            used_at=datetime.now() if item.get("used") else None,
            used_by=0 if item.get("used") else None,
        ))
        added += 1
    db.commit()
    return JSONResponse({"ok": True, "added": added, "skipped": skipped, "used_updated": used_updated, "deleted": deleted})


@router.get("/api/v1/invite_codes/unsynced")
async def api_get_unsynced_invite_codes(request: Request, db: Session = Depends(get_db)):
    if not _verify_api_token(request, db):
        raise HTTPException(401, "Unauthorized")
    unsynced_new = db.query(InviteCode).filter_by(source_node="local", hidden=False).all()
    unsynced_usage = db.query(InviteCode).filter(
        InviteCode.used == True,
        InviteCode.usage_synced == False,
        InviteCode.source_node != "local",
        InviteCode.hidden == False,
    ).all()
    codes = []
    seen = set()
    for c in unsynced_new:
        if c.code not in seen:
            seen.add(c.code)
            codes.append({
                "code": c.code,
                "source_node": "local",
                "used": c.used,
                "used_at": c.used_at.strftime("%Y-%m-%d %H:%M:%S") if c.used_at else None,
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            })
    for c in unsynced_usage:
        if c.code not in seen:
            seen.add(c.code)
            codes.append({
                "code": c.code,
                "source_node": c.source_node,
                "used": True,
                "used_at": c.used_at.strftime("%Y-%m-%d %H:%M:%S") if c.used_at else None,
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            })
    return JSONResponse({"codes": codes})


@router.post("/api/v1/invite_codes/check")
async def api_check_invite_codes(request: Request, db: Session = Depends(get_db)):
    if not _verify_api_token(request, db):
        raise HTTPException(401, "Unauthorized")
    body = await request.json()
    check_codes = body.get("codes", [])
    existing = []
    for c in check_codes:
        if db.query(InviteCode).filter_by(code=c).first():
            existing.append(c)
    return JSONResponse({"existing": existing})


@router.post("/api/v1/banned_users/receive")
async def api_receive_banned_users(request: Request, db: Session = Depends(get_db)):
    if not _verify_api_token(request, db):
        raise HTTPException(401, "Unauthorized")
    body = await request.json()
    incoming = body.get("bans", [])
    added, removed = 0, 0
    for item in incoming:
        username = item.get("username", "").strip()
        email = item.get("email", "").strip()
        if not username or not email:
            continue
        if item.get("action") == "unban":
            bu = db.query(BannedUser).filter_by(username=username).first()
            if bu:
                db.delete(bu)
                removed += 1
        else:
            if not db.query(BannedUser).filter_by(username=username).first():
                db.add(BannedUser(
                    username=username,
                    email=email,
                    source_node=item.get("source_node", "remote"),
                ))
                added += 1
    db.commit()
    return JSONResponse({"ok": True, "added": added, "removed": removed})
