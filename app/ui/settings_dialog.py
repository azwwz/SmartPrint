"""设置界面（阶段八）。

readme 阶段八要求「增加设置界面」「增加默认打印机设置」。

这里只放**日常会改**的几项，改完写回 config/config.json：

- 打印用哪台打印机（下拉列出本机真实打印机，也可以手输完整名称）；
- 每批管理多少张纸（默认 5，readme 6.x 的口径）；
- 翻面方向（沿长边 / 沿短边），这个值同时驱动双面窗口里的示意图提示；
- SumatraPDF 路径、打印设置、提交超时（阶段五的参数，出问题时要能改）；
- 备份保留份数与是否打包单页文件（阶段八的数据备份）。

点「保存并应用」后返回新的 AppConfig，主窗口据此重建打印相关服务，
不需要退出程序。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.config import (
    DEFAULT_FLIP_DIRECTION,
    FLIP_DIRECTIONS,
    AppConfig,
    update_config,
)
from app.printer.windows_printers import (
    PrinterError,
    default_printer_name,
    list_real_printers,
    set_default_printer,
)
from app.utils.paths import CONFIG_FILE

logger = logging.getLogger("smartprint.ui.settings_dialog")

#: 每批纸张数的取值范围
MIN_SHEETS_PER_BATCH = 1
MAX_SHEETS_PER_BATCH = 20


class SettingsDialog(QDialog):
    """程序设置。"""

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(620, 560)
        self.config = config
        self.config_path = Path(config_path) if config_path else CONFIG_FILE
        #: 保存成功后是新的配置，取消时为 None
        self.saved_config: AppConfig | None = None
        self.show_errors = True

        self._build_ui()
        self.load_from_config(config)

    # -- 界面 ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(self._build_printer_group())
        layout.addWidget(self._build_paper_group())
        layout.addWidget(self._build_backup_group())

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color: #666;")
        layout.addWidget(self.info_label)

        layout.addStretch(1)

        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton("保存并应用(&S)", QDialogButtonBox.AcceptRole)
        self.cancel_button = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        self.defaults_button = buttons.addButton("恢复默认值(&R)", QDialogButtonBox.ResetRole)
        self.save_button.clicked.connect(self._on_save)
        self.cancel_button.clicked.connect(self.reject)
        self.defaults_button.clicked.connect(self._on_restore_defaults)
        layout.addWidget(buttons)

    def _build_printer_group(self) -> QGroupBox:
        group = QGroupBox("打印机")
        form = QFormLayout(group)

        self.printer_combo = QComboBox(self)
        self.printer_combo.setEditable(True)
        self.printer_combo.setInsertPolicy(QComboBox.NoInsert)
        self.printer_combo.setMinimumWidth(300)
        form.addRow("打印用打印机：", self.printer_combo)

        row = QHBoxLayout()
        self.refresh_printers_button = QPushButton("重新读取打印机(&L)")
        self.refresh_printers_button.clicked.connect(self._on_refresh_printers)
        row.addWidget(self.refresh_printers_button)
        self.set_default_button = QPushButton("设为 Windows 默认打印机(&W)")
        self.set_default_button.clicked.connect(self._on_set_windows_default)
        row.addWidget(self.set_default_button)
        row.addStretch(1)
        wrapper = QWidget(self)
        wrapper.setLayout(row)
        form.addRow("", wrapper)

        self.printer_hint = QLabel("")
        self.printer_hint.setWordWrap(True)
        self.printer_hint.setStyleSheet("color: #777;")
        form.addRow("", self.printer_hint)

        self.sumatra_edit = QLineEdit(self)
        self.sumatra_edit.setPlaceholderText("留空则自动在 drivers/、常见目录和注册表里查找")
        sumatra_row = QHBoxLayout()
        sumatra_row.addWidget(self.sumatra_edit, 1)
        self.sumatra_button = QPushButton("浏览…(&O)")
        self.sumatra_button.clicked.connect(self._on_browse_sumatra)
        sumatra_row.addWidget(self.sumatra_button)
        sumatra_wrapper = QWidget(self)
        sumatra_wrapper.setLayout(sumatra_row)
        form.addRow("SumatraPDF 路径：", sumatra_wrapper)

        self.print_settings_edit = QLineEdit(self)
        self.print_settings_edit.setPlaceholderText("例如 simplex，留空使用默认值")
        form.addRow("打印设置：", self.print_settings_edit)

        self.timeout_spin = QDoubleSpinBox(self)
        self.timeout_spin.setRange(5.0, 600.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSuffix(" 秒")
        form.addRow("等待打印命令超时：", self.timeout_spin)

        return group

    def _build_paper_group(self) -> QGroupBox:
        group = QGroupBox("纸张与翻面")
        form = QFormLayout(group)

        self.sheets_spin = QSpinBox(self)
        self.sheets_spin.setRange(MIN_SHEETS_PER_BATCH, MAX_SHEETS_PER_BATCH)
        self.sheets_spin.setSuffix(" 张 / 批")
        form.addRow("每批管理的物理纸张：", self.sheets_spin)

        self.flip_combo = QComboBox(self)
        for key, label in FLIP_DIRECTIONS:
            self.flip_combo.addItem(label, key)
        form.addRow("翻面方向：", self.flip_combo)

        flip_hint = QLabel(
            "翻面方向必须按实际打印机实测：先用一份两页的 PDF 试打一张，"
            "确认背面内容没有倒置、也没有印在同一面的反面。"
        )
        flip_hint.setWordWrap(True)
        flip_hint.setStyleSheet("color: #777;")
        form.addRow("", flip_hint)
        return group

    def _build_backup_group(self) -> QGroupBox:
        group = QGroupBox("数据备份")
        form = QFormLayout(group)

        self.backup_keep_spin = QSpinBox(self)
        self.backup_keep_spin.setRange(1, 100)
        self.backup_keep_spin.setSuffix(" 份")
        form.addRow("最多保留备份：", self.backup_keep_spin)

        self.backup_jobs_check = QCheckBox("备份时一并打包 Data/jobs 里的单页文件（体积较大）")
        form.addRow("", self.backup_jobs_check)

        self.backup_dir_label = QLabel("")
        self.backup_dir_label.setWordWrap(True)
        self.backup_dir_label.setStyleSheet("color: #777;")
        form.addRow("备份目录：", self.backup_dir_label)
        return group

    # -- 数据 ---------------------------------------------------------------

    def load_from_config(self, config: AppConfig) -> None:
        """把配置填进控件。"""
        self.config = config
        self._reload_printer_items(config.printer_name)
        self.sumatra_edit.setText(config.printer.sumatra_path)
        self.print_settings_edit.setText(config.printer.print_settings)
        self.timeout_spin.setValue(float(config.printer.timeout_seconds))
        self.sheets_spin.setValue(int(config.sheets_per_batch))
        index = self.flip_combo.findData(config.flip_direction)
        self.flip_combo.setCurrentIndex(index if index >= 0 else 0)
        self.backup_keep_spin.setValue(int(config.backup.keep))
        self.backup_jobs_check.setChecked(bool(config.backup.include_jobs))
        self.backup_dir_label.setText(str(config.backup_dir))
        self.info_label.setText(
            f"配置文件：{self.config_path}　每批 {config.sheets_per_batch} 张纸，"
            f"翻面方向：{config.flip_direction_text}"
        )

    def _reload_printer_items(self, current: str) -> None:
        """重新读取打印机列表，并保证当前配置值在列表里。"""
        self.printer_combo.clear()
        names: list[str] = []
        try:
            names = [printer.name for printer in list_real_printers()]
            self.printer_hint.setText(
                f"本机可用真实打印机 {len(names)} 台；PDF24 等虚拟打印机不会出现在这里。"
            )
        except PrinterError as exc:
            self.printer_hint.setText(f"读取打印机列表失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 非 Windows 或系统异常时不让界面崩
            self.printer_hint.setText(f"读取打印机列表失败：{exc}")
        if current and current not in names:
            names.insert(0, current)
        self.printer_combo.addItems(names)
        if current:
            self.printer_combo.setCurrentText(current)
        try:
            system_default = default_printer_name()
        except Exception:  # noqa: BLE001
            system_default = ""
        if system_default:
            self.printer_hint.setText(
                self.printer_hint.text() + f"　Windows 默认打印机：{system_default}"
            )

    def changes(self) -> dict:
        """把界面上的值整理成 update_config 需要的字典。"""
        return {
            "printer": {
                "name": self.printer_combo.currentText().strip(),
                "sumatra_path": self.sumatra_edit.text().strip(),
                "print_settings": self.print_settings_edit.text().strip() or "simplex",
                "timeout_seconds": float(self.timeout_spin.value()),
            },
            "paper": {"sheets_per_batch": int(self.sheets_spin.value())},
            "duplex": {"flip_direction": self.flip_combo.currentData()},
            "backup": {
                "keep": int(self.backup_keep_spin.value()),
                "include_jobs": bool(self.backup_jobs_check.isChecked()),
            },
        }

    # -- 动作 ---------------------------------------------------------------

    def _on_refresh_printers(self) -> None:
        current = self.printer_combo.currentText().strip() or self.config.printer_name
        self._reload_printer_items(current)

    def _on_browse_sumatra(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择 SumatraPDF.exe", "", "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if path:
            self.sumatra_edit.setText(path)

    def _on_set_windows_default(self) -> None:
        name = self.printer_combo.currentText().strip()
        if not name:
            self._warn("请先选择一台打印机。")
            return
        ok = set_default_printer(name)
        if ok:
            self.info_label.setText(f"已把 Windows 默认打印机设为：{name}")
        else:
            self._warn(
                "没能把 Windows 默认打印机改成这台。\n\n"
                "常见原因：需要管理员权限，或者系统策略不允许修改默认打印机。\n"
                "这不会影响 SmartPrint 自己的设置——SmartPrint 只会把页面发给这里选中的打印机。"
            )

    def _on_restore_defaults(self) -> None:
        from app.core.config import DEFAULT_CONFIG

        printer = DEFAULT_CONFIG["printer"]
        self._reload_printer_items(printer["name"])
        self.sumatra_edit.setText(printer["sumatra_path"])
        self.print_settings_edit.setText(printer["print_settings"])
        self.timeout_spin.setValue(float(printer["timeout_seconds"]))
        self.sheets_spin.setValue(int(DEFAULT_CONFIG["paper"]["sheets_per_batch"]))
        index = self.flip_combo.findData(DEFAULT_FLIP_DIRECTION)
        self.flip_combo.setCurrentIndex(index if index >= 0 else 0)
        self.backup_keep_spin.setValue(int(DEFAULT_CONFIG["backup"]["keep"]))
        self.backup_jobs_check.setChecked(bool(DEFAULT_CONFIG["backup"]["include_jobs"]))

    def _on_save(self) -> None:
        name = self.printer_combo.currentText().strip()
        if not name:
            self._warn("打印用打印机不能为空。")
            return
        sumatra = self.sumatra_edit.text().strip()
        if sumatra and not Path(sumatra).exists():
            self._warn(f"SumatraPDF 路径不存在：\n{sumatra}\n\n如果想让程序自动查找，请把这个框留空。")
            return
        try:
            self.saved_config = update_config(self.changes(), self.config_path)
        except OSError as exc:
            self._warn(f"保存配置失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 配置写坏时提示而不是崩
            logger.exception("保存配置失败")
            self._warn(f"保存配置失败：{exc}")
            return
        self.accept()

    def _warn(self, text: str) -> None:
        if self.show_errors:
            QMessageBox.warning(self, "设置", text)
        else:
            self.info_label.setText(text)
