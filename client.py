"""
图像监控客户端 - PySide6 GUI
定时轮询服务端，检查所有图像是否有更新，同时展示全部图像
第一个图像为主图占整行，其余图像每行3个
所有连接配置从 client_config.yaml 读取
"""

import os
import sys
import uuid
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
    QGridLayout,
    QLabel,
    QStatusBar,
    QGroupBox,
    QScrollArea,
    QSizePolicy,
    QFrame,
)

# ───────── 加载配置 ─────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.yaml")


def load_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ───────── 单个图像卡片 Widget ─────────
class ImageCard(QGroupBox):
    """一个图像的展示卡片：标题 + 状态 + 图像"""

    def __init__(self, name: str, is_primary: bool = False, parent=None):
        super().__init__(name, parent)
        self.image_name = name
        self._is_primary = is_primary
        self._last_modified_iso: str | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # 状态行
        info_row = QHBoxLayout()
        self._lbl_modified = QLabel("最后更新时间: --")
        self._lbl_md5 = QLabel("MD5: --")
        self._lbl_size = QLabel("大小: --")
        info_row.addWidget(self._lbl_modified)
        info_row.addWidget(self._lbl_md5)
        info_row.addWidget(self._lbl_size)
        info_row.addStretch()
        layout.addLayout(info_row)

        # 图像
        self._lbl_image = QLabel("暂无图像")
        self._lbl_image.setAlignment(Qt.AlignCenter)
        min_h = 400 if self._is_primary else 180
        self._lbl_image.setMinimumHeight(min_h)
        self._lbl_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._lbl_image.setStyleSheet("background-color: #2b2b2b; color: #aaa; font-size: 14px;")
        self._lbl_image.setFrameShape(QFrame.StyledPanel)
        layout.addWidget(self._lbl_image, stretch=1)

    def update_status(self, info: dict):
        if not info.get("exists"):
            self._last_modified_iso = None
            self._lbl_modified.setText("最后更新时间: 文件不存在")
            self._lbl_md5.setText("MD5: --")
            self._lbl_size.setText("大小: --")
            return
        self._last_modified_iso = info.get("last_modified", "")
        self._refresh_elapsed()
        self._lbl_md5.setText(f"MD5: {info.get('md5', '--')}")
        size_bytes = info.get("size", 0)
        if size_bytes >= 1024 * 1024:
            size_str = f"{size_bytes / 1024 / 1024:.2f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"
        self._lbl_size.setText(f"大小: {size_str}")

    def update_image(self, data: bytes):
        img = QImage()
        img.loadFromData(data)
        if img.isNull():
            self._lbl_image.setText("图像解码失败")
            return
        pixmap = QPixmap.fromImage(img)
        scaled = pixmap.scaled(
            self._lbl_image.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._lbl_image.setPixmap(scaled)

    def refresh_elapsed(self):
        self._refresh_elapsed()

    def _refresh_elapsed(self):
        if self._last_modified_iso:
            elapsed = _format_elapsed(self._last_modified_iso)
            self._lbl_modified.setText(
                f"最后更新时间: {self._last_modified_iso}  （{elapsed}前）"
            )


def _format_elapsed(iso_str: str) -> str:
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


# ───────── 后台轮询线程 ─────────
class PollerThread(QThread):
    """后台线程：批量查询所有图像状态，拉取所有有变化的图像"""

    status_ready = Signal(list)
    image_ready = Signal(str, bytes)
    error_occurred = Signal(str)

    def __init__(self, server_url: str, api_key: str, client_id: str,
                 last_md5_map: dict):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.client_id = client_id
        self.last_md5_map = last_md5_map
        self._running = True

    def run(self):
        if not self._running:
            return
        try:
            headers = {
                "X-Api-Key": self.api_key,
                "X-Client-Id": self.client_id,
            }

            resp = requests.get(
                f"{self.server_url}/allsky/api/images/status",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 403:
                self.error_occurred.emit("API Key 无效，服务器拒绝访问")
                return
            if resp.status_code == 409:
                detail = resp.json().get("detail", "该 Key 已被其他客户端占用")
                self.error_occurred.emit(detail)
                return
            resp.raise_for_status()
            images_info: list = resp.json().get("images", [])
            self.status_ready.emit(images_info)

            for info in images_info:
                name = info.get("name", "")
                if not name or not info.get("exists"):
                    continue
                new_md5 = info.get("md5", "")
                old_md5 = self.last_md5_map.get(name)
                if new_md5 and new_md5 == old_md5:
                    continue

                resp_img = requests.get(
                    f"{self.server_url}/allsky/api/image/{name}/data",
                    headers=headers,
                    timeout=30,
                )
                resp_img.raise_for_status()
                self.image_ready.emit(name, resp_img.content)
                self.last_md5_map[name] = new_md5

        except requests.ConnectionError:
            self.error_occurred.emit("无法连接到服务器")
        except Exception as exc:
            self.error_occurred.emit(f"轮询异常: {exc}")

    def stop(self):
        self._running = False


# ───────── 主窗口 ─────────
GRID_COLUMNS = 3  # 其余图像每行显示的列数


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图像监控客户端")
        self.resize(1100, 850)

        cfg = load_config()
        self._server_url = cfg.get("server_url", "http://127.0.0.1:8120")
        self._api_key = cfg.get("api_key", "key-abc123456")
        self._poll_interval = cfg.get("poll_interval", 5)
        self._poller: PollerThread | None = None
        self._client_id = str(uuid.uuid4())

        self._last_md5_map: dict[str, str] = {}
        self._image_cards: dict[str, ImageCard] = {}
        self._image_order: list[str] = []  # 保持服务端返回的顺序

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_poll)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_all_elapsed)
        self._elapsed_timer.start(1000)

        QTimer.singleShot(300, self._start_polling)

    # ── UI 构建 ──
    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        self._container = QWidget()
        self._main_layout = QVBoxLayout(self._container)
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._main_layout.setSpacing(12)

        # 主图区域（第一个图像）
        self._primary_card: ImageCard | None = None

        # 网格区域（其余图像，每行3个）
        self._grid_widget = QWidget()
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setSpacing(10)
        # 每列等比例拉伸，撑满窗口宽度
        for col in range(GRID_COLUMNS):
            self._grid_layout.setColumnStretch(col, 1)
        self._main_layout.addWidget(self._grid_widget)

        self._main_layout.addStretch()

        scroll.setWidget(self._container)
        self.setCentralWidget(scroll)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪")

    def _rebuild_layout(self, names: list[str]):
        """根据图像名列表重建布局：第一个为主图，其余每行3个"""
        if not names:
            return

        # 主图
        primary_name = names[0]
        if self._primary_card is None or self._primary_card.image_name != primary_name:
            # 移除旧主图
            if self._primary_card is not None:
                self._main_layout.removeWidget(self._primary_card)
                self._primary_card.setParent(None)
                # 如果旧主图 name 还在列表中，后面会作为小图重建
                old_name = self._primary_card.image_name
                if old_name in self._image_cards:
                    del self._image_cards[old_name]
                self._primary_card.deleteLater()
                self._primary_card = None

            card = ImageCard(primary_name, is_primary=True)
            self._image_cards[primary_name] = card
            self._primary_card = card
            # 插入到 grid_widget 之前（位置 0）
            self._main_layout.insertWidget(0, card, stretch=2)

        # 清空网格中的旧卡片
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w:
                name = getattr(w, "image_name", None)
                if name and name in self._image_cards and name != primary_name:
                    del self._image_cards[name]
                w.setParent(None)
                w.deleteLater()

        # 其余图像按每行 GRID_COLUMNS 个填入网格
        rest = names[1:]
        for idx, name in enumerate(rest):
            row = idx // GRID_COLUMNS
            col = idx % GRID_COLUMNS
            card = ImageCard(name, is_primary=False)
            self._image_cards[name] = card
            self._grid_layout.addWidget(card, row, col)

    # ── 控制 ──
    def _start_polling(self):
        if not self._server_url or not self._api_key:
            self._status_bar.showMessage("配置文件缺少 server_url 或 api_key")
            return
        self._timer.start(self._poll_interval * 1000)
        self._status_bar.showMessage(f"监控中 — 每 {self._poll_interval} 秒轮询一次")
        self._do_poll()

    def _do_poll(self):
        if self._poller and self._poller.isRunning():
            return
        self._poller = PollerThread(
            self._server_url,
            self._api_key,
            self._client_id,
            self._last_md5_map,
        )
        self._poller.status_ready.connect(self._on_status)
        self._poller.image_ready.connect(self._on_image)
        self._poller.error_occurred.connect(self._on_error)
        self._poller.start()

    def _update_all_elapsed(self):
        for card in self._image_cards.values():
            card.refresh_elapsed()

    # ── 信号槽 ──
    def _on_status(self, images_info: list):
        new_names = [info.get("name", "") for info in images_info if info.get("name")]

        # 图像列表变化时重建布局
        if new_names != self._image_order:
            self._image_order = new_names
            self._rebuild_layout(new_names)

        # 更新每个卡片的状态
        for info in images_info:
            name = info.get("name", "")
            card = self._image_cards.get(name)
            if card:
                card.update_status(info)

    def _on_image(self, name: str, data: bytes):
        card = self._image_cards.get(name)
        if card:
            card.update_image(data)
        self._status_bar.showMessage(f"[{name}] 图像已更新 — {len(data)} bytes")

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
