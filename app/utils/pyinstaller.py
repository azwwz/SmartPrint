"""打包相关的路径与命令（阶段八：PyInstaller 目录版）。

只用一份常量描述「打出来的东西长什么样」，打包脚本、自检脚本和使用说明都从这里取，
避免三处各写一份路径。
"""

from __future__ import annotations

from pathlib import Path

from app.utils.paths import PROJECT_ROOT

#: 打包配置文件名（项目根目录下）
SPEC_NAME = "SmartPrint.spec"
#: PyInstaller 的输出目录（项目根目录下）
DIST_DIR_NAME = "dist"
#: 目录版程序的名字（dist/SmartPrint/SmartPrint.exe）
APP_DIR_NAME = "SmartPrint"
EXECUTABLE_NAME = "SmartPrint.exe"
#: 依赖库子目录（PyInstaller 6 起把依赖放进 _internal）
INTERNAL_DIR_NAME = "_internal"


def spec_file() -> Path:
    return PROJECT_ROOT / SPEC_NAME


def dist_dir() -> Path:
    return PROJECT_ROOT / DIST_DIR_NAME


def expected_executable() -> Path:
    """按约定算出的 exe 路径（不保证存在）。"""
    return dist_dir() / APP_DIR_NAME / EXECUTABLE_NAME


def is_packaged_output(exe: Path) -> bool:
    """判断这个 exe 是不是我们约定的目录版产物（同级应有 _internal）。"""
    exe = Path(exe)
    return exe.name == EXECUTABLE_NAME and (exe.parent / INTERNAL_DIR_NAME).is_dir()


def build_command() -> str:
    """打包命令，写进使用说明和报错提示里。"""
    return "python -m PyInstaller --noconfirm --clean SmartPrint.spec"
