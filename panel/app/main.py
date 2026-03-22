import os
import re
import json
import secrets
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, List

import psutil
from fastapi import (FastAPI, Request, Response, Depends, HTTPException,
                     status, Form, UploadFile, File, WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import engine, get_db, SessionLocal
from .models import User, Instance, SmtpConfig, ServerNode, SiteConfig
from .auth import (get_password_hash, authenticate_user, create_access_token,
                   get_current_user_from_cookie, verify_password, SECRET_KEY, ALGORITHM)
from .docker_manager import (
    create_user_instance_async, get_creation_progress,
    stop_user_instance, start_user_instance, restart_user_instance,
    delete_user_instance, get_instance_status, get_container_logs,
    get_all_instances_status, calc_ports, calc_extra_ports,
    pull_and_recreate, pull_and_recreate_single,
    get_pull_progress, get_single_pull_progress,
    create_single_service_async,
)
from .filemanager import (
    list_dir, read_file, write_file, delete_path, make_dir,
    get_shortcuts, get_root, is_text_file,
    download_file, upload_file,
    move_path, copy_path, rename_path,
    extract_archive, compress_path,
)
from .email_service import send_email, start_expiry_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── DB 初始化 ────────────────────────────────────────────────
def _run_migrations():
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        insp = inspect(engine)

        # users 表新列
        users_cols = [c["name"] for c in insp.get_columns("users")]
        if "expire_at" not in users_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN expire_at DATETIME"))
            conn.commit()

        # instances 表新列
        if insp.has_table("instances"):
            inst_cols = [c["name"] for c in insp.get_columns("instances")]
            if "extra_ports_json" not in inst_cols:
                conn.execute(text("ALTER TABLE instances ADD COLUMN extra_ports_json TEXT DEFAULT '[]'"))
                conn.commit()


from .models import Base
Base.metadata.create_all(bind=engine)
_run_migrations()

app = FastAPI(title="Bot Platform")
templates = Jinja2Templates(directory="/app/templates")

HOST = os.environ.get("PLATFORM_HOST", "localhost")

# ── 模板全局工具 ─────────────────────────────────────────────
def status_badge(s: str) -> str:
    return {"running": "success", "exited": "secondary", "not_found": "dark",
            "error": "danger", "creating": "warning"}.get(s, "secondary")

def user_expired(user) -> bool:
    if user.expire_at is None:
        return False
    return datetime.utcnow() > user.expire_at

def days_until_expire(user) -> Optional[int]:
    if user.expire_at is None:
        return None
    delta = user.expire_at - datetime.utcnow()
    return delta.days

def regex_match(value: str, pattern: str) -> bool:
    return bool(re.search(pattern, value, re.IGNORECASE))

templates.env.globals["status_badge"]       = status_badge
templates.env.globals["user_expired"]       = user_expired
templates.env.globals["days_until_expire"]  = days_until_expire
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

        # api_token
        cfg = db.query(SiteConfig).filter_by(key="api_token").first()
        if not cfg:
            db.add(SiteConfig(key="api_token", value=secrets.token_hex(32)))
            db.commit()
        # public_view_token (用于公开总览页)
        vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
        if not vcfg:
            db.add(SiteConfig(key="public_view_token", value=secrets.token_hex(16)))
            db.commit()
        # site_name
        scfg = db.query(SiteConfig).filter_by(key="site_name").first()
        if not scfg:
            db.add(SiteConfig(key="site_name", value="Bot Platform"))
            db.commit()
    finally:
        db.close()

_bootstrap()
start_expiry_scheduler(get_db)


# ════════════════════════════════════════════════════════════
#  认证
# ════════════════════════════════════════════════════════════
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                db: Session = Depends(get_db)):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "用户名或密码错误"}, status_code=401)
    # 登录时立即检查到期状态
    if user.expire_at and user.expire_at < datetime.utcnow():
        from .docker_manager import stop_user_instance
        try:
            stop_user_instance(user.username)
        except Exception:
            pass
    token = create_access_token({"sub": user.username})
    resp  = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, max_age=86400)
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html",
        {"request": request, "errors": [], "username": "", "email": ""})

@app.post("/register")
async def register(request: Request,
                   username: str = Form(...), email: str = Form(...),
                   password: str = Form(...), password2: str = Form(...),
                   db: Session = Depends(get_db)):
    errors = []
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
    if errors:
        return templates.TemplateResponse("register.html",
            {"request": request, "errors": errors, "username": username, "email": email})
    db.add(User(username=username, email=email,
                hashed_password=get_password_hash(password)))
    db.commit()
    return RedirectResponse("/login?registered=1", status_code=302)


