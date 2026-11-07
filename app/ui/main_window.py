"""SmartPrint 主窗口（阶段三：任务管理界面；阶段四、阶段五逐步接入）。

界面分成四块：

1. 顶部：任务统计，以及扫描 / 打开 incoming / 刷新的常用按钮。
2. 筛选栏：按状态筛选、按文件名或任务号搜索。
3. 主体：左侧任务表格（点击表头排序、可多选），右侧任务详情与任务记录。
   表格上方是任务操作工具栏（预览、状态变更、删除、打开源文件）。
4. 底部：运行日志，记录接收与用户操作，便于回溯。

阶段三不接触真实打印机：只管理任务记录、预览 PDF、手工调整任务状态。
阶段四增加页面拆分；阶段五增加「打印单页」，从这里开始真的会出纸。
"""

from __future__ import annotations

import html
import importlib.metadata
import logging
import os
import platform
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.core.config import AppConfig, load_config
from app.core.duplex import PHASE_CANCELLED
from app.core.models import Task
from app.core.task_status import ACTIVE_STATUSES, TaskStatus
from app.core.version import APP_NAME, APP_TITLE, APP_VERSION
from app.database.db import Database
from app.database.task_repository import TaskRepository
from app.services.archive_service import ArchiveService
from app.services.backup_service import BackupError, BackupService
from app.services.task_admin import (
    FILE_ARCHIVED,
    FILE_DELETED,
    DeleteOutcome,
    TaskAdminError,
    TaskAdminService,
)
from app.services.duplex_service import DuplexPrintService
from app.services.page_jobs import PageJobService
from app.services.print_service import PrintService
from app.services.task_intake import TaskIntakeService
from app.ui.anomaly_dialog import AnomalyDialog
from app.ui.duplex_dialog import DuplexDialog
from app.ui.log_view_dialog import LogViewDialog
from app.ui.page_plan_dialog import PagePlanDialog
from app.ui.pdf_preview_dialog import PdfPreviewDialog
from app.ui.print_page_dialog import PrintPageDialog
from app.ui.settings_dialog import SettingsDialog
from app.utils.logging_setup import log_file_path
from app.utils.paths import CONFIG_FILE, INCOMING_DIR, PROJECT_ROOT

logger = logging.getLogger("smartprint.ui.main_window")

#: 界面轮询接收服务通知的间隔（毫秒）
POLL_INTERVAL_MS = 500

#: 搜索框输入后延迟多久再查询（毫秒）
SEARCH_DELAY_MS = 250

#: 任务列表最多显示多少条
TASK_LIMIT = 1000

_TASK_COLUMNS = (
    "任务号",
    "文件名",
    "页数",
    "预计纸张",
    "批次数",
    "纸张",
    "创建时间",
    "状态",
    "打印进度",
)

#: 每一列对应的排序字段（表头点击排序）
_COLUMN_SORT_KEYS: dict[int, str] = {
    0: "task_uid",
    1: "document_name",
    2: "page_count",
    3: "page_count",
    4: "page_count",
    5: "paper_size",
    6: "created_at",
    7: "status",
}

#: 状态列、进度列在表格里的下标
_STATUS_COLUMN = 7
_PROGRESS_COLUMN = 8

_STATUS_ORDER: tuple[TaskStatus, ...] = tuple(TaskStatus)

#: 状态筛选下拉框：显示文字 → 要筛选的状态集合（None 表示全部）
_STATUS_FILTERS: tuple[tuple[str, tuple[TaskStatus, ...] | None], ...] = (
    ("全部任务", None),
    (
        "未结束任务",
        (
            TaskStatus.PENDING,
            TaskStatus.READY,
            TaskStatus.PRINTING_FRONT,
            TaskStatus.WAITING_FLIP,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.PAUSED,
            TaskStatus.ERROR,
        ),
    ),
    ("待处理", (TaskStatus.PENDING,)),
    ("已准备", (TaskStatus.READY,)),
    ("已暂停", (TaskStatus.PAUSED,)),
    ("待补打", (TaskStatus.REPRINT,)),
    ("异常待处理", (TaskStatus.ERROR,)),
    ("已完成", (TaskStatus.COMPLETED,)),
    ("已取消", (TaskStatus.CANCELLED,)),
)

#: 状态颜色（界面文字颜色）
_STATUS_COLORS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "#B8860B",
    TaskStatus.READY: "#1F6FB2",
    TaskStatus.PRINTING_FRONT: "#7A3EA8",
    TaskStatus.WAITING_FLIP: "#B35900",
    TaskStatus.PRINTING_BACK: "#7A3EA8",
    TaskStatus.REPRINT: "#B35900",
    TaskStatus.COMPLETED: "#2E7D32",
    TaskStatus.PAUSED: "#607D8B",
    TaskStatus.ERROR: "#C62828",
    TaskStatus.CANCELLED: "#757575",
}


def _package_version(distribution: str) -> str:
    """读取已安装的包版本，未安装时返回提示文字。"""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "未安装"


