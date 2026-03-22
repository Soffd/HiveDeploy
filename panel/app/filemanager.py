"""
通过 Docker Python SDK 直接读写容器内文件，支持上传/下载
"""
import docker
import os
import tarfile
import io
from typing import List, Dict

SERVICE_CONFIG = {
    "astrbot": {
        "root": "/AstrBot",
        "shortcuts": {
            "📦 插件目录":  "/AstrBot/data/plugins",
            "⚙️ 配置目录":  "/AstrBot/data/config",
            "📁 数据目录":  "/AstrBot/data",
            "🐍 核心代码":  "/AstrBot/astrbot",
            "📋 日志":      "/AstrBot/data/logs",
            "🏠 根目录":    "/AstrBot",
        }
    },
    "napcat": {
        "root": "/app",
        "shortcuts": {
            "⚙️ NapCat配置": "/app/config",
            "📁 应用目录":   "/app",
            "🏠 QQ数据":     "/root/.config/QQ",
            "🌐 根目录":     "/",
        }
    }
}

TEXT_EXTS = {
    ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".py", ".js", ".ts", ".md", ".html", ".css",
    ".sh", ".bash", ".env", ".log", ".xml", ".csv", ".properties",
    ".rst", ".jsx", ".tsx", ".vue", ".sql",
}


def _get_container(username: str, service: str):
    client = docker.from_env()
    return client.containers.get(f"{service}_{username}")


def _exec(username: str, service: str, cmd: str) -> tuple:
    try:
        c = _get_container(username, service)
        rc, output = c.exec_run(cmd, demux=False)
        text = output.decode("utf-8", errors="replace") if output else ""
        return text, rc if rc is not None else 0
    except Exception as e:
        return str(e), 1


def list_dir(username: str, service: str, path: str) -> Dict:
    stdout, rc = _exec(username, service,
        f'sh -c \'ls -lA --time-style=+ "{path}" 2>&1\'')
    if rc != 0:
        return {"entries": [], "error": stdout}

    entries = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("total") or line.startswith("ls:"):
            continue
        parts = line.split(None, 8)
        if len(parts) < 2:
            continue
        perms = parts[0]
        name  = parts[-1].strip()
        if name in (".", ".."):
            continue
        if " -> " in name:
            name = name.split(" -> ")[0].strip()
        size    = parts[4] if len(parts) > 4 else "0"
        is_dir  = perms.startswith("d") or perms.startswith("l")
        full    = (path.rstrip("/") + "/" + name).replace("//", "/")
        entries.append({
            "name":     name,
            "is_dir":   is_dir,
            "size":     size,
            "perms":    perms,
            "full_path": full,
        })

    entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["name"].lower()))
    return {"entries": entries, "error": ""}


def read_file(username: str, service: str, path: str) -> Dict:
    size_out, _ = _exec(username, service, f'stat -c "%s" "{path}"')
    try:
        if int(size_out.strip()) > 512 * 1024:
            return {"content": "", "error": f"文件过大（{int(size_out.strip())//1024}KB），不支持在线编辑"}
    except Exception:
        pass

    try:
        c = _get_container(username, service)
        bits, _ = c.get_archive(path)
        buf = io.BytesIO()
        for chunk in bits:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            member = tar.getmembers()[0]
            f = tar.extractfile(member)
            content = f.read().decode("utf-8", errors="replace") if f else ""
        return {"content": content, "error": ""}
    except Exception as e:
        return {"content": "", "error": str(e)}


