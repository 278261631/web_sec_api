"""
图像监控客户端 - PySide6 GUI
定时轮询服务端，检查所有图像是否有更新，同时展示全部图像
第一个图像为主图占整行，其余图像每行3个
点击图像可弹出大图预览
所有连接配置从 client_config.yaml 读取
"""

import os
import sys
import uuid
from datetime import datetime, timezone

import yaml
import requests
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QPixmap, QImage, QCursor
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
    QDialog,
)

# ───────── 加载配置 ─────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.yaml")


def load_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ───────── 可点击的图像标签 ─────────
class ClickableLabel(QLabel):
    """点击可触发信号的 QLabel"""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ───────── 大图预览对话框 ─────────
class ImagePreviewDialog(QDialog):
    """
    弹出窗口，默认 1:1 显示图像
    滚轮缩放（不滚动），左键拖拽平移，点击图像关闭
    """

    ZOOM_FACTOR = 1.15
    ZOOM_MIN = 0.1
    ZOOM_MAX = 10.0

    def __init__(self, title: str, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"大图预览 — {title}")
        self.setMinimumSize(600, 400)

        self._original_pixmap = pixmap
        self._zoom = 1.0

        # 拖拽状态
        self._dragging = False
        self._drag_start = None   # 鼠标起始位置
        self._scroll_start_h = 0  # 拖拽开始时滚动条位置
        self._scroll_start_v = 0
        self._drag_moved = False  # 是否真正拖拽移动过（区分点击和拖拽）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 缩放提示
        self._lbl_zoom = QLabel("100%")
        self._lbl_zoom.setAlignment(Qt.AlignCenter)
        self._lbl_zoom.setStyleSheet(
            "background-color: rgba(0,0,0,180); color: #fff; "
            "font-size: 13px; padding: 4px 12px;"
        )
        self._lbl_zoom.setFixedHeight(28)
        layout.addWidget(self._lbl_zoom)

        # 滚动区域（隐藏滚动条，只用拖拽平移）
        self._scroll = QScrollArea()
        self._scroll.setAlignment(Qt.AlignCenter)
        self._scroll.setStyleSheet("background-color: #1a1a1a;")
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._lbl_image = QLabel()
        self._lbl_image.setAlignment(Qt.AlignCenter)
        self._lbl_image.setStyleSheet("background-color: #1a1a1a;")
        self._lbl_image.setCursor(QCursor(Qt.OpenHandCursor))
        self._scroll.setWidget(self._lbl_image)

        layout.addWidget(self._scroll, stretch=1)

        # 默认 1:1 显示
        self._apply_zoom()

        # 对话框大小
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 0.9)
        max_h = int(screen.height() * 0.9)
        dialog_w = min(pixmap.width() + 40, max_w)
        dialog_h = min(pixmap.height() + 70, max_h)
        self.resize(dialog_w, dialog_h)

    def _apply_zoom(self):
        w = int(self._original_pixmap.width() * self._zoom)
        h = int(self._original_pixmap.height() * self._zoom)
        scaled = self._original_pixmap.scaled(
            w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._lbl_image.setPixmap(scaled)
        self._lbl_image.resize(scaled.size())
        self._lbl_zoom.setText(f"{int(self._zoom * 100)}%")

    def wheelEvent(self, event):
        """滚轮只缩放，不滚动"""
        event.accept()  # 吃掉事件，不传给 ScrollArea
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom = min(self._zoom * self.ZOOM_FACTOR, self.ZOOM_MAX)
        elif delta < 0:
            self._zoom = max(self._zoom / self.ZOOM_FACTOR, self.ZOOM_MIN)
        self._apply_zoom()

    def mousePressEvent(self, event):
        """左键按下：开始拖拽"""
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._drag_start = event.globalPosition().toPoint()
            self._scroll_start_h = self._scroll.horizontalScrollBar().value()
            self._scroll_start_v = self._scroll.verticalScrollBar().value()
            self._lbl_image.setCursor(QCursor(Qt.ClosedHandCursor))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """拖拽移动图像"""
        if self._dragging and self._drag_start is not None:
            delta = event.globalPosition().toPoint() - self._drag_start
            if abs(delta.x()) > 3 or abs(delta.y()) > 3:
                self._drag_moved = True
            self._scroll.horizontalScrollBar().setValue(
                self._scroll_start_h - delta.x()
            )
            self._scroll.verticalScrollBar().setValue(
                self._scroll_start_v - delta.y()
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """左键松开：若未拖拽则视为点击关闭"""
        if event.button() == Qt.LeftButton:
            was_dragging = self._drag_moved
            self._dragging = False
            self._drag_start = None
            self._drag_moved = False
            self._lbl_image.setCursor(QCursor(Qt.OpenHandCursor))
            if not was_dragging:
                self.close()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)


# ───────── 单个图像卡片 Widget ─────────
class ImageCard(QGroupBox):
    """一个图像的展示卡片：标题 + 状态 + 图像（可点击预览大图）"""

    def __init__(self, name: str, is_primary: bool = False, parent=None):
        super().__init__(name, parent)
        self.image_name = name
        self._is_primary = is_primary
        self._last_modified_iso: str | None = None
        self._full_pixmap: QPixmap | None = None  # 保存原始分辨率图像
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
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

        # 可点击图像
        self._lbl_image = ClickableLabel("暂无图像")
        self._lbl_image.setAlignment(Qt.AlignCenter)
        min_h = 400 if self._is_primary else 180
        self._lbl_image.setMinimumHeight(min_h)
        self._lbl_image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._lbl_image.setStyleSheet("background-color: #2b2b2b; color: #aaa; font-size: 14px;")
        self._lbl_image.setFrameShape(QFrame.StyledPanel)
        self._lbl_image.setCursor(QCursor(Qt.PointingHandCursor))
        self._lbl_image.clicked.connect(self._show_preview)
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
            self._full_pixmap = None
            return
        self._full_pixmap = QPixmap.fromImage(img)
        scaled = self._full_pixmap.scaled(
            self._lbl_image.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._lbl_image.setPixmap(scaled)

    def _show_preview(self):
        """点击图像时弹出大图预览"""
        if self._full_pixmap and not self._full_pixmap.isNull():
            dlg = ImagePreviewDialog(self.image_name, self._full_pixmap, self)
            dlg.exec()

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
        self._image_order: list[str] = []
        self._image_levels: list[str] = []

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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._main_layout = QVBoxLayout(self._container)
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._main_layout.setSpacing(12)

        # _dynamic_widgets 保存所有动态添加的 widget（ImageCard 或 grid 容器）
        self._dynamic_widgets: list[QWidget] = []

        self._main_layout.addStretch()

        scroll.setWidget(self._container)
        self.setCentralWidget(scroll)
        self._scroll = scroll

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("就绪")

    def _make_sub_grid(self, sub_names: list[str]) -> QWidget:
        """将一组 sub 图像名打包成一个 grid 容器 widget"""
        grid_widget = QWidget()
        grid_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        grid_layout = QGridLayout(grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(10)
        for col in range(GRID_COLUMNS):
            grid_layout.setColumnStretch(col, 1)
        for idx, name in enumerate(sub_names):
            row = idx // GRID_COLUMNS
            col = idx % GRID_COLUMNS
            card = ImageCard(name, is_primary=False)
            self._image_cards[name] = card
            grid_layout.addWidget(card, row, col)
        return grid_widget

    def _rebuild_layout(self, images_info: list[dict]):
        """按原始顺序重建布局：main 独占一行，连续 sub 打包为 grid（每行3个）"""
        if not images_info:
            return

        # ── 清理旧的动态 widget ──
        for w in self._dynamic_widgets:
            self._main_layout.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        self._dynamic_widgets.clear()
        self._image_cards.clear()

        # ── 按顺序遍历，积攒连续 sub 后统一 flush ──
        pending_sub: list[str] = []
        insert_pos = 0  # 插入位置（在尾部 stretch 之前）

        def _flush_subs():
            nonlocal insert_pos
            if not pending_sub:
                return
            grid = self._make_sub_grid(list(pending_sub))
            self._dynamic_widgets.append(grid)
            self._main_layout.insertWidget(insert_pos, grid)
            insert_pos += 1
            pending_sub.clear()

        for info in images_info:
            name = info.get("name", "")
            level = info.get("level", "sub")
            if not name:
                continue

            if level == "main":
                # 先 flush 积攒的 sub
                _flush_subs()
                # 添加 main 卡片
                card = ImageCard(name, is_primary=True)
                self._image_cards[name] = card
                self._dynamic_widgets.append(card)
                self._main_layout.insertWidget(insert_pos, card, stretch=2)
                insert_pos += 1
            else:
                pending_sub.append(name)

        # 尾部剩余 sub
        _flush_subs()

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
        # 用 (name, level) 组合判断是否需要重建布局
        new_key = [(info.get("name", ""), info.get("level", "sub")) for info in images_info if info.get("name")]
        old_key = [(n, l) for n, l in zip(self._image_order, self._image_levels)]

        if new_key != old_key:
            self._image_order = [k[0] for k in new_key]
            self._image_levels = [k[1] for k in new_key]
            self._rebuild_layout(images_info)

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

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_container_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_container_width()

    def _sync_container_width(self):
        viewport_w = self._scroll.viewport().width()
        self._container.setMaximumWidth(viewport_w)

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
