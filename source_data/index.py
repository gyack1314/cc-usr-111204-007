"""LAS 2.0 曲线采集、磁盘索引与标准深度窗随机访问。

设计约束：
- 全部数值经 Decimal 解析、Fraction 做有理数深度换算，NULL 按原单位精确匹配；
- 曲线值逐条写入 sqlite，构建与查询都不要求把全部曲线值驻留内存；
- 重测记录与缺测值原样保留，不插值、不平均、不合并；
- 文件损坏只报告已完成前缀、失败位置与不完整尾，不猜测同步点；
- 查询前重算源文件 SHA256，原文件变更则拒绝查询。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Optional

from .io import load
from .las import (LasParseError, Line, iter_events, iter_tokens,
                  parse_decimal)

getcontext().prec = 28

_DIRECTION_TEXT = {1: 'down', -1: 'up', 0: None}


class IndexConfigError(ValueError):
    """映射配置本身不合法，或单位无显式映射而拒绝正式转换。"""


class IndexQueryError(ValueError):
    """查询阶段错误：索引失效、run 未声明、跨井混入等。"""


class _RunFailed(Exception):
    """单 run 数据区损坏的内部控制信号，携带失败定位。"""

    def __init__(self, reason: str, line: int, byte_offset: int):
        super().__init__(reason)
        self.reason = reason
        self.line = line
        self.byte_offset = byte_offset


# ---------------------------------------------------------------- 有理数
def rational(value, label: str, positive: bool = False) -> Fraction:
    """配置中的比例/偏移必须是显式有理数字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise IndexConfigError(f'{label} 必须是有理数字符串')
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise IndexConfigError(f'{label} 不是合法有理数: {value!r}')
    if positive and result <= 0:
        raise IndexConfigError(f'{label} 必须为正数')
    return result


def fraction_to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def decimal_to_fraction(value: Decimal) -> Fraction:
    return Fraction(value)


# ---------------------------------------------------------------- 配置
@dataclass(frozen=True)
class RunSpec:
    run_id: str
    well_id: str
    file: str
    depth_column: int
    depth_scale: Fraction
    depth_offset: Fraction


@dataclass(frozen=True)
class CurveSpec:
    run_id: str
    column: int
    name: str
    unit: str
    scale: Fraction
    offset: Fraction


@dataclass
class Config:
    runs: list
    curves: list
    unit_aliases: dict
    window: Optional[tuple]


def _nonempty_str(obj, key, label):
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise IndexConfigError(f'{label} 的 {key} 必须是非空字符串')
    return value


def validate_config(cfg: dict) -> Config:
    if not isinstance(cfg, dict):
        raise IndexConfigError('配置顶层必须为对象')
    raw_runs = cfg.get('runs')
    if not isinstance(raw_runs, list) or not raw_runs:
        raise IndexConfigError('runs 必须是非空列表')
    runs, run_ids = [], set()
    for item in raw_runs:
        if not isinstance(item, dict):
            raise IndexConfigError('run 声明必须是对象')
        run_id = _nonempty_str(item, 'run_id', 'run')
        if run_id in run_ids:
            raise IndexConfigError(f'run_id 重复: {run_id}')
        run_ids.add(run_id)
        well_id = _nonempty_str(item, 'well_id', 'run')
        file_name = _nonempty_str(item, 'file', 'run')
        depth_column = item.get('depth_column')
        if type(depth_column) is not int or depth_column < 0:
            raise IndexConfigError(f'{run_id}: depth_column 必须是非负整数列号')
        runs.append(RunSpec(
            run_id, well_id, file_name, depth_column,
            rational(item.get('depth_scale'), 'depth_scale', positive=True),
            rational(item.get('depth_offset'), 'depth_offset')))
    raw_curves = cfg.get('curve_map')
    if not isinstance(raw_curves, list):
        raise IndexConfigError('curve_map 必须是列表')
    curves, pairs = [], set()
    for item in raw_curves:
        if not isinstance(item, dict):
            raise IndexConfigError('curve_map 条目必须是对象')
        run_id = _nonempty_str(item, 'run_id', 'curve_map')
        if run_id not in run_ids:
            raise IndexConfigError(f'curve_map 引用了未声明的 run: {run_id}')
        column = item.get('column')
        if type(column) is not int or column < 0:
            raise IndexConfigError(f'{run_id}: 映射列号必须是非负整数')
        if (run_id, column) in pairs:
            raise IndexConfigError(f'{run_id}: 第 {column} 列被重复映射')
        pairs.add((run_id, column))
        name = _nonempty_str(item, 'name', 'curve_map')
        unit = _nonempty_str(item, 'unit', 'curve_map')
        curves.append(CurveSpec(
            run_id, column, name, unit,
            rational(item.get('scale'), 'scale', positive=True),
            rational(item.get('offset'), 'offset')))
    aliases_raw = cfg.get('unit_aliases', {})
    if not isinstance(aliases_raw, dict):
        raise IndexConfigError('unit_aliases 必须是对象')
    aliases = {}
    for canonical, alias_list in aliases_raw.items():
        if not isinstance(canonical, str) or not isinstance(alias_list, list) \
                or any(not isinstance(a, str) for a in alias_list):
            raise IndexConfigError('unit_aliases 结构为 规范单位->字符串列表')
        aliases[canonical] = set(alias_list) | {canonical}
    window = None
    if 'window_metres' in cfg:
        raw_window = cfg['window_metres']
        if not isinstance(raw_window, list) or len(raw_window) != 2:
            raise IndexConfigError('window_metres 必须是含两个端点的列表')
        lo = rational(raw_window[0], 'window_metres[0]')
        hi = rational(raw_window[1], 'window_metres[1]')
        if lo > hi:
            raise IndexConfigError('window_metres 下界不得大于上界')
        window = (lo, hi)
    return Config(runs, curves, aliases, window)