# ════════════════════════════════════════════════════════════
#  Dashboard
# ════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(get_current_user_from_cookie),
                    db: Session = Depends(get_db)):
    instance = db.query(Instance).filter_by(user_id=user.id).first()
    container_status = get_instance_status(user.username) if instance else {}
    extra_ports = []
    if instance and instance.extra_ports_json:
        try:
            extra_ports = json.loads(instance.extra_ports_json)
        except Exception:
            pass
    available_extra = calc_extra_ports(user.id) if instance else []
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "instance": instance,
        "container_status": container_status, "host": HOST,
        "extra_ports": extra_ports, "available_extra_ports": available_extra,
    })


# ════════════════════════════════════════════════════════════
#  Profile
# ════════════════════════════════════════════════════════════
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: User = Depends(get_current_user_from_cookie)):
    return templates.TemplateResponse("profile.html",
        {"request": request, "user": user, "errors": []})

@app.post("/profile/password")
async def change_password(request: Request,
                          old_password: str = Form(...),
                          new_password: str = Form(...),
                          new_password2: str = Form(...),
                          user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    errors = []
    if not verify_password(old_password, user.hashed_password):
        errors.append("当前密码错误")
    if new_password != new_password2:
        errors.append("两次新密码不一致")
    if len(new_password) < 6:
        errors.append("新密码至少 6 位")
    if errors:
        return templates.TemplateResponse("profile.html",
            {"request": request, "user": user, "errors": errors})
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    resp = RedirectResponse("/login?pw_changed=1", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ════════════════════════════════════════════════════════════
#  实例管理 API
# ════════════════════════════════════════════════════════════
@app.post("/api/instance/create")
async def create_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    if db.query(Instance).filter_by(user_id=user.id).first():
        return JSONResponse({"error": "实例已存在"})

    ports = calc_ports(user.id)

    def _cb(result, error=None):
        _db = SessionLocal()
        try:
            inst = _db.query(Instance).filter_by(user_id=user.id).first()
            if result:
                inst.astrbot_container_id = result["astrbot_container_id"]
                inst.napcat_container_id  = result["napcat_container_id"]
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
        napcat_ws_port=ports["napcat_ws"],
        extra_ports_json="[]",
        status="creating",
    )
    db.add(inst); db.commit()
    create_user_instance_async(user.username, user.id, _cb)
    return JSONResponse({"ok": True})

@app.get("/api/instance/progress")
async def instance_progress(user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_creation_progress(user.username))

@app.post("/api/instance/start")
async def start_instance(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    start_user_instance(user.username)
    inst.status = "running"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/api/instance/stop")
async def stop_instance(user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    stop_user_instance(user.username)
    inst.status = "stopped"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/api/instance/restart")
async def restart_instance(user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    restart_user_instance(user.username)
    return RedirectResponse("/dashboard", status_code=302)

# 单服务启动/停止/重启
@app.post("/api/instance/start/{service}")
async def start_single(service: str,
                       user: User = Depends(get_current_user_from_cookie),
                       db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"): raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    start_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/api/instance/stop/{service}")
async def stop_single(service: str,
                      user: User = Depends(get_current_user_from_cookie),
                      db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"): raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    stop_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/api/instance/restart/{service}")
async def restart_single(service: str,
                         user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"): raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    restart_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)

# 单服务创建（无实例时也可用，自动建立实例记录）
@app.post("/api/instance/create/{service}")
async def create_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"): raise HTTPException(400)

    # 如果没有实例记录，先创建
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst:
        ports = calc_ports(user.id)
        inst = Instance(
            user_id=user.id,
            astrbot_port=ports["astrbot_web"],
            napcat_web_port=ports["napcat_web"],
            napcat_ws_port=ports["napcat_ws"],
            extra_ports_json="[]",
            status="creating",
        )
        db.add(inst); db.commit()

    extra_ports = json.loads(inst.extra_ports_json or "[]")
    create_single_service_async(user.username, user.id, service, extra_ports)
    return JSONResponse({"ok": True})

@app.post("/api/instance/delete/{service}")
async def delete_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"): raise HTTPException(400)
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    delete_user_instance(user.username, service)
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/api/instance/delete")
async def delete_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: raise HTTPException(404)
    delete_user_instance(user.username)
    db.delete(inst); db.commit()
    return RedirectResponse("/dashboard", status_code=302)

# 全量更新
@app.post("/api/instance/update")
async def update_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"error": "实例不存在"})
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    pull_and_recreate(user.username, user.id, extra_ports)
    return JSONResponse({"ok": True})

@app.get("/api/instance/update_progress")
async def update_progress(user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_pull_progress(user.username))

# 单服务更新
@app.post("/api/instance/update/{service}")
async def update_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db)):
    if service not in ("astrbot", "napcat"):
        raise HTTPException(400, "无效服务")
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"error": "实例不存在"})
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    pull_and_recreate_single(user.username, user.id, service, extra_ports)
    return JSONResponse({"ok": True})

