"""异常记录总览窗口（阶段八）。

readme 阶段八要求「增加异常处理界面」。这里把 ``duplex_anomalies`` 里的记录
汇总成一张表：哪份文档、第几轮、第几张纸、哪一面出问题、原因、有没有补打完。

这一页只负责**看得全、找得到**：真正的处置动作（打印异常、补打、暂停、取消）
仍在「手动双面打印」窗口里做，点「打开该任务的双面打印窗口」可以直接跳过去。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.duplex import DuplexAnomaly
from app.database.task_repository import TaskRepository

logger = logging.getLogger("smartprint.ui.anomaly_dialog")

#: 表格列
ANOMALY_COLUMNS = (
    "时间",
    "任务号",
    "文档",
    "轮次",
    "纸张",
    "异常面",
    "正面页",
    "背面页",
    "原因",
    "状态",
    "补打结果",
)

#: 一次最多列出的记录数
ANOMALY_LIMIT = 500


def _status_text(anomaly: DuplexAnomaly) -> str:
    return "已补打完成" if anomaly.resolved else "待补打"


class AnomalyDialog(QDialog):
    """异常记录总览。"""

    def __init__(
        self,
        repository: TaskRepository,
        parent: QWidget | None = None,
        *,
        open_duplex: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("异常记录")
        self.resize(1080, 620)
        self.repository = repository
        #: 点「打开该任务的双面打印窗口」时调用，参数是 task_uid
        self.open_duplex = open_duplex
        #: 当前表格里每一行对应的异常（含任务号）
        self.rows: list[DuplexAnomaly] = []
        self.show_errors = True
        self._documents: dict[str, str] = {}

        self._build_ui()
        self.reload()

    # -- 界面 ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "只要一张纸的任意一面打印异常，整张纸（正面与背面）都会记在这里，"
            "并进入待补打清单。补打同样逐页提交、逐页确认，两面都确认才算完成；"
            "补打时再次异常会进入下一轮，记录会再增加一条。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #555;")
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.unresolved_check = QCheckBox("只看待补打")
        self.unresolved_check.setChecked(True)
        self.unresolved_check.toggled.connect(lambda _checked: self.apply_filter())
        row.addWidget(self.unresolved_check)

        row.addSpacing(12)
        row.addWidget(QLabel("关键字："))
        self.keyword_edit = QLineEdit(self)
        self.keyword_edit.setPlaceholderText("任务号、文档名、异常原因…")
        self.keyword_edit.setClearButtonEnabled(True)
        self.keyword_edit.textChanged.connect(lambda _text: self.apply_filter())
        row.addWidget(self.keyword_edit, 1)

        self.refresh_button = QPushButton("刷新(&R)")
        self.refresh_button.clicked.connect(self.reload)
        row.addWidget(self.refresh_button)

        self.open_button = QPushButton("打开该任务的双面打印窗口(&O)")
        self.open_button.clicked.connect(self._on_open_duplex)
        row.addWidget(self.open_button)
        layout.addLayout(row)

        self.table = QTableWidget(0, len(ANOMALY_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(ANOMALY_COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(len(ANOMALY_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        layout.addWidget(self.table, 3)

        self.detail_view = QPlainTextEdit(self)
        self.detail_view.setReadOnly(True)
        self.detail_view.setMaximumBlockCount(200)
        self.detail_view.setFixedHeight(110)
        layout.addWidget(self.detail_view, 1)

        footer = QHBoxLayout()
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #666;")
        footer.addWidget(self.info_label, 1)
        self.close_button = QPushButton("关闭(&C)")
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

    # -- 数据 ---------------------------------------------------------------

    def reload(self) -> None:
        try:
            records = self.repository.list_duplex_anomalies(None, limit=ANOMALY_LIMIT)
        except Exception as exc:  # noqa: BLE001 - 打开数据库失败时提示而不是崩
            logger.exception("读取异常记录失败")
            if self.show_errors:
                QMessageBox.warning(self, "异常记录", f"读取异常记录失败：{exc}")
            records = []
        self.rows = list(records)
        self._documents.clear()
        for record in self.rows:
            if record.task_uid not in self._documents:
                task = None
                try:
                    task = self.repository.get_task(record.task_uid)
                except Exception:  # noqa: BLE001
                    task = None
                self._documents[record.task_uid] = (
                    task.document_name if task else record.task_uid
                )
        self.apply_filter()

    def apply_filter(self) -> None:
        keyword = self.keyword_edit.text().strip().lower()
        only_open = self.unresolved_check.isChecked()
        visible: list[DuplexAnomaly] = []
        for record in self.rows:
            if only_open and record.resolved:
                continue
            if keyword:
                haystack = " ".join(
                    [
                        record.task_uid,
                        self._documents.get(record.task_uid, ""),
                        record.reason,
                        record.face_text,
                        str(record.sheet_number),
                    ]
                ).lower()
                if keyword not in haystack:
                    continue
            visible.append(record)

        self.table.setRowCount(len(visible))
        for row, record in enumerate(visible):
            values = (
                f"{record.created_at:%Y-%m-%d %H:%M:%S}" if record.created_at else "",
                record.task_uid,
                self._documents.get(record.task_uid, ""),
                record.round_text,
                f"第 {record.sheet_number} 张",
                record.face_text,
                str(record.front_page),
                str(record.back_page) if record.back_page else "无（空白背面）",
                record.reason or "（未填写）",
                _status_text(record),
                record.result_text if record.resolved else "",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                if column in (0, 3, 4, 5, 6, 7, 9):
                    item.setTextAlignment(Qt.AlignCenter)
                if record.resolved:
                    item.setForeground(Qt.darkGreen)
                self.table.setItem(row, column, item)

        total = len(self.rows)
        open_count = sum(1 for record in self.rows if not record.resolved)
        self.info_label.setText(
            f"共 {total} 条异常记录，其中待补打 {open_count} 条；当前显示 {len(visible)} 条"
        )
        self.detail_view.setPlainText("")
        self._on_selection_changed()

    # -- 动作 ---------------------------------------------------------------

    def _current_record(self) -> DuplexAnomaly | None:
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return None
        keyword = self.keyword_edit.text().strip().lower()
        only_open = self.unresolved_check.isChecked()
        visible: list[DuplexAnomaly] = []
        for record in self.rows:
            if only_open and record.resolved:
                continue
            if keyword:
                haystack = " ".join(
                    [
                        record.task_uid,
                        self._documents.get(record.task_uid, ""),
                        record.reason,
                        record.face_text,
                        str(record.sheet_number),
                    ]
                ).lower()
                if keyword not in haystack:
                    continue
            visible.append(record)
        if row < len(visible):
            return visible[row]
        return None

    def _on_selection_changed(self) -> None:
        record = self._current_record()
        enabled = record is not None and bool(self.open_duplex)
        self.open_button.setEnabled(enabled)
        if record is None:
            return
        document = self._documents.get(record.task_uid, "")
        self.detail_view.setPlainText(
            f"文档：{document}\n"
            f"任务号：{record.task_uid}\n"
            f"轮次：{record.round_text}\n"
            f"纸张：第 {record.sheet_number} 张（正面第 {record.front_page} 页，"
            f"背面第 {record.back_page or '—'} 页）\n"
            f"异常面：{record.face_text}\n"
            f"原因：{record.reason or '（未填写）'}\n"
            f"处理方式：{record.action}\n"
            f"状态：{_status_text(record)}"
            + (
                f"（第 {record.resolved_round} 轮补打完成于 "
                f"{record.resolved_at:%Y-%m-%d %H:%M:%S}）"
                if record.resolved and record.resolved_at
                else ""
            )
        )

    def _on_open_duplex(self) -> None:
        record = self._current_record()
        if record is None or not self.open_duplex:
            return
        task = None
        try:
            task = self.repository.get_task(record.task_uid)
        except Exception:  # noqa: BLE001
            task = None
        if task is None:
            if self.show_errors:
                QMessageBox.information(
                    self, "异常记录", f"任务已不存在：{record.task_uid}"
                )
            return
        self.open_duplex(record.task_uid)
