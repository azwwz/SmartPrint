"""文件名解析：识别 PDF24 按模板生成的文件名。

PDF24 的文件名模板为：

    %Y-%m-%d_%H-%M-%S_$id_$fileName

实际示例：

    2026-11-05_16-17-26_1FD3F68B_文档1.pdf

其中：

- `2026-11-05_16-17-26`：生成时间。
- `1FD3F68B`：PDF24 的任务 ID，同一份文档重复打印会得到不同的 ID。
- `文档1.pdf`：原始文档名（保留下来给用户看）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 时间戳部分 + 任务 ID + 原始文件名，原始文件名本身可能包含下划线，所以最后一段贪心匹配
_PDF24_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
    r"_(?P<job_id>[0-9A-Za-z]{4,32})"
    r"_(?P<name>.+)$"
)


@dataclass(frozen=True)
class ParsedFileName:
    """文件名解析结果。"""

    file_name: str
    document_name: str
    job_id: str | None = None
    generated_at: datetime | None = None

    @property
    def matched_template(self) -> bool:
        """是否符合 PDF24 模板（不符合时说明文件可能来自其他途径）。"""
        return self.job_id is not None


def parse_pdf24_file_name(file_name: str) -> ParsedFileName:
    """解析 PDF24 生成的文件名，匹配失败时退化为「使用整个文件名」。"""
    name = Path(file_name).name
    stem = Path(name).stem

    match = _PDF24_PATTERN.match(stem)
    if not match:
        return ParsedFileName(file_name=name, document_name=name)

    document_name = match.group("name") or name
    if not Path(document_name).suffix:
        document_name = f"{document_name}.pdf"

    generated_at: datetime | None = None
    try:
        generated_at = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H-%M-%S"
        )
    except ValueError:
        generated_at = None

    return ParsedFileName(
        file_name=name,
        document_name=document_name,
        job_id=match.group("job_id"),
        generated_at=generated_at,
    )
