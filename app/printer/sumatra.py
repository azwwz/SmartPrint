"""SumatraPDF 调用（阶段五）。

readme 4.4 决定了 SmartPrint 不自己写打印驱动：PyMuPDF 准备单页 PDF，
SumatraPDF 负责把这一页交给 Windows 打印系统。

本模块只做三件事：

1. 找到 SumatraPDF（配置路径 → ``drivers/`` → 常见安装目录 → 注册表 → PATH）；
2. 拼出可靠的命令行（``-print-to "<打印机>" -print-settings "1" -silent <PDF>``）；
3. 启动进程、等待退出、把退出码和输出翻译成可读结论。

几个来自实际测试的要点（非常容易踩坑）：

* **必须带 ``-silent``**。不带的话 SumatraPDF 出错时会弹一个模态对话框，
  进程永远不退出，调用方就卡在那里。带上 ``-silent`` 后它会安静地失败并
  给出退出码。
* **不要用 ``-help`` / ``-version`` / ``-list-printers`` 探测版本**。
  这几个参数在 3.6.1 上会打开 GUI 窗口并一直运行，无法用于「探测」。
  版本号改为从文件版本信息读取（见 :func:`read_version`）。
* **退出码与官方文档不完全一致**。官方 3.7 文档写的是 0/2/3/4/5/6，
  但实测 3.6.1 在「打印机不存在」「文件不存在」时都返回 1。因此这里把
  ``0`` 当作成功，其他任何值一律当作失败，并原样保留退出码与输出，
  不假装能分辨具体原因。
* **退出码 0 只代表「已经把作业交给 Windows 打印系统」**，不代表纸张
  已经出来。离线、缺纸、卡纸都发生在 spooler 之后，SumatraPDF 无法得知。
  这正是 readme 3.1 / 10.3 反复强调的边界。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.utils.paths import DRIVERS_DIR, PROJECT_ROOT

logger = logging.getLogger("smartprint.printer.sumatra")

#: 可执行文件名
SUMATRA_EXE_NAME = "SumatraPDF.exe"

#: 常见安装位置（相对 Program Files 的固定子目录）
COMMON_INSTALL_DIRS: tuple[str, ...] = (
    r"C:\Program Files\SumatraPDF",
    r"C:\Program Files (x86)\SumatraPDF",
    r"D:\Program\SumatraPDF",
    r"D:\Program Files\SumatraPDF",
)

#: 注册表里的安装信息位置（HKCU / HKLM 各一份）
_UNINSTALL_KEYS: tuple[tuple[int, str], ...] = (
    (0x80000001, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SumatraPDF"),  # HKCU
    (0x80000002, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SumatraPDF"),  # HKLM
)

#: 退出码含义（官方文档 3.7 的定义，实测 3.6.1 并不完全遵守，仅作参考）
EXIT_CODE_HINTS: dict[int, str] = {
    0: "已提交到 Windows 打印系统",
    2: "无法打开文件（不存在或格式不支持）",
    3: "文档不允许打印",
    4: "打印机不存在或不可用",
    5: "打印机驱动或设备出错",
    6: "打印被策略禁用",
}

#: 等待 SumatraPDF 退出的默认超时（秒）。打印大页面时 spooler 可能较慢，
#: 但 SumatraPDF 自己只是「提交并退出」，正常都在几秒内完成。
DEFAULT_TIMEOUT_SECONDS = 60.0


class SumatraError(Exception):
    """找不到或无法启动 SumatraPDF。"""


@dataclass
class SumatraResult:
    """一次 SumatraPDF 调用的结果。"""

    command: list[str]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    launched: bool = False
    error: str = ""
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def submitted(self) -> bool:
        """命令是否成功提交（退出码 0 且没有超时）。"""
        return self.launched and not self.timed_out and self.returncode == 0

    @property
    def exit_hint(self) -> str:
        if self.returncode is None:
            return ""
        return EXIT_CODE_HINTS.get(self.returncode, "未知退出码")

    @property
    def summary_text(self) -> str:
        if not self.launched:
            return f"未启动：{self.error or '未知原因'}"
        if self.timed_out:
            return f"等待超时（{self.duration_seconds:.1f} 秒），SumatraPDF 仍未退出"
        if self.returncode == 0:
            return f"命令已提交（退出码 0，用时 {self.duration_seconds:.1f} 秒）"
        return (
            f"命令失败（退出码 {self.returncode}：{self.exit_hint}，"
            f"用时 {self.duration_seconds:.1f} 秒）"
        )

    @property
    def command_text(self) -> str:
        """便于写进日志和数据库的命令行文本。"""
        return format_command(self.command)

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
            "launched": self.launched,
            "submitted": self.submitted,
            "error": self.error,
            "started_at": self.started_at.isoformat(timespec="seconds"),
        }


def format_command(command: list[str]) -> str:
    """把参数列表还原成可读的命令行（带引号，便于复制复现）。"""
    parts: list[str] = []
    for item in command:
        text = str(item)
        if not text or any(ch in text for ch in ' "\t'):
            parts.append(f'"{text}"')
        else:
            parts.append(text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------


def _registry_candidates() -> list[Path]:
    """从注册表读取 SumatraPDF 安装目录（读不到就返回空列表）。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []

    found: list[Path] = []
    for hive, subkey in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hive, subkey) as key:  # type: ignore[arg-type]
                for value_name in ("InstallLocation", "DisplayIcon"):
                    try:
                        raw, _type = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    text = str(raw or "").strip().strip('"')
                    if not text:
                        continue
                    path = Path(text.split(",")[0].strip().strip('"'))
                    if path.suffix.lower() == ".exe":
                        found.append(path)
                    else:
                        found.append(path / SUMATRA_EXE_NAME)
        except OSError:
            continue
    return found


