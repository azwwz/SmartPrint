"""打印提交记录（阶段五）。

一次「打印某一页」的完整证据链：

    哪个任务 → 哪一页 → 哪个单页文件 → 哪台打印机 → 什么命令
    → 进程退出码 → 提交前后的队列快照

为什么要落盘：readme 10.2 要求关键操作必须有记录。阶段五虽然还没有
「用户确认完整出纸」，但「命令发给了哪台打印机、什么时候发的、退出码是
多少、当时队列里有什么」必须能事后查清——否则将来出现重复打印或漏页，
根本说不清是哪一步出的问题。

特别强调（readme 3.3 / 10.3）：本记录里的 ``submitted`` 只表示
「打印命令已发送」，**不代表纸张已经完整出纸**。阶段五不提供任何
「自动判定打印成功」的字段，纸张是否完整由用户在阶段六确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: 提交状态（只描述「命令发送」这一步，不描述纸张）
SUBMIT_PENDING = "pending"          # 已生成命令，尚未执行
SUBMIT_SUBMITTED = "submitted"      # SumatraPDF 返回 0：命令已交给 Windows
SUBMIT_FAILED = "failed"            # SumatraPDF 返回非 0
SUBMIT_TIMEOUT = "timeout"          # 等待进程退出超时
SUBMIT_NOT_LAUNCHED = "not_launched"  # 进程根本没起来（找不到 exe / 权限）
SUBMIT_BLOCKED = "blocked"          # 预检未通过，没有真的发送

SUBMIT_LABELS: dict[str, str] = {
    SUBMIT_PENDING: "等待提交",
    SUBMIT_SUBMITTED: "命令已发送",
    SUBMIT_FAILED: "命令失败",
    SUBMIT_TIMEOUT: "等待超时",
    SUBMIT_NOT_LAUNCHED: "未启动",
    SUBMIT_BLOCKED: "预检未通过",
}

#: 这些状态表示「命令确实交给了 Windows 打印系统」
DELIVERED_STATES = frozenset({SUBMIT_SUBMITTED})


def submit_label(value: str) -> str:
    return SUBMIT_LABELS.get(value, value)


@dataclass
class PrintJob:
    """一次页面打印提交。"""

    task_uid: str
    page_number: int
    file_path: Path
    printer_name: str
    status: str = SUBMIT_PENDING
    command: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    #: 提交前 / 提交后的队列快照（JSON 文本，便于原样回放当时的情况）
    queue_before: str = ""
    queue_after: str = ""
    #: 提交后队列里新出现的作业号（用于阶段六/七追踪这一页）
    job_id: int | None = None
    message: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    id: int | None = None

    @property
    def status_text(self) -> str:
        return submit_label(self.status)

    @property
    def file_name(self) -> str:
        return Path(self.file_path).name

    @property
    def delivered(self) -> bool:
        """命令是否已交给 Windows（仅此而已，与纸张无关）。"""
        return self.status in DELIVERED_STATES

    @property
    def duration_text(self) -> str:
        if self.duration_ms <= 0:
            return "—"
        if self.duration_ms < 1000:
            return f"{self.duration_ms} 毫秒"
        return f"{self.duration_ms / 1000:.1f} 秒"

    @property
    def created_at_text(self) -> str:
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S")

    def summary_text(self) -> str:
        parts = [
            f"第 {self.page_number} 页 → {self.printer_name}",
            self.status_text,
        ]
        if self.exit_code is not None:
            parts.append(f"退出码 {self.exit_code}")
        if self.job_id:
            parts.append(f"队列作业 #{self.job_id}")
        return "，".join(parts)
