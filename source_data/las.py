"""LAS 2.0 流式词法与段解析。

只负责结构切分并保留原始字节区间，不做地质解释，也不按“看起来像深度”的
数字猜测同步点。事件流由调用方逐行消费，避免把整段 ASCII 数据驻留内存。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Optional

# 符号、小数、E/D 指数均支持；整串匹配，拒绝空串与杂散字符。
NUMBER_RE = re.compile(r'[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eEdD][+-]?\d+)?\Z')
_TOKEN_RE = re.compile(rb'\S+')
_WS_RE = re.compile(r'\s')

_SECTION_TABLE = {
    'V': 'version', 'VERSION': 'version',
    'W': 'well', 'WELL': 'well',
    'C': 'curve', 'CURVE': 'curve',
    'P': 'parameter', 'PARAMETER': 'parameter',
    'A': 'ascii', 'ASCII': 'ascii',
    'O': 'other', 'OTHER': 'other',
}


class LasParseError(ValueError):
    """词法/结构错误，携带物理行号与字节偏移，不做同步猜测。"""

    def __init__(self, message: str, line: Optional[int] = None,
                 byte_offset: Optional[int] = None):
        super().__init__(message)
        self.line = line
        self.byte_offset = byte_offset


@dataclass(frozen=True)
class Line:
    number: int          # 1 起编物理行号
    start: int           # 行内容首字节偏移（含）
    end: int             # 行内容尾字节偏移（不含换行）
    newline_end: int     # 含换行后的下一偏移
    text: str            # 不含换行的 UTF-8 文本


@dataclass(frozen=True)
class Entry:
    mnemonic: str
    unit: str
    value: str
    description: str


@dataclass(frozen=True)
class Token:
    line: int
    start: int           # 文件级字节偏移（含）
    end: int             # 文件级字节偏移（不含）
    text: str


def section_kind(marker: str) -> Optional[str]:
    """段名大小写不敏感，支持单字母与全称；未知段返回 None。"""
    name = marker[1:].strip().upper()
    if not name:
        return None
    return _SECTION_TABLE.get(name) or _SECTION_TABLE.get(name[0])


def parse_header_line(text: str, line: Optional[Line] = None) -> Entry:
    """按 LAS 分隔规则切头条目。

    首个点号分助记符；点后到首个空白为单位（无单位时单位为空串）；
    其后到首个冒号为值；冒号之后整体作为说明，说明中的点号/冒号不再拆分。
    """
    line_no = line.number if line else None
    line_start = line.start if line else None
    dot = text.find('.')
    if dot <= 0:
        raise LasParseError('头条目缺少助记符分隔点', line_no, line_start)
    mnemonic = text[:dot]
    rest = text[dot + 1:]
    ws = _WS_RE.search(rest)
    if ws is None:
        unit, tail = rest, ''
    else:
        unit, tail = rest[:ws.start()], rest[ws.end():].lstrip()
    colon = tail.find(':')
    if colon < 0:
        value, description = tail.rstrip(), ''
    else:
        value, description = tail[:colon].rstrip(), tail[colon + 1:]
    if not mnemonic.strip():
        raise LasParseError('助记符为空', line_no, line_start)
    return Entry(mnemonic, unit, value, description)


def parse_decimal(token: str) -> Decimal:
    """严格数字词法，D/d 指数按 LAS 习惯转 E，Decimal 保留精度。"""
    if not NUMBER_RE.match(token):
        raise LasParseError(f'非法数字 token: {token!r}')
    return Decimal(token.replace('D', 'E').replace('d', 'e'))


def iter_tokens(line: Line, content: bytes) -> Iterator[Token]:
    """数据行按空白切 token，偏移换算为文件级字节区间。"""
    for m in _TOKEN_RE.finditer(content):
        yield Token(line.number, line.start + m.start(),
                    line.start + m.end(), m.group().decode('utf8'))


def iter_events(lines: Iterator[tuple[Line, bytes]]):
    """生成 (事件类型, 段类型, Line) 事件流。

    事件类型：section / entry / data / comment / blank / other。
    段类型为 las 规范名或 None（未知段）；注释与空行可以出现在任意段。
    """
    kind: Optional[str] = None
    for line, content in lines:
        text = line.text
        if not text.strip():
            yield 'blank', kind, line
            continue
        if text.startswith('#'):
            yield 'comment', kind, line
            continue
        if text.startswith('~'):
            kind = section_kind(text)
            yield 'section', kind, line
            continue
        if kind is None:
            raise LasParseError('段外条目，缺少段头', line.number, line.start)
        if kind in ('version', 'well', 'curve', 'parameter'):
            yield 'entry', kind, (parse_header_line(text, line), line)
        elif kind == 'ascii':
            yield 'data', kind, (line, content)
        else:
            yield 'other', kind, line
