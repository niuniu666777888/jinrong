#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
agent_demo/policy.py —— 对账规范检索（BM25 简化版）
================================================================================
生产环境这里是"向量 + BM25 混合检索"（主项目 qa_prototype.py 已实现）。
演示版只留 BM25：规范条款都很短（一句一条），关键词命中已经够用，
而且不引 numpy/faiss，整个 demo 零依赖可跑。

接口保持 search(query) -> [{clause_no, doc_title, content, score}]，
换成混合检索时上层工具一行都不用改。
================================================================================
"""

from __future__ import annotations

import math
import re
from typing import Dict, List

from db import q


def tokenize(text: str) -> List[str]:
    """中文二元组 + 英文数字整词。二元组对"长款/短款"这类术语召回更稳。"""
    text = text.lower()
    tokens: List[str] = []

    for m in re.finditer(r"[a-z][a-z0-9_\-]*|\d+(?:\.\d+)?", text):
        tokens.append(m.group())

    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        seg = m.group()
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))

    return tokens


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: List[Dict[str, int]] = []
        self.lens: List[int] = []
        self.avg_len = 1.0
        self.idf: Dict[str, float] = {}

    def fit(self, corpus: List[List[str]]) -> "BM25":
        n = len(corpus)
        df: Dict[str, int] = {}

        for tokens in corpus:
            freq: Dict[str, int] = {}
            for t in tokens:
                freq[t] = freq.get(t, 0) + 1
            self.docs.append(freq)
            self.lens.append(len(tokens))
            for t in freq:
                df[t] = df.get(t, 0) + 1

        self.avg_len = (sum(self.lens) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        return self

    def scores(self, query: List[str]) -> List[float]:
        out = [0.0] * len(self.docs)
        for term in query:
            idf = self.idf.get(term)
            if not idf:
                continue
            for i, freq in enumerate(self.docs):
                f = freq.get(term, 0)
                if not f:
                    continue
                norm = f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg_len)
                out[i] += idf * f * (self.k1 + 1) / norm
        return out


_INDEX: Dict[str, object] = {}


def _ensure_index() -> Dict[str, object]:
    if _INDEX:
        return _INDEX
    rows = q("SELECT * FROM policy_chunk ORDER BY id")
    corpus = [tokenize(r["doc_title"] + " " + r["content"]) for r in rows]
    _INDEX.update({"rows": rows, "bm25": BM25().fit(corpus)})
    return _INDEX


def search(query: str, top_k: int = 3) -> List[Dict]:
    idx = _ensure_index()
    bm25: BM25 = idx["bm25"]  # type: ignore[assignment]
    rows: List[Dict] = idx["rows"]  # type: ignore[assignment]

    scores = bm25.scores(tokenize(query))
    order = sorted(range(len(rows)), key=lambda i: -scores[i])[:top_k]

    return [
        {
            "clause_no": rows[i]["clause_no"],
            "doc_title": rows[i]["doc_title"],
            "content": rows[i]["content"],
            "effective_from": rows[i]["effective_from"],
            "score": round(scores[i], 3),
        }
        for i in order
        if scores[i] > 0
    ]