@app.get("/api/instance/update_progress/{service}")
async def update_single_progress(service: str,
                                  user: User = Depends(get_current_user_from_cookie)):
    return JSONResponse(get_single_pull_progress(user.username, service))

# 弹性端口
@app.get("/api/instance/extra_ports")
async def get_extra_ports(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"mappings": [], "available": []})
    mappings = json.loads(inst.extra_ports_json or "[]")
    used = {m["host_port"] for m in mappings}
    available = [p for p in calc_extra_ports(user.id) if p not in used]
    return JSONResponse({"mappings": mappings, "available": available})

@app.post("/api/instance/extra_ports")
async def save_extra_ports(request: Request,
                           user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db)):
    inst = db.query(Instance).filter_by(user_id=user.id).first()
    if not inst: return JSONResponse({"error": "实例不存在"})
    body = await request.json()
    mappings = body.get("mappings", [])
    allowed  = set(calc_extra_ports(user.id))
    for m in mappings:
        if m.get("host_port") not in allowed:
            return JSONResponse({"error": f"端口 {m.get('host_port')} 不在允许范围内"})
    inst.extra_ports_json = json.dumps(mappings)
    db.commit()
    # 重建容器使新端口生效
    pull_and_recreate(user.username, user.id, mappings)
    return JSONResponse({"ok": True})


# ════════════════════════════════════════════════════════════
#  日志
# ════════════════════════════════════════════════════════════
@app.get("/api/instance/napcat_token")
async def get_napcat_token(user: User = Depends(get_current_user_from_cookie)):
    """提取 NapCat WebUI Token，通过 Docker SDK 获取全量日志"""
    import re as _re
    import docker as _docker
    try:
        client = _docker.from_env()
        container = client.containers.get(f"napcat_{user.username}")
        # 不传 tail 参数 = 全量日志，stream=False 直接返回 bytes
        logs = container.logs(stream=False, timestamps=False)
        text = logs.decode("utf-8", errors="replace")
        matches = _re.findall(r'WebUi Token:\s*([a-f0-9]+)', text, _re.IGNORECASE)
        if matches:
            return JSONResponse({"token": matches[-1]})
    except Exception as e:
        logger.error(f"获取 NapCat token 失败: {e}")
    return JSONResponse({"token": None})


@app.get("/logs/{service}", response_class=HTMLResponse)
async def view_logs(service: str, request: Request,
                    user: User = Depends(get_current_user_from_cookie)):
    if service not in ("astrbot", "napcat"):
        raise HTTPException(404)
    logs = get_container_logs(user.username, service, 200)
    return templates.TemplateResponse("logs.html",
        {"request": request, "user": user, "service": service, "logs": logs})


# ════════════════════════════════════════════════════════════
#  Web 终端
# ════════════════════════════════════════════════════════════
@app.get("/terminal/{service}", response_class=HTMLResponse)
async def terminal_page(service: str, request: Request,
                        user: User = Depends(get_current_user_from_cookie)):
    if service not in ("astrbot", "napcat"):
        raise HTTPException(404)
    return templates.TemplateResponse("terminal.html",
        {"request": request, "user": user, "service": service})

