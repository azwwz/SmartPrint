"""打印单页对话框（阶段五）。

这是阶段五的「真实打印」入口，只做一件事：把**一个页面**送到 HP LaserJet 1020。

窗口从上到下：

1. 任务信息与页面选择（页码、属于第几张纸的哪一面、对应的单页文件）；
2. 打印机选择（默认取 config.json 的目标打印机，可临时改；PDF24 等虚拟打印机
   会被明确标出并禁止选中）；
3. 预检结果（打印机是否在线、SumatraPDF 是否找到、队列里有什么）；
4. 「打印这一页」与「演练」按钮；
5. 结果区：命令行、退出码、提交前后的队列快照、提示。

安全约定（对应 readme 3.3 / 10.3）：

* 打印按钮点下去之前会再确认一次「只会打印这一页」；
* 发送成功后界面只会提示「命令已发送」，不会显示「打印成功」；
* 队列状态只作为参考信息展示，绝不据此自动继续下一页。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.config import AppConfig
from app.core.models import Task
from app.core.page_planner import face_label, face_of_page, page_file_name, sheet_number_of_page
from app.services.print_service import (
    LEVEL_BLOCKED,
    PrintOutcome,
    PrintReadiness,
    PrintService,
    describe_outcome,
    format_readiness,
)

logger = logging.getLogger("smartprint.ui.print_page")

ERROR_COLOR = "#C62828"
OK_COLOR = "#2E7D32"
WARN_COLOR = "#E65100"
MUTED_COLOR = "#666666"


class PrintPageDialog(QDialog):
    """打印单页窗口。"""

    def __init__(
        self,
        task: Task,
        service: PrintService | None = None,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.config = config
        self.service = service or PrintService.from_config(config)
        self.readiness: PrintReadiness | None = None
        self.last_outcome: PrintOutcome | None = None
        self.error_message = ""
        #: 验收脚本会关掉弹窗，避免阻塞
        self.show_errors = True
        #: 是否需要二次确认（验收脚本会关掉）
        self.confirm_before_print = True

        self.setWindowTitle(f"打印单页 — {task.document_name}")
        self.resize(860, 700)

        self._build_ui()
        self.refresh_readiness()

    # -- 界面搭建 -----------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_target_group())
        layout.addWidget(self._build_readiness_group(), 1)
        layout.addWidget(self._build_result_group(), 2)
        layout.addLayout(self._build_buttons())

    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QVBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        title = QLabel(self.task.document_name)
        font = title.font()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        row.addWidget(title)

        self.info_label = QLabel(
            f"任务号 {self.task.task_uid}　|　{self.task.page_count} 页　|　"
            f"{self.task.size_text}　|　源文件 {self.task.source_path.name}"
        )
        self.info_label.setStyleSheet(f"color:{MUTED_COLOR};")
        row.addWidget(self.info_label)
        return header

    def _build_target_group(self) -> QGroupBox:
        group = QGroupBox("打印目标")
        row = QHBoxLayout(group)

        row.addWidget(QLabel("页码："))
        self.page_spin = QSpinBox(group)
        self.page_spin.setRange(1, max(1, int(self.task.page_count)))
        self.page_spin.setValue(1)
        self.page_spin.setSuffix(" 页")
        self.page_spin.setToolTip("要打印的页码（从 1 开始，与 PDF 阅读器显示的页码一致）")
        self.page_spin.valueChanged.connect(self._on_page_changed)
        row.addWidget(self.page_spin)

        self.page_info_label = QLabel("")
        self.page_info_label.setStyleSheet(f"color:{MUTED_COLOR};")
        row.addWidget(self.page_info_label)

        row.addSpacing(12)
        row.addWidget(QLabel("打印机："))
        self.printer_combo = QComboBox(group)
        self.printer_combo.setMinimumWidth(260)
        self.printer_combo.currentIndexChanged.connect(self._on_printer_changed)
        row.addWidget(self.printer_combo)

        self.refresh_button = QPushButton("刷新(&R)", group)
        self.refresh_button.setToolTip("重新读取打印机列表、SumatraPDF 与打印队列")
        self.refresh_button.clicked.connect(self.refresh_readiness)
        row.addWidget(self.refresh_button)
        row.addStretch(1)

        self._on_page_changed(self.page_spin.value())
        return group

    def _build_readiness_group(self) -> QGroupBox:
        group = QGroupBox("打印前检查（打印机 / SumatraPDF / 队列）")
        box = QVBoxLayout(group)
        self.readiness_view = QPlainTextEdit(group)
        self.readiness_view.setReadOnly(True)
        self.readiness_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self.readiness_view.setMinimumHeight(120)
        box.addWidget(self.readiness_view)
        return group

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("本次结果（命令是否已发送，不等于纸张已出）")
        box = QVBoxLayout(group)
        self.result_label = QLabel("尚未发送。")
        self.result_label.setWordWrap(True)
        box.addWidget(self.result_label)

        self.result_view = QPlainTextEdit(group)
        self.result_view.setReadOnly(True)
        self.result_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self.result_view.setPlainText(
            "提示：阶段五只负责把页面交给打印机。\n"
            "命令发送成功、队列为空，都不能证明纸张已经完整出纸；\n"
            "请以实际纸张为准，阶段六才会加入逐面确认。"
        )
        box.addWidget(self.result_view, 1)
        return group

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.dry_run_check = QCheckBox("演练（只预检并生成命令，不真的发送）")
        self.dry_run_check.setChecked(False)
        self.dry_run_check.setToolTip("勾选后不会启动 SumatraPDF，适合先检查命令是否正确")
        row.addWidget(self.dry_run_check)

        self.force_check = QCheckBox("打印机脱机时仍然发送（不推荐）")
        self.force_check.setChecked(False)
        self.force_check.setToolTip(
            "默认情况下打印机脱机会拒绝发送：作业只会堆在队列里，可能开机后突然打印"
        )
        row.addWidget(self.force_check)

        row.addStretch(1)

        self.print_button = QPushButton("打印这一页(&P)", self)
        self.print_button.setDefault(True)
        self.print_button.clicked.connect(self.print_current_page)
        row.addWidget(self.print_button)

        self.close_button = QPushButton("关闭(&C)", self)
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        return row

    # -- 页面信息 -----------------------------------------------------------

    def _on_page_changed(self, page_number: int) -> None:
        page = int(page_number)
        face = face_of_page(page)
        sheet = sheet_number_of_page(page)
        total_sheets = (int(self.task.page_count) + 1) // 2
        self.page_info_label.setText(
            f"第 {sheet} / {total_sheets} 张纸 · {face_label(face)} · "
            f"单页文件 {page_file_name(page)}"
        )

    # -- 打印机列表 ---------------------------------------------------------

    def _fill_printers(self, readiness: PrintReadiness) -> None:
        """填充打印机下拉框：虚拟打印机也列出来，但标注且不可选。"""
        current = self.printer_combo.currentData()
        wanted = self.service.printer_name

        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        for printer in readiness.printers:
            if printer.is_virtual:
                self.printer_combo.addItem(f"{printer.name}（虚拟打印机，不能选）", printer.name)
                index = self.printer_combo.count() - 1
                self.printer_combo.model().item(index).setEnabled(False)
            else:
                suffix = " · 脱机" if printer.is_offline else ""
                self.printer_combo.addItem(
                    f"{printer.name}{suffix}（{printer.port_name or '端口未知'}）",
                    printer.name,
                )

        # 选中：当前选择 → 配置目标 → 第一台真实打印机
        target_index = -1
        for index in range(self.printer_combo.count()):
            data = self.printer_combo.itemData(index)
            if current and data == current:
                target_index = index
                break
        if target_index < 0:
            target_index = self._index_of_printer(readiness.target.name if readiness.target else "")
        if target_index < 0:
            real = [i for i, p in enumerate(readiness.printers) if not p.is_virtual]
            target_index = real[0] if real else -1
        if target_index >= 0:
            self.printer_combo.setCurrentIndex(target_index)
            if readiness.target is not None and wanted:
                self.printer_combo.setToolTip(
                    f"配置里的目标打印机是「{wanted}」，"
                    f"系统中解析为「{readiness.target.name}」"
                )
        self.printer_combo.blockSignals(False)

    def _index_of_printer(self, name: str) -> int:
        if not name:
            return -1
        for index in range(self.printer_combo.count()):
            if self.printer_combo.itemData(index) == name:
                return index
        return -1

    def selected_printer(self) -> str:
        data = self.printer_combo.currentData()
        return str(data) if data else ""

    def _on_printer_changed(self, _index: int) -> None:
        self.refresh_readiness(keep_printer=True)
        self._update_buttons()

    # -- 预检 ---------------------------------------------------------------

    def refresh_readiness(self, keep_printer: bool = False) -> bool:
        """重新做一次环境检查，并把结果写进界面。

        Args:
            keep_printer: 是否保留当前下拉框选择（切换打印机时为 True，
                这样检查的就是用户刚刚选中的那台，而不是配置里的默认打印机）。
        """
        self.error_message = ""
        selected = self.selected_printer() if keep_printer else ""
        try:
            readiness = self.service.check_readiness(printer_name=selected or None)
        except Exception as exc:  # noqa: BLE001 - 环境异常不应让窗口崩掉
            logger.exception("打印前检查失败")
            self.error_message = str(exc)
            self.readiness_view.setPlainText(f"检查失败：{exc}")
            self.readiness = None
            self.readiness_view.setStyleSheet(f"color:{ERROR_COLOR};")
            self._update_buttons()
            return False

        self.readiness = readiness
        if not keep_printer:
            self._fill_printers(readiness)
        self.readiness_view.setPlainText(self._readiness_text(readiness))
        color = {
            LEVEL_BLOCKED: ERROR_COLOR,
            "warn": WARN_COLOR,
        }.get(readiness.level, OK_COLOR)
        self.readiness_view.setStyleSheet(
            f"color:{color}; font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self._update_buttons()
        return readiness.ok

    def _readiness_text(self, readiness: PrintReadiness) -> str:
        text = format_readiness(readiness)
        if readiness.target is None and readiness.real_printers:
            text += "\n可用的真实打印机：\n"
            for printer in readiness.real_printers:
                text += f"  · {printer.name}（{printer.port_name or '端口未知'}，{printer.status_text}）\n"
        return text

    def _update_buttons(self) -> None:
        blocked = self.readiness is None or self.readiness.level == LEVEL_BLOCKED
        allow_force = (
            self.readiness is not None
            and bool(self.readiness.problems)
            and all("脱机" in item or "当前状态" in item for item in self.readiness.problems)
            and self.readiness.target is not None
        )
        self.force_check.setEnabled(allow_force and not self.dry_run_check.isChecked())
        if not allow_force:
            self.force_check.setChecked(False)
        self.print_button.setEnabled(not blocked or (allow_force and self.force_check.isChecked()))

    # -- 打印 ---------------------------------------------------------------

    def print_current_page(self) -> PrintOutcome | None:
        """执行打印；返回结果（验收脚本可以直接调用）。"""
        if self.confirm_before_print and not self.dry_run_check.isChecked():
            page = self.page_spin.value()
            answer = QMessageBox.question(
                self,
                "确认打印",
                f"即将把第 {page} 页发送到：\n{self.selected_printer()}\n\n"
                "只打印这一页。打印命令发送成功后，程序不会自动继续下一页，"
                "也不会替你判定纸张是否完整出纸。\n\n确定要发送吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None

        self.print_button.setEnabled(False)
        try:
            outcome = self.service.print_page(
                self.task,
                self.page_spin.value(),
                dry_run=self.dry_run_check.isChecked(),
                allow_blocked_printer=self.force_check.isChecked(),
                printer_name=self.selected_printer() or None,
            )
        except Exception as exc:  # noqa: BLE001 - 打印异常要显示给用户
            logger.exception("打印失败")
            self.error_message = str(exc)
            self.result_label.setText(f"<span style='color:{ERROR_COLOR};'>打印失败：{exc}</span>")
            if self.show_errors:
                QMessageBox.warning(self, "打印失败", str(exc))
            return None
        finally:
            self._update_buttons()

        self.last_outcome = outcome
        self.error_message = "" if outcome.ok or outcome.dry_run else (outcome.error or outcome.status_text)

        if outcome.dry_run:
            color = MUTED_COLOR
            headline = "演练完成：未发送任何打印数据"
        elif outcome.ok:
            color = WARN_COLOR
            headline = (
                f"命令已发送（退出码 {outcome.sumatra.returncode if outcome.sumatra else '—'}）："
                "这不代表纸张已经完整出纸，请检查打印机"
            )
        else:
            color = ERROR_COLOR
            headline = f"未发送或发送失败：{outcome.status_text}"
        self.result_label.setText(f"<span style='color:{color};'>{headline}</span>")
        self.result_view.setPlainText(describe_outcome(outcome))

        if outcome.warnings:
            logger.warning(
                "任务 %s 第 %s 页提示：%s",
                self.task.task_uid,
                outcome.page_number,
                "；".join(outcome.warnings),
            )
        return outcome

    def dry_run_page(self) -> PrintOutcome | None:
        """只预检并生成命令（不发送），供验收脚本调用。"""
        previous = self.dry_run_check.isChecked()
        self.dry_run_check.setChecked(True)
        try:
            return self.print_current_page()
        finally:
            self.dry_run_check.setChecked(previous)
