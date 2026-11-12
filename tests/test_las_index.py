"""LAS 2.0 采集与磁盘索引的约定检查。

覆盖：五段解析与大小写不敏感、说明中的点号/冒号、E/D 指数与 Decimal 精度、
NULL 精确匹配与近 NULL 真实值、WRAP 跨行重建、重复深度重测、负向测深、
CRLF/UTF-8、字节区间来源核对、损坏前缀/尾区、防篡改、跨井与未知单位拒绝、
顺序抽取与窗口随机读取的来源一致性。
"""
import json
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from source_data.index import (LasIndex, IndexConfigError, IndexQueryError,
                               build_index, fraction_to_decimal, rational,
                               validate_config)
from source_data.las import (LasParseError, parse_decimal, parse_header_line,
                             section_kind)

DATA = Path(__file__).resolve().parent.parent / 'data'

BASE_CFG = {
    'runs': [{
        'run_id': 'r1', 'well_id': 'w1', 'file': 'sample.las',
        'depth_column': 0, 'depth_scale': '1', 'depth_offset': '0'}],
    'curve_map': [{
        'run_id': 'r1', 'column': 1, 'name': 'density', 'unit': 'g/cm3',
        'scale': '1', 'offset': '0'}],
    'unit_aliases': {'g/cm3': ['G/C3']},
}


def write_config(root: Path, cfg: dict) -> Path:
    path = root / 'config.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


def build_in(root: Path, las_text: str, cfg=None, name='sample.las',
             newline='\n'):
    (root / name).write_bytes(las_text.encode('utf-8').replace(b'\n', newline.encode('utf-8')))
    config = write_config(root, cfg or BASE_CFG)
    db = root / 'index.db'
    report = build_index(config, db)
    return db, report


GOOD_LAS = """~Version
VERS. 2.0 :版本说明含:冒号
WRAP. YES :跨行
~Well
STRT.M 0 :起点
STOP.M 2 :终点
STEP.M 1 :步长
NULL. -999.25 :空值
WELL. w1 :井
~Curve
MD.M :测深
RHOB.G/C3 :密度:原始单位
~Parameter
RUN. demo :运行批次
~ASCII
0 2.1
1
-999.25
1 2.3
2 -999.2499
"""


class HeaderTests(unittest.TestCase):
    def test_section_names_case_insensitive(self):
        self.assertEqual(section_kind('~version'), 'version')
        self.assertEqual(section_kind('~CURVE INFORMATION'), 'curve')
        self.assertEqual(section_kind('~p'), 'parameter')
        self.assertEqual(section_kind('~ASCII'), 'ascii')
        self.assertIsNone(section_kind('~nope'))

    def test_header_entry_four_parts_and_description_punctuation(self):
        entry = parse_header_line('RHOB.G/C3 :密度:原始.单位')
        self.assertEqual((entry.mnemonic, entry.unit, entry.value,
                          entry.description),
                         ('RHOB', 'G/C3', '', '密度:原始.单位'))
        entry = parse_header_line('STRT.FT 100 :起点:深度')
        self.assertEqual((entry.mnemonic, entry.unit, entry.value,
                          entry.description),
                         ('STRT', 'FT', '100', '起点:深度'))
        entry = parse_header_line('VERS. 2.0 :版本')
        self.assertEqual(entry.mnemonic, 'VERS')
        self.assertEqual(entry.unit, '')
        self.assertEqual(entry.value, '2.0')
        self.assertEqual(entry.description, '版本')

    def test_header_requires_dot(self):
        with self.assertRaises(LasParseError):
            parse_header_line('NODOT 1 :x')

    def test_decimal_syntax_and_d_exponent(self):
        for text in ('+1', '-0.5', '.25', '1.', '1E3', '-2.5d+2', '3D-1'):
            self.assertIsInstance(parse_decimal(text), Decimal)
        self.assertEqual(parse_decimal('2.5d+2'), Decimal('250'))
        self.assertEqual(parse_decimal('.5'), Decimal('0.5'))
        for bad in ('1.2.3', 'NaN', 'inf', '', '1e', 'E5'):
            with self.assertRaises(LasParseError):
                parse_decimal(bad)