@app.websocket("/ws/terminal/{service}")
async def terminal_ws(websocket: WebSocket, service: str):
    import docker as docker_lib
    from jose import jwt, JWTError

    # JWT 鉴权（从 cookie）
    token = websocket.cookies.get("access_token")
    if not token:
        await websocket.close(code=4001); return
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username: raise ValueError()
    except Exception:
        await websocket.close(code=4001); return

    if service not in ("astrbot", "napcat"):
        await websocket.close(code=4002); return

    await websocket.accept()
    client = docker_lib.from_env()
    container_name = f"{service}_{username}"

    try:
        container = client.containers.get(container_name)
    except docker_lib.errors.NotFound:
        await websocket.send_text("\r\n容器不存在\r\n")
        await websocket.close(); return

    exec_id = client.api.exec_create(
        container.id, ["/bin/sh"], stdin=True, tty=True,
        environment={"TERM": "xterm-256color"},
    )["Id"]
    exec_sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
    raw = exec_sock._sock
    raw.setblocking(False)

    import asyncio, select

    loop = asyncio.get_event_loop()

    async def _read_docker():
        while True:
            try:
                rlist, _, _ = await loop.run_in_executor(None, select.select, [raw], [], [], 0.05)
                if rlist:
                    data = await loop.run_in_executor(None, raw.recv, 4096)
                    if not data: break
                    await websocket.send_bytes(data)
            except Exception:
                break

    async def _read_ws():
        while True:
            try:
                msg = await websocket.receive()
                if "bytes" in msg:
                    await loop.run_in_executor(None, raw.sendall, msg["bytes"])
                elif "text" in msg:
                    data = json.loads(msg["text"])
                    if data.get("type") == "resize":
                        client.api.exec_resize(exec_id, height=data["rows"], width=data["cols"])
            except WebSocketDisconnect:
                break
            except Exception:
                break

    try:
        await asyncio.gather(_read_docker(), _read_ws())
    finally:
        try: raw.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════
#  文件管理
# ════════════════════════════════════════════════════════════
@app.get("/files/{service}", response_class=HTMLResponse)
async def files_page(service: str, request: Request, path: str = None,
                     saved: bool = False,
                     user: User = Depends(get_current_user_from_cookie)):
    if service not in ("astrbot", "napcat"):
        raise HTTPException(404)
    root      = get_root(service)
    path      = path or root
    shortcuts = get_shortcuts(service)
    astrbot_root = get_root("astrbot")
    napcat_root  = get_root("napcat")

    error_msg = ""
    is_file   = False
    entries   = []
    file_content = ""
    filename  = ""
    parent_path = None

    ptype = "dir"
    try:
        from .filemanager import path_exists
        ptype = path_exists(user.username, service, path)
    except Exception:
        pass

    if ptype == "file":
        is_file  = True
        filename = path.split("/")[-1]
        parent_path = "/".join(path.split("/")[:-1]) or "/"
        if is_text_file(filename):
            result = read_file(user.username, service, path)
            file_content = result["content"]
            error_msg    = result["error"]
        else:
            error_msg = "该文件为二进制文件，无法在线编辑。请下载后修改。"
    else:
        parent_path = "/".join(path.split("/")[:-1]) or None
        if path == "/" or path == root:
            parent_path = None
        result  = list_dir(user.username, service, path)
        entries = result["entries"]
        error_msg = result["error"]

    return templates.TemplateResponse("files.html", {
        "request": request, "user": user, "service": service,
        "current_path": path, "parent_path": parent_path,
        "shortcuts": shortcuts, "entries": entries, "is_file": is_file,
        "file_content": file_content, "filename": filename,
        "error_msg": error_msg, "saved": saved,
        "astrbot_root": astrbot_root, "napcat_root": napcat_root,
    })

@app.post("/files/{service}/save")
async def save_file(service: str, path: str = Form(...), content: str = Form(...),
                    user: User = Depends(get_current_user_from_cookie)):
    result = write_file(user.username, service, path, content)
    if result["error"]:
        return RedirectResponse(f"/files/{service}?path={path}&error={result['error']}", 302)
    return RedirectResponse(f"/files/{service}?path={path}&saved=1", 302)

@app.post("/files/{service}/delete")
async def delete_file(service: str, path: str = Form(...),
                      return_path: str = Form("/"),
                      user: User = Depends(get_current_user_from_cookie)):
    delete_path(user.username, service, path)
    return RedirectResponse(f"/files/{service}?path={return_path}", 302)

@app.post("/files/{service}/mkdir")
async def mkdir(service: str, base_path: str = Form(...),
                dirname: str = Form(...), return_path: str = Form("/"),
                user: User = Depends(get_current_user_from_cookie)):
    new_path = (base_path.rstrip("/") + "/" + dirname).replace("//", "/")
    make_dir(user.username, service, new_path)
    return RedirectResponse(f"/files/{service}?path={return_path}", 302)

