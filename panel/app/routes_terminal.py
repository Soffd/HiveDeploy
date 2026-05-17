import json
import asyncio
import select
import logging

import docker as docker_lib
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi import Request, Depends, HTTPException
from jose import jwt, JWTError

from .bootstrap import app, templates
from .auth import get_current_user_from_cookie, SECRET_KEY, ALGORITHM
from .models import User
from .filemanager import get_terminal_root

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/terminal/{service}", response_class=HTMLResponse)
async def terminal_page(service: str, request: Request,
                        user: User = Depends(get_current_user_from_cookie)):
    if service not in ("astrbot", "napcat", "llonebot"):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "terminal.html",
        {"user": user, "service": service})


@router.websocket("/ws/terminal/{service}")
async def terminal_ws(websocket: WebSocket, service: str):
    token = websocket.cookies.get("access_token")
    if not token:
        await websocket.close(code=4001); return
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username: raise ValueError()
    except Exception:
        await websocket.close(code=4001); return

    if service not in ("astrbot", "napcat", "llonebot"):
        await websocket.close(code=4002); return

    await websocket.accept()
    client = docker_lib.from_env()
    container_name = f"{service}_{username}"

    try:
        container = client.containers.get(container_name)
    except docker_lib.errors.NotFound:
        await websocket.send_text("\r\n容器不存在\r\n")
        await websocket.close(); return

    start_dir = get_terminal_root(service)
    shell_bootstrap = (
        f'cd "{start_dir}" 2>/dev/null || cd /; '
        'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"; '
        'if command -v bash >/dev/null 2>&1; then exec bash -l; fi; '
        'if command -v ash >/dev/null 2>&1; then exec ash -l; fi; '
        'exec sh'
    )
    exec_id = client.api.exec_create(
        container.id, ["/bin/sh", "-lc", shell_bootstrap], stdin=True, tty=True,
        environment={
            "TERM": "xterm-256color",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        },
    )["Id"]
    exec_sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
    raw = exec_sock._sock
    raw.setblocking(False)

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