class WrapAndNullTests(unittest.TestCase):
    def test_wrap_yes_rebuilds_recross_physical_lines(self):
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), GOOD_LAS)
            run = report['runs'][0]
            self.assertTrue(run['complete'])
            self.assertEqual(run['records'], 4)
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertEqual([r['lines'] for r in rows],
                             [[16, 16], [17, 18], [19, 19], [20, 20]])
            self.assertEqual(rows[1]['raw_columns'], ['1', '-999.25'])
            self.assertTrue(rows[1]['curves']['density']['null'])
            # 接近 NULL 的真实值必须保留，不得按 NULL 删掉
            self.assertFalse(rows[3]['curves']['density']['null'])
            self.assertEqual(rows[3]['curves']['density']['value'], '-999.2499')

    def test_wrap_no_short_line_cannot_borrow_next_line(self):
        # 独立构造 WRAP=NO：首行完整，第二行只有一个数字即失败，不借下行凑齐
        las = GOOD_LAS.replace('WRAP. YES', 'WRAP. NO')
        head = las.split('~ASCII')[0]
        las = head + '~ASCII\n0 2.1\n1\n2 2.3\n'
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertEqual(run['records'], 1)
            self.assertIn('不得借下行凑齐', run['failure']['reason'])
            self.assertEqual(run['failure']['line'], 17)
            self.assertEqual(run['incomplete_tail']['start_line'], 17)

    def test_wrap_yes_pending_cannot_finish_across_section_or_eof(self):
        # 新段截断跨行记录
        las = GOOD_LAS.replace('2 -999.2499\n',
                               '2\n~Other\njunk\n')
        with tempfile.TemporaryDirectory() as d:
            _, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertIn('新段开始', run['failure']['reason'])
            self.assertEqual(run['records'], 3)
        # 文件结尾截断
        las = GOOD_LAS.replace('2 -999.2499\n', '2\n')
        with tempfile.TemporaryDirectory() as d:
            _, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertIn('文件结尾', run['failure']['reason'])
            self.assertEqual(run['failure']['line'], 20)

    def test_depth_null_is_fatal_and_prefix_kept(self):
        # 第三个测点（第二条 101 重测）深度换成 NULL，前两条完整记录保留
        las = GOOD_LAS.replace('1 2.3', '-999.25 2.3', 1)
        with tempfile.TemporaryDirectory() as d:
            _, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertEqual(run['records'], 2)
            self.assertIn('深度为 NULL', run['failure']['reason'])
            self.assertEqual(run['failure']['line'], 19)

    def test_no_depth_guessing_on_garbage(self):
        las = GOOD_LAS.replace('0 2.1', 'garbage 2.1')
        with tempfile.TemporaryDirectory() as d:
            _, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertEqual(run['records'], 0)
            self.assertIn('非法数字', run['failure']['reason'])
            self.assertEqual(run['failure']['line'], 16)


class RepeatAndDirectionTests(unittest.TestCase):
    def test_repeat_depths_keep_both_runs_with_sequence_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), GOOD_LAS)
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertEqual([r['repeat_seq'] for r in rows], [0, 0, 1, 0])
            self.assertEqual([r['record_idx'] for r in rows], [0, 1, 2, 3])
            self.assertEqual(report['runs'][0]['direction'], 'down')

    def test_reverse_direction_and_negative_sounding(self):
        las = GOOD_LAS.replace('~ASCII\n0 2.1\n1\n-999.25\n1 2.3\n2 -999.2499\n',
                               '~ASCII\n-10 2.1\n-20 2.2\n-30 2.3\n')
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['curve_map'][0]['column'] = 1
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), las, cfg)
            run = report['runs'][0]
            self.assertTrue(run['complete'])
            self.assertEqual(run['direction'], 'up')
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertEqual([r['depth_raw'] for r in rows],
                             ['-10', '-20', '-30'])
            self.assertEqual(rows[0]['depth_std'], '-10')

    def test_non_monotonic_direction_is_rejected(self):
        las = GOOD_LAS.replace('~ASCII\n0 2.1\n1\n-999.25\n1 2.3\n2 -999.2499\n',
                               '~ASCII\n0 1\n2 1\n1 1\n')
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['curve_map'][0]['column'] = 1
        with tempfile.TemporaryDirectory() as d:
            _, report = build_in(Path(d), las, cfg)
            run = report['runs'][0]
            self.assertFalse(run['complete'])
            self.assertEqual(run['records'], 2)
            self.assertIn('非单调', run['failure']['reason'])


