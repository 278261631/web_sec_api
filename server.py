"""
图像监控服务端 - 提供 API 供客户端检查图像更新并获取图像
需要 API Key 鉴权
"""

import os
import sys
import hashlib
from datetime import datetime, timezone

import yaml
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Response
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

# ───────── FastAPI 应用 ─────────
app = FastAPI(title="图像监控 API", version="1.0.0")


def verify_api_key(x_api_key: str | None):
    """校验请求头中的 API Key"""
    if not x_api_key or x_api_key not in API_KEYS:
        raise HTTPException(status_code=403, detail="无效的 API Key 或未提供 API Key")


def _file_md5(path: str) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_image_info() -> dict:
    """获取图像文件的元信息"""
    if not os.path.isfile(IMAGE_PATH):
        return {"exists": False}

    stat = os.stat(IMAGE_PATH)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    md5 = _file_md5(IMAGE_PATH)
    return {
        "exists": True,
        "last_modified": mtime,
        "size": stat.st_size,
        "md5": md5,
    }


# ───────── 路由 ─────────

@app.get("/")
def root():
    return {"message": "图像监控 API 正在运行"}


@app.get("/api/image/status")
def image_status(x_api_key: str | None = Header(None)):
    """
    检查图像是否存在、最后修改时间和 MD5
    客户端可据此判断是否需要重新拉取图像
    """
    verify_api_key(x_api_key)
    info = _get_image_info()
    return JSONResponse(content=info)


@app.get("/api/image/data")
def image_data(x_api_key: str | None = Header(None)):
    """
    返回图像二进制数据
    """
    verify_api_key(x_api_key)

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
