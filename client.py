"""
图像监控客户端 - PySide6 GUI
定时轮询服务端，检查图像是否有更新，若有则拉取并展示
"""

import os
import sys

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
    QLineEdit,
    QPushButton,
    QSpinBox,
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


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True)


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
                f"{self.server_url}/api/image/status",
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
                f"{self.server_url}/api/image/data",
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
        self._polling = False
        self._poller: PollerThread | None = None

        self._build_ui()

        # 定时器
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_poll)

    # ── UI 构建 ──
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # --- 配置区 ---
        config_group = QGroupBox("连接设置")
        config_layout = QVBoxLayout(config_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("服务器地址:"))
        self._edit_url = QLineEdit(self._server_url)
        self._edit_url.setMinimumWidth(300)
        row1.addWidget(self._edit_url)
        row1.addWidget(QLabel("API Key:"))
        self._edit_key = QLineEdit(self._api_key)
        self._edit_key.setMinimumWidth(200)
        row1.addWidget(self._edit_key)
        config_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("轮询间隔 (秒):"))
        self._spin_interval = QSpinBox()
        self._spin_interval.setRange(1, 3600)
        self._spin_interval.setValue(self._poll_interval)
        row2.addWidget(self._spin_interval)
        row2.addStretch()

        self._btn_start = QPushButton("▶ 开始监控")
        self._btn_start.setFixedWidth(140)
        self._btn_start.clicked.connect(self._toggle_polling)
        row2.addWidget(self._btn_start)

        self._btn_once = QPushButton("手动刷新")
        self._btn_once.setFixedWidth(100)
        self._btn_once.clicked.connect(self._do_poll)
        row2.addWidget(self._btn_once)

        config_layout.addLayout(row2)
        main_layout.addWidget(config_group)

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
    def _toggle_polling(self):
        if self._polling:
            self._stop_polling()
        else:
            self._start_polling()

    def _start_polling(self):
        self._server_url = self._edit_url.text().strip()
        self._api_key = self._edit_key.text().strip()
        self._poll_interval = self._spin_interval.value()

        if not self._server_url or not self._api_key:
            self._status_bar.showMessage("请填写服务器地址和 API Key")
            return

        # 保存配置
        save_config({
            "server_url": self._server_url,
            "api_key": self._api_key,
            "poll_interval": self._poll_interval,
        })

        self._polling = True
        self._btn_start.setText("■ 停止监控")
        self._edit_url.setEnabled(False)
        self._edit_key.setEnabled(False)
        self._spin_interval.setEnabled(False)

        self._timer.start(self._poll_interval * 1000)
        self._status_bar.showMessage(f"监控中 — 每 {self._poll_interval} 秒轮询一次")
        self._do_poll()  # 立即执行一次

    def _stop_polling(self):
        self._polling = False
        self._timer.stop()
        self._btn_start.setText("▶ 开始监控")
        self._edit_url.setEnabled(True)
        self._edit_key.setEnabled(True)
        self._spin_interval.setEnabled(True)
        self._status_bar.showMessage("已停止监控")

    def _do_poll(self):
        """启动一次后台轮询"""
        if self._poller and self._poller.isRunning():
            return  # 上一次还没完成

        self._server_url = self._edit_url.text().strip()
        self._api_key = self._edit_key.text().strip()

        self._poller = PollerThread(self._server_url, self._api_key)
        self._poller.status_ready.connect(self._on_status)
        self._poller.image_ready.connect(self._on_image)
        self._poller.error_occurred.connect(self._on_error)
        self._poller.start()

    # ── 信号槽 ──
    def _on_status(self, info: dict):
        if not info.get("exists"):
            self._lbl_modified.setText("最后更新时间: 文件不存在")
            self._lbl_md5.setText("MD5: --")
            self._lbl_size.setText("大小: --")
            return
        self._lbl_modified.setText(f"最后更新时间: {info.get('last_modified', '--')}")
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