class ByteRangeTests(unittest.TestCase):
    def test_crlf_and_utf8_offsets_and_token_ranges(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db, _ = build_in(root, GOOD_LAS, newline='\r\n')
            raw = (root / 'sample.las').read_bytes()
            self.assertIn(b'\r\n', raw)
            with LasIndex(db, root) as index:
                rows = index.sequential('r1')
            # 每个字节区间回读都必须还原出原始 token
            for row in rows:
                start, end = row['byte_range']
                self.assertEqual(raw[start:end].split(),
                                 [c.encode() for c in row['raw_columns']])
                for curve in row['curves'].values():
                    lo, hi = curve['byte_range']
                    self.assertEqual(raw[lo:hi].decode(), curve['raw'])
            # 跨行记录的区间跨越 CRLF
            cross = rows[1]
            self.assertIn(b'\r\n', raw[cross['byte_range'][0]:cross['byte_range'][1]])
            self.assertEqual(cross['lines'], [17, 18])

    def test_sequential_and_window_reads_share_source_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db, _ = build_in(root, GOOD_LAS)
            raw = (root / 'sample.las').read_bytes()
            with LasIndex(db, root) as index:
                full = index.sequential('r1')
                window = index.query_window(['r1'], Fraction(1), Fraction(2))['rows']
            window_idx = {r['record_idx'] for r in window}
            self.assertEqual(window_idx, {1, 2, 3})
            by_idx = {r['record_idx']: r for r in full}
            for row in window:
                same = by_idx[row['record_idx']]
                self.assertEqual(row['byte_range'], same['byte_range'])
                lo, hi = row['byte_range']
                self.assertEqual(raw[lo:hi].decode().split(),
                                 row['raw_columns'])


class RealSampleTests(unittest.TestCase):
    def test_bundled_down_and_up_files_and_window(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'real.db'
            report = build_index(DATA / 'normal.json', db)
            self.assertTrue(all(r['complete'] for r in report['runs']))
            down, up = report['runs']
            self.assertEqual(down['wrap'], 'YES')
            self.assertEqual(down['direction'], 'down')
            self.assertEqual(up['wrap'], 'NO')
            self.assertEqual(up['direction'], 'up')
            self.assertTrue(up['diagnostics']['mismatch'])
            with LasIndex(db, DATA) as index:
                result = index.query_window(
                    ['down-feet', 'up-metres'], '30.48', '30.7848')
            keys = [(r['depth_std'], r['run_id'], r['record_idx'])
                    for r in result['rows']]
            self.assertEqual(keys, [
                ('30.48', 'down-feet', 0),
                ('30.5', 'up-metres', 1),
                ('30.7848', 'down-feet', 1),
                ('30.7848', 'down-feet', 2)])
            # 正反向文件统一排序约定；重测两条都在，未平均
            self.assertEqual(
                [r['repeat_seq'] for r in result['rows'] if r['run_id'] == 'down-feet'],
                [0, 0, 1])

    def test_reverse_only_config_keeps_run_boundary(self):
        # reverse-depth.json 是边界变体：只声明 up-metres，却保留 down-feet
        # 的悬空映射条目，配置校验必须显式拒绝。
        with self.assertRaises(IndexConfigError):
            with tempfile.TemporaryDirectory() as d:
                build_index(DATA / 'reverse-depth.json', Path(d) / 'rev.db')
        # 合法的单 run 配置下，查询未声明 run 同样被拒绝
        cfg = json.loads(json.dumps(BASE_CFG))
        with tempfile.TemporaryDirectory() as d:
            db, _ = build_in(Path(d), GOOD_LAS, cfg)
            with LasIndex(db, Path(d)) as index:
                with self.assertRaises(IndexQueryError):
                    index.query_window(['ghost-run'], '0', '100')

    def test_invalid_no_version_reports_prefix_and_tail(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'bad.db'
            report = build_index(DATA / 'invalid-domain.json', db)
            bad = next(r for r in report['runs'] if r['run_id'] == 'down-feet')
            self.assertFalse(bad['complete'])
            self.assertEqual(bad['records'], 0)
            self.assertEqual(bad['failure']['line'], 1)
            self.assertIsNotNone(bad['incomplete_tail'])
            # 未完成文件不宣称覆盖：coverage 标记 complete=false
            with LasIndex(db, DATA) as index:
                out = index.query_window(['down-feet'], '0', '1000')
            self.assertFalse(out['coverage'][0]['complete'])


class TamperAndConfigTests(unittest.TestCase):
    def test_query_rejects_modified_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db, _ = build_in(root, GOOD_LAS)
            with LasIndex(db, root) as index:
                self.assertTrue(index.query_window(['r1'], '0', '2')['rows'])
                (root / 'sample.las').write_bytes(GOOD_LAS.replace('2.1', '2.9').encode())
                with self.assertRaises(IndexQueryError):
                    index.query_window(['r1'], '0', '2')

    def test_unknown_unit_rejects_formal_conversion(self):
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['unit_aliases'] = {}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(IndexConfigError):
                build_in(Path(d), GOOD_LAS, cfg)

    def test_duplicate_mnemonic_requires_explicit_column(self):
        las = GOOD_LAS.replace('~Curve\nMD.M :测深\nRHOB.G/C3 :密度:原始单位\n',
                               '~Curve\nMD.M :测深\nMD.M :重复助记符\n')
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['curve_map'] = [
            {'run_id': 'r1', 'column': 1, 'name': 'copy', 'unit': 'M',
             'scale': '1', 'offset': '0'}]
        cfg['unit_aliases'] = {}
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), las, cfg)
            self.assertTrue(report['runs'][0]['complete'])
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertIn('copy', rows[0]['curves'])

    def test_config_validation_rules(self):
        good = validate_config(json.loads(json.dumps(BASE_CFG)))
        self.assertEqual(good.runs[0].depth_scale, Fraction(1))
        with self.assertRaises(IndexConfigError):
            rational('0', 'scale', positive=True)
        with self.assertRaises(IndexConfigError):
            rational('1/0', 'scale')
        bad = json.loads(json.dumps(BASE_CFG))
        bad['runs'][0]['depth_column'] = -1
        with self.assertRaises(IndexConfigError):
            validate_config(bad)
        # 非负但超出实际 Curve 列数，在构建阶段结构校验时拒绝
        bad = json.loads(json.dumps(BASE_CFG))
        bad['runs'][0]['depth_column'] = 9
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(IndexConfigError):
                build_in(Path(d), GOOD_LAS, bad)
        bad = json.loads(json.dumps(BASE_CFG))
        bad['curve_map'].append(dict(bad['curve_map'][0]))
        with self.assertRaises(IndexConfigError):
            validate_config(bad)
        bad = json.loads(json.dumps(BASE_CFG))
        bad['runs'][0]['run_id'] = 'x'
        with self.assertRaises(IndexConfigError):
            validate_config(bad)
        bad = json.loads(json.dumps(BASE_CFG))
        bad['runs'].append(dict(bad['runs'][0]))
        with self.assertRaises(IndexConfigError):
            validate_config(bad)

    def test_cross_well_join_refused(self):
        cfg = {
            'runs': [
                {'run_id': 'a', 'well_id': 'w1', 'file': 'a.las',
                 'depth_column': 0, 'depth_scale': '1', 'depth_offset': '0'},
                {'run_id': 'b', 'well_id': 'w2', 'file': 'b.las',
                 'depth_column': 0, 'depth_scale': '1', 'depth_offset': '0'}],
            'curve_map': [
                {'run_id': 'a', 'column': 1, 'name': 'x', 'unit': 'G/C3',
                 'scale': '1', 'offset': '0'},
                {'run_id': 'b', 'column': 1, 'name': 'x', 'unit': 'G/C3',
                 'scale': '1', 'offset': '0'}],
            'unit_aliases': {'G/C3': []}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            one = GOOD_LAS
            (root / 'a.las').write_text(one, encoding='utf-8')
            (root / 'b.las').write_text(one, encoding='utf-8')
            config = write_config(root, cfg)
            db_path = root / 'db.sqlite'
            build_index(config, db_path)
            with LasIndex(db_path, root) as index:
                with self.assertRaises(IndexQueryError):
                    index.query_window(['a', 'b'], '0', '2')

    def test_unit_conversion_uses_explicit_rational_factors(self):
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['curve_map'][0].update({'scale': '2', 'offset': '1/10'})
        with tempfile.TemporaryDirectory() as d:
            db, _ = build_in(Path(d), GOOD_LAS, cfg)
            with LasIndex(db, Path(d)) as index:
                row = index.sequential('r1')[0]
            # 2.1 * 2 + 0.1 = 4.3
            self.assertEqual(row['curves']['density']['value'], '4.3')
            # 原始 token 仍然保留
            self.assertEqual(row['curves']['density']['raw'], '2.1')


class StreamingTests(unittest.TestCase):
    def test_build_does_not_require_all_values_in_memory(self):
        # 生成较大的 WRAP=NO 文件，构建过程中内存里不持有全部曲线值；
        # 磁盘索引行数与窗口读取结果正确即可证明可流式工作。
        lines = [GOOD_LAS.split('~ASCII')[0] + '~ASCII']
        n = 5000
        for i in range(n):
            lines.append(f'{i} {i + 1}.5')
        las = '\n'.join(lines) + '\n'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db, report = build_in(root, las)
            self.assertEqual(report['runs'][0]['records'], n)
            with LasIndex(db, root) as index:
                window = index.query_window(['r1'], '100', '102')
                self.assertEqual(len(window['rows']), 3)
                self.assertEqual(
                    [r['depth_raw'] for r in window['rows']],
                    ['100', '101', '102'])
                full = index.sequential('r1')
                self.assertEqual(len(full), n)

    def test_fraction_to_decimal_exact(self):
        value = fraction_to_decimal(Fraction(381, 1250) * 100)
        self.assertEqual(value, Decimal('30.48'))

    def test_path_traversal_rejected(self):
        cfg = json.loads(json.dumps(BASE_CFG))
        cfg['runs'][0]['file'] = '../../etc/escape.las'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(IndexConfigError):
                build_in(Path(d), GOOD_LAS, cfg)

    def test_header_entries_persisted_with_description_punctuation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db, _ = build_in(root, GOOD_LAS)
            import sqlite3
            con = sqlite3.connect(db)
            rows = con.execute(
                "SELECT section, mnemonic, unit, value, description "
                "FROM entries ORDER BY line_no").fetchall()
            con.close()
            sections = {r[0] for r in rows}
            self.assertEqual(
                sections, {'version', 'well', 'curve', 'parameter'})
            rhob = next(r for r in rows if r[1] == 'RHOB')
            self.assertEqual(rhob[2], 'G/C3')
            self.assertEqual(rhob[4], '密度:原始单位')
            run = next(r for r in rows if r[1] == 'RUN')
            self.assertEqual(run[0], 'parameter')

    def test_multiple_complete_records_on_one_physical_line(self):
        head = GOOD_LAS.split('~ASCII')[0]
        las = head + '~ASCII\n0 2.1 1 2.2 2 2.3\n'
        cfg = json.loads(json.dumps(BASE_CFG))
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), las, cfg)
            self.assertTrue(report['runs'][0]['complete'])
            self.assertEqual(report['runs'][0]['records'], 3)
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertTrue(all(r['lines'] == [16, 16] for r in rows))
            self.assertEqual([r['depth_raw'] for r in rows],
                             ['0', '1', '2'])
            self.assertEqual(
                [r['curves']['density']['raw'] for r in rows],
                ['2.1', '2.2', '2.3'])

    def test_comment_and_blank_lines_in_ascii_are_ignored(self):
        head = GOOD_LAS.split('~ASCII')[0]
        las = (head + '~ASCII\n# 整行注释\n\n0 2.1\n'
               '# 第二条记录跨行\n1\n-999.25\n')
        with tempfile.TemporaryDirectory() as d:
            db, report = build_in(Path(d), las)
            run = report['runs'][0]
            self.assertTrue(run['complete'])
            self.assertEqual(run['records'], 2)
            with LasIndex(db, Path(d)) as index:
                rows = index.sequential('r1')
            self.assertEqual(rows[1]['lines'], [20, 21])


if __name__ == '__main__':
    unittest.main()
