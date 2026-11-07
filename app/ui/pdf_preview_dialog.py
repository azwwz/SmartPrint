"""PDF 预览对话框（阶段三）。

用途：打印前确认文档内容是否正确，避免把错误的文件送进打印机。

第一版只做「看得见」的功能，不做 PDF 编辑：

- 上一页 / 下一页 / 跳转到指定页
- 缩放（50% ~ 300%）
- 适应宽度 / 适应页面
- 显示总页数与当前页
- 显示页面尺寸

实现说明：用 PyMuPDF 把页面渲染成图片再交给 Qt 显示。渲染结果按
（页码, 缩放比例）缓存，翻回上一页时不需要重新渲染。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

try:  # PyMuPDF 2.x 推荐使用 pymupdf 包名
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("smartprint.ui.pdf_preview")

#: 缩放档位（百分比）
ZOOM_LEVELS = (50, 75, 100, 125, 150, 200, 300)

#: 1pt = 1/72 英寸；按 96 DPI 屏幕显示时 100% 约等于 1.333 倍
SCREEN_SCALE = 96 / 72

#: 最多缓存多少个页面图像
CACHE_LIMIT = 12

FIT_WIDTH_TEXT = "适应宽度"
FIT_PAGE_TEXT = "适应页面"


class PdfPreviewDialog(QDialog):
    """只读 PDF 预览窗口。"""

    def __init__(
        self,
        pdf_path: Path | str,
        parent: QWidget | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.pdf_path = Path(pdf_path)
        self.error_message: str = ""

        self._document = None
        self._page_index = 0
        self._zoom = 100
        self._fit_mode: str | None = None
        self._cache: OrderedDict[tuple[int, float], QPixmap] = OrderedDict()

        # 适应宽度时窗口尺寸变化会重新计算缩放，合并短时间内的多次 resize
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render)

        window_title = self.pdf_path.name if not title else f"{title} - {self.pdf_path.name}"
        self.setWindowTitle(f"PDF 预览 - {window_title}")
        self.resize(940, 780)

        self._build_ui()
        self._install_shortcuts()
        self._open_document()
        self._render()

    # -- 界面 ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        layout.addLayout(self._build_toolbar())

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background: #5a5a5a; border: 1px solid #444; }")

        self.view_label = QLabel()
        self.view_label.setAlignment(Qt.AlignCenter)
        self.view_label.setStyleSheet("background: #5a5a5a; color: #ffffff; padding: 12px;")
        self.view_label.setText("正在加载…")
        self.scroll.setWidget(self.view_label)

        layout.addWidget(self.scroll, 1)
        layout.addLayout(self._build_footer())

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.prev_button = QPushButton("◀ 上一页")
        self.prev_button.clicked.connect(self.previous_page)
        row.addWidget(self.prev_button)

        self.page_spin = QSpinBox(self)
        self.page_spin.setMinimum(1)
        self.page_spin.setMaximum(1)
        self.page_spin.setFixedWidth(70)
        self.page_spin.setAlignment(Qt.AlignRight)
        self.page_spin.valueChanged.connect(self._on_spin_changed)
        row.addWidget(self.page_spin)

        self.page_info_label = QLabel("第 0 / 0 页")
        row.addWidget(self.page_info_label)

        self.next_button = QPushButton("下一页 ▶")
        self.next_button.clicked.connect(self.next_page)
        row.addWidget(self.next_button)

        row.addStretch(1)

        self.zoom_combo = QComboBox(self)
        for level in ZOOM_LEVELS:
            self.zoom_combo.addItem(f"{level}%")
        self.zoom_combo.addItem(FIT_WIDTH_TEXT)
        self.zoom_combo.addItem(FIT_PAGE_TEXT)
        self.zoom_combo.setCurrentText("100%")
        self.zoom_combo.currentTextChanged.connect(self._on_zoom_changed)
        row.addWidget(self.zoom_combo)

        self.zoom_out_button = QPushButton("缩小 −")
        self.zoom_out_button.clicked.connect(self.zoom_out)
        row.addWidget(self.zoom_out_button)

        self.zoom_in_button = QPushButton("放大 +")
        self.zoom_in_button.clicked.connect(self.zoom_in)
        row.addWidget(self.zoom_in_button)

        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)

        return row

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("color: #666;")
        row.addWidget(self.size_label)
        row.addStretch(1)

        self.hint_label = QLabel("提示：← → 翻页，Ctrl+滚轮或缩放按钮调整大小")
        self.hint_label.setStyleSheet("color: #888;")
        row.addWidget(self.hint_label)
        return row

    def _install_shortcuts(self) -> None:
        for key, slot in (
            (Qt.Key_Left, self.previous_page),
            (Qt.Key_PageUp, self.previous_page),
            (Qt.Key_Right, self.next_page),
            (Qt.Key_PageDown, self.next_page),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(slot)

    # -- 文档 ---------------------------------------------------------------

    def _open_document(self) -> None:
        if not self.pdf_path.is_file():
            self._fail(f"找不到 PDF 文件：\n{self.pdf_path}")
            return

        try:
            document = pymupdf.open(self.pdf_path)
        except Exception as exc:  # PyMuPDF 会抛出多种异常类型
            logger.warning("打开 PDF 预览失败：%s - %s", self.pdf_path, exc)
            self._fail(f"无法打开 PDF：\n{exc}")
            return

        if document.needs_pass:
            document.close()
            self._fail("PDF 已加密，需要密码才能预览。")
            return

        if document.page_count <= 0:
            document.close()
            self._fail("PDF 没有任何页面。")
            return

        self._document = document
        self.page_spin.setRange(1, document.page_count)
        logger.debug("PDF 预览已打开：%s（%s 页）", self.pdf_path, document.page_count)

    def _fail(self, message: str) -> None:
        self.error_message = message
        self.view_label.setText(message)
        self.view_label.setStyleSheet(
            "background: #5a5a5a; color: #ffd6d6; padding: 24px; font-size: 14px;"
        )
        for widget in (
            self.prev_button,
            self.next_button,
            self.zoom_combo,
            self.zoom_in_button,
            self.zoom_out_button,
            self.page_spin,
        ):
            widget.setEnabled(False)
        self.page_info_label.setText("无法预览")

    # -- 渲染 ---------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self._document.page_count if self._document is not None else 0

    @property
    def current_page(self) -> int:
        """当前页码（从 1 开始，无文档时为 0）。"""
        return self._page_index + 1 if self._document is not None else 0

    @property
    def rendered_pixmap_size(self) -> tuple[int, int]:
        pixmap = self.view_label.pixmap()
        if pixmap is None:
            return (0, 0)
        return (pixmap.width(), pixmap.height())

    def _current_scale(self, page) -> float:
        if self._fit_mode == FIT_WIDTH_TEXT:
            available = max(240, self.scroll.viewport().width() - 24)
            return _clamp(available / page.rect.width, 0.2, 5.0)
        if self._fit_mode == FIT_PAGE_TEXT:
            available_w = max(240, self.scroll.viewport().width() - 24)
            available_h = max(240, self.scroll.viewport().height() - 24)
            return _clamp(
                min(available_w / page.rect.width, available_h / page.rect.height), 0.2, 5.0
            )
        return self._zoom / 100 * SCREEN_SCALE

    def _render(self) -> None:
        if self._document is None:
            return
        try:
            page = self._document.load_page(self._page_index)
        except Exception as exc:
            logger.exception("读取 PDF 页面失败：第 %s 页", self._page_index + 1)
            self.view_label.setText(f"读取页面失败：{exc}")
            return

        scale = self._current_scale(page)
        key = (self._page_index, round(scale, 4))
        pixmap = self._cache.get(key)
        if pixmap is None:
            try:
                pixmap = _render_page(page, scale)
            except Exception as exc:
                logger.exception("渲染 PDF 页面失败：第 %s 页", self._page_index + 1)
                self.view_label.setText(f"渲染页面失败：{exc}")
                return
            self._cache[key] = pixmap
            while len(self._cache) > CACHE_LIMIT:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)

        self.view_label.setPixmap(pixmap)
        self.view_label.setStyleSheet("background: #5a5a5a; color: #ffffff;")
        self.view_label.resize(pixmap.size())

        self.page_info_label.setText(f"第 {self.current_page} / {self.page_count} 页")
        self.size_label.setText(
            f"页面：{page.rect.width:.0f} × {page.rect.height:.0f} pt  |  "
            f"显示比例：{self._scale_text(scale)}"
        )
        self._update_buttons()

    def _scale_text(self, scale: float) -> str:
        if self._fit_mode:
            return f"{self._fit_mode}（{scale / SCREEN_SCALE * 100:.0f}%）"
        return f"{self._zoom}%"

    def _update_buttons(self) -> None:
        self.prev_button.setEnabled(self._page_index > 0)
        self.next_button.setEnabled(self._page_index < self.page_count - 1)
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(self.current_page or 1)
        self.page_spin.blockSignals(False)

    # -- 翻页 ---------------------------------------------------------------

    def goto_page(self, number: int) -> None:
        """跳转到指定页（从 1 开始）。"""
        if self._document is None:
            return
        index = min(max(1, int(number)), self.page_count) - 1
        if index == self._page_index and self.view_label.pixmap() is not None:
            return
        self._page_index = index
        self._render()

    def next_page(self) -> None:
        if self._document is not None and self._page_index < self.page_count - 1:
            self._page_index += 1
            self._render()

    def previous_page(self) -> None:
        if self._document is not None and self._page_index > 0:
            self._page_index -= 1
            self._render()

    def _on_spin_changed(self, value: int) -> None:
        if self._document is not None and value != self.current_page:
            self.goto_page(value)

    # -- 缩放 ---------------------------------------------------------------

    def set_zoom(self, percent: int) -> None:
        self._zoom = int(_clamp(percent, ZOOM_LEVELS[0], ZOOM_LEVELS[-1]))
        self._fit_mode = None
        index = self.zoom_combo.findText(f"{self._zoom}%")
        if index >= 0:
            self.zoom_combo.blockSignals(True)
            self.zoom_combo.setCurrentIndex(index)
            self.zoom_combo.blockSignals(False)
        self._render()

    def zoom_in(self) -> None:
        for level in ZOOM_LEVELS:
            if level > self._effective_percent():
                self.set_zoom(level)
                return

    def zoom_out(self) -> None:
        for level in reversed(ZOOM_LEVELS):
            if level < self._effective_percent():
                self.set_zoom(level)
                return

    def fit_width(self) -> None:
        self._fit_mode = FIT_WIDTH_TEXT
        self._sync_combo(FIT_WIDTH_TEXT)
        self._render()

    def fit_page(self) -> None:
        self._fit_mode = FIT_PAGE_TEXT
        self._sync_combo(FIT_PAGE_TEXT)
        self._render()

    def _effective_percent(self) -> int:
        if self._document is None:
            return self._zoom
        try:
            page = self._document.load_page(self._page_index)
        except Exception:
            return self._zoom
        return round(self._current_scale(page) / SCREEN_SCALE * 100)

    def _sync_combo(self, text: str) -> None:
        index = self.zoom_combo.findText(text)
        if index >= 0:
            self.zoom_combo.blockSignals(True)
            self.zoom_combo.setCurrentIndex(index)
            self.zoom_combo.blockSignals(False)

    def _on_zoom_changed(self, text: str) -> None:
        if text == FIT_WIDTH_TEXT:
            self.fit_width()
        elif text == FIT_PAGE_TEXT:
            self.fit_page()
        elif text.endswith("%"):
            try:
                percent = int(text[:-1])
            except ValueError:
                return
            if percent != self._zoom or self._fit_mode:
                self.set_zoom(percent)

    # -- 事件 ---------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名风格)
        super().resizeEvent(event)
        if self._fit_mode:
            self._render_timer.start()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名风格)
        self._render_timer.stop()
        self._cache.clear()
        if self._document is not None:
            try:
                self._document.close()
            except Exception:  # pragma: no cover - 关闭失败不影响退出
                logger.exception("关闭 PDF 文档时出现异常")
            self._document = None
        super().closeEvent(event)


def _render_page(page, scale: float) -> QPixmap:
    """把一页 PDF 渲染成 QPixmap。"""
    matrix = pymupdf.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    image = QImage(
        pix.samples,
        pix.width,
        pix.height,
        pix.stride,
        QImage.Format_RGB888,
    ).copy()  # copy()：pix.samples 是临时缓冲区，必须拷贝出来
    return QPixmap.fromImage(image)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
