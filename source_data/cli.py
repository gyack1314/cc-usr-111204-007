"""显示资料库存与原始字段摘要。"""
import argparse
import json
from pathlib import Path
from .io import load
from .domain import summarize

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['inspect']);p.add_argument('file');a=p.parse_args()
    value,digest=load(a.file)
    print(json.dumps({'source_sha256':digest,'summary':summarize(value,Path(a.file).parent)},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
