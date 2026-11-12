"""检查已有资料库存、原始读取和虚拟源的基本约定。"""
import json
import tempfile
import unittest
from pathlib import Path
from source_data.io import load,asset,integer,unique
from source_data.domain import summarize
from source_data.replay import Replay

class MaterialTests(unittest.TestCase):
    def test_normal(self):
        original,digest=load('data/normal.json');value=summarize(original,Path('data'))
        self.assertEqual(value,summarize(json.loads(json.dumps(original)),Path('data')))
        self.assertEqual(len(digest),64)
        self.assertEqual(len(value),2);self.assertTrue(all(r['physical_lines']>10 for r in value))
    def test_bad_material(self):
        with self.assertRaises((ValueError,KeyError,TypeError)):summarize(load('data/invalid-domain.json')[0],Path('data'))
    def test_additional_material(self):
        for p in Path('data').glob('*.json'):
            if p.name.startswith('invalid-') or p.name in ('root.json','base.json','normal.json'):continue
            with self.subTest(file=p.name):self.assertIsNotNone(summarize(load(p)[0],p.parent))
    def test_strict_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data.json'
            for raw in ('[]','null','{"a":NaN}'):
                p.write_text(raw,encoding='utf8')
                with self.assertRaises(ValueError):load(p)
            p.write_text('{"中文":2}',encoding='utf8');self.assertEqual(load(p)[0],{'中文':2})
    def test_path_boundary(self):
        with self.assertRaises(ValueError):asset(Path('data'),'../README.md')
        self.assertEqual(asset(Path('.'),'README.md')[1],asset(Path('.'),'README.md')[1])
    def test_replay_copies(self):
        source=Replay([{'operation':'Read','arguments':{'n':2},'response':{'values':[1,2]}}])
        with self.assertRaises(ValueError):source.call('Write',{})
        self.assertEqual(source.remaining(),1)
        result=source.call('Read',{'n':2});result['values'].append(3)
        self.assertEqual(source.steps[0]['response']['values'],[1,2])
        with self.assertRaises(RuntimeError):source.call('Read',{})
    def test_identity_and_integer(self):
        with self.assertRaises(ValueError):unique([{'id':'a'},{'id':'a'}],'id')
        for x in (True,-1,1.5):
            with self.assertRaises(ValueError):integer(x)
