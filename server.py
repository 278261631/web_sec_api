"""
图像监控服务端 - 提供 API 供客户端检查图像更新并获取图像
需要 API Key 鉴权

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

IMAGE_PATH: str = config.get("image_path", "D:/github/test_img/test.png")
HOST: str = config.get("host", "0.0.0.0")
PORT: int = config.get("port", 8120)
API_KEYS: list[str] = config.get("api_keys", [])
# 后台检测间隔（秒）
CHECK_INTERVAL: float = config.get("check_interval", 2.0)
# 客户端活跃超时时间（秒），超过此时间未活动视为离线，其他客户端可接管该 Key
SESSION_TIMEOUT: float = config.get("session_timeout", 30.0)

# ───────── FastAPI 应用 ─────────
app = FastAPI(title="图像监控 API", version="1.0.0")


# ───────── 会话管理：每个 Key 只允许一个活跃客户端 ─────────
# key -> {"client_id": 客户端唯一ID, "ip": 来源IP, "last_active": 最后活动时间戳}
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
            # 同一个 client_id 视为同一客户端，直接刷新活跃时间
            if session["client_id"] == client_id:
                session["last_active"] = now
                return
            # 不同 client_id 但上一个客户端已超时，允许新客户端接管
            if elapsed > SESSION_TIMEOUT:
                _active_sessions[x_api_key] = {
                    "client_id": client_id, "ip": client_ip, "last_active": now,
                }
                return
            # 不同 client_id 且上一个客户端仍活跃，拒绝
            raise HTTPException(
                status_code=409,
                detail=f"该 API Key 已被另一个客户端占用"
                       f"（ID: {session['client_id'][:8]}…, IP: {session['ip']}），"
                       f"请等待 {int(SESSION_TIMEOUT - elapsed)} 秒后重试或使用其他 Key",
            )
        # 首次使用该 Key
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

    def __init__(self, image_path: str, interval: float = 2.0):
        self._path = image_path
        self._interval = interval
        self._lock = threading.Lock()

        self._md5: str | None = None
        self._size: int = 0
        self._exists: bool = False
        # 实际检测到内容变化的时间
        self._last_changed: str | None = None

        # 初始化一次快照
        self._refresh()

        # 启动后台守护线程
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def _refresh(self):
        """读取文件当前状态，若 MD5 变化则更新 last_changed"""
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
                # 内容发生了变化（或首次读取）
                self._md5 = md5
                self._last_changed = datetime.now(timezone.utc).isoformat()

    def _watch_loop(self):
        while True:
            time.sleep(self._interval)
            self._refresh()

    def get_info(self) -> dict:
        with self._lock:
            if not self._exists:
                return {"exists": False}
            return {
                "exists": True,
                "last_modified": self._last_changed,
                "size": self._size,
                "md5": self._md5,
            }


# 全局追踪器实例
tracker = ImageTracker(IMAGE_PATH, CHECK_INTERVAL)


# ───────── 路由 ─────────

@app.get("/allsky/")
def root():
    return {"message": "图像监控 API 正在运行"}


@app.get("/allsky/api/image/status")
def image_status(
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """
    检查图像是否存在、最后更换时间和 MD5
    last_modified 为服务端检测到图像内容变化的真实时间，非文件系统时间
    """
    verify_api_key(
        x_api_key,
        client_id=x_client_id or "",
        client_ip=request.client.host if request.client else "",
    )
    info = tracker.get_info()
    return JSONResponse(content=info)


@app.get("/allsky/api/image/data")
def image_data(
    request: Request,
    x_api_key: str | None = Header(None),
    x_client_id: str | None = Header(None),
):
    """
    返回图像二进制数据
    """
    verify_api_key(
        x_api_key,
        client_id=x_client_id or "",
        client_ip=request.client.host if request.client else "",
    )

    if not os.path.isfile(IMAGE_PATH):
        raise HTTPException(status_code=404, detail="图像文件不存在")

    with open(IMAGE_PATH, "rb") as f:
        data = f.read()

    # 根据扩展名确定 MIME 类型
    ext = os.path.splitext(IMAGE_PATH)[1].lower()
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
    print(f"监控图像路径: {IMAGE_PATH}")
    print(f"允许的 API Key 数量: {len(API_KEYS)}")
    print(f"启动服务: {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