# ---------------------------------------------------------------- 行读取
class LineReader:
    """分块读盘：边累计 SHA256 边产出物理行，曲线值不整体驻留内存。"""

    CHUNK = 1 << 16

    def __init__(self, path: Path):
        self._f = open(path, 'rb')
        self._buf = bytearray()
        self._offset = 0
        self.line_no = 0
        self.bytes_total = 0
        self.hasher = hashlib.sha256()

    def _emit(self, raw_line: bytes, consumed: int):
        self.line_no += 1
        start = self._offset
        content = raw_line[:-1] if raw_line.endswith(b'\r') else raw_line
        end = start + len(content)
        line = Line(self.line_no, start, end, start + consumed,
                    content.decode('utf-8'))
        self._offset = start + consumed
        return line, bytes(content)

    def __iter__(self) -> Iterator[tuple]:
        while True:
            chunk = self._f.read(self.CHUNK)
            if not chunk:
                break
            self.hasher.update(chunk)
            self.bytes_total += len(chunk)
            self._buf.extend(chunk)
            while True:
                lf = self._buf.find(b'\n')
                if lf < 0:
                    break
                raw_line = bytes(self._buf[:lf])
                del self._buf[:lf + 1]
                yield self._emit(raw_line, len(raw_line) + 1)
        if self._buf:
            raw_line = bytes(self._buf)
            self._buf.clear()
            yield self._emit(raw_line, len(raw_line))
        self._f.close()

    def digest(self) -> str:
        return self.hasher.hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 构建状态
@dataclass
class _RunState:
    spec: RunSpec
    maps: list
    aliases: dict = field(default_factory=dict)
    sections: set = field(default_factory=set)
    first_section: Optional[str] = None
    entries: list = field(default_factory=list)
    curve_entries: list = field(default_factory=list)
    well_by_mnemonic: dict = field(default_factory=dict)
    wrap: Optional[str] = None
    null_value: Optional[Decimal] = None
    null_text: Optional[str] = None
    pending: list = field(default_factory=list)        # (Token, Decimal|None)
    record_count: int = 0
    last_depth: Optional[Decimal] = None
    direction: int = 0
    depth_counts: dict = field(default_factory=dict)
    depths: list = field(default_factory=list)
    failed: Optional[dict] = None


