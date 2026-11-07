# -*- mode: python ; coding: utf-8 -*-
"""SmartPrint 的 PyInstaller 打包配置（阶段八）。

构建命令（项目根目录下）：

    python -m PyInstaller --noconfirm --clean SmartPrint.spec

产物是目录版：dist/SmartPrint/SmartPrint.exe。整个 dist/SmartPrint 目录可以直接拷到
任何一台 Windows 机器上双击运行，不需要安装 Python，也不需要 PySide6 / PyMuPDF。

配置、日志、数据库都落在 exe 所在目录下的 config/、logs/、Data/ 里，属于绿色软件，
删除目录即可完全卸载。

图标与版本资源：

- `resources/smartprint.ico` 由 `tools/make_icon.py` 生成，既是 exe 在资源管理器里的图标，
  也是窗口和任务栏图标（程序运行时从 `RESOURCES_DIR` 读取，所以要通过 `datas` 一起打包）；
- `resources/version_info.txt` 写进 exe 的版本资源，「属性 → 详细信息」里才不是空白。
两者缺失时本条配置会自动跳过，保证没有资源的机器也能打包。

关于 conda 环境的坑：conda 把 sqlite3.dll、ffi-8.dll 这类第三方运行库放在
`<env>/Library/bin` 下，PyInstaller 默认不会跟过去分析，结果 exe 一启动就报
`ImportError: DLL load failed`。下面的 `_conda_runtime_dlls()` 会把 Python 扩展模块
（`DLLs/*.pyd`）真正依赖的、并且能在 `Library/bin` 里找到的 DLL 都收进来。
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)
PREFIX = Path(sys.prefix)

#: Windows 自带、不能也不需要打包的 DLL
_SYSTEM_DLL_PREFIXES = (
    "api-ms-win-",
    "ext-ms-win-",
    "kernel32",
    "kernelbase",
    "user32",
    "advapi32",
    "gdi32",
    "gdiplus",
    "shell32",
    "shcore",
    "ole32",
    "oleaut32",
    "combase",
    "comdlg32",
    "comctl32",
    "shlwapi",
    "version",
    "winspool",
    "ws2_32",
    "crypt32",
    "bcrypt",
    "ncrypt",
    "secur32",
    "iphlpapi",
    "netapi32",
    "rpcrt4",
    "setupapi",
    "cfgmgr32",
    "imm32",
    "msvcrt",
    "msvcp",
    "vcruntime",
    "ucrtbase",
    "python3",
    "ntdll",
    "powrprof",
    "userenv",
    "dwmapi",
    "uxtheme",
    "d3d",
    "dxgi",
    "opengl",
    "wtsapi32",
    "mpr",
    "winmm",
    "dbghelp",
    "psapi",
    "authz",
    "normaliz",
    "dnsapi",
    "wldap32",
    "wevtapi",
    "sspicli",
    "profapi",
    "cryptbase",
)


def _system_like(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _SYSTEM_DLL_PREFIXES)


def _imports(path: Path) -> list[str]:
    """读一个 PE 文件的导入表（pefile 是 PyInstaller 的依赖，打包时一定在）。"""
    result: list[str] = []
    try:
        import pefile  # noqa: PLC0415 - 只有打包时才用得到

        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
            result.append(Path(entry.dll.decode("ascii", "ignore")).name)
    except Exception:  # noqa: BLE001 - 读不出来就当没有依赖，不影响主流程
        pass
    finally:
        try:
            pe.close()  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass
    return result


def _conda_runtime_dlls() -> list[tuple[str, str]]:
    """找出 Python 扩展模块依赖、但位于 conda `Library/bin` 里的运行库。"""
    search_dirs = [PREFIX / "Library" / "bin", PREFIX / "Library" / "mingw-w64" / "bin"]
    available: dict[str, Path] = {}
    for directory in search_dirs:
        if directory.is_dir():
            for dll in directory.glob("*.dll"):
                available[dll.name.lower()] = dll
    if not available:
        return []

    pending = [p for p in (PREFIX / "DLLs").glob("*.pyd")] + [p for p in (PREFIX / "DLLs").glob("*.dll")]
    found: dict[str, Path] = {}
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen or not current.exists():
            continue
        seen.add(current)
        for name in _imports(current):
            key = name.lower()
            if key in found or _system_like(name):
                continue
            target = available.get(key)
            if target is not None:
                found[key] = target
                pending.append(target)
    return sorted((str(path), ".") for path in found.values())


binaries = _conda_runtime_dlls()

#: 程序图标与版本资源（缺失时留空，不阻断打包）
ICON_PATH = ROOT / "resources" / "smartprint.ico"
VERSION_FILE = ROOT / "resources" / "version_info.txt"
ICON_ARG = str(ICON_PATH) if ICON_PATH.is_file() else None
VERSION_ARG = str(VERSION_FILE) if VERSION_FILE.is_file() else None

#: 只读资源：图标等随程序一起打包，运行时通过 app.utils.paths.RESOURCES_DIR 访问
datas = []
if (ROOT / "resources").is_dir():
    datas.append((str(ROOT / "resources"), "resources"))

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 开发机上装了这些包，但 SmartPrint 用不到，排除掉能显著减小体积
    excludes=[
        "tkinter",
        "matplotlib",
        "pandas",
        "numpy",
        "scipy",
        "IPython",
        "pytest",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmartPrint",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口程序：双击不弹黑框
    icon=ICON_ARG,  # exe 在资源管理器里的图标
    version=VERSION_ARG,  # exe 的「属性 → 详细信息」
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SmartPrint",
)
