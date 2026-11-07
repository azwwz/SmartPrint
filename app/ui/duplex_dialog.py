"""手动双面打印窗口（阶段六）。

readme 阶段六要求用户任何时候都能看清：

    当前任务 / 当前批次 / 当前纸张 / 当前打印面 / 当前页码 / 状态

并且只提供四种操作：打印正常、打印异常、暂停任务、取消任务。
本窗口把这些信息和操作集中在一个地方，严格遵守两条规则：

* 一面提交之后，**必须先由用户确认完整出纸**（或报告异常）才能提交下一页；
* 队列空闲、退出码 0 都不会自动推进流程。

窗口结构：

1. 顶部：任务信息（任务号、页数、纸张）。
2. 进度区：当前批次、当前纸张、打印面、页码、状态、进度百分比。
3. 纸张表格：每张物理纸的正面 / 背面页码与状态（当前纸张高亮）。
4. 本次结果：命令行、退出码、队列快照与「命令已发送 ≠ 纸张已出」的提示。
5. 操作按钮：开始 / 提交当前页 / 打印正常 / 打印异常 / 确认已翻面 / 暂停 / 取消。
6. 流程日志：本次窗口内每一步的时间与结论。
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.config import AppConfig
from app.core.duplex import (
    FACE_CONFIRMED,
    FACE_SUBMITTED,
    FACE_VOID,
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_ERROR,
    PHASE_FRONT,
    PHASE_PAUSED,
    PHASE_WAITING_FLIP,
)
from app.core.models import Task
from app.services.duplex_service import (
    DuplexPrintService,
    DuplexServiceError,
    DuplexSubmitResult,
)
from app.services.print_service import describe_outcome
from app.ui.sheet_diagram import (
    SheetDiagramWidget,
    flip_instruction_text,
    flip_scope_text,
)

logger = logging.getLogger("smartprint.ui.duplex")

OK_COLOR = "#2E7D32"
WARN_COLOR = "#E65100"
ERROR_COLOR = "#C62828"
MUTED_COLOR = "#666666"
ACTIVE_COLOR = "#7A3EA8"

_SHEET_COLUMNS = ("轮次", "批次", "纸张", "正面", "正面状态", "背面", "背面状态", "整张状态")

_PHASE_COLORS = {
    PHASE_FRONT: ACTIVE_COLOR,
    PHASE_CONFIRM: WARN_COLOR,
    PHASE_WAITING_FLIP: WARN_COLOR,
    PHASE_BACK: ACTIVE_COLOR,
    PHASE_PAUSED: MUTED_COLOR,
    PHASE_COMPLETED: OK_COLOR,
    PHASE_CANCELLED: MUTED_COLOR,
    PHASE_ERROR: ERROR_COLOR,
}

_FACE_COLORS = {
    FACE_CONFIRMED: OK_COLOR,
    FACE_SUBMITTED: WARN_COLOR,
    FACE_VOID: ERROR_COLOR,
}

INTRO_TEXT = (
    "这里会把每一张纸拆成正反两面，逐面提交、逐面确认：\n"
    "· 每次只提交一个页面；\n"
    "· 提交后必须由你检查实际纸张，确认「打印正常」才会继续下一页；\n"
    "· 本批正面全部确认后提示翻面，确认已翻面才开始打印背面"
    "（背面从纸堆最上面那张纸打起，即纸张编号降序）；\n"
    "· 正面打完最后一张纸落在最上面，翻面前请先整叠理齐；\n"
    "· 打印命令发送成功、Windows 队列空闲，都不代表纸张已经完整出纸；\n"
    "· 任意一面出现异常时，整张纸（正反两面）一起作废，退出当前循环；\n"
    "· 当前循环全部结束后，自动开始补打循环，逐张补打、同样逐面确认；\n"
    "· 补打轮只处理一张纸：正面确认后单独翻这一张，再确认背面；\n"
    "· 补打中再次异常，这张纸进入下一轮补打清单。\n\n"
    "每批多少张纸由设置里的「每批纸张数」决定，补打轮与本批大小无关。\n"
    "点「开始手动双面打印」后，所有进度都会写入数据库，关闭程序也不会丢失。"
)


class DuplexDialog(QDialog):
    """逐面确认的手动双面打印窗口。"""

    def __init__(
        self,
        task: Task,
        service: DuplexPrintService,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.service = service
        self.config = config
        self.snapshot = service.snapshot(task.task_uid, task)
        self.last_result: DuplexSubmitResult | None = None
        self.log_lines: list[str] = []
        #: 验收脚本会关掉弹窗，避免阻塞
        self.confirm_dialogs = True
        self.show_errors = True

        self.setWindowTitle(f"手动双面打印 — {task.document_name}")
        self.resize(980, 820)

        self._build_ui()
        self._log("窗口已打开" + ("，读取到已有进度" if self.snapshot else "，尚未开始"))
        self.refresh()

    # -- 界面搭建 -----------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_progress_group())
        layout.addWidget(self._build_diagram_group())
        layout.addWidget(self._build_sheet_group(), 3)
        layout.addWidget(self._build_anomaly_group(), 1)
        layout.addWidget(self._build_result_group(), 2)
        layout.addWidget(self._build_log_group(), 1)
        layout.addLayout(self._build_buttons())

    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QVBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        title = QLabel(self.task.document_name)
        font = title.font()
        font.setPointSize(13)
        font.setBold(True)
        title.setFont(font)
        row.addWidget(title)

        self.info_label = QLabel(
            f"任务号 {self.task.task_uid}　|　{self.task.page_count} 页　|　"
            f"{self.task.size_text}　|　预计 "
            f"{(int(self.task.page_count) + 1) // 2} 张纸"
        )
        self.info_label.setStyleSheet(f"color:{MUTED_COLOR};")
        row.addWidget(self.info_label)
        return header

    def _build_progress_group(self) -> QGroupBox:
        group = QGroupBox("当前进度（未确认之前不会提交下一页）")
        box = QVBoxLayout(group)

        self.headline_label = QLabel("尚未开始")
        font = self.headline_label.font()
        font.setPointSize(12)
        font.setBold(True)
        self.headline_label.setFont(font)
        self.headline_label.setWordWrap(True)
        box.addWidget(self.headline_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        reprint_grid = QGridLayout()
        reprint_grid.setHorizontalSpacing(18)
        reprint_grid.setVerticalSpacing(4)

        def add_field(target: QGridLayout, row: int, column: int, name: str) -> QLabel:
            label_name = QLabel(f"{name}：")
            label_name.setStyleSheet(f"color:{MUTED_COLOR};")
            value = QLabel("—")
            value.setStyleSheet("font-weight: bold;")
            value.setWordWrap(True)
            target.addWidget(label_name, row, column * 2)
            target.addWidget(value, row, column * 2 + 1)
            return value

        self.batch_label = add_field(grid, 0, 0, "当前批次")
        self.sheet_label = add_field(grid, 0, 1, "当前纸张")
        self.face_label = add_field(grid, 0, 2, "当前打印面")
        self.page_label = add_field(grid, 0, 3, "当前页码")
        self.progress_label = add_field(grid, 1, 0, "总进度")
        self.task_status_label = add_field(grid, 1, 1, "任务状态")
        self.flip_label = add_field(grid, 1, 2, "翻面确认")
        self.result_count_label = add_field(grid, 1, 3, "剩余待确认")
        box.addLayout(grid)

        # 阶段七：轮次、纸张进度、待补打清单、异常记录
        self.round_label = add_field(reprint_grid, 0, 0, "当前轮次")
        self.sheet_progress_label = add_field(reprint_grid, 0, 1, "纸张进度")
        self.reprint_label = add_field(reprint_grid, 0, 2, "待补打清单")
        self.anomaly_label = add_field(reprint_grid, 0, 3, "异常记录")
        box.addLayout(reprint_grid)

        self.next_label = QLabel("")
        self.next_label.setWordWrap(True)
        self.next_label.setStyleSheet(f"color:{MUTED_COLOR};")
        box.addWidget(self.next_label)
        return group

    def _build_diagram_group(self) -> QGroupBox:
        """阶段八：纸张正反面示意图 + 翻面方向提示。"""
        group = QGroupBox("纸张示意图与翻面方向（照着这张图放纸）")
        row = QHBoxLayout(group)
        row.setContentsMargins(8, 8, 8, 8)

        self.diagram = SheetDiagramWidget(group)
        self.diagram.setMinimumWidth(360)
        await_placeholder = self.service.config.flip_direction
        self.diagram.set_sheet(
            sheet_number=1,
            front_page=1,
            back_page=2,
            front_state="pending",
            back_state="pending",
            current_side="front",
            flip_direction=await_placeholder,
        )
        self.diagram.clear()
        row.addWidget(self.diagram, 2)

        right = QVBoxLayout()
        self.flip_hint_label = QLabel("")
        self.flip_hint_label.setWordWrap(True)
        self.flip_hint_label.setText(flip_instruction_text(await_placeholder))
        right.addWidget(self.flip_hint_label)

        self.diagram_note_label = QLabel("")
        self.diagram_note_label.setWordWrap(True)
        self.diagram_note_label.setStyleSheet(f"color:{MUTED_COLOR};")
        right.addWidget(self.diagram_note_label)
        right.addStretch(1)
        row.addLayout(right, 3)
        return group

    def _build_sheet_group(self) -> QGroupBox:
        group = QGroupBox("纸张清单（一张纸两个面；任意一面异常则整张纸作废）")
        box = QVBoxLayout(group)
        self.sheet_table = QTableWidget(0, len(_SHEET_COLUMNS), self)
        self.sheet_table.setHorizontalHeaderLabels(list(_SHEET_COLUMNS))
        self.sheet_table.verticalHeader().setVisible(False)
        self.sheet_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sheet_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sheet_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sheet_table.setAlternatingRowColors(True)
        header = self.sheet_table.horizontalHeader()
        for column in range(len(_SHEET_COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(_SHEET_COLUMNS) - 1, QHeaderView.Stretch)
        box.addWidget(self.sheet_table)
        return group

    def _build_anomaly_group(self) -> QGroupBox:
        group = QGroupBox("异常与补打记录（整张纸作废；正常循环结束后统一补打）")
        box = QVBoxLayout(group)
        self.anomaly_view = QPlainTextEdit(group)
        self.anomaly_view.setReadOnly(True)
        self.anomaly_view.setMaximumBlockCount(500)
        self.anomaly_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self.anomaly_view.setPlainText("还没有异常记录。")
        box.addWidget(self.anomaly_view)
        return group

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("本次结果（命令已发送 ≠ 纸张已出）")
        box = QVBoxLayout(group)
        self.result_label = QLabel("尚未提交任何页面。")
        self.result_label.setWordWrap(True)
        box.addWidget(self.result_label)

        self.result_view = QPlainTextEdit(group)
        self.result_view.setReadOnly(True)
        self.result_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self.result_view.setPlainText(INTRO_TEXT)
        box.addWidget(self.result_view, 1)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("流程日志（本次窗口）")
        box = QVBoxLayout(group)
        self.log_view = QPlainTextEdit(group)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        box.addWidget(self.log_view)
        return group

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.start_button = QPushButton("开始手动双面打印(&B)", self)
        self.start_button.setToolTip("创建打印计划：按每批纸张数拆分，先正面、翻面后背面")
        self.start_button.clicked.connect(self._on_start_clicked)
        row.addWidget(self.start_button)

        self.submit_button = QPushButton("提交当前页(&S)", self)
        self.submit_button.setToolTip("只提交当前这一面；提交后必须确认完整出纸才能继续")
        self.submit_button.clicked.connect(self._on_submit_clicked)
        row.addWidget(self.submit_button)

        self.confirm_button = QPushButton("打印正常：确认完整出纸(&G)", self)
        self.confirm_button.setToolTip("已经检查实际纸张，确认这一面完整出纸")
        self.confirm_button.clicked.connect(self._on_confirm_clicked)
        row.addWidget(self.confirm_button)

        self.anomaly_button = QPushButton("打印异常(&E)", self)
        self.anomaly_button.setToolTip("这一面没有完整出纸：整张纸作废，正反两面都需要补打")
        self.anomaly_button.clicked.connect(self._on_anomaly_clicked)
        row.addWidget(self.anomaly_button)

        self.flip_button = QPushButton("确认已翻面，开始打印背面(&F)", self)
        self.flip_button.setToolTip("整理好本批纸张并放回进纸托盘后，再点这里")
        self.flip_button.clicked.connect(self._on_flip_clicked)
        row.addWidget(self.flip_button)

        row.addStretch(1)

        self.pause_button = QPushButton("暂停任务(&T)", self)
        self.pause_button.clicked.connect(self._on_pause_clicked)
        row.addWidget(self.pause_button)

        self.cancel_button = QPushButton("取消任务(&X)", self)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self.cancel_button)

        self.close_button = QPushButton("关闭(&C)", self)
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        return row

    # -- 刷新 ---------------------------------------------------------------

    def refresh(self) -> None:
        """重新读取服务状态并刷新界面（每次操作后都会调用）。"""
        try:
            self.snapshot = self.service.snapshot(self.task.task_uid, self.task)
        except Exception:  # noqa: BLE001 - 数据库异常不应让窗口崩掉
            logger.exception("读取双面打印状态失败")
            self.snapshot = None
        self._update_progress()
        self._update_sheet_table()
        self._update_buttons()

    def _update_progress(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            self.headline_label.setText("尚未开始手动双面打印")
            self.headline_label.setStyleSheet(f"color:{MUTED_COLOR};")
            for label in (
                self.batch_label,
                self.sheet_label,
                self.face_label,
                self.page_label,
                self.progress_label,
                self.task_status_label,
                self.flip_label,
                self.result_count_label,
                self.round_label,
                self.sheet_progress_label,
                self.reprint_label,
                self.anomaly_label,
            ):
                label.setText("—")
            self.next_label.setText(
                "说明：开始后会按每批 "
                f"{self.service.config.sheets_per_batch} 张纸拆分，逐面提交、逐面确认。"
            )
            self._update_diagram(None)
            return

        color = _PHASE_COLORS.get(snapshot.phase, MUTED_COLOR)
        self.headline_label.setText(snapshot.status_text)
        self.headline_label.setStyleSheet(f"color:{color};")
        self.batch_label.setText(snapshot.batch_position_text)
        self.sheet_label.setText(snapshot.sheet_position_text)
        self.face_label.setText(snapshot.face_text)
        self.page_label.setText(snapshot.page_text)
        self.progress_label.setText(snapshot.progress_text)
        task = self.service.repository.get_task(self.task.task_uid)
        self.task_status_label.setText(task.status.label if task else "—")
        if snapshot.phase in (PHASE_BACK, PHASE_WAITING_FLIP):
            self.flip_label.setText(
                "已确认" if snapshot.flip_confirmed_for_current else "等待确认翻面"
            )
        elif snapshot.phase in (PHASE_FRONT, PHASE_CONFIRM):
            self.flip_label.setText("未确认（正面打印中）")
        else:
            self.flip_label.setText("—")
        remaining = max(0, snapshot.total_faces - snapshot.confirmed_count)
        self.result_count_label.setText(f"{remaining} 面")

        self.round_label.setText(
            f"{snapshot.round_text}（第 {snapshot.current_round} 轮，共第 {snapshot.latest_round} 轮）"
            if snapshot.has_reprint
            else snapshot.round_text
        )
        self.sheet_progress_label.setText(snapshot.sheet_progress_text)
        pending = snapshot.reprint_pending_count
        if pending:
            self.reprint_label.setText(snapshot.reprint_pending_text)
        elif snapshot.is_reprint_round:
            self.reprint_label.setText(f"{snapshot.round_text}进行中：{snapshot.reprint_round_text}")
        else:
            self.reprint_label.setText("无")
        self.reprint_label.setStyleSheet(
            f"font-weight: bold; color:{ERROR_COLOR};" if pending else "font-weight: bold;"
        )
        self.anomaly_label.setText(snapshot.anomaly_text)
        self.anomaly_label.setStyleSheet(
            f"font-weight: bold; color:{WARN_COLOR};" if snapshot.anomaly_count else "font-weight: bold;"
        )
        self.next_label.setText(snapshot.next_action_text)
        self._update_anomaly_view(snapshot)
        self._update_diagram(snapshot)

    def _find_sheet(self, snapshot, sheet_number: int, round_index: int):
        for sheet in snapshot.sheets:
            if sheet.number == sheet_number and sheet.round_index == round_index:
                return sheet
        return None

    def _update_diagram(self, snapshot) -> None:
        """阶段八：把当前这张纸的正反面画出来，并给出翻面方向提示。"""
        direction = self.service.config.flip_direction
        self.flip_hint_label.setText(flip_instruction_text(direction))
        if snapshot is None:
            self.diagram.clear()
            self.diagram_note_label.setText(
                "开始打印后，这里会画出当前这张纸的正面和背面，"
                "并用蓝框标出「现在应该放哪一面」。"
            )
            return
        sheet = None
        if snapshot.current is not None:
            sheet = self._find_sheet(
                snapshot, snapshot.current.sheet_number, snapshot.current.round_index
            )
        if sheet is None and snapshot.sheets:
            sheet = snapshot.sheets[-1]
        if sheet is None:
            self.diagram.clear()
            self.diagram_note_label.setText("还没有纸张信息。")
            return
        current_side = ""
        if (
            snapshot.current is not None
            and snapshot.current.sheet_number == sheet.number
            and snapshot.current.round_index == sheet.round_index
        ):
            current_side = snapshot.current.face
        self.diagram.set_sheet(
            sheet_number=sheet.number,
            front_page=sheet.front_page,
            back_page=sheet.back_page,
            front_state=sheet.front_status,
            back_state=sheet.back_status,
            current_side=current_side,
            round_index=sheet.round_index,
            flip_direction=direction,
        )
        if snapshot.phase == PHASE_WAITING_FLIP:
            note = flip_scope_text(
                direction,
                scope="本批纸张",
                sheet_number=(
                    sheet.number if sheet.round_index > 0 else None
                ),
                round_index=sheet.round_index,
            )
            self.diagram_note_label.setStyleSheet(f"color:{WARN_COLOR};")
        else:
            note = self.diagram.description + "　蓝框是现在该放进打印机的那一面。"
            self.diagram_note_label.setStyleSheet(f"color:{MUTED_COLOR};")
        self.diagram_note_label.setText(note)

    def _update_anomaly_view(self, snapshot) -> None:
        """刷新异常与补打记录（readme 7.6 的可见部分）。"""
        lines: list[str] = []
        if snapshot.reprint_pending_count:
            lines.append(
                f"待补打清单（{snapshot.reprint_pending_count} 张）："
                f"{snapshot.reprint_pending_text}"
            )
            lines.append(
                "补打规则：等当前正常循环全部结束后统一处理；同样逐页提交、"
                "逐页确认，整张纸两面都确认才算完成。"
            )
        elif snapshot.is_reprint_round:
            lines.append(
                f"{snapshot.round_text}进行中：{snapshot.reprint_round_text}"
            )
            lines.append(
                "补打规则：逐张处理，同一张纸正面确认后翻面、再确认背面；"
                "补打中再次异常，这张纸会进入下一轮补打清单。"
            )
        if snapshot.anomaly_count:
            if lines:
                lines.append("")
            lines.append(f"异常记录：{snapshot.anomaly_text}")
            for anomaly in snapshot.anomalies:
                lines.append(f"· {anomaly.to_text()}")
        else:
            lines.append("还没有异常记录。")
        text = "\n".join(lines)
        if self.anomaly_view.toPlainText() != text:
            self.anomaly_view.setPlainText(text)

    def _update_sheet_table(self) -> None:
        snapshot = self.snapshot
        sheets = snapshot.sheets if snapshot else ()
        self.sheet_table.blockSignals(True)
        self.sheet_table.setRowCount(len(sheets))
        current_sheet = snapshot.current_sheet_number if snapshot else None
        current_round = snapshot.current_round if snapshot else 0
        current_row = None
        for row, sheet in enumerate(sheets):
            values = (
                sheet.round_text,
                f"第 {sheet.batch_index} 批",
                f"第 {sheet.number} 张",
                sheet.front_text,
                self._face_status_text(sheet.front_status),
                sheet.back_text,
                self._face_status_text(sheet.back_status),
                sheet.status_text,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column in (0, 1, 2, 3, 5):
                    item.setTextAlignment(Qt.AlignCenter)
                if column in (4, 6):
                    status = sheet.front_status if column == 4 else sheet.back_status
                    item.setForeground(QColor(_FACE_COLORS.get(status, "#333333")))
                if sheet.voided:
                    item.setForeground(QColor(ERROR_COLOR))
                elif sheet.round_index > 0:
                    item.setForeground(QColor(ACTIVE_COLOR))
                if (
                    sheet.number == current_sheet
                    and sheet.round_index == current_round
                ):
                    item.setBackground(QColor("#FFF4D6"))
                    current_row = row
                self.sheet_table.setItem(row, column, item)
        self.sheet_table.blockSignals(False)
        if current_row is not None:
            self.sheet_table.selectRow(current_row)

    @staticmethod
    def _face_status_text(status: str) -> str:
        from app.core.duplex import face_status_label

        return face_status_label(status)

    def _update_buttons(self) -> None:
        snapshot = self.snapshot
        active = snapshot is not None and not snapshot.is_finished
        self.start_button.setEnabled(snapshot is None or snapshot.is_finished)
        if snapshot is None:
            self.start_button.setText("开始手动双面打印(&B)")
        elif snapshot.is_finished:
            self.start_button.setText("重新开始打印(&B)")
        else:
            self.start_button.setText("已在打印流程中(&B)")

        self.submit_button.setEnabled(bool(snapshot and snapshot.can_submit))
        self.confirm_button.setEnabled(bool(snapshot and snapshot.can_confirm))
        self.anomaly_button.setEnabled(bool(snapshot and snapshot.can_confirm))
        self.flip_button.setEnabled(bool(snapshot and snapshot.can_flip))

        paused = bool(snapshot and snapshot.can_resume)
        self.pause_button.setText("恢复任务(&U)" if paused else "暂停任务(&T)")
        self.pause_button.setEnabled(bool(snapshot and (snapshot.can_pause or paused)))
        self.cancel_button.setEnabled(bool(snapshot and snapshot.can_cancel))
        self.submit_button.setDefault(
            bool(snapshot and snapshot.can_submit)
        )

        if not active:
            self.submit_button.setEnabled(False)
            self.confirm_button.setEnabled(False)
            self.anomaly_button.setEnabled(False)
            self.flip_button.setEnabled(False)

    # -- 操作 ---------------------------------------------------------------

    def _on_start_clicked(self) -> None:
        self.start(restart=bool(self.snapshot and self.snapshot.is_finished))

    def start(self, *, restart: bool = False):
        """开始（或重新开始）双面打印；返回最新快照。"""
        try:
            snapshot = self.service.start(self.task, restart=restart)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("开始手动双面打印失败")
            self._handle_error(f"开始失败：{exc}")
            return None
        self.snapshot = snapshot
        self._log(
            f"开始手动双面打印：{snapshot.session.total_sheets} 张纸，"
            f"{snapshot.session.total_batches} 批，共 {len(snapshot.faces)} 个打印面"
        )
        self._set_result("开始成功，等待提交第一个打印面。", MUTED_COLOR)
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    def _on_submit_clicked(self) -> None:
        self.submit_current()

    def submit_current(self) -> DuplexSubmitResult | None:
        """提交当前打印面（验收脚本可以直接调用）。"""
        snapshot = self.snapshot
        if snapshot is None or not snapshot.can_submit:
            self._handle_error("当前没有可以提交的打印面")
            return None
        face = snapshot.current
        if self.confirm_dialogs:
            answer = QMessageBox.question(
                self,
                "确认提交",
                f"即将提交：{face.full_label if face else '当前打印面'}\n"
                f"单页文件：{face.page_file_name if face else '—'}\n\n"
                "只提交这一页。命令发送成功后不会自动继续下一页，"
                "必须由你确认实际纸张完整出纸。\n\n确定要发送吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None

        self.submit_button.setEnabled(False)
        try:
            result = self.service.submit_current(self.task)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - 打印异常要显示给用户
            logger.exception("提交打印面失败")
            self._handle_error(f"提交失败：{exc}")
            return None
        finally:
            self._update_buttons()

        self.last_result = result
        if result.outcome is not None:
            self.result_view.setPlainText(describe_outcome(result.outcome))
        if result.dry_run:
            self._set_result("演练完成：没有发送任何打印数据。", MUTED_COLOR)
        elif not result.submitted:
            self._set_result(f"未发送：{result.error}", ERROR_COLOR)
            for message in result.messages:
                self._log(message)
        elif result.outcome is not None and result.outcome.ok:
            self._set_result(
                "命令已发送。现在请检查实际纸张，确认这一面完整出纸后再点「打印正常」。",
                WARN_COLOR,
            )
        else:
            self._set_result(
                "打印命令已发出，但返回异常。纸上可能已有内容，请检查后再选择"
                "「打印正常」或「打印异常」；程序不会自动重试。",
                ERROR_COLOR,
            )
        if result.snapshot is not None and result.snapshot.current is not None:
            face = result.snapshot.current
            if result.submitted:
                self._log(
                    f"已提交 {face.full_label}（{face.page_file_name}），等待用户确认"
                )
        self.refresh()
        return result

    def _on_confirm_clicked(self) -> None:
        self.confirm_normal()

    def confirm_normal(self):
        """用户确认当前打印面完整出纸。"""
        if self.snapshot is None or not self.snapshot.can_confirm:
            self._handle_error("当前没有等待确认的打印面")
            return None
        face = self.snapshot.current
        if self.confirm_dialogs:
            answer = QMessageBox.question(
                self,
                "确认完整出纸",
                f"确认 {face.full_label if face else '这一面'} 已经完整、正常地从打印机出来了吗？\n\n"
                "确认后才会继续下一个打印面。如果纸张有问题，请点「打印异常」。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
        try:
            snapshot = self.service.confirm_current(self.task)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        self.snapshot = snapshot
        self._log(
            f"用户确认{face.full_label if face else '当前面'}完整出纸；"
            f"进度 {snapshot.progress_text}"
        )
        self._set_result(
            f"已确认完整出纸。下一步：{snapshot.next_action_text}", OK_COLOR
        )
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    def _on_flip_clicked(self) -> None:
        self.confirm_flip()

    def confirm_flip(self):
        """用户确认已经整理纸张并翻面。"""
        if self.snapshot is None or not self.snapshot.can_flip:
            self._handle_error("当前还不能确认翻面")
            return None
        before = self.snapshot
        current = before.current
        direction = self.service.config.flip_direction
        if before.is_reprint_round:
            scope = (
                f"{before.round_text}：第 {current.sheet_number if current else '?'} 张纸。\n"
                + flip_scope_text(
                    direction,
                    scope="这一张纸",
                    sheet_number=current.sheet_number if current else None,
                    round_index=before.current_round,
                )
            )
        else:
            scope = (
                f"第 {before.current_batch} 批（本批 {before.batch_sheet_count} 张纸）。\n"
                "1. 检查本批所有正面是否完整；\n"
                "2. 取出打印好的纸张；\n"
                f"3. {flip_instruction_text(direction)}"
            )
        if self.confirm_dialogs:
            answer = QMessageBox.question(
                self,
                "确认翻面",
                f"请确认已经处理好纸张：\n{scope}\n\n"
                "确认现在可以开始打印背面吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
        try:
            snapshot = self.service.confirm_flip(self.task)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        self.snapshot = snapshot
        self._log(f"{before.round_text}：用户确认已翻面，开始逐页打印背面")
        self._set_result("翻面已确认。现在可以逐页提交背面。", OK_COLOR)
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    def _on_anomaly_clicked(self) -> None:
        self.report_anomaly()

    def report_anomaly(self, reason: str = ""):
        """报告当前打印面异常：整张纸作废，退出当前循环，稍后统一补打。"""
        if self.snapshot is None or not self.snapshot.can_confirm:
            self._handle_error("当前没有待核对的打印面")
            return None
        before = self.snapshot
        face = before.current
        sheet = next(
            (
                item
                for item in before.sheets
                if face is not None
                and item.number == face.sheet_number
                and item.round_index == face.round_index
            ),
            None,
        )
        reason = reason.strip()
        if not reason and self.confirm_dialogs:
            text, ok = QInputDialog.getText(
                self,
                "异常说明（可留空）",
                "简单说明异常情况（例如：卡纸、缺纸、纸张污损）：",
            )
            if ok:
                reason = text.strip()
        if self.confirm_dialogs:
            answer = QMessageBox.warning(
                self,
                "整张纸作废",
                f"{before.round_text} · 第 {face.sheet_number if face else '?'} 张纸"
                "将被整张作废：\n"
                f"正面：{sheet.front_text if sheet else '?'}　"
                f"背面：{sheet.back_text if sheet else '?'}\n\n"
                "按规则，只要一面异常，正反两面都需要重新打印，"
                "不能只补打其中一面。\n\n"
                "请先检查打印机、取出或处理异常纸张，确认后这张纸会退出当前循环；"
                "等当前正常纸张全部完成，再统一补打。\n\n"
                "确定要标记这张纸作废吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
        try:
            snapshot = self.service.report_anomaly(self.task, reason)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        self.snapshot = snapshot
        sheet_number = face.sheet_number if face else "?"
        note = f"（{reason}）" if reason else ""
        self._log(
            f"{before.round_text} 第 {sheet_number} 张纸整张作废{note}；"
            "正反两面都进入待补打清单"
        )
        if snapshot.current_round > before.current_round:
            message = (
                f"第 {sheet_number} 张纸整张作废；已开始{snapshot.round_text}。"
                "补打同样逐页提交、逐页确认，整张纸两面都确认才算完成。"
            )
            self._set_result(message, WARN_COLOR)
            self._log(message)
        elif snapshot.phase == PHASE_COMPLETED:
            message = "最后一张纸已作废并完成补打，任务完成。"
            self._set_result(message, OK_COLOR)
            self._log(message)
        else:
            message = (
                f"第 {sheet_number} 张纸整张作废，已退出当前循环；"
                f"正常循环继续，待补打 {snapshot.reprint_pending_count} 张。"
                "等当前循环结束后会统一补打。"
            )
            self._set_result(message, ERROR_COLOR)
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    def _on_pause_clicked(self) -> None:
        if self.snapshot is not None and self.snapshot.can_resume:
            self.resume_task()
        else:
            self.pause_task()

    def pause_task(self):
        try:
            snapshot = self.service.pause(self.task)
        except (DuplexServiceError, Exception) as exc:  # noqa: BLE001
            if isinstance(exc, DuplexServiceError):
                self._handle_error(str(exc))
            else:
                logger.exception("暂停任务失败")
                self._handle_error(f"暂停失败：{exc}")
            return None
        self.snapshot = snapshot
        self._log("任务已暂停；未确认的打印面保持待核对")
        self._set_result("任务已暂停，进度已保存。", MUTED_COLOR)
        self.refresh()
        return snapshot

    def resume_task(self):
        try:
            snapshot = self.service.resume(self.task)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        self.snapshot = snapshot
        self._log(f"任务已恢复；当前状态：{snapshot.status_text}")
        self._set_result("任务已恢复。", OK_COLOR)
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    def _on_cancel_clicked(self) -> None:
        self.cancel_task()

    def cancel_task(self):
        if self.snapshot is None or not self.snapshot.can_cancel:
            self._handle_error("当前状态不能取消任务")
            return None
        if self.confirm_dialogs:
            answer = QMessageBox.question(
                self,
                "取消任务",
                f"确定取消这个打印任务吗？\n\n"
                f"已确认 {self.snapshot.confirmed_count}/{self.snapshot.total_faces} 面，"
                "进度记录会保留。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
        try:
            snapshot = self.service.cancel(self.task)
        except DuplexServiceError as exc:
            self._handle_error(str(exc))
            return None
        self.snapshot = snapshot
        self._log("任务已取消")
        self._set_result("任务已取消，进度记录已保留。", MUTED_COLOR)
        self.result_view.setPlainText(snapshot.to_text())
        self.refresh()
        return snapshot

    # -- 辅助 ---------------------------------------------------------------

    def _set_result(self, text: str, color: str) -> None:
        self.result_label.setText(f"<span style='color:{color};'>{text}</span>")

    def _log(self, text: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {text}"
        self.log_lines.append(line)
        self.log_view.appendPlainText(line)

    def _handle_error(self, message: str) -> None:
        logger.warning("双面打印操作被拒绝：%s", message)
        self._log(f"操作未执行：{message}")
        self._set_result(message, ERROR_COLOR)
        if self.show_errors:
            QMessageBox.warning(self, "操作未执行", message)