def candidate_paths(configured: str | Path | None = None) -> list[Path]:
    """按优先级列出所有可能的 SumatraPDF 路径（去重，保持顺序）。"""
    candidates: list[Path] = []

    def add(item: str | Path | None) -> None:
        if not item:
            return
        path = Path(item).expanduser()
        if path not in candidates:
            candidates.append(path)

    if configured:
        add(configured)

    # 项目自带的 drivers/SumatraPDF/ 与 drivers/
    add(DRIVERS_DIR / "SumatraPDF" / SUMATRA_EXE_NAME)
    add(DRIVERS_DIR / "SumatraPDF" / "SumatraPDF.exe")
    add(DRIVERS_DIR / SUMATRA_EXE_NAME)

    for directory in COMMON_INSTALL_DIRS:
        add(Path(directory) / SUMATRA_EXE_NAME)

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            add(Path(base) / "SumatraPDF" / SUMATRA_EXE_NAME)

    for path in _registry_candidates():
        add(path)

    on_path = shutil.which(SUMATRA_EXE_NAME)
    if on_path:
        add(on_path)

    return candidates


def find_sumatra(configured: str | Path | None = None) -> Path | None:
    """返回第一个真实存在的 SumatraPDF 路径；找不到返回 None。"""
    for path in candidate_paths(configured):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def read_version(exe: str | Path | None) -> str:
    """读取 SumatraPDF 文件版本。

    不使用 ``SumatraPDF.exe -version``：3.6.1 上带参数会打开 GUI 并一直运行。
    这里改为读 Windows 文件版本信息，不启动进程也就不会卡住。
    """
    if not exe:
        return ""
    path = Path(exe)
    if not path.is_file():
        return ""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL

        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return ""

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        major = info.dwFileVersionMS >> 16
        minor = info.dwFileVersionMS & 0xFFFF
        build = info.dwFileVersionLS >> 16
        revision = info.dwFileVersionLS & 0xFFFF
        return f"{major}.{minor}.{build}.{revision}"
    except Exception:  # noqa: BLE001 - 版本读不到不影响打印
        logger.debug("读取 SumatraPDF 版本失败", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_print_args(
    pdf_path: str | Path,
    printer_name: str,
    page_numbers: str | int | None = None,
    *,
    extra_settings: str = "",
    silent: bool = True,
) -> list[str]:
    """拼出打印一个（单页）PDF 的参数列表。

    Args:
        pdf_path: 要打印的 PDF。阶段五总是单页文件，所以页码默认是 ``1``。
        printer_name: Windows 打印机名称（必须是系统里的完整名称）。
        page_numbers: ``-print-settings`` 的页码部分，默认 ``"1"``。
        extra_settings: 追加的打印设置，例如 ``simplex`` 或 ``paper=A4``。
        silent: 是否带 ``-silent``（强烈建议保持 True，否则出错会弹窗卡住）。
    """
    settings: list[str] = []
    sheet_range = "1" if page_numbers is None else str(page_numbers).strip()
    if sheet_range:
        settings.append(sheet_range)
    if extra_settings:
        settings.extend(
            part.strip() for part in extra_settings.split(",") if part.strip()
        )

    args: list[str] = ["-print-to", str(printer_name)]
    if settings:
        args += ["-print-settings", ",".join(settings)]
    if silent:
        args.append("-silent")
    args.append(str(pdf_path))
    return args


def build_command(
    exe: str | Path,
    pdf_path: str | Path,
    printer_name: str,
    page_numbers: str | int | None = None,
    *,
    extra_settings: str = "",
    silent: bool = True,
) -> list[str]:
    """完整命令行：``[SumatraPDF.exe, -print-to, ...]``。"""
    return [str(exe)] + build_print_args(
        pdf_path,
        printer_name,
        page_numbers,
        extra_settings=extra_settings,
        silent=silent,
    )


def parse_command(command: list[str]) -> dict[str, str]:
    """从命令行里取回关键参数（写日志与验收脚本时用）。"""
    info = {"printer": "", "settings": "", "file": "", "silent": "否"}
    index = 0
    while index < len(command):
        item = command[index]
        if item in ("-print-to", "/t") and index + 1 < len(command):
            info["printer"] = command[index + 1]
            index += 2
            continue
        if item == "-print-settings" and index + 1 < len(command):
            info["settings"] = command[index + 1]
            index += 2
            continue
        if item == "-silent":
            info["silent"] = "是"
        index += 1
    rest = [
        item
        for item in command[1:]
        if not item.startswith("-")
        and item not in (info["printer"], info["settings"])
    ]
    info["file"] = rest[-1] if rest else ""
    return info


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def run(
    command: list[str],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    cwd: Path | None = None,
) -> SumatraResult:
    """启动 SumatraPDF 并等待它退出。

    无论成功失败都返回 :class:`SumatraResult`，不抛异常——打印失败是
    正常业务分支（打印机离线、找不到打印机），由服务层决定怎么记录。
    """
    result = SumatraResult(command=list(command))
    started = time.monotonic()

    try:
        completed = subprocess.run(  # noqa: S603 - 参数列表形式，不经过 shell
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else str(PROJECT_ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        result.duration_seconds = time.monotonic() - started
        result.error = f"找不到打印程序：{command[0]}"
        logger.error(result.error)
        return result
    except subprocess.TimeoutExpired as exc:
        result.duration_seconds = time.monotonic() - started
        result.timed_out = True
        result.launched = True
        result.stdout = _decode(exc.stdout)
        result.stderr = _decode(exc.stderr)
        result.error = "SumatraPDF 在超时前没有退出"
        logger.warning(
            "打印命令超时（%.1f 秒）：%s", result.duration_seconds, result.command_text
        )
        return result
    except OSError as exc:
        result.duration_seconds = time.monotonic() - started
        result.error = f"启动打印程序失败：{exc}"
        logger.error(result.error)
        return result

    result.duration_seconds = time.monotonic() - started
    result.launched = True
    result.returncode = completed.returncode
    result.stdout = completed.stdout or ""
    result.stderr = completed.stderr or ""

    if result.returncode == 0:
        logger.info(
            "打印命令已提交（用时 %.1f 秒）：%s", result.duration_seconds, result.command_text
        )
    else:
        logger.warning(
            "打印命令失败（退出码 %s，用时 %.1f 秒）：%s%s",
            result.returncode,
            result.duration_seconds,
            result.command_text,
            f"\n输出：{_short(result.stdout)}" if result.stdout.strip() else "",
        )
    return result


def _decode(value: bytes | str | None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def print_pdf(
    exe: str | Path,
    pdf_path: str | Path,
    printer_name: str,
    page_numbers: str | int | None = None,
    *,
    extra_settings: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> SumatraResult:
    """定位好的 exe + 单页 PDF + 目标打印机 → 一次真实打印调用。"""
    command = build_command(
        exe,
        pdf_path,
        printer_name,
        page_numbers,
        extra_settings=extra_settings,
    )
    return run(command, timeout=timeout)
