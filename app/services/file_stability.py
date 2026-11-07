"""文件稳定性检查。

PDF24 保存 PDF 时，文件是「边写边出现」的：目录里先出现 0 字节文件，
然后体积逐步变大。如果一发现文件就开始读取，很容易读到半个 PDF。

因此接收流程必须等待：

1. 文件体积不再变化；
2. 修改时间不再变化；
3. 且这种「不变」持续了 `stability_seconds`。

超时或服务停止时抛出 FileNotStableError。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("smartprint.services.file_stability")


class FileNotStableError(Exception):
    """文件在允许的时间内没有达到稳定状态。"""


@dataclass(frozen=True)
class StableFileState:
    """稳定后的文件状态。"""

    path: Path
    size: int
    mtime: float
    waited_seconds: float


def wait_until_stable(
    path: Path,
    *,
    stability_seconds: float,
    poll_interval_seconds: float,
    max_wait_seconds: float,
    min_size_bytes: int,
    stop_event: threading.Event | None = None,
) -> StableFileState:
    """等待文件写入完成。

    成功时返回 StableFileState；文件消失、超时或服务停止时抛出 FileNotStableError。
    """
    file_path = Path(path)
    start = time.monotonic()
    last_signature: tuple[int, int] | None = None
    last_change_at = start
    reported_wait = False

    while True:
        if stop_event is not None and stop_event.is_set():
            raise FileNotStableError("接收服务已停止，取消等待")

        try:
            stat = os.stat(file_path)
        except FileNotFoundError as exc:
            raise FileNotStableError("文件已不存在（可能被移动或删除）") from exc
        except OSError as exc:
            raise FileNotStableError(f"无法读取文件状态：{exc}") from exc

        now = time.monotonic()
        signature = (stat.st_size, stat.st_mtime_ns)

        if signature != last_signature:
            # 文件仍在变化（或第一次看到它）
            last_signature = signature
            last_change_at = now
            if not reported_wait and stat.st_size > 0:
                logger.debug("文件正在写入：%s（当前 %s 字节）", file_path.name, stat.st_size)
                reported_wait = True
        else:
            unchanged_for = now - last_change_at
            if unchanged_for >= stability_seconds:
                if stat.st_size < min_size_bytes:
                    raise FileNotStableError(
                        f"文件体积过小（{stat.st_size} 字节，小于 {min_size_bytes} 字节），可能不是有效 PDF"
                    )
                waited = now - start
                logger.info(
                    "文件写入完成：%s（%s 字节，等待 %.1f 秒）",
                    file_path.name,
                    stat.st_size,
                    waited,
                )
                return StableFileState(
                    path=file_path,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    waited_seconds=waited,
                )

        if now - start > max_wait_seconds:
            raise FileNotStableError(f"等待文件写入完成超过 {max_wait_seconds:.0f} 秒")

        # 用局部变量控制等待，保证 stop_event 能较快生效
        if stop_event is not None:
            if stop_event.wait(poll_interval_seconds):
                raise FileNotStableError("接收服务已停止，取消等待")
        else:
            time.sleep(poll_interval_seconds)