_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, well_id TEXT NOT NULL, file TEXT NOT NULL,
  sha256 TEXT NOT NULL, depth_column INTEGER NOT NULL,
  depth_scale TEXT NOT NULL, depth_offset TEXT NOT NULL,
  wrap TEXT NOT NULL, null_text TEXT NOT NULL, direction INTEGER,
  record_count INTEGER NOT NULL, complete INTEGER NOT NULL,
  fail_reason TEXT, fail_line INTEGER, fail_byte INTEGER,
  tail_start_line INTEGER, tail_start_byte INTEGER, tail_end_byte INTEGER,
  diag_json TEXT NOT NULL, cfg_sha256 TEXT NOT NULL);
CREATE TABLE entries(
  run_id TEXT NOT NULL, section TEXT NOT NULL, line_no INTEGER NOT NULL,
  mnemonic TEXT NOT NULL, unit TEXT NOT NULL, value TEXT NOT NULL,
  description TEXT NOT NULL);
CREATE TABLE curves(
  run_id TEXT NOT NULL, column_idx INTEGER NOT NULL,
  mnemonic TEXT NOT NULL, unit TEXT NOT NULL,
  map_name TEXT, map_unit TEXT, scale TEXT, offset TEXT,
  PRIMARY KEY(run_id, column_idx));
CREATE TABLE records(
  run_id TEXT NOT NULL, record_idx INTEGER NOT NULL,
  depth_raw TEXT NOT NULL, std_num INTEGER NOT NULL, std_den INTEGER NOT NULL,
  direction INTEGER NOT NULL, repeat_seq INTEGER NOT NULL,
  byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL,
  first_line INTEGER NOT NULL, last_line INTEGER NOT NULL,
  PRIMARY KEY(run_id, record_idx));
CREATE TABLE tokens(
  run_id TEXT NOT NULL, record_idx INTEGER NOT NULL, column_idx INTEGER NOT NULL,
  raw TEXT NOT NULL, is_null INTEGER NOT NULL, line_no INTEGER NOT NULL,
  byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL,
  conv_num INTEGER, conv_den INTEGER,
  PRIMARY KEY(run_id, record_idx, column_idx));
