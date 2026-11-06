"""检索层：中文二元组分词 + BM25。

刻意不用 jieba / rank_bm25 / faiss：
- 依赖为零，离线可跑；
- 制度类语料短、术语固定，二元组 + BM25 足够；
- 打分可解释（每个词贡献多少 idf 都能算出来）。
"""

import math
import re
import threading
from typing import Dict, List, Sequence

CLUSTER = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
SATURATION = 2.0       # 把无界的 BM25 压到 [0,1)：score = raw / (raw + K)


def tokenize(text: str) -> List[str]:
    """英文数字整体保留，中文切二元组。"""
    tokens: List[str] = []
    for m in CLUSTER.finditer(text.lower()):
        chunk = m.group()
        if chunk[0].isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return tokens


class BM25:
    """标准 Okapi BM25。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: List[List[str]] = []
        self.df: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.avgdl = 0.0

    def fit(self, corpus: Sequence[Sequence[str]]) -> "BM25":
        self.docs = [list(d) for d in corpus]
        n = len(self.docs)
        self.df = {}
        for doc in self.docs:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1
        # 平滑 idf，避免高频词贡献为负
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in self.df.items()}
        self.avgdl = (sum(len(d) for d in self.docs) / n) if n else 0.0
        return self

    def scores(self, query: Sequence[str]) -> List[float]:
        out: List[float] = []
        for doc in self.docs:
            dl = len(doc) or 1
            tf: Dict[str, int] = {}
            for t in doc:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for term in query:
                if term not in tf:
                    continue
                idf = self.idf.get(term, 0.0)
                f = tf[term]
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(s)
        return out


_lock = threading.Lock()
_index: Dict[str, object] = {"bm25": None, "rows": [], "built": False}


def build(rows: List[Dict]) -> None:
    """rows: [{id, clause_no, title, content}, ...]"""
    corpus = [tokenize(r["title"] + "。" + r["content"]) for r in rows]
    with _lock:
        _index["bm25"] = BM25().fit(corpus)
        _index["rows"] = rows
        _index["built"] = True


def is_built() -> bool:
    return bool(_index["built"])


def search(query: str, k: int = 3) -> List[Dict]:
    bm25: BM25 = _index["bm25"]  # type: ignore[assignment]
    rows = _index["rows"]
    if not bm25 or not rows:
        return []

    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    raw = bm25.scores(q_tokens)
    out: List[Dict] = []
    for row, score in zip(rows, raw):
        # 标题命中给一点加成：制度里"标题就是答案"的条款很常见
        title_tokens = set(tokenize(row["title"]))
        hit = sum(1 for t in set(q_tokens) if t in title_tokens)
        boosted = score * (1.0 + 0.2 * min(hit, 2))
        out.append({
            "id": row["id"],
            "clause_no": row["clause_no"],
            "title": row["title"],
            "content": row["content"],
            "score": round(boosted / (boosted + SATURATION), 4) if boosted > 0 else 0.0,
        })
    out.sort(key=lambda x: (-x["score"], x["clause_no"]))
    return [x for x in out[:k] if x["score"] > 0]
