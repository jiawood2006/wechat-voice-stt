#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asr_fix — 语音转写后处理纠错（确定性、零成本、可审计）
=====================================================
只做「高置信度同音/近音错词」替换，规则来自 asr_fix_dict.json；每次替换返回明细，
调用方写入日志。设计原则：
  ① 只替换明确无歧义的错形态（歧义词不进表，如"反应"是合法词，别放进来）
  ② 绝不动数字/金额/日期（宁可不动，不能改错钱）
  ③ 每次替换都可审计（subs 明细），可随时关闭：ASR_FIX=0

为什么不用大模型纠错？实测（见 README「准确率」一节）：换更大模型不提准确率、只变慢；
同音字歧义来自语音通道本身的低码率压缩，用一张可审计的错词表更准、更快、更省钱。

用法：
  python3 asr_fix.py "文本"        # 打印修正后文本
  from asr_fix import fix; fix("文本") -> (修正文本, [("反用","响应"), ...])
"""
import json, os, pathlib, re, sys
from typing import Dict, List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
DICT_PATH = os.environ.get("ASR_FIX_DICT", str(_HERE / "asr_fix_dict.json"))
# 数字/金额/日期保护：这些片段不做任何替换
_NUM = re.compile(r"[0-9０-９]+(?:[.,．][0-9０-９]+)?%?|[一二三四五六七八九十百千万亿]+(?:元|块|万|亿|年|月|日|号)")


def load_dict(path: str = DICT_PATH) -> dict:
    try:
        d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, str)}


def fix(text: str, mapping: Optional[Dict[str, str]] = None) -> Tuple[str, List[tuple]]:
    """返回 (修正文本, 替换明细)。ASR_FIX=0 时原样返回。"""
    if os.environ.get("ASR_FIX", "1") == "0" or not text:
        return text, []
    mapping = mapping if mapping is not None else load_dict()
    if not mapping:
        return text, []

    protected: List[str] = []

    def _shield(m):
        protected.append(m.group(0))
        return f"\x00{len(protected)-1}\x00"

    shielded = _NUM.sub(_shield, text)
    subs = []
    out = shielded
    for wrong, right in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if wrong in out:
            out = out.replace(wrong, right)
            subs.append((wrong, right))

    def _unshield(m):                     # 还原被保护的数字片段
        return protected[int(m.group(1))]

    out = re.sub(r"\x00(\d+)\x00", _unshield, out)
    return out, subs


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    fixed, subs = fix(t)
    print(fixed)
    if subs:
        print(f"# 替换 {len(subs)} 处: {subs}", file=sys.stderr)
