"""页面拆分对话框（阶段四）。

在这里可以看清「哪一页印在哪一面、哪一批、对应哪个单页文件」，
并在确认无误后生成打印文件：

- 上方：文档信息与每批纸张数量（默认取 config.json 的 paper.sheets_per_batch）；
- 中间三个页签：
  「批次拆分」每一批的纸张数与正反面页码；
  「打印顺序」逐页展开的提交顺序（每次只提交一个页面）；
  「页面检查」逐页尺寸、文字量与空白页提示；
- 下方：生成单页文件 / 打开任务目录 / 关闭，以及上一次生成的结果提示。

阶段四不接触真实打印机：这里只生成按页码命名的单页 PDF（page_001.pdf…），
真正把文件交给打印程序是阶段五、六的事情。

对话框只负责展示与触发，真正的计算和文件生成在
app/core/page_planner.py 与 app/services/page_jobs.py。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.config import AppConfig
from app.core.models import Task
from app.core.page_planner import (
    BACK_ORDER_DESCENDING,
    PagePlan,
    page_file_name,
)
from app.core.pdf_splitter import PageScan
from app.services.page_jobs import GenerationResult, PageJobError, PageJobService
from app.ui.pdf_preview_dialog import PdfPreviewDialog
from app.ui.settings_dialog import (
    MAX_SHEETS_PER_BATCH,
    MIN_SHEETS_PER_BATCH,
)

logger = logging.getLogger("smartprint.ui.page_plan")

#: 「批次拆分」表的列
BATCH_COLUMNS = ("批次", "纸张数", "正面页码", "背面页码", "单页文件", "说明")

#: 「打印顺序」表的列（一次提交 = 一行）
STEP_COLUMNS = ("顺序", "批次", "纸张", "打印面", "页码", "单页文件")

#: 「页面检查」表的列
PAGE_COLUMNS = ("页码", "尺寸", "文字数", "图片", "状态")

#: 每批纸张数量的可选范围——直接复用设置窗口的常量（settings_dialog 已导入），
#: 这样「页面拆分」和「设置」永远显示同一个上限，不会一处 20 一处 50。

#: 最后一批 / 空白页的提示底色
LAST_BATCH_COLOR = QColor("#E8F5E9")
BLANK_ROW_COLOR = QColor("#FFF8E1")
ERROR_COLOR = "#C62828"
OK_COLOR = "#2E7D32"


class PagePlanDialog(QDialog):
    """打印批次拆分与单页文件生成窗口。"""

    def __init__(
        self,
        task: Task,
        service: PageJobService | None = None,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.config = config
        self.service = service or PageJobService.from_config(
            config or AppConfig(), repository=None
        )
        self.plan: PagePlan | None = None
        self.scan: PageScan | None = None
        self.last_result: GenerationResult | None = None
        self.error_message: str = ""
        #: 出错时是否弹窗（自动化验收脚本会关掉它，避免阻塞）
        self.show_errors = True

        self.setWindowTitle(f"页面拆分 — {task.document_name}")
        self.resize(980, 680)

        self._build_ui()
        self.refresh()

    # -- 界面搭建 -----------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_controls())
        self._refresh_rule_label()
        layout.addWidget(self._build_warnings())

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_batch_table(), "批次拆分")
        self.tabs.addTab(self._build_step_table(), "打印顺序")
        self.tabs.addTab(self._build_page_table(), "页面检查")
        layout.addWidget(self.tabs, 1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        layout.addLayout(self._build_buttons())

    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QVBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        title = QLabel(self.task.document_name)
        title_font = title.font()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        row.addWidget(title)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color:#555;")
        row.addWidget(self.info_label)
        return header

    def _build_controls(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("每批纸张："))
        self.sheets_spin = QSpinBox(bar)
        self.sheets_spin.setRange(MIN_SHEETS_PER_BATCH, MAX_SHEETS_PER_BATCH)
        self.sheets_spin.setValue(self.service.sheets_per_batch)
        self.sheets_spin.setSuffix(" 张")
        self.sheets_spin.setToolTip("一批最多打印多少张纸（一张纸两个页面）")
        self.sheets_spin.valueChanged.connect(self._on_sheets_changed)
        row.addWidget(self.sheets_spin)

        self.plan_label = QLabel("")
        row.addWidget(self.plan_label)
        row.addStretch(1)

        self.rule_label = QLabel("")
        self.rule_label.setStyleSheet("color:#777;")
        row.addWidget(self.rule_label)
        return bar

    def _refresh_rule_label(self) -> None:
        """显示当前顺序规则（读实际配置，默认背面按纸张编号降序）。"""
        if self.plan is not None:
            back_order = self.plan.back_order
        else:
            back_order = getattr(self.service, "back_order", BACK_ORDER_DESCENDING)
        if back_order == BACK_ORDER_DESCENDING:
            back_text = "背面：按纸张编号降序（10、8、6、4…，先打纸堆最上面那张纸的背面）"
        else:
            back_text = "背面：按纸张编号升序（2、4、6、8…）"
        self.rule_label.setText(f"正面：奇数页升序（1、3、5…）　{back_text}")

    def _build_warnings(self) -> QWidget:
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color:#E65100;")
        self.warning_label.hide()
        return self.warning_label

    @staticmethod
    def _prepare_table(table: QTableWidget) -> None:
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)

    def _build_batch_table(self) -> QTableWidget:
        self.batch_table = QTableWidget(0, len(BATCH_COLUMNS), self)
        self.batch_table.setHorizontalHeaderLabels(BATCH_COLUMNS)
        self._prepare_table(self.batch_table)
        self.batch_table.itemDoubleClicked.connect(self._on_batch_activated)

        header = self.batch_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        return self.batch_table

    def _build_step_table(self) -> QTableWidget:
        self.step_table = QTableWidget(0, len(STEP_COLUMNS), self)
        self.step_table.setHorizontalHeaderLabels(STEP_COLUMNS)
        self._prepare_table(self.step_table)
        self.step_table.itemDoubleClicked.connect(self._on_step_activated)

        header = self.step_table.horizontalHeader()
        for column in (0, 1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        return self.step_table

    def _build_page_table(self) -> QTableWidget:
        self.page_table = QTableWidget(0, len(PAGE_COLUMNS), self)
        self.page_table.setHorizontalHeaderLabels(PAGE_COLUMNS)
        self._prepare_table(self.page_table)
        self.page_table.setSelectionMode(QAbstractItemView.SingleSelection)

        header = self.page_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        return self.page_table

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.generate_button = QPushButton("生成单页文件(&G)", self)
        self.generate_button.setToolTip(
            "每一页生成一个单页 PDF（page_001.pdf…），写入 Data/jobs/<任务号>/"
        )
        self.generate_button.clicked.connect(self.generate)
        row.addWidget(self.generate_button)

        self.open_dir_button = QPushButton("打开任务目录(&O)", self)
        self.open_dir_button.setToolTip("在资源管理器中打开该任务的打印文件目录")
        self.open_dir_button.clicked.connect(self.open_job_dir)
        self.open_dir_button.setEnabled(False)
        row.addWidget(self.open_dir_button)

        row.addStretch(1)

        self.close_button = QPushButton("关闭(&C)", self)
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        return row

    # -- 数据刷新 -----------------------------------------------------------

    def refresh(self) -> bool:
        """重新读取页面并计算批次；返回是否成功。"""
        self.error_message = ""
        try:
            self.scan = self.service.inspect(self.task)
            self.plan = self.service.build_plan(
                self.task, self.sheets_spin.value(), self.scan.page_count
            )
        except PageJobError as exc:
            self.scan = None
            self.plan = None
            self.error_message = str(exc)
            self._fill_error(str(exc))
            return False

        self._fill_header()
        self._refresh_rule_label()
        self._fill_batches()
        self._fill_steps()
        self._fill_pages()
        self._fill_warnings()
        self._fill_summary()
        self.generate_button.setEnabled(bool(self.plan and self.plan.batch_count))
        self.open_dir_button.setEnabled(self._job_dir_exists())
        return True

    def _fill_error(self, message: str) -> None:
        self.batch_table.setRowCount(0)
        self.step_table.setRowCount(0)
        self.page_table.setRowCount(0)
        self.plan_label.setText("")
        self.warning_label.hide()
        self.summary_label.setText(f"<span style='color:{ERROR_COLOR};'>{message}</span>")
        self.generate_button.setEnabled(False)
        self.open_dir_button.setEnabled(False)

    def _fill_header(self) -> None:
        if self.scan is None:
            return
        self.info_label.setText(
            f"任务号 {self.task.task_uid}　|　{self.scan.page_count} 页　|　"
            f"{self.scan.paper_size}　|　源文件 {self.task.source_path.name}"
        )

    def _fill_batches(self) -> None:
        plan = self.plan
        self.batch_table.setRowCount(0)
        if plan is None:
            return

        self.plan_label.setText(f"＝ {plan.summary_text}")
        self.batch_table.setRowCount(plan.batch_count)
        bold = QFont()
        bold.setBold(True)
        for row, batch in enumerate(plan.batches):
            values = (
                f"第 {batch.index} / {batch.total_batches} 批",
                f"{batch.sheet_count} 张",
                batch.front_pages_text,
                batch.back_pages_text,
                f"{len(batch.file_names)} 个",
                batch.status_text,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setFont(bold)
                if batch.is_last:
                    item.setBackground(QBrush(LAST_BATCH_COLOR))
                self.batch_table.setItem(row, column, item)
        self.batch_table.resizeRowsToContents()

    def _fill_steps(self) -> None:
        """把打印计划展开成「一次提交一行」，看清逐页提交的顺序。"""
        plan = self.plan
        self.step_table.setRowCount(0)
        if plan is None:
            return

        steps = plan.print_steps
        self.step_table.setRowCount(len(steps))
        for row, step in enumerate(steps):
            values = (
                str(row + 1),
                f"第 {step.batch_index} 批",
                f"第 {step.sheet_number} 张",
                step.face_text,
                f"第 {step.page_number} 页",
                step.file_name,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.step_table.setItem(row, column, item)
        self.step_table.resizeRowsToContents()

    def _fill_pages(self) -> None:
        scan = self.scan
        self.page_table.setRowCount(0)
        if scan is None:
            return

        self.page_table.setRowCount(scan.page_count)
        for row, page in enumerate(scan.pages):
            values = (
                str(page.number),
                page.size_text,
                f"{page.text_chars:,}",
                str(page.image_count),
                page.blank_text,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if page.blank:
                    item.setBackground(QBrush(BLANK_ROW_COLOR))
                self.page_table.setItem(row, column, item)
        self.page_table.resizeRowsToContents()

    def _fill_warnings(self) -> None:
        messages: list[str] = []
        if self.scan is not None:
            messages.extend(self.scan.warnings)
        if messages:
            self.warning_label.setText("提示：" + "；".join(messages))
            self.warning_label.show()
        else:
            self.warning_label.hide()

    def _fill_summary(self) -> None:
        if self.plan is None:
            self.summary_label.setText("")
            return

        plan = self.plan
        parts = [
            f"{plan.page_count} 页 → {plan.sheet_count} 张纸 → {plan.batch_count} 批"
            f"（每批 {plan.sheets_per_batch} 张）",
            f"共需提交 {len(plan.print_steps)} 次，每次只提交一个页面",
        ]

        existing = self.service.existing_batches(self.task.task_uid)
        if existing:
            generated_at = existing[0].created_at.strftime("%Y-%m-%d %H:%M")
            parts.append(f"上次生成：{len(existing)} 批（{generated_at}）")

        if plan.blank_back_sheets:
            parts.append(
                f"第 {'、'.join(str(n) for n in plan.blank_back_sheets)} 张纸没有背面内容"
            )

        self.summary_label.setText(
            f"<span style='color:{OK_COLOR};'>{'；'.join(parts)}。</span>"
        )

    # -- 交互 ---------------------------------------------------------------

    def _on_sheets_changed(self, _value: int) -> None:
        # 只重新计算计划，不写文件；页数不变时无需重新读取 PDF
        if self.scan is None:
            self.refresh()
            return
        if self.plan is not None:
            try:
                self.plan = self.service.build_plan(
                    self.task, self.sheets_spin.value(), self.scan.page_count
                )
            except PageJobError as exc:
                self.error_message = str(exc)
                self._fill_error(str(exc))
                return
        self._refresh_rule_label()
        self._fill_batches()
        self._fill_steps()
        self._fill_summary()

    def page_file_path(self, page_number: int) -> Path:
        """某一页的单页文件路径：优先生成结果，其次任务目录。"""
        if self.last_result is not None:
            path = self.last_result.page_file(page_number)
            if path is not None:
                return path
        return self.service.job_dir_for(self.task) / page_file_name(page_number)

    def _on_batch_activated(self, item: QTableWidgetItem) -> None:
        """双击批次行，预览这一批的第一页（正面第一页）。"""
        if self.plan is None:
            return
        row = item.row()
        if not 0 <= row < len(self.plan.batches):
            return
        batch = self.plan.batches[row]
        if not batch.front_pages:
            return
        path = self.page_file_path(batch.front_pages[0])
        if not path.is_file():
            return
        PdfPreviewDialog(
            path, self, title=f"{path.name}（第 {batch.index} 批正面第 {batch.front_pages[0]} 页）"
        ).exec()

    def _on_step_activated(self, item: QTableWidgetItem) -> None:
        """双击打印顺序中的一行，预览对应页面的单页文件。"""
        if self.plan is None:
            return
        row = item.row()
        steps = self.plan.print_steps
        if not 0 <= row < len(steps):
            return
        step = steps[row]
        path = self.page_file_path(step.page_number)
        if not path.is_file():
            return
        PdfPreviewDialog(path, self, title=f"{path.name}（{step.label}）").exec()

    def generate(self) -> bool:
        """按当前设置生成单页文件；返回是否成功。"""
        if self.plan is None:
            return False

        self.generate_button.setEnabled(False)
        try:
            result = self.service.generate(self.task, self.sheets_spin.value())
        except PageJobError as exc:
            self.error_message = str(exc)
            self.last_result = None
            self.summary_label.setText(
                f"<span style='color:{ERROR_COLOR};'>生成失败：{exc}</span>"
            )
            self.open_dir_button.setEnabled(self._job_dir_exists())
            logger.warning("生成打印文件失败：%s（%s）", self.task.task_uid, exc)
            if self.show_errors:
                QMessageBox.warning(self, "生成失败", str(exc))
            return False
        finally:
            self.generate_button.setEnabled(self.plan is not None)

        self.error_message = ""
        self.last_result = result
        self.plan = result.plan
        self._refresh_rule_label()
        self._fill_batches()
        self._fill_steps()
        self._fill_summary()

        message = (
            f"<span style='color:{OK_COLOR};'>已生成 {len(result.pages)} 个单页文件"
            f"（{result.page_count} 页 / {result.sheet_count} 张纸 / {result.batch_count} 批，"
            f"{_count_files(result)} 个文件，{result.size_text}），目录：{result.job_dir}</span>"
        )
        if result.warnings:
            message += (
                f"<br/><span style='color:#E65100;'>提示："
                f"{'；'.join(result.warnings)}</span>"
            )
        self.summary_label.setText(message)
        self.open_dir_button.setEnabled(True)
        return True

    def open_job_dir(self) -> None:
        """在资源管理器中打开任务目录。"""
        directory = (
            self.last_result.job_dir if self.last_result else self.service.job_dir_for(self.task)
        )
        if not directory.is_dir():
            if self.show_errors:
                QMessageBox.information(self, "目录还不存在", f"{directory}\n\n请先生成单页文件。")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def job_dir(self) -> Path:
        return self.service.job_dir_for(self.task)

    def _job_dir_exists(self) -> bool:
        return self.job_dir().is_dir()


def _count_files(result: GenerationResult) -> int:
    return sum(1 for path in result.file_paths if path is not None)
