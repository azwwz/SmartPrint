"""日志查看窗口（阶段八）。

readme 阶段八要求「增加日志查看」。这里的日志指 ``logs/smartprint.log``：
任务接收、PDF 读取、打印命令、用户操作、异常与恢复都写在这个文件里
（见 ``app/utils/logging_setup.py``，按 5MB 轮转、保留 5 份）。

窗口只做四件事：读文件尾部若干行、按级别筛选、按关键字筛选、刷新。
不写日志、不删日志，避免把出问题时的现场弄丢。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.utils.logging_setup import log_file_path

logger = logging.getLogger("smartprint.ui.log_view_dialog")

#: 级别筛选项：(下拉文字, 匹配的日志级别标记)
LEVEL_CHOICES = (
    ("全部级别", ""),
    ("仅错误", "ERROR"),
    ("仅警告及以上", "WARNING"),
    ("仅信息", "INFO"),
)

#: 自动刷新间隔
REFRESH_INTERVAL_MS = 2000


def _level_matches(line: str, level: str) -> bool:
    """日志行是否满足级别筛选。"""
    if not level:
        return True
    if level == "WARNING":
        return "[WARNING]" in line or "[ERROR]" in line or "[CRITICAL]" in line
    return f"[{level}]" in line


class LogViewDialog(QDialog):
    """查看运行日志。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        log_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(940, 640)
        self.log_path = Path(log_path) if log_path else Path(log_file_path())
        #: 最近一次读到的原始行
        self.lines: list[str] = []
        #: 当前筛选后显示的行
        self.visible_lines: list[str] = []
        self.show_errors = True

        self._build_ui()
        self.reload()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)

    # -- 界面 ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("级别："))
        self.level_combo = QComboBox(self)
        for label, _key in LEVEL_CHOICES:
            self.level_combo.addItem(label)
        self.level_combo.currentIndexChanged.connect(lambda _index: self.apply_filter())
        row.addWidget(self.level_combo)

        row.addSpacing(12)
        row.addWidget(QLabel("关键字："))
        self.keyword_edit = QLineEdit(self)
        self.keyword_edit.setPlaceholderText("任务号、文件名、错误片段…")
        self.keyword_edit.setClearButtonEnabled(True)
        self.keyword_edit.textChanged.connect(lambda _text: self.apply_filter())
        row.addWidget(self.keyword_edit, 1)

        row.addSpacing(12)
        row.addWidget(QLabel("读取行数："))
        self.tail_spin = QSpinBox(self)
        self.tail_spin.setRange(100, 20000)
        self.tail_spin.setSingleStep(500)
        self.tail_spin.setValue(2000)
        self.tail_spin.valueChanged.connect(lambda _value: self.reload())
        row.addWidget(self.tail_spin)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        self.refresh_button = QPushButton("刷新(&R)")
        self.refresh_button.clicked.connect(self.reload)
        row2.addWidget(self.refresh_button)
        self.auto_check = QCheckBox("自动刷新（每 2 秒）")
        self.auto_check.setChecked(True)
        row2.addWidget(self.auto_check)
        self.open_button = QPushButton("打开日志所在目录(&O)")
        self.open_button.clicked.connect(self._on_open_folder)
        row2.addWidget(self.open_button)
        row2.addStretch(1)
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #666;")
        row2.addWidget(self.info_label)
        layout.addLayout(row2)

        self.view = QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        self.view.setFont(font)
        self.view.setMaximumBlockCount(20000)
        layout.addWidget(self.view, 1)

        hint = QLabel(
            f"日志文件：{self.log_path}　（按 5MB 轮转，保留 5 份；"
            "日志只读，程序不会在查看时改动它）"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777;")
        layout.addWidget(hint)

    # -- 数据 ---------------------------------------------------------------

    def _tail(self, count: int) -> list[str]:
        """读文件最后若干行。"""
        path = self.log_path
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError as exc:  # pragma: no cover - 文件被占用时
            logger.warning("读取日志失败：%s", exc)
            if self.show_errors:
                QMessageBox.warning(self, "运行日志", f"读取日志失败：{exc}")
            return []
        lines = content.splitlines()
        if count > 0 and len(lines) > count:
            lines = lines[-count:]
        return lines

    def reload(self) -> None:
        """重新读文件并套用筛选。"""
        self.lines = self._tail(int(self.tail_spin.value()))
        self.apply_filter()

    def apply_filter(self) -> None:
        keyword = self.keyword_edit.text().strip().lower()
        level = LEVEL_CHOICES[self.level_combo.currentIndex()][1]
        visible = [
            line
            for line in self.lines
            if _level_matches(line, level) and (not keyword or keyword in line.lower())
        ]
        self.visible_lines = visible
        self.view.setPlainText("\n".join(visible))
        if visible:
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        size_text = ""
        if self.log_path.exists():
            size_text = f"　文件 {self.log_path.stat().st_size / 1024:.0f} KB"
        self.info_label.setText(f"共 {len(self.lines)} 行，显示 {len(visible)} 行{size_text}")

    # -- 动作 ---------------------------------------------------------------

    def _on_timer(self) -> None:
        if self.auto_check.isChecked() and self.isVisible():
            self.reload()

    def _on_open_folder(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        folder = self.log_path.parent
        if not folder.exists():
            if self.show_errors:
                QMessageBox.information(self, "运行日志", f"日志目录还不存在：{folder}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