@app.get("/files/{service}/download")
async def download(service: str, path: str,
                   user: User = Depends(get_current_user_from_cookie)):
    result = download_file(user.username, service, path)
    if result["error"]:
        raise HTTPException(400, result["error"])
    filename = result["filename"]
    data     = result["data"]
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/files/{service}/upload")
async def upload(service: str, upload_path: str = Form(...),
                 file: UploadFile = File(...),
                 user: User = Depends(get_current_user_from_cookie)):
    data   = await file.read()
    result = upload_file(user.username, service, upload_path, file.filename, data)
    if result["error"]:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return JSONResponse({"ok": True, "filename": file.filename})


@app.post("/files/{service}/move")
async def move_file(service: str, request: Request,
                    user: User = Depends(get_current_user_from_cookie)):
    body = await request.json()
    result = move_path(user.username, service, body["src"], body["dst"])
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@app.post("/files/{service}/copy")
async def copy_file(service: str, request: Request,
                    user: User = Depends(get_current_user_from_cookie)):
    body = await request.json()
    result = copy_path(user.username, service, body["src"], body["dst"])
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@app.post("/files/{service}/rename")
async def rename_file(service: str, src: str = Form(...), dst: str = Form(...),
                      user: User = Depends(get_current_user_from_cookie)):
    result = rename_path(user.username, service, src, dst)
    if result["error"]:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/files/{service}/extract")
async def extract_file(service: str, path: str = Form(...),
                       dest_dir: str = Form(...),
                       user: User = Depends(get_current_user_from_cookie)):
    result = extract_archive(user.username, service, path, dest_dir)
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@app.post("/files/{service}/compress")
async def compress_file(service: str, src: str = Form(...),
                        dest_zip: str = Form(...),
                        user: User = Depends(get_current_user_from_cookie)):
    result = compress_path(user.username, service, src, dest_zip)
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@app.get("/files/{service}/preview")
async def preview_file(service: str, path: str,
                       user: User = Depends(get_current_user_from_cookie)):
    """流式预览文件（图片/视频/音频）"""
    result = download_file(user.username, service, path)
    if result["error"]:
        raise HTTPException(400, result["error"])
    filename = result["filename"].lower()
    if any(filename.endswith(e) for e in (".jpg",".jpeg",".png",".gif",".webp",".svg",".bmp")):
        media_type = "image/" + (filename.rsplit(".",1)[-1].replace("jpg","jpeg"))
    elif any(filename.endswith(e) for e in (".mp4",".webm",".ogg",".mov")):
        media_type = "video/" + filename.rsplit(".",1)[-1]
    elif any(filename.endswith(e) for e in (".mp3",".wav",".ogg",".flac",".aac",".m4a")):
        media_type = "audio/" + filename.rsplit(".",1)[-1]
    else:
        media_type = "application/octet-stream"
    return StreamingResponse(
        iter([result["data"]]),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{result["filename"]}"'},
    )



@app.get("/api/server/stats")
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
#  管理后台
# ════════════════════════════════════════════════════════════
def require_admin(user: User = Depends(get_current_user_from_cookie)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="权限不足")
    return user

@app.get("/admin", response_class=HTMLResponse)
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
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user,
        "users": users, "instance_data": instance_data, "host": HOST,
        "astrbot_running": astrbot_running, "napcat_running": napcat_running,
    })

@app.post("/admin/user/create")
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
    expire_at = datetime.utcnow() + timedelta(days=_expire_days) if _expire_days else None
    db.add(User(
        username=username, email=email,
        hashed_password=get_password_hash(password),
        is_admin=_is_admin, expire_at=expire_at,
    ))
    db.commit()
    return RedirectResponse("/admin?created=1", 302)