def write_file(username: str, service: str, path: str, content: str) -> Dict:
    try:
        c = _get_container(username, service)
        data = content.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=os.path.basename(path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        c.put_archive(os.path.dirname(path), buf)
        return {"error": ""}
    except Exception as e:
        return {"error": str(e)}


def download_file(username: str, service: str, path: str) -> Dict:
    """提取容器内文件，返回文件名和字节数据"""
    try:
        c = _get_container(username, service)
        bits, stat = c.get_archive(path)
        buf = io.BytesIO()
        for chunk in bits:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            member = tar.getmembers()[0]
            f = tar.extractfile(member)
            if f is None:
                return {"error": "无法读取文件（可能是目录）"}
            data = f.read()
        filename = os.path.basename(path)
        return {"filename": filename, "data": data, "error": ""}
    except Exception as e:
        return {"filename": "", "data": b"", "error": str(e)}


def upload_file(username: str, service: str, dest_dir: str, filename: str, data: bytes) -> Dict:
    """将文件上传到容器指定目录"""
    try:
        c = _get_container(username, service)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        c.put_archive(dest_dir, buf)
        return {"error": ""}
    except Exception as e:
        return {"error": str(e)}


def delete_path(username: str, service: str, path: str) -> Dict:
    out, rc = _exec(username, service, f'rm -rf "{path}"')
    return {"error": out if rc != 0 else ""}


def make_dir(username: str, service: str, path: str) -> Dict:
    out, rc = _exec(username, service, f'mkdir -p "{path}"')
    return {"error": out if rc != 0 else ""}


def path_exists(username: str, service: str, path: str) -> str:
    out, rc = _exec(username, service,
        f'sh -c \'[ -f "{path}" ] && echo file || ([ -d "{path}" ] && echo dir || echo notfound)\'')
    result = out.strip()
    return result if result in ("file", "dir") else "notfound"


def get_shortcuts(service: str) -> Dict:
    return SERVICE_CONFIG.get(service, {}).get("shortcuts", {})


def get_root(service: str) -> str:
    return SERVICE_CONFIG.get(service, {}).get("root", "/")


def is_text_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in TEXT_EXTS or "." not in filename


def move_path(username: str, service: str, src: str, dst: str) -> Dict:
    out, rc = _exec(username, service, f'mv "{src}" "{dst}"')
    return {"error": out if rc != 0 else ""}


def copy_path(username: str, service: str, src: str, dst: str) -> Dict:
    out, rc = _exec(username, service, f'cp -r "{src}" "{dst}"')
    return {"error": out if rc != 0 else ""}


def rename_path(username: str, service: str, src: str, dst: str) -> Dict:
    out, rc = _exec(username, service, f'mv "{src}" "{dst}"')
    return {"error": out if rc != 0 else ""}


def extract_archive(username: str, service: str, path: str, dest_dir: str) -> Dict:
    lpath = path.lower()
    if lpath.endswith(".zip"):
        # 用 Python zipfile，路径用 repr() 自动加引号
        py_code = (
            f"import zipfile,os;"
            f"os.makedirs({repr(dest_dir)},exist_ok=True);"
            f"zipfile.ZipFile({repr(path)}).extractall({repr(dest_dir)})"
        )
        out, rc = _exec(username, service, f"python3 -c {repr(py_code)} 2>&1")
        return {"error": out if rc != 0 else ""}
    elif lpath.endswith((".tar.gz", ".tgz")):
        out, rc = _exec(username, service, f"sh -c 'mkdir -p {repr(dest_dir)} && tar -xzf {repr(path)} -C {repr(dest_dir)} 2>&1'")
    elif lpath.endswith(".tar.bz2"):
        out, rc = _exec(username, service, f"sh -c 'mkdir -p {repr(dest_dir)} && tar -xjf {repr(path)} -C {repr(dest_dir)} 2>&1'")
    elif lpath.endswith(".tar"):
        out, rc = _exec(username, service, f"sh -c 'mkdir -p {repr(dest_dir)} && tar -xf {repr(path)} -C {repr(dest_dir)} 2>&1'")
    elif lpath.endswith(".gz"):
        out, rc = _exec(username, service, f"gunzip -kf {repr(path)} 2>&1")
    else:
        return {"error": "不支持的格式（支持 zip/tar.gz/tar.bz2/tar/gz）"}
    return {"error": out if rc != 0 else ""}


def compress_path(username: str, service: str, src: str, dest_zip: str) -> Dict:
    parent = "/".join(src.rstrip("/").split("/")[:-1]) or "/"
    name   = src.rstrip("/").split("/")[-1]
    out, rc = _exec(username, service,
        f'sh -c \'cd "{parent}" && zip -r "{dest_zip}" "{name}" 2>&1\'')
    return {"error": out if rc != 0 else ""}


def get_file_info(username: str, service: str, path: str) -> Dict:
    out, rc = _exec(username, service,
        f'sh -c \'stat -c "%s %F %Y" "{path}" 2>&1\'')
    if rc != 0:
        return {"error": out}
    parts = out.strip().split(None, 2)
    return {
        "size": int(parts[0]) if parts else 0,
        "type": parts[1] if len(parts) > 1 else "",
        "mtime": parts[2] if len(parts) > 2 else "",
        "error": "",
    }