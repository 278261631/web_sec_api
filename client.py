"""
图像监控客户端 - PySide6 GUI
定时轮询服务端，检查图像是否有更新，若有则拉取并展示
所有连接配置从 client_config.yaml 读取
"""

import os
import sys
from datetime import datetime, timezone

import yaml
import requests
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QStatusBar,
    QGroupBox,
    QScrollArea,
    QSizePolicy,
)

# ───────── 加载配置 ─────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.yaml")


def load_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ───────── 后台轮询线程 ─────────
class PollerThread(QThread):
    """后台线程：检查图像状态 & 拉取图像"""

    status_ready = Signal(dict)       # 发送 status 信息
    image_ready = Signal(bytes)       # 发送图像字节
    error_occurred = Signal(str)      # 发送错误信息

    def __init__(self, server_url: str, api_key: str):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self._last_md5: str | None = None
        self._running = True

    def run(self):
        """单次轮询（由 QTimer 触发，每次启动线程执行一次）"""
        if not self._running:
            return
        try:
            headers = {"X-Api-Key": self.api_key}

            # 1. 查询状态
            resp = requests.get(
                f"{self.server_url}/allsky/api/image/status",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 403:
                self.error_occurred.emit("API Key 无效，服务器拒绝访问")
                return
            resp.raise_for_status()
            info: dict = resp.json()
            self.status_ready.emit(info)

            if not info.get("exists"):
                self.error_occurred.emit("服务器上图像文件不存在")
                return

            # 2. 比较 MD5，如有变化则拉取图像
            new_md5 = info.get("md5", "")
            if new_md5 and new_md5 == self._last_md5:
                return  # 图像未变化

            resp_img = requests.get(
                f"{self.server_url}/allsky/api/image/data",
                headers=headers,
                timeout=30,
            )
            resp_img.raise_for_status()
            self.image_ready.emit(resp_img.content)
            self._last_md5 = new_md5

        except requests.ConnectionError:
            self.error_occurred.emit("无法连接到服务器")
        except Exception as exc:
            self.error_occurred.emit(f"轮询异常: {exc}")

    def stop(self):
        self._running = False


# ───────── 主窗口 ─────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图像监控客户端")
        self.resize(900, 700)

        cfg = load_config()
        self._server_url = cfg.get("server_url", "http://127.0.0.1:8120")
        self._api_key = cfg.get("api_key", "key-abc123456")
        self._poll_interval = cfg.get("poll_interval", 5)
        self._poller: PollerThread | None = None
        self._last_modified_iso: str | None = None

        self._build_ui()

        # 轮询定时器
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_poll)

        # 时间差刷新定时器（每秒更新一次距今时间）
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

        # 启动后自动开始监控
        QTimer.singleShot(300, self._start_polling)

    # ── UI 构建 ──
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # --- 状态信息区 ---
        info_group = QGroupBox("图像状态")
        info_layout = QHBoxLayout(info_group)
        self._lbl_modified = QLabel("最后更新时间: --")
        self._lbl_md5 = QLabel("MD5: --")
        self._lbl_size = QLabel("大小: --")
        info_layout.addWidget(self._lbl_modified)
        info_layout.addWidget(self._lbl_md5)
        info_layout.addWidget(self._lbl_size)
        info_layout.addStretch()
        main_layout.addWidget(info_group)

        # --- 图像展示区 ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)

        self._lbl_image = QLabel("暂无图像")
        self._lbl_image.setAlignment(Qt.AlignCenter)
        self._lbl_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._lbl_image.setStyleSheet("background-color: #2b2b2b; color: #aaa; font-size: 18px;")
        scroll.setWidget(self._lbl_image)
        main_layout.addWidget(scroll, stretch=1)

        # --- 状态栏 ---
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪")

    # ── 控制 ──
    def _start_polling(self):
        if not self._server_url or not self._api_key:
            self._status_bar.showMessage("配置文件缺少 server_url 或 api_key")
            return

        self._timer.start(self._poll_interval * 1000)
        self._status_bar.showMessage(f"监控中 — 每 {self._poll_interval} 秒轮询一次")
        self._do_poll()  # 立即执行一次

    def _do_poll(self):
        """启动一次后台轮询"""
        if self._poller and self._poller.isRunning():
            return  # 上一次还没完成

        self._poller = PollerThread(self._server_url, self._api_key)
        self._poller.status_ready.connect(self._on_status)
        self._poller.image_ready.connect(self._on_image)
        self._poller.error_occurred.connect(self._on_error)
        self._poller.start()

    # ── 时间差计算 ──
    @staticmethod
    def _format_elapsed(iso_str: str) -> str:
        """将 ISO 时间字符串与当前时间比较，返回 'X天X小时X分X秒' 格式"""
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            total_seconds = int(delta.total_seconds())
            if total_seconds < 0:
                return "刚刚"
            days, remainder = divmod(total_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{days}天{hours}小时{minutes}分{seconds}秒"
        except Exception:
            return "--"

    def _update_elapsed(self):
        """每秒刷新一次距今时间"""
        if self._last_modified_iso:
            elapsed = self._format_elapsed(self._last_modified_iso)
            self._lbl_modified.setText(f"最后更新时间: {self._last_modified_iso}  （{elapsed}前）")

    # ── 信号槽 ──
    def _on_status(self, info: dict):
        if not info.get("exists"):
            self._last_modified_iso = None
            self._lbl_modified.setText("最后更新时间: 文件不存在")
            self._lbl_md5.setText("MD5: --")
            self._lbl_size.setText("大小: --")
            return
        self._last_modified_iso = info.get("last_modified", "")
        elapsed = self._format_elapsed(self._last_modified_iso) if self._last_modified_iso else "--"
        self._lbl_modified.setText(f"最后更新时间: {self._last_modified_iso}  （{elapsed}前）")
        self._lbl_md5.setText(f"MD5: {info.get('md5', '--')}")
        size_bytes = info.get("size", 0)
        if size_bytes >= 1024 * 1024:
            size_str = f"{size_bytes / 1024 / 1024:.2f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"
        self._lbl_size.setText(f"大小: {size_str}")

    def _on_image(self, data: bytes):
        img = QImage()
        img.loadFromData(data)
        if img.isNull():
            self._lbl_image.setText("图像解码失败")
            return
        pixmap = QPixmap.fromImage(img)
        # 自适应缩放到标签大小
        scaled = pixmap.scaled(
            self._lbl_image.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._lbl_image.setPixmap(scaled)
        self._status_bar.showMessage(
            f"图像已更新 — {pixmap.width()}x{pixmap.height()} — "
            f"{len(data)} bytes"
        )

    def _on_error(self, msg: str):
        self._status_bar.showMessage(f"⚠ {msg}")

    def closeEvent(self, event):
        if self._poller:
            self._poller.stop()
            self._poller.quit()
            self._poller.wait(2000)
        event.accept()


# ───────── 入口 ─────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