@app.post("/admin/user/{user_id}/toggle")
async def admin_toggle_user(user_id: int, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        u.is_active = not u.is_active; db.commit()
    return RedirectResponse("/admin", 302)

@app.post("/admin/user/{user_id}/reset_password")
async def admin_reset_password(user_id: int, user: User = Depends(require_admin),
                               db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u:
        u.hashed_password = get_password_hash("123456"); db.commit()
    return RedirectResponse("/admin?reset=1", 302)

@app.post("/admin/user/{user_id}/delete")
async def admin_delete_user(user_id: int, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and not u.is_admin:
        delete_user_instance(u.username)
        if u.instance: db.delete(u.instance)
        db.delete(u); db.commit()
    return RedirectResponse("/admin", 302)

@app.post("/admin/user/{user_id}/set_expire")
async def admin_set_expire(user_id: int, request: Request,
                           user: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    body = await request.json()
    u = db.query(User).filter_by(id=user_id).first()
    if not u: raise HTTPException(404)

    # 必须在修改前记录旧状态
    was_expired = u.expire_at is not None and u.expire_at < datetime.utcnow()

    action = body.get("action")
    if action == "clear":
        u.expire_at = None
    elif action == "set":
        date_str = body.get("date")
        u.expire_at = datetime.strptime(date_str, "%Y-%m-%d") if date_str else None
    elif action in ("add30", "add90", "add365"):
        days = int(action[3:])
        base = max(u.expire_at, datetime.utcnow()) if u.expire_at else datetime.utcnow()
        u.expire_at = base + timedelta(days=days)
    db.commit()

    # 续期后（之前已到期，现在未到期）→ 重启容器
    now_expired = u.expire_at is not None and u.expire_at < datetime.utcnow()
    if was_expired and not now_expired:
        try:
            start_user_instance(u.username)
            logger.info(f"续期后重启容器: {u.username}")
        except Exception as e:
            logger.error(f"续期重启容器失败 {u.username}: {e}")
    # 手动设置为已过期 → 立即停止容器
    elif not was_expired and now_expired:
        try:
            stop_user_instance(u.username)
            logger.info(f"设置到期后停止容器: {u.username}")
        except Exception as e:
            logger.error(f"设置到期停止容器失败 {u.username}: {e}")

    return JSONResponse({"ok": True, "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None})


@app.post("/admin/run_expiry_check")
async def admin_run_expiry_check(user: User = Depends(require_admin),
                                  db: Session = Depends(get_db)):
    """手动触发到期检查（调试用）"""
    from .email_service import check_and_enforce_expiry
    now = datetime.utcnow()
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


@app.post("/admin/instance/{user_id}/restart")
async def admin_restart_instance(user_id: int, user: User = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user_id).first()
    if u and u.instance: restart_user_instance(u.username)
    return RedirectResponse("/admin", 302)


# ════════════════════════════════════════════════════════════
#  SMTP 设置
# ════════════════════════════════════════════════════════════
@app.get("/admin/smtp", response_class=HTMLResponse)
async def smtp_page(request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg:
        cfg = SmtpConfig(id=1); db.add(cfg); db.commit()
    return templates.TemplateResponse("smtp.html",
        {"request": request, "user": user, "cfg": cfg})

@app.post("/admin/smtp")
async def smtp_save(
    host: str = Form(""), port: int = Form(465),
    username: str = Form(""), password: str = Form(""),
    from_email: str = Form(""), from_name: str = Form("Bot Platform"),
    use_tls: bool = Form(False), enabled: bool = Form(False),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg:
        cfg = SmtpConfig(id=1); db.add(cfg)
    cfg.host=host; cfg.port=port; cfg.username=username
    cfg.password=password; cfg.from_email=from_email
    cfg.from_name=from_name; cfg.use_tls=use_tls; cfg.enabled=enabled
    db.commit()
    return RedirectResponse("/admin/smtp?saved=1", 302)

@app.post("/admin/smtp/test")
async def smtp_test(to_email: str = Form(...),
                    user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    cfg = db.query(SmtpConfig).filter_by(id=1).first()
    if not cfg or not cfg.enabled:
        return JSONResponse({"error": "SMTP 未启用"})
    ok = send_email(to_email, "Bot Platform 测试邮件",
        "<h2>✅ SMTP 配置正常</h2><p>如果你收到这封邮件，说明 SMTP 配置已成功。</p>", cfg)
    return JSONResponse({"ok": ok, "error": "" if ok else "发送失败，请检查配置"})


# ════════════════════════════════════════════════════════════
#  节点管理 + 总览
# ════════════════════════════════════════════════════════════
@app.get("/admin/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    nodes     = db.query(ServerNode).order_by(ServerNode.id).all()
    api_token = db.query(SiteConfig).filter_by(key="api_token").first()
    local_url = f"https://{HOST}"
    return templates.TemplateResponse("nodes.html", {
        "request": request, "user": user, "nodes": nodes,
        "api_token": api_token.value if api_token else "",
        "local_url": local_url,
    })

@app.post("/admin/nodes")
async def node_add(name: str = Form(...), url: str = Form(...),
                   api_token: str = Form(...),
                   user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    db.add(ServerNode(name=name, url=url.rstrip("/"), api_token=api_token))
    db.commit()
    return RedirectResponse("/admin/nodes?added=1", 302)

@app.post("/admin/nodes/{node_id}/delete")
async def node_delete(node_id: int, user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    n = db.query(ServerNode).filter_by(id=node_id).first()
    if n: db.delete(n); db.commit()
    return RedirectResponse("/admin/nodes", 302)

@app.post("/admin/nodes/regen_token")
async def regen_token(user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if cfg:
        cfg.value = secrets.token_hex(32)
    else:
        db.add(SiteConfig(key="api_token", value=secrets.token_hex(32)))
    db.commit()
    return RedirectResponse("/admin/nodes?regen=1", 302)

@app.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse("overview.html",
        {"request": request, "user": user})

@app.get("/api/aggregate_nodes")
async def aggregate_nodes(user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """汇聚本地 + 所有远程节点数据"""
    results = []

    # 本地
    local_users = db.query(User).filter_by(is_active=True).count()
    local_inst  = db.query(Instance).count()
    all_stat    = get_all_instances_status()
    instances   = []
    for u in db.query(User).filter(User.instance != None).all():
        if u.instance:
            st = all_stat.get(u.username, {})
            instances.append({
                "username": u.username,
                "astrbot": st.get("astrbot", "unknown"),
                "napcat":  st.get("napcat",  "unknown"),
                "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None,
            })
    mem  = psutil.virtual_memory()
    cpu  = psutil.cpu_percent(interval=0.3)
    results.append({
        "name": os.environ.get("SITE_NAME", "本服务器"),
        "url":  f"https://{HOST}",
        "user_count": local_users,
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
                "name":           node.name,
                "url":            node.url,
                "user_count":     data.get("user_count", 0),
                "instance_count": data.get("instance_count", 0),
                "cpu_percent":    data.get("cpu_percent", 0),
                "mem_percent":    data.get("mem_percent", 0),
                "instances":      data.get("instances", []),
                "error":          None,
            })
        except Exception as e:
            results.append({
                "name": node.name, "url": node.url,
                "user_count": 0, "instance_count": 0,
                "cpu_percent": 0, "mem_percent": 0,
                "instances": [], "error": str(e),
            })

    return JSONResponse(results)


# ── 对外状态接口 ─────────────────────────────────────────────

@app.get("/api/v1/status")
async def api_v1_status(request: Request, db: Session = Depends(get_db)):
    """节点间互调的状态接口，Bearer Token 鉴权"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = auth[7:]
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if not cfg or cfg.value != token:
        raise HTTPException(401, "Invalid token")

    users    = db.query(User).filter_by(is_active=True).count()
    inst_cnt = db.query(Instance).count()
    all_stat = get_all_instances_status()
    instances = []
    for u in db.query(User).all():
        if u.instance:
            st = all_stat.get(u.username, {})
            instances.append({
                "username": u.username,
                "astrbot":  st.get("astrbot", "unknown"),
                "napcat":   st.get("napcat",  "unknown"),
                "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None,
            })
    mem = psutil.virtual_memory()
    return JSONResponse({
        "server_name":    os.environ.get("SITE_NAME", HOST),
        "user_count":     users,
        "instance_count": inst_cnt,
        "cpu_percent":    psutil.cpu_percent(interval=0.3),
        "mem_percent":    mem.percent,
        "mem_used":       mem.used,
        "mem_total":      mem.total,
        "instances":      instances,
    })


@app.get("/status", response_class=HTMLResponse)
async def public_status_page(request: Request, token: str = "",
                              db: Session = Depends(get_db)):
    """公开总览页面，需要 view token"""
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    valid = vcfg and token == vcfg.value
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    site_name = scfg.value if scfg else "Bot Platform"
    return templates.TemplateResponse("public_status.html", {
        "request": request,
        "valid": valid,
        "token": token,
        "site_name": site_name,
    })


@app.get("/api/public/stats")
async def public_stats_api(token: str = "", db: Session = Depends(get_db)):
    """公开聚合数据接口，需要 view token"""
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    if not vcfg or token != vcfg.value:
        raise HTTPException(401, "Invalid token")

    results = []
    # 本地数据
    local_users = db.query(User).filter_by(is_active=True).count()
    local_inst  = db.query(Instance).count()
    all_stat    = get_all_instances_status()
    running_ab  = sum(1 for s in all_stat.values() if s.get("astrbot") == "running")
    running_nc  = sum(1 for s in all_stat.values() if s.get("napcat") == "running")
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    results.append({
        "name":            scfg.value if scfg else os.environ.get("SITE_NAME", "本服务器"),
        "url":             f"https://{HOST}",
        "user_count":      local_users,
        "instance_count":  local_inst,
        "running_astrbot": running_ab,
        "running_napcat":  running_nc,
        "cpu_percent":     psutil.cpu_percent(interval=0.3),
        "mem_percent":     mem.percent,
        "mem_used":        mem.used,
        "mem_total":       mem.total,
        "disk_percent":    disk.percent,
        "disk_used":       disk.used,
        "disk_total":      disk.total,
        "error":           None,
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
                "name":            node.name,
                "url":             node.url,
                "user_count":      data.get("user_count", 0),
                "instance_count":  data.get("instance_count", 0),
                "running_astrbot": sum(1 for i in data.get("instances",[]) if i.get("astrbot")=="running"),
                "running_napcat":  sum(1 for i in data.get("instances",[]) if i.get("napcat")=="running"),
                "cpu_percent":     data.get("cpu_percent", 0),
                "mem_percent":     data.get("mem_percent", 0),
                "mem_used":        0, "mem_total":  0,
                "disk_percent":    0, "disk_used":  0, "disk_total": 0,
                "error":           None,
            })
        except Exception as e:
            results.append({
                "name": node.name, "url": node.url,
                "user_count": 0, "instance_count": 0,
                "running_astrbot": 0, "running_napcat": 0,
                "cpu_percent": 0, "mem_percent": 0,
                "mem_used": 0, "mem_total": 0,
                "disk_percent": 0, "disk_used": 0, "disk_total": 0,
                "error": str(e),
            })

    total_users = sum(n["user_count"] for n in results)
    total_inst  = sum(n["instance_count"] for n in results)
    total_ab    = sum(n["running_astrbot"] for n in results)
    total_nc    = sum(n["running_napcat"] for n in results)

    return JSONResponse({
        "nodes":       results,
        "total_users": total_users,
        "total_inst":  total_inst,
        "total_ab":    total_ab,
        "total_nc":    total_nc,
    })


# 管理员设置公开总览
@app.get("/admin/public_status", response_class=HTMLResponse)
async def admin_public_status_page(request: Request, user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    vcfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    scfg = db.query(SiteConfig).filter_by(key="site_name").first()
    return templates.TemplateResponse("admin_public_status.html", {
        "request": request, "user": user,
        "view_token": vcfg.value if vcfg else "",
        "site_name": scfg.value if scfg else "Bot Platform",
        "host": HOST,
    })

@app.post("/admin/public_status")
async def admin_public_status_save(
    site_name: str = Form("Bot Platform"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    for key, val in [("site_name", site_name)]:
        cfg = db.query(SiteConfig).filter_by(key=key).first()
        if cfg: cfg.value = val
        else: db.add(SiteConfig(key=key, value=val))
    db.commit()
    return RedirectResponse("/admin/public_status?saved=1", 302)

@app.post("/admin/public_status/regen_token")
async def regen_view_token(user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    cfg = db.query(SiteConfig).filter_by(key="public_view_token").first()
    new_token = secrets.token_hex(16)
    if cfg: cfg.value = new_token
    else: db.add(SiteConfig(key="public_view_token", value=new_token))
    db.commit()
    return RedirectResponse("/admin/public_status?regen=1", 302)


async def public_status(request: Request, db: Session = Depends(get_db)):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = auth_header[7:]
    cfg = db.query(SiteConfig).filter_by(key="api_token").first()
    if not cfg or cfg.value != token:
        raise HTTPException(401, "Invalid token")

    users     = db.query(User).filter_by(is_active=True).count()
    inst_cnt  = db.query(Instance).count()
    all_stat  = get_all_instances_status()
    instances = []
    for u in db.query(User).filter(User.instance != None).all():
        if u.instance:
            st = all_stat.get(u.username, {})
            instances.append({
                "username": u.username,
                "astrbot": st.get("astrbot", "unknown"),
                "napcat":  st.get("napcat",  "unknown"),
                "expire_at": u.expire_at.strftime("%Y-%m-%d") if u.expire_at else None,
            })
    mem = psutil.virtual_memory()
    return JSONResponse({
        "server_name":    os.environ.get("SITE_NAME", HOST),
        "user_count":     users,
        "instance_count": inst_cnt,
        "cpu_percent":    psutil.cpu_percent(interval=0.3),
        "mem_percent":    mem.percent,
        "instances":      instances,
    })