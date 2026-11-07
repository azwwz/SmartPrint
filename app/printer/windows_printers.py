"""Windows 打印机枚举与打印队列观察（阶段五）。

阶段五需要先回答两个问题，然后才能把页面交给打印机：

1. **这台电脑上有哪些打印机？哪一台才是真正的 HP LaserJet 1020？**
   readme 明确要求「排除 PDF24 虚拟打印机」，因为 PDF24 是本项目的
   **输入**入口（它会把打印内容变回 PDF），把页面发给它等于原地打转。

2. **选定的打印机现在是什么状态？队列里有什么？**
   这只用于辅助观察（readme 3.1、10.3）：队列空闲不代表纸张已经出来，
   队列里堆着别人的任务也不代表我们的页面已经打到纸上。这些信息只写进
   日志和界面，永远不能代替用户确认。

实现方式：直接调用 Windows 打印子系统（winspool.drv）的 Win32 API，
而不是 `Get-Printer` / `wmic`：

* `Get-Printer` 属于 WMI/CIM，在受限账号、无 WMI 服务的机器上会直接报
  「拒绝访问」，本项目运行环境里就是这样；
* `wmic` 在新版 Windows 上已经被移除；
* `EnumPrinters` / `OpenPrinter` / `EnumJobs` 只需要普通用户权限即可读取。

所有 API 都只在 Windows 上存在，因此在非 Windows 平台导入本模块也安全：
`_load_spooler()` 会返回 None，调用方得到「当前平台不支持打印机枚举」的
明确结果，而不是让程序启动失败。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("smartprint.printer.windows_printers")

# ---------------------------------------------------------------------------
# 常量（来自 Windows SDK 的 winspool.h）
# ---------------------------------------------------------------------------

PRINTER_ENUM_LOCAL = 0x00000002
PRINTER_ENUM_CONNECTIONS = 0x00000004
PRINTER_ENUM_NAME = 0x00000008

PRINTER_ATTRIBUTE_QUEUED = 0x00000001
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400
PRINTER_ATTRIBUTE_LOCAL = 0x00000040
PRINTER_ATTRIBUTE_NETWORK = 0x00000010
PRINTER_ATTRIBUTE_SHARED = 0x00000008

#: PRINTER_INFO_2.Status 的位标志
PRINTER_STATUS_PAUSED = 0x00000001
PRINTER_STATUS_ERROR = 0x00000002
PRINTER_STATUS_PENDING_DELETION = 0x00000004
PRINTER_STATUS_PAPER_JAM = 0x00000008
PRINTER_STATUS_PAPER_OUT = 0x00000010
PRINTER_STATUS_MANUAL_FEED = 0x00000020
PRINTER_STATUS_PAPER_PROBLEM = 0x00000040
PRINTER_STATUS_OFFLINE = 0x00000080
PRINTER_STATUS_IO_ACTIVE = 0x00000100
PRINTER_STATUS_BUSY = 0x00000200
PRINTER_STATUS_PRINTING = 0x00000400
PRINTER_STATUS_OUTPUT_BIN_FULL = 0x00000800
PRINTER_STATUS_NOT_AVAILABLE = 0x00001000
PRINTER_STATUS_WAITING = 0x00002000
PRINTER_STATUS_PROCESSING = 0x00004000
PRINTER_STATUS_INITIALIZING = 0x00008000
PRINTER_STATUS_WARMING_UP = 0x00010000
PRINTER_STATUS_TONER_LOW = 0x00020000
PRINTER_STATUS_NO_TONER = 0x00040000
PRINTER_STATUS_DOOR_OPEN = 0x00400000
PRINTER_STATUS_USER_INTERVENTION = 0x00100000
PRINTER_STATUS_OUT_OF_MEMORY = 0x00200000
PRINTER_STATUS_SERVER_UNKNOWN = 0x00800000
PRINTER_STATUS_POWER_SAVE = 0x01000000

_STATUS_FLAGS: tuple[tuple[int, str], ...] = (
    (PRINTER_STATUS_OFFLINE, "脱机"),
    (PRINTER_STATUS_PAUSED, "已暂停"),
    (PRINTER_STATUS_ERROR, "错误"),
    (PRINTER_STATUS_PAPER_JAM, "卡纸"),
    (PRINTER_STATUS_PAPER_OUT, "缺纸"),
    (PRINTER_STATUS_PAPER_PROBLEM, "纸张问题"),
    (PRINTER_STATUS_DOOR_OPEN, "门未关"),
    (PRINTER_STATUS_NO_TONER, "无墨粉"),
    (PRINTER_STATUS_TONER_LOW, "墨粉不足"),
    (PRINTER_STATUS_OUTPUT_BIN_FULL, "出纸盒满"),
    (PRINTER_STATUS_USER_INTERVENTION, "需要人工干预"),
    (PRINTER_STATUS_NOT_AVAILABLE, "不可用"),
    (PRINTER_STATUS_SERVER_UNKNOWN, "服务器未知"),
    (PRINTER_STATUS_PENDING_DELETION, "正在删除"),
    (PRINTER_STATUS_OUT_OF_MEMORY, "内存不足"),
    (PRINTER_STATUS_PRINTING, "打印中"),
    (PRINTER_STATUS_PROCESSING, "处理中"),
    (PRINTER_STATUS_BUSY, "忙"),
    (PRINTER_STATUS_IO_ACTIVE, "正在传输"),
    (PRINTER_STATUS_WAITING, "等待中"),
    (PRINTER_STATUS_INITIALIZING, "初始化中"),
    (PRINTER_STATUS_WARMING_UP, "预热中"),
    (PRINTER_STATUS_MANUAL_FEED, "等待手动进纸"),
    (PRINTER_STATUS_POWER_SAVE, "省电模式"),
)

#: 一眼就能判断「现在肯定打不出纸」的状态位
BLOCKING_STATUS_FLAGS = frozenset(
    {
        PRINTER_STATUS_OFFLINE,
        PRINTER_STATUS_PAUSED,
        PRINTER_STATUS_ERROR,
        PRINTER_STATUS_PAPER_JAM,
        PRINTER_STATUS_PAPER_OUT,
        PRINTER_STATUS_NO_TONER,
        PRINTER_STATUS_DOOR_OPEN,
        PRINTER_STATUS_NOT_AVAILABLE,
        PRINTER_STATUS_SERVER_UNKNOWN,
        PRINTER_STATUS_USER_INTERVENTION,
        PRINTER_STATUS_PENDING_DELETION,
    }
)

#: JOB_INFO_1.Status 的位标志
JOB_STATUS_PAUSED = 0x00000001
JOB_STATUS_ERROR = 0x00000002
JOB_STATUS_DELETING = 0x00000004
JOB_STATUS_SPOOLING = 0x00000008
JOB_STATUS_PRINTING = 0x00000010
JOB_STATUS_OFFLINE = 0x00000020
JOB_STATUS_PAPER_OUT = 0x00000040
JOB_STATUS_PRINTED = 0x00000080
JOB_STATUS_DELETED = 0x00000100
JOB_STATUS_BLOCKED_DEVQ = 0x00000200
JOB_STATUS_USER_INTERVENTION = 0x00000400
JOB_STATUS_RESTART = 0x00000800
JOB_STATUS_COMPLETE = 0x00001000
JOB_STATUS_RETAINED = 0x00002000

_JOB_FLAGS: tuple[tuple[int, str], ...] = (
    (JOB_STATUS_ERROR, "错误"),
    (JOB_STATUS_OFFLINE, "脱机"),
    (JOB_STATUS_PAPER_OUT, "缺纸"),
    (JOB_STATUS_BLOCKED_DEVQ, "设备队列阻塞"),
    (JOB_STATUS_USER_INTERVENTION, "需要人工干预"),
    (JOB_STATUS_PAUSED, "已暂停"),
    (JOB_STATUS_DELETING, "正在删除"),
    (JOB_STATUS_DELETED, "已删除"),
    (JOB_STATUS_SPOOLING, "正在写入队列"),
    (JOB_STATUS_PRINTING, "正在打印"),
    (JOB_STATUS_PRINTED, "已打印"),
    (JOB_STATUS_COMPLETE, "已完成"),
    (JOB_STATUS_RETAINED, "保留在队列"),
    (JOB_STATUS_RESTART, "重新开始"),
)

ERROR_INSUFFICIENT_BUFFER = 122

#: 名称里出现这些词就认为它是「虚拟打印机」，不参与真实打印
VIRTUAL_PRINTER_KEYWORDS: tuple[str, ...] = (
    "pdf24",
    "pdf24 creator",
    "microsoft print to pdf",
    "microsoft xps document writer",
    "onenote",
    "fax",
    "wondershare",
    "pdfelement",
    "导出为wps",
    "wps pdf",
    "kingsoft",
    "xps",
    "snagit",
    "cuteqpdf",
    "dopdf",
    "bullzip",
    "adobe pdf",
    "nitro",
    "print to file",
)

#: 端口名字里出现这些片段，也视为虚拟打印机
VIRTUAL_PORT_KEYWORDS: tuple[str, ...] = (
    "portprompt:",
    "nul:",
    "shrfax:",
    "\\\\.\\pipe\\",
    "kingsoft",
    "pdfelement",
    "file:",
)


class PrinterError(Exception):
    """打印机操作失败（枚举、打开或读取失败）。"""


@dataclass(frozen=True)
class PrinterInfo:
    """一台已安装的打印机。"""

    name: str
    port_name: str = ""
    driver_name: str = ""
    status: int = 0
    attributes: int = 0
    job_count: int = 0
    is_default: bool = False
    server_name: str = ""
    comment: str = ""
    location: str = ""
    #: 是否判定为虚拟打印机（PDF24 / Microsoft Print to PDF 等）
    is_virtual: bool = False

    @property
    def is_offline(self) -> bool:
        return bool(self.attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE) or bool(
            self.status & PRINTER_STATUS_OFFLINE
        )

    @property
    def is_network(self) -> bool:
        return bool(self.attributes & PRINTER_ATTRIBUTE_NETWORK) or self.name.startswith("\\\\")

    @property
    def status_flags(self) -> tuple[int, ...]:
        return tuple(flag for flag, _label in _STATUS_FLAGS if self.status & flag)

    @property
    def status_text(self) -> str:
        """把 Status 位标志翻译成中文；没有标志时返回「就绪」。"""
        labels = [label for flag, label in _STATUS_FLAGS if self.status & flag]
        if not labels:
            return "就绪" if not self.is_offline else "脱机"
        return "、".join(labels)

    @property
    def blocking_status_flags(self) -> tuple[int, ...]:
        return tuple(flag for flag in self.status_flags if flag in BLOCKING_STATUS_FLAGS)

    @property
    def kind_text(self) -> str:
        if self.is_virtual:
            return "虚拟打印机"
        return "网络打印机" if self.is_network else "本地打印机"

    @property
    def label(self) -> str:
        tags = [self.kind_text]
        if self.is_default:
            tags.append("默认")
        if not self.is_virtual and self.is_offline:
            tags.append("脱机")
        return f"{self.name}（{'，'.join(tags)}）"


@dataclass(frozen=True)
class PrintJobInfo:
    """打印队列中的一个任务（JOB_INFO_1）。"""

    job_id: int
    document: str = ""
    user: str = ""
    machine: str = ""
    printer_name: str = ""
    datatype: str = ""
    status: int = 0
    total_pages: int = 0
    pages_printed: int = 0
    position: int = 0
    submitted: str = ""

    @property
    def status_text(self) -> str:
        labels = [label for flag, label in _JOB_FLAGS if self.status & flag]
        return "、".join(labels) if labels else "排队中"

    @property
    def is_failed(self) -> bool:
        return bool(
            self.status
            & (
                JOB_STATUS_ERROR
                | JOB_STATUS_OFFLINE
                | JOB_STATUS_PAPER_OUT
                | JOB_STATUS_BLOCKED_DEVQ
                | JOB_STATUS_USER_INTERVENTION
            )
        )


@dataclass
class QueueSnapshot:
    """某一个打印机队列在某一刻的样子。"""

    printer_name: str
    jobs: tuple[PrintJobInfo, ...] = ()
    error: str = ""
    observed_at: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def job_count(self) -> int:
        return len(self.jobs)

    @property
    def max_job_id(self) -> int:
        return max((job.job_id for job in self.jobs), default=0)

    @property
    def failed_jobs(self) -> tuple[PrintJobInfo, ...]:
        return tuple(job for job in self.jobs if job.is_failed)

    def find(self, job_id: int) -> PrintJobInfo | None:
        return next((job for job in self.jobs if job.job_id == job_id), None)

    def summary_text(self) -> str:
        if self.error:
            return f"读取队列失败：{self.error}"
        if not self.jobs:
            return "队列为空"
        details = "；".join(
            f"#{job.job_id} {job.document or '(无标题)'} [{job.status_text}]"
            for job in self.jobs[:4]
        )
        more = f"……共 {len(self.jobs)} 个" if len(self.jobs) > 4 else ""
        return f"{len(self.jobs)} 个任务：{details}{more}"


# ---------------------------------------------------------------------------
# ctypes 结构体
# ---------------------------------------------------------------------------


class SYSTEMTIME(ctypes.Structure):
    """ctypes.wintypes 里没有 SYSTEMTIME，只能自己声明。"""

    _fields_ = [
        ("wYear", wt.WORD),
        ("wMonth", wt.WORD),
        ("wDayOfWeek", wt.WORD),
        ("wDay", wt.WORD),
        ("wHour", wt.WORD),
        ("wMinute", wt.WORD),
        ("wSecond", wt.WORD),
        ("wMilliseconds", wt.WORD),
    ]

    def text(self) -> str:
        if not self.wYear:
            return ""
        return (
            f"{self.wYear:04d}-{self.wMonth:02d}-{self.wDay:02d} "
            f"{self.wHour:02d}:{self.wMinute:02d}:{self.wSecond:02d}"
        )


class PRINTER_INFO_2W(ctypes.Structure):
    _fields_ = [
        ("pServerName", wt.LPWSTR),
        ("pPrinterName", wt.LPWSTR),
        ("pShareName", wt.LPWSTR),
        ("pPortName", wt.LPWSTR),
        ("pDriverName", wt.LPWSTR),
        ("pComment", wt.LPWSTR),
        ("pLocation", wt.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wt.LPWSTR),
        ("pPrintProcessor", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
        ("pParameters", wt.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p),
        ("Attributes", wt.DWORD),
        ("Priority", wt.DWORD),
        ("DefaultPriority", wt.DWORD),
        ("StartTime", wt.DWORD),
        ("UntilTime", wt.DWORD),
        ("Status", wt.DWORD),
        ("cJobs", wt.DWORD),
        ("AveragePPM", wt.DWORD),
    ]


class PRINTER_INFO_4W(ctypes.Structure):
    _fields_ = [
        ("pPrinterName", wt.LPWSTR),
        ("pServerName", wt.LPWSTR),
        ("Attributes", wt.DWORD),
    ]


class JOB_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("JobId", wt.DWORD),
        ("pPrinterName", wt.LPWSTR),
        ("pMachineName", wt.LPWSTR),
        ("pUserName", wt.LPWSTR),
        ("pDocument", wt.LPWSTR),
        ("pDatatype", wt.LPWSTR),
        ("pStatus", wt.LPWSTR),
        ("Status", wt.DWORD),
        ("Priority", wt.DWORD),
        ("Position", wt.DWORD),
        ("TotalPages", wt.DWORD),
        ("PagesPrinted", wt.DWORD),
        ("Submitted", SYSTEMTIME),
    ]


_SPOOLER = None
_SPOOLER_LOADED = False


def _load_spooler():
    """加载 winspool.drv 并声明函数签名；失败时返回 None。"""
    global _SPOOLER, _SPOOLER_LOADED
    if _SPOOLER_LOADED:
        return _SPOOLER
    _SPOOLER_LOADED = True

    if not sys.platform.startswith("win"):
        logger.info("当前平台不是 Windows，打印机枚举不可用")
        return None

    try:
        spooler = ctypes.WinDLL("winspool.drv", use_last_error=True)
    except OSError as exc:  # pragma: no cover - 只在系统异常时发生
        logger.warning("加载 winspool.drv 失败：%s", exc)
        return None

    spooler.EnumPrintersW.argtypes = [
        wt.DWORD,
        wt.LPWSTR,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD),
    ]
    spooler.EnumPrintersW.restype = wt.BOOL

    spooler.GetDefaultPrinterW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    spooler.GetDefaultPrinterW.restype = wt.BOOL

    # 阶段八：设置界面里的「设为 Windows 默认打印机」需要它
    spooler.SetDefaultPrinterW.argtypes = [wt.LPWSTR]
    spooler.SetDefaultPrinterW.restype = wt.BOOL

    spooler.OpenPrinterW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.HANDLE), ctypes.c_void_p]
    spooler.OpenPrinterW.restype = wt.BOOL

    spooler.ClosePrinter.argtypes = [wt.HANDLE]
    spooler.ClosePrinter.restype = wt.BOOL

    spooler.EnumJobsW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD),
    ]
    spooler.EnumJobsW.restype = wt.BOOL

    _SPOOLER = spooler
    return spooler


def is_supported() -> bool:
    """当前平台是否支持打印机枚举。"""
    return _load_spooler() is not None


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


def _read_enum(call) -> tuple[ctypes.Array, int] | None:
    """先问需要多大缓冲，再一次性取出数据（EnumPrinters / EnumJobs 的固定套路）。

    这两个 API 的调用方式都是：第一次传 NULL 得到 ``needed``（并以
    ERROR_INSUFFICIENT_BUFFER 失败），再按 ``needed`` 分配缓冲调用第二次。
    """
    needed = wt.DWORD(0)
    returned = wt.DWORD(0)
    call(None, 0, ctypes.byref(needed), ctypes.byref(returned))
    if needed.value == 0:
        return None
    buffer = ctypes.create_string_buffer(needed.value)
    if not call(buffer, needed.value, ctypes.byref(needed), ctypes.byref(returned)):
        raise PrinterError(f"读取打印信息失败（错误码 {ctypes.get_last_error()}）")
    return buffer, int(returned.value)


def _cast_array(buffer: ctypes.Array, struct, count: int):
    """把缓冲解释成 ``struct[count]``。"""
    return ctypes.cast(buffer, ctypes.POINTER(struct * max(1, count))).contents


def _enum_printers(flags: int, level: int) -> list:
    spooler = _load_spooler()
    if spooler is None:
        return []

    def call(buffer, size, needed_ptr, returned_ptr):
        return spooler.EnumPrintersW(
            flags, None, level, buffer, size, needed_ptr, returned_ptr
        )

    result = _read_enum(call)
    if result is None:
        return []
    buffer, count = result
    struct = PRINTER_INFO_2W if level == 2 else PRINTER_INFO_4W
    array = _cast_array(buffer, struct, count)
    return [array[index] for index in range(count)]


def default_printer_name() -> str:
    """系统默认打印机名称；读取失败时返回空字符串。"""
    spooler = _load_spooler()
    if spooler is None:
        return ""
    needed = wt.DWORD(0)
    spooler.GetDefaultPrinterW(None, ctypes.byref(needed))
    if not needed.value:
        return ""
    buffer = ctypes.create_unicode_buffer(needed.value)
    if not spooler.GetDefaultPrinterW(buffer, ctypes.byref(needed)):
        return ""
    return buffer.value


def set_default_printer(name: str) -> bool:
    """把 Windows 默认打印机设为 `name`（阶段八设置界面用）。

    只改系统的默认打印机，不影响 SmartPrint 自己配置的打印机。
    名称不存在或权限不足时返回 False，不抛异常。
    """
    text = (name or "").strip()
    if not text:
        return False
    spooler = _load_spooler()
    if spooler is None:
        return False
    try:
        ok = bool(spooler.SetDefaultPrinterW(text))
    except (AttributeError, OSError) as exc:  # pragma: no cover - 系统异常时才发生
        logger.warning("设置默认打印机失败：%s", exc)
        return False
    if ok:
        logger.info("已把 Windows 默认打印机设为：%s", text)
    else:
        logger.warning("设置 Windows 默认打印机失败：%s（%s）", text, ctypes.get_last_error())
    return ok


def looks_virtual(name: str, port_name: str = "", driver_name: str = "") -> bool:
    """按名称、端口和驱动判断是不是虚拟打印机。

    PDF24 是 SmartPrint 的**输入**设备，绝不能把页面再发回给它；
    Microsoft Print to PDF、OneNote、Fax 等同理，它们不会真的出纸。
    """
    haystack = " ".join(part or "" for part in (name, driver_name)).lower()
    port = (port_name or "").lower()
    if any(keyword in haystack for keyword in VIRTUAL_PRINTER_KEYWORDS):
        return True
    return any(keyword in port for keyword in VIRTUAL_PORT_KEYWORDS)


def list_printers(include_virtual: bool = True) -> list[PrinterInfo]:
    """列出本机（含连接型）打印机。

    Args:
        include_virtual: 是否包含 PDF24 等虚拟打印机。

    Raises:
        PrinterError: Win32 调用失败。
    """
    if _load_spooler() is None:
        raise PrinterError("当前系统不支持打印机枚举（仅 Windows 可用）")

    default_name = default_printer_name()
    printers: list[PrinterInfo] = []
    seen: set[str] = set()

    for flags in (PRINTER_ENUM_LOCAL, PRINTER_ENUM_CONNECTIONS):
        try:
            infos = _enum_printers(flags, 2)
        except PrinterError as exc:
            logger.warning("枚举打印机失败（flags=%s）：%s", flags, exc)
            continue
        for info in infos:
            name = info.pPrinterName or ""
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            port = info.pPortName or ""
            driver = info.pDriverName or ""
            item = PrinterInfo(
                name=name,
                port_name=port,
                driver_name=driver,
                status=int(info.Status or 0),
                attributes=int(info.Attributes or 0),
                job_count=int(info.cJobs or 0),
                is_default=name.lower() == default_name.lower(),
                server_name=info.pServerName or "",
                comment=info.pComment or "",
                location=info.pLocation or "",
                is_virtual=looks_virtual(name, port, driver),
            )
            printers.append(item)

    printers.sort(key=lambda item: (item.is_virtual, item.name.lower()))
    if include_virtual:
        return printers
    return [item for item in printers if not item.is_virtual]


def list_real_printers() -> list[PrinterInfo]:
    """只列出能真的出纸的打印机（排除 PDF24 等虚拟打印机）。"""
    return [item for item in list_printers() if not item.is_virtual]


def list_virtual_printers() -> list[PrinterInfo]:
    return [item for item in list_printers() if item.is_virtual]


# ---------------------------------------------------------------------------
# 名称解析
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def resolve_printer_name(
    wanted: str,
    printers: Iterable[PrinterInfo] | None = None,
    *,
    allow_virtual: bool = False,
) -> PrinterInfo | None:
    """把配置里的打印机名称解析成真实存在的一台打印机。

    为什么需要这一步：Windows 里同名驱动装的打印机常常带后缀，
    例如本项目目标打印机在系统里的完整名称是
    ``HP LaserJet 1020 1016811``，而配置里写的是 ``HP LaserJet 1020``。
    直接把这个名字交给 SumatraPDF 会得到「打印机不存在」。

    匹配顺序（先精确、后宽松）：

    1. 名称完全相同（不区分大小写）；
    2. 名称去空格后相同；
    3. 打印机名以配置名开头（``HP LaserJet 1020 1016811``）；
    4. 配置名包含打印机名（配置写得比实际更详细时）；
    5. 驱动名与配置名相同。

    虚拟打印机默认不参与匹配（readme 要求排除 PDF24）。
    """
    candidates = list(printers) if printers is not None else list_printers()
    if not allow_virtual:
        candidates = [item for item in candidates if not item.is_virtual]

    target = _normalize(wanted)
    if not target:
        return None

    for item in candidates:
        if _normalize(item.name) == target:
            return item
    for item in candidates:
        if _normalize(item.name).replace(" ", "") == target.replace(" ", ""):
            return item
    for item in candidates:
        if _normalize(item.name).startswith(target):
            return item
    for item in candidates:
        if target.startswith(_normalize(item.name)):
            return item
    for item in candidates:
        if _normalize(item.driver_name) == target:
            return item
    return None


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


def enum_jobs(printer_name: str) -> QueueSnapshot:
    """读取某个打印机当前的打印队列（只读，不会修改任何任务）。"""
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    spooler = _load_spooler()
    if spooler is None:
        return QueueSnapshot(
            printer_name=printer_name, error="当前系统不支持读取打印队列", observed_at=now
        )

    handle = wt.HANDLE()
    if not spooler.OpenPrinterW(printer_name, ctypes.byref(handle), None):
        code = ctypes.get_last_error()
        return QueueSnapshot(
            printer_name=printer_name,
            error=f"无法打开打印机（错误码 {code}）",
            observed_at=now,
        )

    try:
        def call(buffer, size, needed_ptr, returned_ptr):
            return spooler.EnumJobsW(
                handle, 0, 0xFFFF, 1, buffer, size, needed_ptr, returned_ptr
            )

        try:
            result = _read_enum(call)
        except PrinterError as exc:
            return QueueSnapshot(printer_name=printer_name, error=str(exc), observed_at=now)

        if result is None:
            return QueueSnapshot(printer_name=printer_name, jobs=(), observed_at=now)

        buffer, count = result
        array = _cast_array(buffer, JOB_INFO_1W, count)
        jobs = tuple(
            PrintJobInfo(
                job_id=int(info.JobId),
                document=info.pDocument or "",
                user=info.pUserName or "",
                machine=info.pMachineName or "",
                printer_name=info.pPrinterName or printer_name,
                datatype=info.pDatatype or "",
                status=int(info.Status or 0),
                total_pages=int(info.TotalPages or 0),
                pages_printed=int(info.PagesPrinted or 0),
                position=int(info.Position or 0),
                submitted=info.Submitted.text(),
            )
            for info in (array[index] for index in range(count))
        )
        return QueueSnapshot(printer_name=printer_name, jobs=jobs, observed_at=now)
    finally:
        spooler.ClosePrinter(handle)


def printer_status_text(printer: PrinterInfo | None) -> str:
    if printer is None:
        return "未知"
    text = printer.status_text
    if printer.is_offline:
        text = f"{text}（脱机）" if "脱机" not in text else text
    return text


@dataclass
class PrintEnvironment:
    """一次环境检查的结果，界面顶部会显示。"""

    supported: bool = True
    printers: list[PrinterInfo] = field(default_factory=list)
    target: PrinterInfo | None = None
    default_name: str = ""
    message: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.target is not None and not self.error)

    @property
    def real_printers(self) -> list[PrinterInfo]:
        return [item for item in self.printers if not item.is_virtual]

    @property
    def virtual_printers(self) -> list[PrinterInfo]:
        return [item for item in self.printers if item.is_virtual]

    def target_text(self) -> str:
        if self.error:
            return f"不可用：{self.error}"
        if self.target is None:
            return f"未找到：{self.message or '（未配置）'}"
        return f"{self.target.name}（{self.target.kind_text}，{self.target.status_text}）"


def inspect_environment(wanted_name: str) -> PrintEnvironment:
    """检查打印机环境：列出打印机、解析目标、给出结论。"""
    if _load_spooler() is None:
        return PrintEnvironment(
            supported=False,
            error="当前系统不支持打印机枚举（仅 Windows 可用）",
        )
    try:
        printers = list_printers()
    except PrinterError as exc:
        return PrintEnvironment(error=str(exc))

    target = resolve_printer_name(wanted_name, printers)
    message = ""
    if target is None:
        real = [item.name for item in printers if not item.is_virtual]
        message = (
            f"系统中没有匹配「{wanted_name}」的真实打印机；"
            f"可选：{'、'.join(real) if real else '（无）'}"
        )
    return PrintEnvironment(
        printers=printers,
        target=target,
        default_name=default_printer_name(),
        message=message,
    )
