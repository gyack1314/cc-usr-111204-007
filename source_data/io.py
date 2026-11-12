"""合成资料的严格读取、路径边界与字节身份。"""
import hashlib
import json
from pathlib import Path

def load(path):
    raw=Path(path).read_bytes()
    value=json.loads(raw.decode('utf8'),parse_constant=lambda x:(_ for _ in ()).throw(ValueError('JSON不允许非有限常量')))
    if not isinstance(value,dict):raise ValueError('资料顶层必须为对象')
    return value,hashlib.sha256(raw).hexdigest()

def asset(root,name):
    if not isinstance(name,str) or not name:raise ValueError('资料相对路径为空')
    base=Path(root).resolve();target=(base/name).resolve()
    if not target.is_relative_to(base) or target==base:raise ValueError('资料路径越界')
    raw=target.read_bytes()
    return raw,hashlib.sha256(raw).hexdigest()

def integer(x,minimum=0):
    if type(x) is not int or x<minimum:raise ValueError('整数越界')
    return x

def unique(rows,key):
    found=set()
    for r in rows:
        value=r[key]
        if not isinstance(value,str) or not value or value in found:raise ValueError('身份非空且唯一')
        found.add(value)
    return found
