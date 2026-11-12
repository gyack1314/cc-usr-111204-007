"""读取测井原始字节并清点物理行。"""
from .io import unique,asset

def summarize(v,root):
    unique(v['runs'],'run_id');rows=[]
    for r in v['runs']:
        raw,digest=asset(root,r['file']);text=raw.decode('utf8')
        if not any(line.strip().upper().startswith('~V') for line in text.splitlines()):raise ValueError('缺少Version段')
        rows.append({'run_id':r['run_id'],'well_id':r['well_id'],'bytes':len(raw),'physical_lines':len(text.splitlines()),'sha256':digest})
    return rows