CREATE INDEX idx_records_std ON records(run_id, std_num, std_den);
"""


def _median_decimal(values: list) -> Optional[Decimal]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _resolve_within(root: Path, name: str) -> Path:
    base = root.resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or target == base:
        raise IndexConfigError(f'资料路径越界: {name}')
    return target


def build_index(config_path, db_path) -> dict:
    """按配置构建磁盘索引，返回可 JSON 序列化的构建报告。"""
    cfg, cfg_sha = load(config_path)
    config = validate_config(cfg)
    db_file = Path(db_path)
    if db_file.exists():
        raise IndexConfigError(f'索引数据库已存在: {db_file}')
    db_file.parent.mkdir(parents=True, exist_ok=True)
    data_root = Path(config_path).resolve().parent
    for spec in config.runs:
        _resolve_within(data_root, spec.file)
    db = sqlite3.connect(db_file)
    try:
        db.executescript(_SCHEMA)
        db.execute('INSERT INTO meta VALUES(?,?)', ('cfg_sha256', cfg_sha))
        reports = []
        maps_by_run = {}
        for spec in config.runs:
            maps_by_run[spec.run_id] = [m for m in config.curves
                                        if m.run_id == spec.run_id]
        for spec in config.runs:
            reports.append(_build_run(
                db, spec, maps_by_run[spec.run_id], config.unit_aliases,
                data_root, cfg_sha))
        db.commit()
    finally:
        db.close()
    return {'config_sha256': cfg_sha, 'runs': reports}


def _build_run(db, spec: RunSpec, maps: list, aliases: dict,
               data_root: Path, cfg_sha: str) -> dict:
    path = data_root / spec.file
    reader = LineReader(path)
    state = _RunState(spec, maps, aliases)
    try:
        try:
            for event, kind, payload in iter_events(reader):
                _handle_event(db, state, event, kind, payload, aliases)
            _finish_run(db, state, reader.bytes_total)
        except _RunFailed as failed:
            state.failed = {'reason': failed.reason, 'line': failed.line,
                            'byte_offset': failed.byte_offset}
        except LasParseError as exc:
            state.failed = {'reason': str(exc),
                            'line': exc.line if exc.line is not None
                            else reader.line_no,
                            'byte_offset': exc.byte_offset if exc.byte_offset
                            is not None else reader._offset}
        except UnicodeDecodeError as exc:
            state.failed = {'reason': f'UTF-8 解码失败: {exc}',
                            'line': reader.line_no,
                            'byte_offset': reader._offset}
    finally:
        reader._f.close()
    digest = reader.digest()
    return _persist_run(db, state, digest, reader.bytes_total, cfg_sha)


def _handle_event(db, state: _RunState, event, kind, payload, aliases):
    if event == 'section':
        if state.first_section is None:
            state.first_section = kind
            if kind != 'version':
                raise LasParseError('Version 段必须位于最前')
        if kind in state.sections and kind != 'other':
            raise LasParseError(f'段重复出现: ~{kind}')
        if state.sections and 'ascii' in state.sections:
            # 新段结束数据区：跨行记录不得跨段补齐。
            _fail_pending(state, '新段开始，跨行记录未凑齐曲线数列')
        state.sections.add(kind)
        if kind == 'ascii':
            _validate_structure(state, aliases)
        return
    if event in ('comment', 'blank', 'other'):
        if event == 'other' and 'ascii' in state.sections:
            _fail_pending(state, '其他段开始，跨行记录未凑齐曲线数列')
        return
    if event == 'entry':
        entry, line = payload
        state.entries.append((kind, line.number, entry))
        if kind == 'well':
            state.well_by_mnemonic[entry.mnemonic.upper()] = entry
        if kind == 'curve':
            state.curve_entries.append(entry)
        if kind == 'version' and entry.mnemonic.upper() == 'WRAP':
            wrap = entry.value.strip().upper()
            if wrap not in ('YES', 'NO'):
                raise LasParseError(f'WRAP 取值非法: {entry.value!r}',
                                    line.number, line.start)
            state.wrap = wrap
        return
    if event == 'data':
        _handle_data_line(db, state, payload)
        return
    raise LasParseError(f'未知事件: {event}')


def _validate_structure(state: _RunState, aliases: dict):
    if 'version' not in state.sections:
        raise LasParseError('缺少 Version 段')
    version_entries = [e for s, _, e in state.entries if s == 'version']
    vers = next((e for e in version_entries
                 if e.mnemonic.upper() == 'VERS'), None)
    if vers is None or not vers.value.strip().startswith('2'):
        raise LasParseError('仅支持 LAS 2.0，VERS 缺失或非 2.x')
    if state.wrap is None:
        raise LasParseError('Version 段缺少 WRAP 声明')
    if 'well' not in state.sections:
        raise LasParseError('缺少 Well 段')
    null_entry = state.well_by_mnemonic.get('NULL')
    if null_entry is None or not null_entry.value.strip():
        raise LasParseError('Well 段缺少 NULL 声明')
    state.null_value = parse_decimal(null_entry.value.strip())
    state.null_text = null_entry.value.strip()
    if 'curve' not in state.sections or not state.curve_entries:
        raise LasParseError('缺少 Curve 段或未声明任何曲线')
    ncurves = len(state.curve_entries)
    if state.spec.depth_column >= ncurves:
        raise IndexConfigError(
            f'{state.spec.run_id}: depth_column={state.spec.depth_column} '
            f'超出 Curve 列数 {ncurves}')
    for mapping in state.maps:
        if mapping.column >= ncurves:
            raise IndexConfigError(
                f'{state.spec.run_id}: 映射列 {mapping.column} 超出 Curve 列数')
        header_unit = state.curve_entries[mapping.column].unit
        allowed = aliases.get(mapping.unit, {mapping.unit})
        if header_unit not in allowed:
            raise IndexConfigError(
                f'{state.spec.run_id} 第 {mapping.column} 列单位 {header_unit!r} '
                f'未在显式映射中声明为 {mapping.unit!r}，拒绝正式转换')


def _fail_pending(state: _RunState, reason: str):
    if state.pending:
        token = state.pending[0][0]
        raise _RunFailed(reason, token.line, token.start)


def _handle_data_line(db, state: _RunState, payload):
    line, content = payload
    parsed = []
    for token in iter_tokens(line, content):
        try:
            decimal_value = parse_decimal(token.text)
        except LasParseError:
            raise _RunFailed(f'非法数字 token: {token.text!r}',
                             token.line, token.start)
        parsed.append((token, decimal_value))
    ncurves = len(state.curve_entries)
    if state.wrap == 'NO':
        if len(parsed) != ncurves:
            raise _RunFailed(
                f'WRAP=NO 数据行应有 {ncurves} 列，实际 {len(parsed)} 列，'
                '不得借下行凑齐', line.number, line.start)
        _finalize_record(db, state, parsed)
        return
    state.pending.extend(parsed)
    while len(state.pending) >= ncurves:
        record_tokens = state.pending[:ncurves]
        del state.pending[:ncurves]
        _finalize_record(db, state, record_tokens)


def _finalize_record(db, state: _RunState, parsed: list):
    spec = state.spec
    depth_token, depth_value = parsed[spec.depth_column]
    if depth_value == state.null_value:
        raise _RunFailed('深度为 NULL，该测点无法索引',
                         depth_token.line, depth_token.start)
    if state.last_depth is not None:
        if depth_value > state.last_depth:
            cmp_dir = 1
        elif depth_value < state.last_depth:
            cmp_dir = -1
        else:
            cmp_dir = 0
        if state.direction == 0 and cmp_dir != 0:
            state.direction = cmp_dir
        elif state.direction != 0 and cmp_dir != 0 \
                and cmp_dir != state.direction:
            raise _RunFailed('有效深度序列非单调（同时出现升、降）',
                             depth_token.line, depth_token.start)
    repeat_seq = state.depth_counts.get(depth_value, 0)
    state.depth_counts[depth_value] = repeat_seq + 1
    std_depth = (decimal_to_fraction(depth_value) * spec.depth_scale
                 + spec.depth_offset)
    record_idx = state.record_count
    byte_start = parsed[0][0].start
    byte_end = parsed[-1][0].end
    lines = sorted({token.line for token, _ in parsed})
    db.execute(
        'INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (spec.run_id, record_idx, format(depth_value, 'f'),
         std_depth.numerator, std_depth.denominator, state.direction,
         repeat_seq, byte_start, byte_end, lines[0], lines[-1]))
    map_by_col = {m.column: m for m in state.maps}
    for column, (token, decimal_value) in enumerate(parsed):
        is_null = decimal_value == state.null_value
        conv_num = conv_den = None
        mapping = map_by_col.get(column)
        if mapping is not None and not is_null:
            converted = (decimal_to_fraction(decimal_value) * mapping.scale
                         + mapping.offset)
            conv_num, conv_den = converted.numerator, converted.denominator
        db.execute('INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (spec.run_id, record_idx, column, token.text,
                    1 if is_null else 0, token.line, token.start, token.end,
                    conv_num, conv_den))
    state.record_count += 1
    state.last_depth = depth_value
    state.depths.append(depth_value)


def _finish_run(db, state: _RunState, bytes_total: int):
    if 'version' not in state.sections:
        raise LasParseError('缺少 Version 段')
    # 结构校验在进入 ASCII 时已做；无 ASCII 段时在此补做。
    if 'ascii' not in state.sections:
        _validate_structure(state, state.aliases)
    if state.pending:
        token = state.pending[0][0]
        raise _RunFailed('文件结尾跨行记录未凑齐曲线数列，不得跨文件结尾补齐',
                         token.line, token.start)


def _diagnostics(state: _RunState) -> dict:
    header = {}
    for key in ('STRT', 'STOP', 'STEP'):
        entry = state.well_by_mnemonic.get(key)
        if entry is not None:
            header[key] = {'value': entry.value.strip(), 'unit': entry.unit}
    observed = {}
    mismatch = []
    if state.depths:
        first, last = state.depths[0], state.depths[-1]
        observed['strt'] = format(first, 'f')
        observed['stop'] = format(last, 'f')
        deltas = [abs(b - a) for a, b in zip(state.depths, state.depths[1:])]
        step = _median_decimal(deltas)
        observed['step'] = None if step is None else format(step, 'f')
        for key, observed_value in (('STRT', first), ('STOP', last)):
            entry = state.well_by_mnemonic.get(key)
            if entry is not None and entry.value.strip():
                try:
                    if parse_decimal(entry.value.strip()) != observed_value:
                        mismatch.append(
                            f'{key} 头值 {entry.value.strip()} 与实际观测 '
                            f'{format(observed_value, "f")} 不一致')
                except LasParseError:
                    mismatch.append(f'{key} 头值无法按数字解析: {entry.value!r}')
        step_entry = state.well_by_mnemonic.get('STEP')
        if step_entry is not None and step_entry.value.strip() and step is not None:
            try:
                if abs(parse_decimal(step_entry.value.strip())) != step:
                    mismatch.append(
                        f'STEP 头值 {step_entry.value.strip()} 与实际中位步长 '
                        f'{format(step, "f")} 不一致')
            except LasParseError:
                mismatch.append(f'STEP 头值无法按数字解析: {step_entry.value!r}')
    return {'header': header, 'observed': observed, 'mismatch': mismatch}


def _persist_run(db, state: _RunState, digest: str, bytes_total: int,
                 cfg_sha: str) -> dict:
    spec = state.spec
    failure = state.failed
    tail = None
    if failure is not None:
        tail = {'start_line': failure['line'],
                'start_byte': failure['byte_offset'],
                'end_byte': bytes_total}
    db.execute('INSERT INTO runs VALUES(' + '?,' * 19 + '?)', (
        spec.run_id, spec.well_id, spec.file, digest, spec.depth_column,
        str(spec.depth_scale), str(spec.depth_offset),
        state.wrap or '', state.null_text or '',
        None if state.direction == 0 else state.direction,
        state.record_count, 0 if failure else 1,
        failure['reason'] if failure else None,
        failure['line'] if failure else None,
        failure['byte_offset'] if failure else None,
        tail['start_line'] if tail else None,
        tail['start_byte'] if tail else None,
        tail['end_byte'] if tail else None,
        _json(_diagnostics(state)), cfg_sha))
    for section, line_no, entry in state.entries:
        db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?)',
                   (spec.run_id, section, line_no, entry.mnemonic, entry.unit,
                    entry.value, entry.description))
    map_by_col = {m.column: m for m in state.maps}
    for column, entry in enumerate(state.curve_entries):
        mapping = map_by_col.get(column)
        db.execute('INSERT INTO curves VALUES(?,?,?,?,?,?,?,?)', (
            spec.run_id, column, entry.mnemonic, entry.unit,
            mapping.name if mapping else None,
            mapping.unit if mapping else None,
            str(mapping.scale) if mapping else None,
            str(mapping.offset) if mapping else None))
    return {
        'run_id': spec.run_id, 'well_id': spec.well_id, 'file': spec.file,
        'sha256': digest, 'wrap': state.wrap,
        'null_value': state.null_text,
        'direction': _DIRECTION_TEXT.get(state.direction),
        'records': state.record_count,
        'complete': failure is None,
        'failure': failure,
        'incomplete_tail': tail,
        'diagnostics': _diagnostics(state),
        'curves': [{'column': column, 'mnemonic': entry.mnemonic,
                    'unit': entry.unit,
                    'mapped_name': (map_by_col[column].name
                                    if column in map_by_col else None)}
                   for column, entry in enumerate(state.curve_entries)]}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 查询
class LasIndex:
    """磁盘索引上的随机访问；打开即校验，查询前再次核对源文件 SHA。"""

    def __init__(self, db_path, data_root=None):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.data_root = Path(data_root) if data_root else None
        self._run_cache = {}

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _run_row(self, run_id: str) -> sqlite3.Row:
        if run_id not in self._run_cache:
            row = self.db.execute('SELECT * FROM runs WHERE run_id=?',
                                  (run_id,)).fetchone()
            if row is None:
                raise IndexQueryError(f'索引中不存在 run: {run_id}')
            self._run_cache[run_id] = row
        return self._run_cache[run_id]

    def verify(self, run_id: str) -> sqlite3.Row:
        row = self._run_row(run_id)
        if self.data_root is None:
            raise IndexQueryError('查询需要 data_root 以核对源文件')
        path = _resolve_within(self.data_root, row['file'])
        current = hash_file(path)
        if current != row['sha256']:
            raise IndexQueryError(
                f'{run_id}: 源文件已变更（SHA256 不符），拒绝查询，请重建索引')
        return row

    def list_runs(self) -> list:
        return [dict(r) for r in self.db.execute(
            'SELECT run_id,well_id,file,complete,record_count,direction '
            'FROM runs ORDER BY run_id')]

    def _check_runs(self, run_ids):
        if not isinstance(run_ids, list) or not run_ids \
                or any(not isinstance(x, str) for x in run_ids):
            raise IndexQueryError('run_ids 必须是非空字符串列表')
        if len(set(run_ids)) != len(run_ids):
            raise IndexQueryError('run_id 列表重复')
        rows = [self.verify(run_id) for run_id in run_ids]
        wells = {row['well_id'] for row in rows}
        if len(wells) > 1:
            raise IndexQueryError(
                f'拒绝跨井拼接：{",".join(sorted(wells))}；跨井必须显式分窗查询')
        return rows

    def query_window(self, run_ids, lo, hi) -> dict:
        """闭合标准深度窗 [lo, hi]，多 run 同井时按统一约定排序。"""
        if isinstance(lo, str):
            lo = Fraction(lo)
        if isinstance(hi, str):
            hi = Fraction(hi)
        if not isinstance(lo, Fraction) or not isinstance(hi, Fraction):
            raise IndexQueryError('窗口端点必须是有理数')
        if lo > hi:
            raise IndexQueryError('窗口下界不得大于上界')
        rows = self._check_runs(run_ids)
        collected = []
        coverage = []
        for row in rows:
            run_id = row['run_id']
            records = self.db.execute(
                'SELECT * FROM records WHERE run_id=? '
                'AND std_num * ? >= ? * std_den '
                'AND std_num * ? <= ? * std_den ORDER BY record_idx',
                (run_id, lo.denominator, lo.numerator,
                 hi.denominator, hi.numerator)).fetchall()
            for record in records:
                collected.append(
                    (Fraction(record['std_num'], record['std_den']),
                     run_id, record['record_idx']))
            coverage.append({'run_id': run_id,
                             'complete': bool(row['complete']),
                             'indexed_records': row['record_count'],
                             'window_records': len(records),
                             'failure': row['fail_reason']})
        collected.sort(key=lambda item: (item[0], item[1], item[2]))
        return {
            'window': [str(lo), str(hi)],
            'ordering': 'standard_depth,run_id,record_idx',
            'coverage': coverage,
            'rows': [self._load_row(run_id, idx)
                     for _, run_id, idx in collected]}

    def sequential(self, run_id: str) -> list:
        """完整顺序抽取：按原记录序号返回该 run 全部已索引测点。"""
        row = self.verify(run_id)
        return [self._load_row(run_id, idx) for idx in range(row['record_count'])]

    def _load_row(self, run_id: str, record_idx: int) -> dict:
        record = self.db.execute(
            'SELECT * FROM records WHERE run_id=? AND record_idx=?',
            (run_id, record_idx)).fetchone()
        token_rows = self.db.execute(
            'SELECT * FROM tokens WHERE run_id=? AND record_idx=? '
            'ORDER BY column_idx', (run_id, record_idx)).fetchall()
        curve_rows = self.db.execute(
            'SELECT * FROM curves WHERE run_id=? ORDER BY column_idx',
            (run_id,)).fetchall()
        curve_meta = {c['column_idx']: c for c in curve_rows}
        curves = {}
        for token in token_rows:
            meta = curve_meta.get(token['column_idx'])
            if meta is None or meta['map_name'] is None:
                continue
            value = None
            if not token['is_null']:
                value = format(fraction_to_decimal(
                    Fraction(token['conv_num'], token['conv_den'])), 'f')
            curves[meta['map_name']] = {
                'raw': token['raw'], 'null': bool(token['is_null']),
                'value': value, 'unit': meta['map_unit'],
                'byte_range': [token['byte_start'], token['byte_end']],
                'line': token['line_no']}
        return {
            'run_id': run_id,
            'record_idx': record['record_idx'],
            'repeat_seq': record['repeat_seq'],
            'direction': _DIRECTION_TEXT.get(record['direction']),
            'depth_raw': record['depth_raw'],
            'depth_std': format(fraction_to_decimal(
                Fraction(record['std_num'], record['std_den'])), 'f'),
            'byte_range': [record['byte_start'], record['byte_end']],
            'lines': [record['first_line'], record['last_line']],
            'raw_columns': [token['raw'] for token in token_rows],
            'curves': curves}