def _collect_environment(incoming_dir=None) -> list[tuple[str, str]]:
    """收集运行环境信息，用于「运行环境自检」对话框。"""
    return [
        ("Python", sys.version.split()[0]),
        ("PySide6", _package_version("PySide6")),
        ("PyMuPDF", _package_version("PyMuPDF")),
        ("watchdog", _package_version("watchdog")),
        ("SQLite", sqlite3.sqlite_version),
        ("程序版本", f"{APP_NAME} {APP_VERSION}"),
        ("操作系统", f"{platform.system()} {platform.release()}"),
        ("项目目录", str(PROJECT_ROOT)),
        ("待处理目录", str(incoming_dir or INCOMING_DIR)),
        ("日志文件", str(log_file_path())),
    ]


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1220, 800)

        self.config = config or load_config()
        self.database = Database(self.config.database_file)
        self.database.initialize()
        self.repository = TaskRepository(self.database)
        self.admin = TaskAdminService(
            self.repository,
            incoming_dir=self.config.incoming_dir,
            archive_dir=self.config.archive_dir,
        )
        self.intake = TaskIntakeService(self.config, self.repository)
        self.page_jobs = PageJobService.from_config(self.config, self.repository)
        self.printer = PrintService(
            config=self.config,
            repository=self.repository,
            page_jobs=self.page_jobs,
        )
        self.duplex = DuplexPrintService(
            repository=self.repository,
            print_service=self.printer,
            config=self.config,
        )

        self._tasks: list[Task] = []
        self._counts: dict[TaskStatus, int] = {status: 0 for status in TaskStatus}
        self._sort_key = "created_at"
        self._sort_desc = True

        self._build_actions()
        self._build_ui()
        self._build_menu()
        self._update_sort_indicator()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_intake)
        self._poll_timer.start()

        self._refresh_tasks()
        self.intake.start()
        self._update_status_bar()
        self._log_restored_tasks()
        logger.info("主窗口已创建，任务接收服务已启动")

    # -- 动作 ---------------------------------------------------------------

    def _make_action(self, text: str, slot, shortcut: str | None = None, tip: str = "") -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.triggered.connect(lambda _checked=False: slot())
        self.addAction(action)
        return action

    def _build_actions(self) -> None:
        self.act_preview = self._make_action(
            "预览 PDF(&P)", self._on_preview_clicked, "Ctrl+P", "查看选中任务的 PDF 内容"
        )
        self.act_open_source = self._make_action(
            "用默认程序打开(&O)", self._on_open_source_clicked, None, "用系统默认 PDF 阅读器打开源文件"
        )
        self.act_open_folder = self._make_action(
            "打开文件所在目录(&F)", self._on_open_folder_of_task, None, "在资源管理器中定位源 PDF"
        )
        self.act_page_plan = self._make_action(
            "页面拆分(&B)…",
            self._on_page_plan_clicked,
            "Ctrl+B",
            "查看每一页印在哪一面，并生成按页码命名的单页打印文件",
        )
        self.act_print_page = self._make_action(
            "打印单页(&J)…",
            self._on_print_page_clicked,
            "Ctrl+J",
            "把选中的某一页发送到真实打印机（PDF24 等虚拟打印机不可选）",
        )
        self.act_duplex = self._make_action(
            "手动双面打印(&D)…",
            self._on_duplex_clicked,
            "Ctrl+D",
            "逐面提交、逐面确认：每次只打印一个页面，确认完整出纸后才继续；"
            "本批正面完成后提示翻面",
        )
        self.act_ready = self._make_action(
            "标记为已准备(&M)", self._on_mark_ready, None, "确认打印参数无误，等待后续打印流程"
        )
        self.act_pause = self._make_action("暂停任务(&T)", self._on_pause, None, "暂停任务，保留进度")
        self.act_resume = self._make_action("恢复任务(&U)", self._on_resume, None, "把暂停的任务恢复为已准备")
        self.act_cancel = self._make_action("取消任务(&C)", self._on_cancel, None, "取消任务并保留记录")
        self.act_delete = self._make_action(
            "删除任务(&D)", self._on_delete_clicked, None, "从任务列表删除，并处理 incoming 中的源 PDF"
        )
        self.act_settings = self._make_action(
            "设置(&S)…",
            self._on_settings_clicked,
            "Ctrl+,",
            "选择打印机、每批纸张数、翻面方向、SumatraPDF 路径与备份策略",
        )
        self.act_anomalies = self._make_action(
            "异常记录(&A)…",
            self._on_anomalies_clicked,
            "Ctrl+Shift+E",
            "查看所有任务里被整张作废的纸张，以及有没有补打完",
        )
        self.act_log = self._make_action(
            "运行日志(&L)…",
            self._on_log_clicked,
            "Ctrl+L",
            "查看 logs/smartprint.log（任务接收、打印命令、异常与恢复）",
        )
        self.act_backup = self._make_action(
            "备份数据(&K)…",
            self._on_backup_clicked,
            None,
            "把数据库和配置打包成 zip，存到 Data/backups",
        )
        self.act_restore = self._make_action(
            "从备份恢复(&V)…",
            self._on_restore_clicked,
            None,
            "用之前的备份替换当前数据库（会先把当前库另存一份）",
        )
        self.act_archive = self._make_action(
            "归档已完成任务(&H)…",
            self._on_archive_clicked,
            None,
            "把已结束的任务源 PDF、单页文件与记录整理到 Data/completed/archive",
        )

    # -- 界面搭建 -----------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 8, 10, 8)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_filter_bar())

        self._toolbar = QToolBar("任务操作", self)
        self._toolbar.setMovable(False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for action in (self.act_preview, self.act_open_source, self.act_open_folder):
            self._toolbar.addAction(action)
        self._toolbar.addAction(self.act_page_plan)
        self._toolbar.addAction(self.act_print_page)
        self._toolbar.addAction(self.act_duplex)
        self._toolbar.addSeparator()
        for action in (self.act_ready, self.act_pause, self.act_resume, self.act_cancel):
            self._toolbar.addAction(action)
        self._toolbar.addSeparator()
        self._toolbar.addAction(self.act_delete)
        self._toolbar.addSeparator()
        self._toolbar.addAction(self.act_anomalies)
        self._toolbar.addAction(self.act_log)
        self._toolbar.addAction(self.act_settings)
        layout.addWidget(self._toolbar)

        layout.addWidget(self._build_body(), 1)
        layout.addWidget(self._build_log_group())
        self.hint_label = self._build_hint_label()
        layout.addWidget(self.hint_label)

        self.setCentralWidget(central)

    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)

        title = QLabel("打印任务")
        title_font = title.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        row.addWidget(title)

        self.summary_label = QLabel("任务总数：0")
        self.summary_label.setStyleSheet("color: #555;")
        row.addWidget(self.summary_label)
        row.addStretch(1)

        self.scan_button = QPushButton("立即扫描待处理目录(&S)")
        self.scan_button.clicked.connect(self._on_scan_clicked)
        row.addWidget(self.scan_button)

        self.open_folder_button = QPushButton("打开 incoming 目录")
        self.open_folder_button.clicked.connect(self._on_open_folder_clicked)
        row.addWidget(self.open_folder_button)

        self.refresh_button = QPushButton("刷新列表(&R)")
        self.refresh_button.clicked.connect(lambda: self._refresh_tasks())
        row.addWidget(self.refresh_button)

        return header

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("状态："))
        self.filter_combo = QComboBox(self)
        for text, _statuses in _STATUS_FILTERS:
            self.filter_combo.addItem(text)
        self.filter_combo.currentIndexChanged.connect(lambda _index: self._refresh_tasks())
        row.addWidget(self.filter_combo)

        row.addSpacing(12)
        row.addWidget(QLabel("搜索："))
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("文件名或任务号，支持部分匹配")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(320)
        self.search_edit.textChanged.connect(self._on_search_changed)
        row.addWidget(self.search_edit)

        self.clear_filter_button = QPushButton("清除筛选")
        self.clear_filter_button.clicked.connect(self._on_clear_filter)
        row.addWidget(self.clear_filter_button)

        row.addStretch(1)

        self.sort_label = QLabel("")
        self.sort_label.setStyleSheet("color: #777;")
        row.addWidget(self.sort_label)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._refresh_tasks)

        return bar

    def _build_body(self) -> QWidget:
        group = QGroupBox("任务列表（点击表头排序，双击预览 PDF，右键更多操作）")
        box = QVBoxLayout(group)

        splitter = QSplitter(Qt.Horizontal)

        self.table = QTableWidget(0, len(_TASK_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(_TASK_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)  # 排序交给数据库完成
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._on_preview_clicked())

        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, len(_TASK_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)

        splitter.addWidget(self.table)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)

        detail_title = QLabel("任务详情")
        detail_title.setStyleSheet("font-weight: bold; color: #444;")
        detail_layout.addWidget(detail_title)

        self.detail_view = QTextBrowser(self)
        self.detail_view.setOpenExternalLinks(False)
        self.detail_view.setMinimumWidth(300)
        detail_layout.addWidget(self.detail_view, 3)

        event_title = QLabel("任务记录（最近 100 条）")
        event_title.setStyleSheet("font-weight: bold; color: #444;")
        detail_layout.addWidget(event_title)

        self.event_view = QPlainTextEdit(self)
        self.event_view.setReadOnly(True)
        self.event_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        detail_layout.addWidget(self.event_view, 2)

        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([820, 360])

        box.addWidget(splitter)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("运行日志（最近 300 条：接收与操作）")
        box = QVBoxLayout(group)
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setFixedHeight(110)
        self.log_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        box.addWidget(self.log_view)
        return group

    def _hint_text(self) -> str:
        """界面底部的用法说明（会跟着设置变）。"""
        return (
            "用法：在 Word / 浏览器 / PDF 阅读器中按 Ctrl+P，选择 PDF24 虚拟打印机，"
            "PDF24 自动保存到 incoming 后即成为待处理任务；双击任务可预览 PDF，点击表头可排序。"
            "「页面拆分」把 PDF 按纸张关系拆成单页文件（page_001.pdf、page_003.pdf…），"
            "存到 Data/jobs/<任务号>/，原始 PDF 不会被修改。"
            f"「打印单页」把选中的某一页交给 {self.config.printer_name}"
            f"（PDF24 等虚拟打印机不可选）；命令发送成功不等于纸张已出，请以实际纸张为准。"
            "「手动双面打印」按物理纸张逐面提交和确认：每次只打印一个页面，"
            f"确认完整出纸后才继续下一页；每批 {self.config.sheets_per_batch} 张纸，"
            "本批正面全部完成后提示翻面（"
            f"{self.config.flip_direction_text}），确认翻面后才打印背面。"
            "任意一面异常时整张纸（正反两面）一起作废，退出当前循环；"
            "正常纸张全部打印完后自动开始补打循环，逐张补打、同样逐面确认，"
            "补打中再次异常则进入下一轮补打清单。"
            "删除任务默认把源 PDF 归档到 Data/completed/removed，避免下次扫描又被登记。"
            "阶段八新增：设置（打印机、每批纸张数、翻面方向、备份策略）、异常记录、"
            "运行日志、数据备份与恢复、任务归档。"
        )

    def _build_hint_label(self) -> QLabel:
        hint = QLabel(self._hint_text())
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return hint

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")

        scan_action = file_menu.addAction("扫描待处理目录(&S)")
        scan_action.setShortcut("F5")
        scan_action.triggered.connect(lambda: self._on_scan_clicked())

        refresh_action = file_menu.addAction("刷新任务列表(&R)")
        refresh_action.setShortcut("F6")
        refresh_action.triggered.connect(lambda: self._refresh_tasks())

        open_incoming_action = file_menu.addAction("打开 incoming 目录(&I)")
        open_incoming_action.triggered.connect(lambda: self._on_open_folder_clicked())

        file_menu.addSeparator()
        backup_action = file_menu.addAction("备份数据(&K)…")
        backup_action.triggered.connect(lambda: self._on_backup_clicked())
        restore_action = file_menu.addAction("从备份恢复(&V)…")
        restore_action.triggered.connect(lambda: self._on_restore_clicked())
        archive_action = file_menu.addAction("归档已完成任务(&H)…")
        archive_action.triggered.connect(lambda: self._on_archive_clicked())

        file_menu.addSeparator()
        quit_action = file_menu.addAction("退出(&Q)")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)

        task_menu = self.menuBar().addMenu("任务(&T)")
        for action in (
            self.act_preview,
            self.act_open_source,
            self.act_open_folder,
        ):
            task_menu.addAction(action)
        task_menu.addSeparator()
        task_menu.addAction(self.act_page_plan)
        task_menu.addAction(self.act_print_page)
        task_menu.addAction(self.act_duplex)
        task_menu.addSeparator()
        for action in (
            self.act_ready,
            self.act_pause,
            self.act_resume,
            self.act_cancel,
        ):
            task_menu.addAction(action)
        task_menu.addSeparator()
        task_menu.addAction(self.act_delete)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        env_action = help_menu.addAction("运行环境自检(&E)")
        env_action.triggered.connect(lambda: self._show_environment())
        log_action = help_menu.addAction("运行日志(&L)…")
        log_action.triggered.connect(lambda: self._on_log_clicked())
        open_log_action = help_menu.addAction("用记事本打开日志文件(&O)")
        open_log_action.triggered.connect(lambda: self._open_log_file())
        anomalies_action = help_menu.addAction("异常记录(&A)…")
        anomalies_action.triggered.connect(lambda: self._on_anomalies_clicked())
        help_menu.addSeparator()
        settings_action = help_menu.addAction("设置(&S)…")
        settings_action.triggered.connect(lambda: self._on_settings_clicked())
        about_action = help_menu.addAction("关于(&A)")
        about_action.triggered.connect(lambda: self._show_about())

    # -- 数据刷新 -----------------------------------------------------------

    def _status_filter(self) -> tuple[TaskStatus, ...] | None:
        index = self.filter_combo.currentIndex()
        if 0 <= index < len(_STATUS_FILTERS):
            return _STATUS_FILTERS[index][1]
        return None

    def _refresh_tasks(self) -> None:
        """从数据库重新加载任务列表（保留当前选中的任务）。"""
        selected_uid = self._current_task_uid()
        try:
            self._tasks = self.repository.list_tasks(
                statuses=self._status_filter(),
                search=self.search_edit.text() if hasattr(self, "search_edit") else "",
                sort_by=self._sort_key,
                descending=self._sort_desc,
                limit=TASK_LIMIT,
            )
            self._counts = self.repository.count_by_status()
        except Exception as exc:
            logger.exception("读取任务列表失败")
            self._append_log(f"读取任务列表失败：{exc}")
            return

        self._fill_table(selected_uid)
        self._update_summary()
        self._update_sort_label()
        self._update_action_state()

    def _fill_table(self, selected_uid: str | None) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._tasks))
        for row, task in enumerate(self._tasks):
            sheets = task.estimated_sheets
            batches = task.estimated_batches(self.config.sheets_per_batch)
            values = (
                task.task_uid,
                task.document_name,
                str(task.page_count),
                f"{sheets} 张",
                f"{batches} 批",
                task.size_text,
                task.created_at_text,
                task.status_label,
                self._task_progress_text(task),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, task.task_uid)
                if column in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == _STATUS_COLUMN:
                    item.setForeground(QColor(_STATUS_COLORS.get(task.status, "#333333")))
                if column == _PROGRESS_COLUMN and text not in ("—", "已完成"):
                    item.setToolTip(text)
                if not task.source_exists:
                    font = item.font()
                    font.setItalic(True)
                    item.setFont(font)
                    item.setToolTip("源 PDF 文件已不在原处（可能被移动或删除）")
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)

        row = self._row_of_uid(selected_uid) if selected_uid else None
        if row is None and self._tasks:
            row = 0
        if row is not None:
            self.table.selectRow(row)
        else:
            self.table.clearSelection()
        self._on_selection_changed()

    def _row_of_uid(self, task_uid: str) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == task_uid:
                return row
        return None

    def _current_task_uid(self) -> str | None:
        task = self._current_task()
        return task.task_uid if task else None

    def _update_summary(self) -> None:
        total = sum(self._counts.values())
        parts = [
            f"{status.label} {self._counts[status]}"
            for status in _STATUS_ORDER
            if self._counts.get(status)
        ]
        text = f"任务总数：{total}"
        if parts:
            text += f"（{'，'.join(parts)}）"
        if len(self._tasks) != total:
            text += f"　当前显示：{len(self._tasks)}"
        self.summary_label.setText(text)

    # -- 选中状态 -----------------------------------------------------------

    def _selected_tasks(self) -> list[Task]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._tasks[row] for row in rows if 0 <= row < len(self._tasks)]

    def _current_task(self) -> Task | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._tasks):
            return None
        return self._tasks[row]

    def _on_selection_changed(self) -> None:
        self._update_detail(self._current_task())
        self._update_action_state()

    def _update_detail(self, task: Task | None) -> None:
        if task is None:
            self.detail_view.setHtml('<p style="color:#888;">尚未选择任务。</p>')
            self.event_view.setPlainText("")
            return

        exists = task.source_exists
        rows = [
            ("任务号", task.task_uid),
            ("状态", task.status_label),
            ("页数", f"{task.page_count} 页"),
            (
                "纸张",
                f"{task.size_text}（{task.width_pt:.0f} × {task.height_pt:.0f} pt）",
            ),
            (
                "预计用纸",
                f"{task.estimated_sheets} 张，约 "
                f"{task.estimated_batches(self.config.sheets_per_batch)} 批"
                f"（每批 {self.config.sheets_per_batch} 张）",
            ),
            ("打印批次", self._batches_text(task)),
            ("最近打印", self._last_print_text(task)),
            ("双面打印", self._duplex_text(task)),
            ("文件大小", f"{task.file_size:,} 字节"),
            ("PDF24 任务", task.source_job_id or "未识别"),
            ("创建时间", task.created_at_text),
            ("更新时间", task.updated_at_text),
            ("源文件", str(task.source_path) + ("" if exists else "（文件已不存在）")),
        ]
        if task.note:
            rows.append(("提示", task.note))

        rows_html = "".join(
            "<tr>"
            f"<td style='color:#666; padding:2px 10px 2px 0; vertical-align:top; white-space:nowrap;'>"
            f"{html.escape(name)}</td>"
            f"<td style='padding:2px 0;'>{html.escape(str(value))}</td>"
            "</tr>"
            for name, value in rows
        )
        missing_html = (
            "<p style='color:#C62828; margin:2px 0 6px 0;'>源 PDF 文件已不在原处，无法预览。</p>"
            if not exists
            else ""
        )
        self.detail_view.setHtml(
            f"<h3 style='margin:0 0 4px 0;'>{html.escape(task.document_name)}</h3>"
            f"<p style='color:{_STATUS_COLORS.get(task.status, '#333')}; font-weight:bold; "
            f"margin:0 0 6px 0;'>状态：{html.escape(task.status_label)}</p>"
            f"{missing_html}"
            f"<table style='font-size:13px;'>{rows_html}</table>"
        )
        self._update_events(task)

    def _task_progress_text(self, task: Task) -> str:
        """任务列表的「打印进度」列（阶段八）。

        没开始双面打印的任务显示「—」；开始了就显示纸张口径的进度，
        补打轮里再带上轮次，让用户一眼看出「还剩几张纸」。
        """
        try:
            if not self.duplex.has_session(task.task_uid):
                return "—"
            snapshot = self.duplex.snapshot(task.task_uid, task)
        except Exception as exc:  # noqa: BLE001 - 读取失败不影响列表
            logger.warning("读取打印进度失败：%s", exc)
            return "读取失败"
        if snapshot is None:
            return "—"
        if snapshot.is_completed:
            return "已完成"
        if snapshot.phase == PHASE_CANCELLED:
            return "已取消"
        text = snapshot.sheet_progress_text
        if snapshot.has_reprint:
            text = f"{snapshot.round_text}：{text}"
        if snapshot.reprint_pending_count and not snapshot.is_reprint_round:
            text += f"（待补打 {snapshot.reprint_pending_count} 张）"
        return text

    def _batches_text(self, task: Task) -> str:
        """详情里显示的打印批次状态（阶段四生成的单页文件）。"""
        try:
            batches = self.repository.list_batches(task.task_uid)
        except sqlite3.Error as exc:  # pragma: no cover - 数据库异常时不让界面崩
            logger.warning("读取批次失败：%s", exc)
            return "读取失败"
        if not batches:
            return "尚未生成（可用「页面拆分」生成单页文件）"
        sheets = sum(batch.sheet_count for batch in batches)
        pages = sum(batch.page_count for batch in batches)
        missing = sum(len(batch.missing_files()) for batch in batches)
        text = f"已生成 {len(batches)} 批，{sheets} 张纸，{pages} 个单页文件"
        if missing:
            text += f"（{missing} 个文件已丢失，建议重新生成）"
        return text

    def _last_print_text(self, task: Task) -> str:
        """详情里显示最近一次打印提交（阶段五）。"""
        try:
            jobs = self.repository.list_print_jobs(task.task_uid, limit=1)
        except Exception as exc:  # noqa: BLE001 - 读取失败不影响详情显示
            logger.warning("读取打印记录失败：%s", exc)
            return "读取失败"
        if not jobs:
            return "尚未打印（可用「打印单页」发送某一页）"
        job = jobs[0]
        text = f"{job.created_at_text} 第 {job.page_number} 页 {job.status_text}"
        if job.job_id:
            text += f"，队列作业 #{job.job_id}"
        return text

    def _duplex_text(self, task: Task) -> str:
        """详情里显示逐面确认的双面打印进度（阶段六 / 阶段七）。"""
        try:
            snapshot = self.duplex.snapshot(task.task_uid, task)
        except Exception as exc:  # noqa: BLE001 - 读取失败不影响详情显示
            logger.warning("读取双面打印状态失败：%s", exc)
            return "读取失败"
        if snapshot is None:
            return "尚未开始（可用「手动双面打印」逐面提交、逐面确认）"
        summary = snapshot.summary_text()
        if snapshot.phase_text in summary:
            text = summary
        else:
            text = f"{snapshot.phase_text}：{summary}"
        if snapshot.reprint_pending_count:
            text += (
                f"（待补打 {snapshot.reprint_pending_count} 张："
                f"{snapshot.reprint_pending_text}）"
            )
        return text

    def _update_events(self, task: Task) -> None:
        try:
            events = self.repository.list_events(task.task_uid, limit=100)
        except Exception as exc:
            logger.exception("读取任务事件失败")
            self.event_view.setPlainText(f"读取任务记录失败：{exc}")
            return
        if not events:
            self.event_view.setPlainText("（暂无记录）")
            return
        lines = [
            f"{event.created_at:%Y-%m-%d %H:%M:%S}  [{event.event_type}] {event.message}"
            for event in reversed(events)
        ]
        self.event_view.setPlainText("\n".join(lines))

    def _update_action_state(self) -> None:
        tasks = self._selected_tasks()
        single = tasks[0] if len(tasks) == 1 else None
        self.act_preview.setEnabled(bool(single and single.source_exists))
        self.act_open_source.setEnabled(bool(single and single.source_exists))
        self.act_open_folder.setEnabled(bool(single))
        self.act_page_plan.setEnabled(bool(single and single.source_exists))
        self.act_print_page.setEnabled(bool(single and single.source_exists))
        self.act_duplex.setEnabled(bool(single and single.source_exists))
        self.act_ready.setEnabled(any(t.status == TaskStatus.PENDING for t in tasks))
        self.act_pause.setEnabled(
            any(
                t.status
                in {
                    TaskStatus.READY,
                    TaskStatus.PRINTING_FRONT,
                    TaskStatus.WAITING_FLIP,
                    TaskStatus.PRINTING_BACK,
                    TaskStatus.REPRINT,
                    TaskStatus.ERROR,
                }
                for t in tasks
            )
        )
        self.act_resume.setEnabled(any(t.status == TaskStatus.PAUSED for t in tasks))
        self.act_cancel.setEnabled(any(t.status in ACTIVE_STATUSES for t in tasks))
        self.act_delete.setEnabled(bool(tasks))

    def _update_sort_label(self) -> None:
        direction = "倒序" if self._sort_desc else "正序"
        names = {
            "task_uid": "任务号",
            "document_name": "文件名",
            "page_count": "页数",
            "paper_size": "纸张",
            "created_at": "创建时间",
            "status": "状态",
        }
        self.sort_label.setText(
            f"排序：{names.get(self._sort_key, self._sort_key)} {direction}"
        )

    def _update_sort_indicator(self) -> None:
        header = self.table.horizontalHeader()
        column = next(
            (col for col, key in _COLUMN_SORT_KEYS.items() if key == self._sort_key), 6
        )
        header.setSortIndicatorShown(True)
        header.setSortIndicator(
            column, Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        )

    # -- 交互：排序与筛选 ---------------------------------------------------

    def _on_header_clicked(self, column: int) -> None:
        key = _COLUMN_SORT_KEYS.get(column)
        if key is None:
            return
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            # 时间列默认倒序（最新的在前），其他列默认正序
            self._sort_desc = key in {"created_at", "updated_at"}
        self._update_sort_indicator()
        self._update_sort_label()
        self._refresh_tasks()

    def _on_search_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _on_clear_filter(self) -> None:
        self.filter_combo.blockSignals(True)
        self.filter_combo.setCurrentIndex(0)
        self.filter_combo.blockSignals(False)
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._refresh_tasks()

    # -- 交互：预览与打开文件 -----------------------------------------------

    def create_preview_dialog(self, task: Task) -> PdfPreviewDialog:
        """创建预览对话框（单独抽出，便于验收脚本直接构造）。"""
        return PdfPreviewDialog(
            task.source_path,
            self,
            title=f"{task.document_name}（{task.task_uid}）",
        )

    def _on_preview_clicked(self) -> None:
        task = self._current_task()
        if task is None:
            return
        if not task.source_exists:
            QMessageBox.warning(
                self, "无法预览", f"源 PDF 文件已不存在：\n{task.source_path}"
            )
            return
        dialog = self.create_preview_dialog(task)
        dialog.exec()

    def _on_open_source_clicked(self) -> None:
        task = self._current_task()
        if task is None:
            return
        self._startfile(task.source_path, "无法打开 PDF")

    # -- 交互：页面拆分（阶段四） -------------------------------------------

    def create_page_plan_dialog(self, task: Task) -> PagePlanDialog:
        """创建页面拆分对话框（单独抽出，便于验收脚本直接构造）。"""
        return PagePlanDialog(task, service=self.page_jobs, config=self.config, parent=self)

    def _on_page_plan_clicked(self) -> None:
        task = self._current_task()
        if task is None:
            return
        if not task.source_exists:
            QMessageBox.warning(
                self, "无法拆分", f"源 PDF 文件已不存在：\n{task.source_path}"
            )
            return
        dialog = self.create_page_plan_dialog(task)
        dialog.exec()

        if dialog.error_message:
            self._append_log(f"页面拆分失败：{dialog.error_message}")
        elif dialog.last_result is not None:
            result = dialog.last_result
            self._append_log(
                f"已生成单页打印文件：{result.task_uid} {result.plan.summary_text}，"
                f"{len(result.pages)} 个单页文件，目录 {result.job_dir}"
            )
        if dialog.last_result is not None or dialog.error_message:
            self._refresh_tasks()
            self._update_detail(task)

    # -- 交互：打印单页（阶段五） -------------------------------------------

    def create_print_dialog(self, task: Task) -> PrintPageDialog:
        """创建打印单页对话框（单独抽出，便于验收脚本直接构造）。"""
        return PrintPageDialog(
            task, service=self.printer, config=self.config, parent=self
        )

    def _on_print_page_clicked(self) -> None:
        task = self._current_task()
        if task is None:
            return
        if not task.source_exists:
            QMessageBox.warning(
                self, "无法打印", f"源 PDF 文件已不存在：\n{task.source_path}"
            )
            return
        dialog = self.create_print_dialog(task)
        dialog.exec()
        self._report_print_outcome(dialog)

    def _report_print_outcome(self, dialog: PrintPageDialog) -> None:
        """把对话框的结果写进运行日志（真正的打印记录已由服务写库）。"""
        outcome = dialog.last_outcome
        if outcome is None:
            if dialog.error_message:
                self._append_log(f"打印单页失败：{dialog.error_message}")
            return

        stamp = f"{datetime.now():%H:%M:%S}"
        if outcome.dry_run:
            self._append_log(f"{stamp} 打印演练（未发送）：{outcome.summary_text()}")
            return

        if outcome.ok:
            self._append_log(
                f"{stamp} 命令已发送：{outcome.summary_text()}"
                f"（队列作业 #{outcome.job.job_id}）"
                if outcome.job and outcome.job.job_id
                else f"{stamp} 命令已发送：{outcome.summary_text()}"
            )
        else:
            self._append_log(f"{stamp} 未发送/发送失败：{outcome.summary_text()}")
        self._refresh_tasks()
        self._update_detail(outcome.task)

    # -- 交互：手动双面打印（阶段六） ---------------------------------------

    def create_duplex_dialog(self, task: Task) -> DuplexDialog:
        """创建手动双面打印窗口（单独抽出，便于验收脚本直接构造）。"""
        return DuplexDialog(task, service=self.duplex, config=self.config, parent=self)

    def _on_duplex_clicked(self) -> None:
        task = self._current_task()
        if task is None:
            return
        if not task.source_exists:
            QMessageBox.warning(
                self, "无法打印", f"源 PDF 文件已不存在：\n{task.source_path}"
            )
            return
        dialog = self.create_duplex_dialog(task)
        dialog.exec()
        self._report_duplex_state(task, dialog)

    def _report_duplex_state(self, task: Task, dialog: DuplexDialog) -> None:
        """把双面打印窗口的结果写进运行日志（真正的进度已由服务写库）。"""
        stamp = f"{datetime.now():%H:%M:%S}"
        snapshot = dialog.snapshot
        if snapshot is None:
            if dialog.log_lines:
                self._append_log(f"{stamp} 手动双面打印：未开始")
            return
        self._append_log(f"{stamp} 手动双面打印：{snapshot.summary_text()}")
        self._refresh_tasks()
        self._update_detail(task)

    def _on_open_folder_of_task(self) -> None:
        task = self._current_task()
        if task is None:
            return
        self._startfile(task.source_path.parent, "无法打开目录")

    def _on_open_folder_clicked(self) -> None:
        directory = self.config.incoming_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法打开目录", f"{directory}\n\n{exc}")
            return
        self._startfile(directory, "无法打开目录")

    def _startfile(self, path: Path, error_title: str) -> None:
        if not path.exists():
            QMessageBox.warning(self, error_title, f"路径不存在：\n{path}")
            return
        try:
            os.startfile(str(path))  # noqa: S606 (Windows 专用)
        except OSError as exc:
            QMessageBox.warning(self, error_title, f"{path}\n\n{exc}")

    def _open_log_file(self) -> None:
        self._startfile(log_file_path(), "无法打开日志文件")

    # -- 交互：状态与删除 ---------------------------------------------------

    def _apply_status(self, target: TaskStatus, action_name: str) -> int:
        """对选中的任务批量应用状态；返回实际变更数量。"""
        tasks = [task for task in self._selected_tasks() if self.admin.can_transition(task.status, target)]
        if not tasks:
            QMessageBox.information(
                self, action_name, "当前选中的任务不能执行这个操作。"
            )
            return 0

        changed = 0
        problems: list[str] = []
        for task in tasks:
            try:
                self.admin.set_status(task.task_uid, target, action_name)
                changed += 1
            except TaskAdminError as exc:
                problems.append(str(exc))
            except Exception as exc:  # 数据库异常等
                logger.exception("变更任务状态失败：%s", task.task_uid)
                problems.append(f"{task.task_uid}：{exc}")

        if changed:
            self._append_log(f"{datetime.now():%H:%M:%S} {action_name}：{changed} 个任务")
        if problems:
            QMessageBox.warning(self, "部分任务未处理", "\n".join(problems[:10]))
        self._refresh_tasks()
        return changed

    def _on_mark_ready(self) -> None:
        self._apply_status(TaskStatus.READY, "标记为已准备")

    def _on_pause(self) -> None:
        self._apply_status(TaskStatus.PAUSED, "暂停任务")

    def _on_resume(self) -> None:
        self._apply_status(TaskStatus.READY, "恢复任务")

    def _on_cancel(self) -> None:
        tasks = [task for task in self._selected_tasks() if task.status in ACTIVE_STATUSES]
        if not tasks:
            QMessageBox.information(self, "取消任务", "当前选中的任务不能取消。")
            return
        answer = QMessageBox.question(
            self,
            "取消任务",
            f"确定要取消选中的 {len(tasks)} 个任务吗？\n\n"
            "任务记录会保留，并将状态标记为「已取消」。\n"
            "注意：这一阶段还没有真正向打印机发送数据，取消不会影响打印机。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            self._apply_status(TaskStatus.CANCELLED, "取消任务")

    def _on_delete_clicked(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            return

        preview = "\n".join(
            f"· {task.document_name}（{task.task_uid}）" for task in tasks[:8]
        )
        if len(tasks) > 8:
            preview += f"\n……共 {len(tasks)} 个任务"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("删除任务")
        box.setText(f"确定要删除选中的 {len(tasks)} 个任务吗？\n\n{preview}")
        box.setInformativeText(
            "任务记录会从数据库中删除，且无法恢复。\n"
            "源 PDF 默认移动到 Data/completed/removed 留档；如果选择删除文件，"
            "文件将不可恢复。"
        )
        checkbox = QCheckBox("同时删除 incoming 目录中的原始 PDF 文件")
        checkbox.setChecked(False)
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)

        if box.exec() != QMessageBox.Yes:
            return
        self.delete_tasks([task.task_uid for task in tasks], delete_files=checkbox.isChecked())

    def delete_tasks(
        self, task_uids: list[str], delete_files: bool = False
    ) -> list[DeleteOutcome]:
        """删除任务（确认对话框之外的真正执行逻辑，验收脚本可以直接调用）。"""
        outcomes = self.admin.delete_tasks(task_uids, delete_files=delete_files)
        deleted = sum(1 for outcome in outcomes if outcome.record_deleted)
        archived = sum(1 for outcome in outcomes if outcome.file_action == FILE_ARCHIVED)
        removed = sum(1 for outcome in outcomes if outcome.file_action == FILE_DELETED)
        failures = [outcome for outcome in outcomes if outcome.error]

        timestamp = f"{datetime.now():%H:%M:%S}"
        if deleted:
            detail = f"删除任务：{deleted} 个"
            if archived:
                detail += f"，归档源文件 {archived} 个"
            if removed:
                detail += f"，删除源文件 {removed} 个"
            self._append_log(f"{timestamp} {detail}")
        for outcome in failures:
            message = f"删除失败 {outcome.task_uid}：{outcome.error}"
            logger.error(message)
            self._append_log(f"{timestamp} {message}")

        # 任务记录没了，它名下的批次文件也一并清理，避免 Data/jobs 里留下孤儿目录
        for outcome in outcomes:
            if not outcome.record_deleted:
                continue
            try:
                self.page_jobs.remove_files(outcome.task_uid)
            except Exception as exc:  # noqa: BLE001 - 清理失败不应影响删除结果
                logger.warning("清理任务目录失败：%s（%s）", outcome.task_uid, exc)

        self._refresh_tasks()
        return outcomes

    # -- 上下文菜单 ---------------------------------------------------------

    def _show_context_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if index.isValid() and index.row() not in {
            item.row() for item in self.table.selectionModel().selectedRows()
        }:
            self.table.selectRow(index.row())

        menu = QMenu(self)
        menu.addAction(self.act_preview)
        menu.addAction(self.act_open_source)
        menu.addAction(self.act_open_folder)
        menu.addSeparator()
        menu.addAction(self.act_page_plan)
        menu.addAction(self.act_print_page)
        menu.addAction(self.act_duplex)
        menu.addSeparator()
        menu.addAction(self.act_ready)
        menu.addAction(self.act_pause)
        menu.addAction(self.act_resume)
        menu.addAction(self.act_cancel)
        menu.addSeparator()
        menu.addAction(self.act_delete)
        menu.exec(self.table.viewport().mapToGlobal(position))

    # -- 接收服务轮询 -------------------------------------------------------

    def _on_scan_clicked(self) -> None:
        self.scan_button.setEnabled(False)
        try:
            queued = self.intake.scan_now(reason="手动扫描")
            self._append_log(
                f"{datetime.now():%H:%M:%S} 已提交扫描，新入队 {queued} 个 PDF"
            )
        finally:
            self.scan_button.setEnabled(True)
        self._update_status_bar()

    def _poll_intake(self) -> None:
        """定时取出接收服务的通知（后台线程不直接操作界面）。"""
        notifications = self.intake.drain_notifications()
        if notifications:
            refresh_needed = False
            for note in notifications:
                if note.kind == "info" and "入队 0 个" in note.message:
                    continue  # 定时扫描没有新文件时不刷屏
                self._append_log(f"{note.created_at:%H:%M:%S} {note.message}")
                if note.kind == "registered":
                    refresh_needed = True
            if refresh_needed:
                self._refresh_tasks()
        self._update_status_bar()

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _log_restored_tasks(self) -> None:
        """启动时提示从数据库恢复了多少任务（对应「退出后恢复任务」）。"""
        total = sum(self._counts.values())
        if not total:
            return
        active = sum(self._counts.get(status, 0) for status in ACTIVE_STATUSES)
        self._append_log(
            f"{datetime.now():%H:%M:%S} 已从数据库恢复 {total} 个任务，"
            f"其中 {active} 个尚未结束"
        )

    def _update_status_bar(self) -> None:
        state = self.intake.state()
        stats = state.stats
        watch_state = "目录监控：运行中" if state.watcher_running else "目录监控：未运行"
        total = sum(self._counts.values())
        self.statusBar().showMessage(
            f"{watch_state} | 队列 {state.pending} 个 | "
            f"当前显示 {len(self._tasks)} / 共 {total} 个任务 | "
            f"发现 {stats.discovered}，已登记 {stats.registered}，"
            f"重复 {stats.duplicates}，失败 {stats.failed} | "
            f"打印机：{self.config.printer_name}（每批 {self.config.sheets_per_batch} 张纸，"
            f"{self.config.flip_direction_text}）"
        )

    # -- 阶段八：设置、日志、异常记录、备份与归档 ---------------------------

    @property
    def backup_service(self) -> BackupService:
        """备份服务（每次按当前配置构造，配置改了立刻生效）。"""
        return BackupService(
            database_file=self.config.database_file,
            backup_dir=self.config.backup_dir,
            config_file=CONFIG_FILE,
            jobs_dir=self.config.jobs_dir,
            keep=self.config.backup.keep,
            include_jobs=self.config.backup.include_jobs,
        )

    @property
    def archive_service(self) -> ArchiveService:
        return ArchiveService(
            self.repository,
            self.config.archive_root,
            jobs_dir=self.config.jobs_dir,
        )

    def create_settings_dialog(self) -> SettingsDialog:
        """创建设置对话框（单独抽出，便于验收脚本直接构造）。"""
        return SettingsDialog(self.config, self, config_path=CONFIG_FILE)

    def _on_settings_clicked(self) -> None:
        dialog = self.create_settings_dialog()
        if dialog.exec() != SettingsDialog.Accepted or dialog.saved_config is None:
            return
        self._apply_config(dialog.saved_config)

    def _apply_config(self, config: AppConfig) -> None:
        """设置保存后重建依赖配置的服务，不必重启程序。"""
        self.config = config
        self.page_jobs = PageJobService.from_config(config, self.repository)
        self.printer = PrintService(
            config=config,
            repository=self.repository,
            page_jobs=self.page_jobs,
        )
        self.duplex = DuplexPrintService(
            repository=self.repository,
            print_service=self.printer,
            config=config,
        )
        self._append_log(
            f"{datetime.now():%H:%M:%S} 设置已更新：打印机 {config.printer_name}，"
            f"每批 {config.sheets_per_batch} 张纸，翻面方向：{config.flip_direction_text}"
        )
        self.hint_label.setText(self._hint_text())
        self._refresh_tasks()
        self._update_status_bar()

    def create_log_view_dialog(self) -> LogViewDialog:
        return LogViewDialog(self, log_path=log_file_path())

    def _on_log_clicked(self) -> None:
        self.create_log_view_dialog().exec()

    def create_anomaly_dialog(self) -> AnomalyDialog:
        return AnomalyDialog(
            self.repository, self, open_duplex=self._open_duplex_of_task
        )

    def _on_anomalies_clicked(self) -> None:
        self.create_anomaly_dialog().exec()

    def _open_duplex_of_task(self, task_uid: str) -> None:
        """从异常记录跳回某个任务的双面打印窗口。"""
        task = self.repository.get_task(task_uid)
        if task is None:
            QMessageBox.information(self, "异常记录", f"任务已不存在：{task_uid}")
            return
        row = self._row_of_uid(task_uid)
        if row is not None:
            self.table.selectRow(row)
        dialog = self.create_duplex_dialog(task)
        dialog.exec()
        self._refresh_tasks()

    def _on_backup_clicked(self) -> None:
        try:
            entry = self.backup_service.create(label="manual")
        except BackupError as exc:
            QMessageBox.warning(self, "备份数据", f"备份失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 备份失败不应让程序崩
            logger.exception("备份失败")
            QMessageBox.warning(self, "备份数据", f"备份失败：{exc}")
            return
        self._append_log(f"{datetime.now():%H:%M:%S} 已备份数据：{entry.path.name}")
        QMessageBox.information(
            self,
            "备份数据",
            f"备份完成：\n{entry.path}\n\n"
            f"{entry.size_text}，包含 {entry.tasks_text}，schema v{entry.schema_version}。",
        )

    def _on_restore_clicked(self) -> None:
        backups = self.backup_service.list_backups()
        if not backups:
            QMessageBox.information(
                self,
                "从备份恢复",
                f"还没有备份。备份默认放在：\n{self.config.backup_dir}",
            )
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择备份文件",
            str(self.config.backup_dir),
            "SmartPrint 备份 (*.zip)",
        )
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "从备份恢复",
            f"将用这个备份替换当前数据库：\n{Path(path).name}\n\n"
            "恢复前会先把当前数据库另存一份。恢复后请重新打开程序，"
            "让所有窗口读到新数据。\n\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            restored = self.backup_service.restore(Path(path))
        except BackupError as exc:
            QMessageBox.warning(self, "从备份恢复", f"恢复失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复备份失败")
            QMessageBox.warning(self, "从备份恢复", f"恢复失败：{exc}")
            return
        self._append_log(f"{datetime.now():%H:%M:%S} 已从备份恢复：{restored.name}")
        QMessageBox.information(
            self,
            "从备份恢复",
            f"已恢复到：\n{restored}\n\n请关闭并重新打开 SmartPrint，确认数据正确。",
        )

    def _on_archive_clicked(self) -> None:
        tasks = [task for task in self._selected_tasks() if ArchiveService.can_archive(task)]
        if not tasks:
            tasks = [
                task
                for task in self.repository.list_tasks(limit=TASK_LIMIT)
                if ArchiveService.can_archive(task)
            ]
        if not tasks:
            QMessageBox.information(
                self, "归档任务", "没有可归档的任务（只有已完成或已取消的任务可以归档）。"
            )
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("归档任务")
        box.setText(f"把 {len(tasks)} 个已结束任务归档到：\n{self.config.archive_root}")
        box.setInformativeText(
            "归档会复制源 PDF 和任务记录到 Data/completed/archive/日期/任务号/，"
            "任务本身仍留在列表里，可以照常查看。"
        )
        pages_check = QCheckBox("同时复制单页打印文件（Data/jobs，体积较大）")
        pages_check.setChecked(False)
        box.setCheckBox(pages_check)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Yes)
        if box.exec() != QMessageBox.Yes:
            return
        result = self.archive_service.archive_tasks(
            tasks, include_pages=pages_check.isChecked()
        )
        self._append_log(
            f"{datetime.now():%H:%M:%S} 归档任务：{result.summary_text()}"
        )
        QMessageBox.information(self, "归档任务", result.summary_text())

    # -- 对话框 -------------------------------------------------------------

    def _show_environment(self) -> None:
        lines = [
            f"{name}：{value}"
            for name, value in _collect_environment(self.config.incoming_dir)
        ]
        lines.append(f"数据库：{self.config.database_file}")
        try:
            readiness = self.printer.check_readiness()
            lines.append(
                f"目标打印机：{self.config.printer_name} → "
                f"{readiness.target.name if readiness.target else '未找到'}"
            )
            if readiness.target is not None:
                lines.append(
                    f"打印机状态：{readiness.target.status_text}"
                    f"（端口 {readiness.target.port_name or '未知'}）"
                )
            lines.append(
                "SumatraPDF："
                + (str(readiness.sumatra_path) if readiness.sumatra_path else "未找到")
                + (f"（{readiness.sumatra_version}）" if readiness.sumatra_version else "")
            )
            lines.append(f"打印队列：{readiness.queue.summary_text() if readiness.queue else '未读取'}")
            for problem in readiness.problems:
                lines.append(f"× {problem}")
            for warning in readiness.warnings:
                lines.append(f"! {warning}")
        except Exception as exc:  # noqa: BLE001 - 自检失败也不让对话框崩掉
            logger.exception("打印环境自检失败")
            lines.append(f"打印环境自检失败：{exc}")
        QMessageBox.information(self, "运行环境自检", "\n".join(lines))

    def _about_text(self) -> str:
        """「关于」对话框的正文（单独抽出来，方便自检和验收脚本读）。"""
        return "\n".join(
            [
                f"{APP_NAME} {APP_VERSION}",
                "",
                "手动双面打印管理工具",
                "",
                "阶段三：任务管理界面（筛选、排序、搜索、PDF 预览、状态管理、删除）",
                "阶段四：页面拆分（物理纸张映射、按页码生成单页 PDF）",
                "阶段五：真实打印（枚举打印机、排除 PDF24、调用 SumatraPDF 打印单页）",
                "阶段六：手动双面打印（逐面提交、逐面确认、翻面确认、整张纸作废）",
                "阶段七：异常恢复与补打（整张纸作废、正常/补打循环分离、多轮补打、异常留痕）",
                "阶段八：界面完善与打包（纸张示意图、翻面方向提示、异常记录、设置、日志查看、"
                "数据备份与恢复、任务归档）",
            ]
        )

    def _show_about(self) -> None:
        QMessageBox.information(self, "关于 SmartPrint", self._about_text())

    # -- 退出 ---------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名风格)
        logger.info("主窗口关闭，正在停止任务接收服务")
        self._poll_timer.stop()
        try:
            self.intake.stop()
        except Exception:
            logger.exception("停止任务接收服务时出现异常")
        super().closeEvent(event)
