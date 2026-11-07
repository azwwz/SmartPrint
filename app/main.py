"""SmartPrint 程序入口。

运行方式（项目根目录 D:\\dev\\SmartPrint 下）：

    python -m app.main

或直接使用配套的 run.ps1 / run.bat；打包后的 exe 双击即可。

阶段八增加了两个命令行开关，方便在**没有开发环境**的机器上确认程序能起来：

    SmartPrint.exe --version              只打印版本号
    SmartPrint.exe --self-test [报告路径]  启动自检：建库、开窗口、读一次任务列表，
                                          把结果写进报告文件，然后用退出码报告成败
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

# 允许直接执行 python app\main.py 时也能 import app.* 下的模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import ensure_config_file  # noqa: E402
from app.core.version import APP_NAME, APP_VERSION  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.utils.logging_setup import log_file_path, setup_logging  # noqa: E402
from app.utils.paths import CONFIG_FILE, RESOURCES_DIR, ensure_dirs, is_frozen  # noqa: E402

logger = logging.getLogger("smartprint.main")

#: 自检报告默认文件名（放在程序目录下）
SELF_TEST_REPORT = "SmartPrint-self-test.txt"

#: 程序图标文件名（由 tools/make_icon.py 生成，打包后位于 RESOURCES_DIR）
ICON_FILE = "smartprint.ico"


def _load_icon() -> QIcon | None:
    """读取程序图标；没有图标文件时返回 None，界面照常运行。"""
    icon_path = RESOURCES_DIR / ICON_FILE
    if not icon_path.is_file():
        return None
    icon = QIcon(str(icon_path))
    if icon.isNull():
        logger.warning("图标文件无法读取，已忽略：%s", icon_path)
        return None
    return icon


def _create_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    icon = _load_icon()
    if icon is not None:
        # 窗口图标与任务栏图标都用同一张图；放在应用级别，所有窗口自动继承
        app.setWindowIcon(icon)
    return app


def _self_test(report_path: Path) -> int:
    """启动自检：确认数据库、任务列表、打印服务都能初始化。

    打包后的窗口程序没有控制台，所以结果写到报告文件里，再由退出码说明成败。
    """
    lines: list[str] = []
    ok = False
    window = None
    try:
        ensure_dirs()
        setup_logging()
        app = _create_app()
        created = ensure_config_file()
        lines.append(f"{APP_NAME} {APP_VERSION} 自检")
        lines.append(f"打包运行：{'是' if is_frozen() else '否'}")
        lines.append(f"Python：{sys.version.split()[0]}")
        lines.append(f"程序目录：{Path(sys.executable if is_frozen() else __file__).resolve().parent}")
        lines.append(f"配置文件：{CONFIG_FILE}{'（本次新建）' if created else ''}")
        lines.append(f"程序图标：{'已加载' if _load_icon() is not None else f'未找到（{RESOURCES_DIR / ICON_FILE}）'}")
        lines.append(f"日志文件：{log_file_path()}")
        lines.append(f"进程参数：{' '.join(sys.argv)}")

        window = MainWindow()
        window.show()
        app.processEvents()

        tasks = window.repository.list_tasks(limit=5)
        counts = window.repository.count_by_status()
        lines.append(f"数据库：{window.config.database_file}")
        lines.append(f"任务总数：{sum(counts.values())}，本次读到 {len(tasks)} 条")
        lines.append(f"打印用打印机：{window.config.printer_name}")
        lines.append(f"每批纸张数：{window.config.sheets_per_batch}")
        lines.append(f"翻面方向：{window.config.flip_direction_text}")
        lines.append(f"任务列表列数：{window.table.columnCount()}")
        lines.append(f"主窗口尺寸：{window.width()}×{window.height()}")
        ok = True
        lines.append("自检结果：成功")
    except Exception as exc:  # noqa: BLE001 - 自检要把失败原因写进报告
        logger.exception("自检失败")
        lines.append(f"自检结果：失败（{type(exc).__name__}：{exc}）")
        lines.append(traceback.format_exc())
    finally:
        if window is not None:
            try:
                window._poll_timer.stop()
                window.intake.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            logger.exception("写自检报告失败：%s", report_path)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--version" in args:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    if "--self-test" in args:
        index = args.index("--self-test")
        report = Path(args[index + 1]) if len(args) > index + 1 else Path.cwd() / SELF_TEST_REPORT
        return _self_test(report)

    ensure_dirs()
    setup_logging()
    ensure_config_file()
    logger.info("=" * 60)
    logger.info("%s %s 启动，Python %s", APP_NAME, APP_VERSION, sys.version.split()[0])

    app = _create_app()

    window = MainWindow()
    window.show()

    exit_code = app.exec()
    logger.info("%s 退出，退出码 %s", APP_NAME, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
