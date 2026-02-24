"""
图像监控服务端 - 提供 API 供客户端检查图像更新并获取图像
需要 API Key 鉴权，支持多图像监控

时间策略：通过比较 MD5 检测图像内容是否真正变化，
变化时记录当前时刻作为"最后更换时间"，而非使用文件系统时间。
"""

import os
import sys
import hashlib
import threading
import time
from datetime import datetime, timezone

import yaml
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

# ───────── 加载配置 ─────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_config.yaml")

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

config = load_config()

HOST: str = config.get("host", "0.0.0.0")
PORT: int = config.get("port", 8120)
API_KEYS: list[str] = config.get("api_keys", [])
CHECK_INTERVAL: float = config.get("check_interval", 5.0)
SESSION_TIMEOUT: float = config.get("session_timeout", 30.0)

# 解析图像列表
IMAGES_CFG: list[dict] = config.get("images", [])
if not IMAGES_CFG:
    print("警告: 配置文件中没有定义任何图像 (images)")

# ───────── FastAPI 应用 ─────────
app = FastAPI(title="图像监控 API", version="1.0.0")


# ───────── 会话管理：每个 Key 只允许一个活跃客户端 ─────────
_active_sessions: dict[str, dict] = {}
_session_lock = threading.Lock()


def verify_api_key(
    x_api_key: str | None,
    client_id: str = "",
    client_ip: str = "",
):
    """校验 API Key 并通过 client_id 检查是否有其他客户端正在使用该 Key"""
    if not x_api_key or x_api_key not in API_KEYS:
        raise HTTPException(status_code=403, detail="无效的 API Key 或未提供 API Key")

    if not client_id:
        raise HTTPException(status_code=400, detail="缺少 X-Client-Id 请求头")

    now = time.time()
    with _session_lock:
        session = _active_sessions.get(x_api_key)
        if session is not None:
            elapsed = now - session["last_active"]
            if session["client_id"] == client_id:
                session["last_active"] = now
                return
            if elapsed > SESSION_TIMEOUT:
                _active_sessions[x_api_key] = {
                    "client_id": client_id, "ip": client_ip, "last_active": now,
                }
                return
            raise HTTPException(
                status_code=409,
                detail=f"该 API Key 已被另一个客户端占用"
                       f"（ID: {session['client_id'][:8]}…, IP: {session['ip']}），"
                       f"请等待 {int(SESSION_TIMEOUT - elapsed)} 秒后重试或使用其他 Key",
            )
        _active_sessions[x_api_key] = {
            "client_id": client_id, "ip": client_ip, "last_active": now,
        }


def _file_md5(path: str) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ───────── 图像变更追踪器 ─────────
class ImageTracker:
    """
    后台线程定期检查图像文件的 MD5，
    当内容发生变化时记录当前时刻为 '最后更换时间'。
    """

    def __init__(self, name: str, image_path: str, interval: float = 5.0):
        self.name = name
        self._path = image_path
        self._interval = interval
        self._lock = threading.Lock()

        self._md5: str | None = None
        self._size: int = 0
        self._exists: bool = False
        self._last_changed: str | None = None

        self._refresh()

        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def _refresh(self):
        if not os.path.isfile(self._path):
            with self._lock:
                self._exists = False
                self._md5 = None
                self._size = 0
            return
        try:
            md5 = _file_md5(self._path)
            size = os.path.getsize(self._path)
        except OSError:
            return
        with self._lock:
            self._exists = True
            self._size = size
            if md5 != self._md5:
                self._md5 = md5
                self._last_changed = datetime.now(timezone.utc).isoformat()

    def _watch_loop(self):
        while True:
            time.sleep(self._interval)
            self._refresh()

    def get_info(self) -> dict:
        with self._lock:
            if not self._exists:
                return {"name": self.name, "exists": False}
            return {
                "name": self.name,
                "exists": True,
                "last_modified": self._last_changed,
                "size": self._size,
                "md5": self._md5,
            }

    @property
    def path(self) -> str:
        return self._path


# ───────── 创建所有图像的追踪器 ─────────
# name -> ImageTracker
trackers: dict[str, ImageTracker] = {}
for img_cfg in IMAGES_CFG:
    name = img_cfg.get("name", "")
    path = img_cfg.get("path", "")
    if name and path:
        trackers[name] = ImageTracker(name, path, CHECK_INTERVAL)


def _get_tracker(name: str) -> ImageTracker:
    """根据名称获取追踪器，不存在则 404"""
    t = trackers.get(name)
    if t is None:
        raise HTTPException(status_code=404, detail=f"图像 '{name}' 不存在")
    return t


def _auth(request: Request, x_api_key: str | None, x_client_id: str | None):
    """统一鉴权"""
    verify_api_key(
        x_api_key,
        client_id=x_client_id or "",
        client_ip=request.client.host if request.client else "",
    )


# ───────── 路由 ─────────

@app.get("/allsky/")
def root():
    return {"message": "图像监控 API 正在运行", "image_count": len(trackers)}


@app.get("/allsky/api/images")
def list_images(
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """返回所有图像的名称列表"""
    _auth(request, x_api_key, x_client_id)
    names = list(trackers.keys())
    return JSONResponse(content={"images": names})


@app.get("/allsky/api/images/status")
def all_images_status(
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """返回所有图像的状态（批量查询）"""
    _auth(request, x_api_key, x_client_id)
    result = [t.get_info() for t in trackers.values()]
    return JSONResponse(content={"images": result})


@app.get("/allsky/api/image/{name}/status")
def image_status(
    name: str,
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """检查指定图像的状态"""
    _auth(request, x_api_key, x_client_id)
    t = _get_tracker(name)
    return JSONResponse(content=t.get_info())


@app.get("/allsky/api/image/{name}/data")
def image_data(
    name: str,
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """返回指定图像的二进制数据"""
    _auth(request, x_api_key, x_client_id)
    t = _get_tracker(name)

    if not os.path.isfile(t.path):
        raise HTTPException(status_code=404, detail="图像文件不存在")

    with open(t.path, "rb") as f:
        data = f.read()

    ext = os.path.splitext(t.path)[1].lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }
    content_type = mime_map.get(ext, "application/octet-stream")

    return Response(content=data, media_type=content_type)


# ───────── 入口 ─────────
if __name__ == "__main__":
    print(f"监控图像数量: {len(trackers)}")
    for n, t in trackers.items():
        print(f"  [{n}] -> {t.path}")
    print(f"允许的 API Key 数量: {len(API_KEYS)}")
    print(f"启动服务: {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
