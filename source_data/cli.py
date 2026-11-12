"""库存摘要、LAS 磁盘索引构建与标准深度窗查询的命令行入口。"""
import argparse
import json
from pathlib import Path

from .domain import summarize
from .index import LasIndex, build_index
from .io import load


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)

    inspect_p = sub.add_parser('inspect', help='显示资料库存与物理行摘要')
    inspect_p.add_argument('file')

    index_p = sub.add_parser('index', help='按映射配置构建 LAS 磁盘索引')
    index_p.add_argument('config')
    index_p.add_argument('database')

    query_p = sub.add_parser('query', help='在磁盘索引上读取标准深度窗')
    query_p.add_argument('database')
    query_p.add_argument('--data-root', required=True)
    query_p.add_argument('--runs', nargs='+', required=True)
    query_p.add_argument('--lo', required=True)
    query_p.add_argument('--hi', required=True)

    seq_p = sub.add_parser('sequential', help='完整顺序抽取某 run 的测点')
    seq_p.add_argument('database')
    seq_p.add_argument('--data-root', required=True)
    seq_p.add_argument('--run', required=True)

    args = parser.parse_args()

    if args.command == 'inspect':
        value, digest = load(args.file)
        _print({'source_sha256': digest,
                'summary': summarize(value, Path(args.file).parent)})
    elif args.command == 'index':
        _print(build_index(args.config, args.database))
    elif args.command == 'query':
        with LasIndex(args.database, Path(args.data_root)) as index:
            _print(index.query_window(args.runs, args.lo, args.hi))
    elif args.command == 'sequential':
        with LasIndex(args.database, Path(args.data_root)) as index:
            _print({'run_id': args.run,
                    'rows': index.sequential(args.run)})


if __name__ == '__main__':
    main()
