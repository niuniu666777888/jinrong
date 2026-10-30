#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
支付对账与差错追踪系统 · 阶段 1「裸问答」最小原型
================================================================================
目标：验证「检索 → 拼提示词 → 调模型」这条链路本身是否成立，
      不涉及 Web、不涉及数据库、不涉及业务写操作。

流程：
    markdown 文件
        → 按 500 字切片（带重叠）
        → 向量化（可配置：在线 embedding / 本地哈希）
        → 存入 FAISS（未安装时自动回退 numpy 精确检索）
    ------------------------------------------------------------------
    命令行提问
        → BM25（关键词）+ 向量（语义）混合检索
        → 取 Top3 片段
        → 拼进提示词
        → 调用 LLM 生成回答
        → 打印答案 + 引用来源 + 两路分数

--------------------------------------------------------------------------------
【复用清单】——本文件里带 ★ 标记的模块，后续阶段（Agent、工单、API 服务）直接复用
--------------------------------------------------------------------------------
  ★ Config          配置对象：参数集中管理，后续搬到 config.yaml / 环境变量
  ★ LLMClient       大模型调用封装：供应商可切换，业务代码不关心是哪家
  ★ Embedder        向量化封装：同样支持切换供应商，离线可退化为哈希向量
  ★ VectorStore     向量检索：FAISS 优先、numpy 兜底，接口保持不变
  ★ BM25            关键词检索：中文用二元组分词，无需 jieba
  ★ clean_query     查询清洗：剔除疑问词，避免「什么/为什么」压过主体词
  ★ HybridRetriever 混合检索：向量余弦 + BM25 缩放加权 + 标题加成
  ★ split_chunks    切片策略：按标题/段落/句子三层切，带重叠与精确偏移
  ★ PromptBuilder   提示词组装：约束、资料、问题三段式，后续接 Agent 时复用

  以下为演示代码，后续会被 Web 层 / 工单系统替换，不必复用：
     main / repl / print_result

--------------------------------------------------------------------------------
【运行示例】
  # 1) 无 API Key：用本地哈希向量 + 抽取式兜底回答，验证链路是否通
  python qa_prototype.py --file "样本.md" --ask "对账差错分哪几类？"

  # 2) 接在线模型（以 DeepSeek 为例）
  set LLM_PROVIDER=deepseek
  set LLM_API_KEY=sk-xxx
  set LLM_MODEL=deepseek-chat
  set EMB_PROVIDER=openai
  set EMB_BASE_URL=https://api.siliconflow.cn/v1
  set EMB_API_KEY=sk-xxx
  set EMB_MODEL=BAAI/bge-m3
  python qa_prototype.py --file "样本.md"

  # 3) 交互模式（不带 --ask 即进入 REPL，输入 exit 退出）
  python qa_prototype.py --file "样本.md"

【依赖】
  numpy（必需，向量运算）；faiss-cpu（可选，装了自动启用）
  其余全部使用标准库，不依赖 requests / openai / jieba / rank_bm25
================================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# ==============================================================================
# ★ 核心模块 0：全局常量
# ==============================================================================
# 这些数字放在文件顶部，是为了让"切片大小""TopK""融合权重"这类关键参数一眼可见。
# 后续阶段应该把它们挪到配置文件里，这里先用常量把语义固定下来。

CHUNK_SIZE = 500      # 每个切片的目标字数：500 字是中文检索的常用折中值
                      #   太小 → 语义被切碎，模型拿不到上下文
                      #   太大 → 一个切片混进多个主题，检索命中率下降
CHUNK_OVERLAP = 60    # 相邻切片的重叠字数：防止一句话正好被切断，导致两边都检索不到
TOP_K = 3             # 送给模型的片段数量：3 条约 1500 字，足够回答单个事实问题
BM25_WEIGHT = 0.6     # 混合检索中 BM25 的权重，向量权重 = 1 - 该值
                      #   支付对账是术语密集场景（长款、短款、调单、联机退货），
                      #   精确词命中比语义泛化更可靠，所以 BM25 给到 6 成。
                      #   接入真实 embedding 后建议调回 0.5；用 --bm25-weight 可随时对比。


# ==============================================================================
# ★ 核心模块 1：配置
# ==============================================================================
@dataclass
class Config:
    """
    全部可调参数集中在这里。

    设计意图：
      1. 业务代码只依赖 Config 对象，不直接读 os.environ，方便测试时构造假配置；
      2. 供应商切换只改配置，不改调用方代码——这是"可配置"的关键；
      3. 后续接 yaml / 配置中心时，只需要改 from_env 这一个函数。
    """

    # ---- LLM 相关 ----
    llm_provider: str = "mock"      # mock / openai / deepseek / ollama
    llm_base_url: str = ""          # 形如 https://api.deepseek.com/v1 ，末尾不要带斜杠
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 60
    llm_temperature: float = 0.2    # 问答场景要稳定，温度调低

    # ---- 向量化相关 ----
    emb_provider: str = "hash"      # openai（任何 OpenAI 兼容的 /embeddings 接口）/ hash（本地）
    emb_base_url: str = ""
    emb_api_key: str = ""
    emb_model: str = ""
    emb_dim: int = 512              # 仅 hash 模式使用；在线模式以接口返回维度为准

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量读取配置；没配的项走默认值，保证零配置也能跑。"""
        env = os.environ.get
        return cls(
            llm_provider=env("LLM_PROVIDER", "mock").lower(),
            llm_base_url=env("LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=env("LLM_API_KEY", ""),
            llm_model=env("LLM_MODEL", ""),
            llm_timeout=int(env("LLM_TIMEOUT", "60")),
            llm_temperature=float(env("LLM_TEMPERATURE", "0.2")),
            emb_provider=env("EMB_PROVIDER", "hash").lower(),
            emb_base_url=env("EMB_BASE_URL", "").rstrip("/"),
            emb_api_key=env("EMB_API_KEY", ""),
            emb_model=env("EMB_MODEL", ""),
            emb_dim=int(env("EMB_DIM", "512")),
        )


# ==============================================================================
# ★ 核心模块 2：HTTP 小工具
# ==============================================================================
def _post_json(url: str, payload: dict, api_key: str = "", timeout: int = 60) -> dict:
    """
    发送 JSON POST 请求并解析返回。

    为什么不用 requests：原型阶段不想引入第三方依赖，
    urllib 是标准库，够用；后续工程化时替换成 httpx 即可，调用点只有这一处。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 把服务端的错误原文带出来，否则只看到 400 会非常难排查
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} 调用失败：{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络不可达：{e.reason}") from e


# ==============================================================================
# ★ 核心模块 3：LLM 调用封装（供应商可配置）
# ==============================================================================
class LLMClient:
    """
    【核心】所有大模型调用的唯一入口。

    对外只暴露一个方法：chat(messages) -> str
    内部按 provider 分发到不同的实现：
      - openai / deepseek：走 OpenAI 兼容协议（绝大多数国内外厂商都支持）
      - ollama：本地部署，走 /api/chat
      - mock：没有 Key 时的兜底，用于验证链路，不产生真实推理

    后续阶段（Agent 编排、工具调用）只需在这里扩展 messages 结构，
    不必改动检索层和业务层。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.provider = cfg.llm_provider
        self.call_count = 0          # 简单计数：后续做成本核算时从这里扩展

        # 启动时就校验一次配置，避免用户问完问题才报错
        if self.provider in ("openai", "deepseek") and not cfg.llm_api_key:
            print("[提示] 未检测到 LLM_API_KEY，自动降级为 mock 模式（抽取式回答）")
            self.provider = "mock"
        if self.provider in ("openai", "deepseek") and not cfg.llm_base_url:
            # 给一个合理默认值，减少配置负担
            self.cfg.llm_base_url = "https://api.deepseek.com/v1" if self.provider == "deepseek" else ""

    # ------------------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]], temperature: Optional[float] = None) -> str:
        """
        统一入口。messages 采用 OpenAI 的格式：[{"role": "system"/"user", "content": "..."}]
        返回模型的纯文本回答。
        """
        self.call_count += 1
        temp = self.cfg.llm_temperature if temperature is None else temperature

        if self.provider == "mock":
            return self._chat_mock(messages)

        if self.provider in ("openai", "deepseek"):
            return self._chat_openai(messages, temp)

        if self.provider == "ollama":
            return self._chat_ollama(messages, temp)

        raise ValueError(f"未知的 LLM_PROVIDER：{self.provider}")

    # ------------------------------------------------------------------
    def _chat_openai(self, messages: List[Dict[str, str]], temperature: float) -> str:
        """OpenAI 兼容协议。DeepSeek、通义、Kimi、vLLM、One-API 网关都走这个分支。"""
        url = f"{self.cfg.llm_base_url}/chat/completions"
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,        # 原型阶段不做流式；后续 Web 层再改成 SSE
        }
        data = _post_json(url, payload, self.cfg.llm_api_key, self.cfg.llm_timeout)

        # 兼容两种常见返回结构，避免某家厂商字段略有差异就崩
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
        if "output" in data:        # 少数网关的兼容层
            return str(data["output"]).strip()
        raise RuntimeError(f"无法解析模型返回：{json.dumps(data, ensure_ascii=False)[:300]}")

    # ------------------------------------------------------------------
    def _chat_ollama(self, messages: List[Dict[str, str]], temperature: float) -> str:
        """本地 Ollama。适合内网私有化场景——支付数据不出内网时走这条路。"""
        base = self.cfg.llm_base_url or "http://127.0.0.1:11434"
        payload = {
            "model": self.cfg.llm_model or "qwen2.5:7b",
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        data = _post_json(f"{base}/api/chat", payload, "", self.cfg.llm_timeout)
        return data.get("message", {}).get("content", "").strip()

    # ------------------------------------------------------------------
    def _chat_mock(self, messages: List[Dict[str, str]]) -> str:
        """
        兜底实现：从提示词里把资料原文抽出来，拼成"带引用的摘录"。

        这样做的价值：在没有 API Key 的机器上，也能完整验证
        「检索是否命中 → 提示词是否拼对 → 引用是否可追溯」三件事。
        它不产生任何推理，纯粹是占位，不要把它的输出当成产品能力。
        """
        user_msg = ""
        for m in reversed(messages):
            if m["role"] == "user":
                user_msg = m["content"]
                break

        # 约定：提示词里资料块用【资料开始】/【资料结束】包裹，这里据此截取
        m = re.search(r"【资料开始】(.*?)【资料结束】", user_msg, re.S)
        context = m.group(1).strip() if m else ""
        question = ""
        m2 = re.search(r"【问题】(.*)$", user_msg, re.S)
        if m2:
            question = m2.group(1).strip()

        if not context:
            return "（mock 模式）没有检索到可用资料，请检查文件路径与切片配置。"

        # 抽取式：保留资料前 400 字，让使用者能肉眼确认检索结果是否正确
        snippet = context[:400].replace("\n", " ")
        return (
            "（当前为 mock 模式，未接入真实模型，以下为检索到的原文摘录）\n\n"
            f"问题：{question}\n\n"
            f"相关原文：{snippet}……\n\n"
            "如需生成式回答，请配置 LLM_PROVIDER / LLM_API_KEY / LLM_MODEL 后重试。"
        )


# ==============================================================================
# ★ 核心模块 4：向量化封装
# ==============================================================================
class Embedder:
    """
    【核心】文本 → 向量的唯一入口。

    两种实现：
      openai：调用任意 OpenAI 兼容的 /embeddings 接口（bge-m3、text-embedding-3 等）
      hash  ：本地哈希向量，无需网络，用于离线跑通链路

    hash 模式的原理（够用但不精）：
      把中文按二元组分词 → 每个词哈希到固定维度的桶里 → 计数 → L2 归一化。
      它捕获的是"字面共现"，不是真正语义；但足以证明检索链路是通的。
      生产环境必须换成真实 embedding 模型。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.provider = cfg.emb_provider
        self.dim = cfg.emb_dim
        self.idf: Dict[str, float] = {}   # 仅 hash 模式使用：语料 IDF 权重表

        if self.provider == "openai" and not (cfg.emb_api_key and cfg.emb_base_url):
            print("[提示] 在线 embedding 配置不全，回退到本地 hash 向量")
            self.provider = "hash"

    # ------------------------------------------------------------------
    def fit(self, corpus_texts: List[str]) -> "Embedder":
        """
        统计语料 IDF 权重（仅 hash 模式有效，在线模型为空操作）。

        为什么需要这一步：直接用词频做向量时，"对账""差错"这类处处出现的词
        权重最高，会把真正有区分度的词（"长款""短款""调单"）淹没，
        导致主题不同的片段分数拉不开。乘上 IDF 之后，通用高频词被压下去，
        向量才真正具备区分能力。在线 embedding 模型已带语义训练，不需要这一步。
        """
        if self.provider != "hash":
            return self

        n = len(corpus_texts)
        df: Dict[str, int] = {}
        for t in corpus_texts:
            for tok in set(tokenize(t)):
                df[tok] = df.get(tok, 0) + 1

        # 平滑 IDF：log((N+1)/(df+1)) + 1，保证未登录词也有正权重
        self.idf = {tok: math.log((n + 1) / (c + 1)) + 1.0 for tok, c in df.items()}
        return self

    # ------------------------------------------------------------------
    def encode(self, texts: List[str]) -> np.ndarray:
        """批量向量化，返回 float32 的 (n, dim) 矩阵，且每行已 L2 归一化。"""
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")

        if self.provider == "openai":
            return self._encode_openai(texts)
        return self._encode_hash(texts)

    # ------------------------------------------------------------------
    def _encode_openai(self, texts: List[str]) -> np.ndarray:
        """调用在线接口。注意：不同厂商一次能传的条数不同，这里分批避免超限。"""
        batch = 32
        all_vecs: List[List[float]] = []

        for i in range(0, len(texts), batch):
            payload = {"model": self.cfg.emb_model, "input": texts[i:i + batch]}
            data = _post_json(
                f"{self.cfg.emb_base_url}/embeddings",
                payload,
                self.cfg.emb_api_key,
                self.cfg.llm_timeout,
            )
            # 接口返回的 data 顺序与输入一致，按 index 排序更稳妥
            items = sorted(data["data"], key=lambda x: x.get("index", 0))
            all_vecs.extend([it["embedding"] for it in items])

        arr = np.asarray(all_vecs, dtype="float32")
        self.dim = arr.shape[1]
        return self._l2_normalize(arr)

    # ------------------------------------------------------------------
    def _encode_hash(self, texts: List[str]) -> np.ndarray:
        """本地哈希向量：不依赖任何模型，纯 CPU，毫秒级。"""
        out = np.zeros((len(texts), self.dim), dtype="float32")

        for row, text in enumerate(texts):
            for token in tokenize(text):
                # 用 Python 内置 hash 不稳定（进程间会变），改用 zlib.crc32 保证可复现
                idx = _stable_hash(token) % self.dim
                # 按 IDF 加权：有区分度的词贡献大，通用词贡献小
                out[row, idx] += self.idf.get(token, 1.0)

        return self._l2_normalize(out)

    # ------------------------------------------------------------------
    @staticmethod
    def _l2_normalize(arr: np.ndarray) -> np.ndarray:
        """按行 L2 归一化：归一化后，向量内积 == 余弦相似度，可直接用内积索引。"""
        norm = np.linalg.norm(arr, axis=1, keepdims=True)
        norm[norm == 0] = 1.0          # 全零行（空文本）保护，避免除零
        return (arr / norm).astype("float32")


def _stable_hash(text: str) -> int:
    """稳定的字符串哈希，保证同一份文档每次运行都映射到同样的桶。"""
    import zlib
    return zlib.crc32(text.encode("utf-8"))


# ==============================================================================
# ★ 核心模块 5：分词
# ==============================================================================
def tokenize(text: str) -> List[str]:
    """
    轻量分词：中文用「二元组」，英文/数字用「整词」。

    为什么不用 jieba：
      1. 少一个依赖，原型能直接跑；
      2. 支付对账领域的专有名词（长款、短款、联机退货）在通用词典里往往切错，
         二元组反而更稳，召回不会漏。

    代价：会产生一些无意义组合（"款短"），但 BM25 的 IDF 会自然压低它们的权重。
    """
    text = text.lower()
    tokens: List[str] = []

    # 英文、数字、带小数点/下划线的编号（如 TX20261030001、0.01）
    for m in re.finditer(r"[a-z][a-z0-9_\-\.]*|\d+(?:\.\d+)?", text):
        tokens.append(m.group())

    # 连续中文 → 二元组；单字成词时保留单字，避免"款"这类查询全部落空
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        seg = m.group()
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])

    return tokens


# ==============================================================================
# ★ 核心模块 6：BM25 检索
# ==============================================================================
class BM25:
    """
    【核心】经典 BM25 实现（Okapi 变体），约 40 行，无需 rank_bm25。

    公式要点：
      score = Σ over 查询词  IDF(t) * (f(t,d) * (k1+1)) / (f(t,d) + k1*(1 - b + b*len(d)/avg_len))
      - IDF 抑制高频词（"的""是"这类二元组）的干扰
      - b 控制文档长度归一化强度，0.75 是通用默认值
    参数 k1=1.5、b=0.75 是多年实践下来的通用取值，中文场景同样适用。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_freqs: List[Dict[str, int]] = []   # 每篇文档的词频表
        self.doc_len: List[int] = []                # 每篇文档的词数
        self.avg_len: float = 0.0
        self.idf: Dict[str, float] = {}
        self.n_docs: int = 0

    # ------------------------------------------------------------------
    def fit(self, corpus_tokens: List[List[str]]) -> "BM25":
        """建索引：统计词频、文档频率、平均长度、IDF。"""
        self.n_docs = len(corpus_tokens)
        df: Dict[str, int] = {}

        for tokens in corpus_tokens:
            freq: Dict[str, int] = {}
            for t in tokens:
                freq[t] = freq.get(t, 0) + 1
            self.doc_freqs.append(freq)
            self.doc_len.append(len(tokens))
            for t in freq:
                df[t] = df.get(t, 0) + 1

        self.avg_len = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0

        # 平滑版 IDF：log(1 + (N - df + 0.5) / (df + 0.5))，恒为正数，避免负分干扰排序
        for t, n in df.items():
            self.idf[t] = float(np.log(1 + (self.n_docs - n + 0.5) / (n + 0.5)))

        return self

    # ------------------------------------------------------------------
    def scores(self, query_tokens: List[str]) -> np.ndarray:
        """返回每个文档的 BM25 得分数组。"""
        result = np.zeros(self.n_docs, dtype="float64")
        if not self.n_docs or not query_tokens:
            return result

        for q in query_tokens:
            idf = self.idf.get(q)
            if idf is None:         # 查询词不在语料里，跳过（IDF 为 0 不影响排序）
                continue
            for i, freq in enumerate(self.doc_freqs):
                f = freq.get(q, 0)
                if f == 0:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avg_len or 1.0))
                result[i] += idf * (f * (self.k1 + 1)) / denom

        return result


# ==============================================================================
# ★ 核心模块 7：向量索引
# ==============================================================================
class VectorStore:
    """
    【核心】向量索引的薄封装。

    对外接口只有 build / search 两个方法，内部实现可换：
      - 装了 faiss-cpu：用 IndexFlatIP（内积 == 余弦，因为向量已归一化）
      - 没装 faiss：用 numpy 矩阵乘法暴力算，几万条以内完全够用

    这样写的好处：上层检索逻辑完全不感知底层是 FAISS 还是 numpy，
    后期数据量涨到百万级，只要把这里换成 IVF/HNSW 索引即可，调用方零改动。
    """

    def __init__(self):
        self.backend = "numpy"      # 实际使用的后端，便于启动时打印
        self.index = None
        self.matrix: Optional[np.ndarray] = None
        self.dim = 0

    # ------------------------------------------------------------------
    def build(self, vectors: np.ndarray) -> None:
        """用全部切片向量建索引。"""
        if vectors.size == 0:
            raise ValueError("向量为空，无法建索引")

        self.dim = vectors.shape[1]

        try:
            import faiss  # 延迟导入：没装也不影响其他功能
            index = faiss.IndexFlatIP(self.dim)     # IP = Inner Product，归一化后等价于余弦
            index.add(vectors)
            self.index = index
            self.backend = "faiss"
        except ImportError:
            # 回退方案：把矩阵留在内存里，检索时做一次矩阵乘法
            self.matrix = vectors
            self.backend = "numpy"

    # ------------------------------------------------------------------
    def search(self, query_vec: np.ndarray, k: int) -> Tuple[np.ndarray, np.ndarray]:
        """返回 (得分数组, 下标数组)，均按得分从高到低排列。"""
        q = query_vec.reshape(1, -1).astype("float32")

        if self.backend == "faiss":
            scores, idx = self.index.search(q, min(k, self.index.ntotal))
            return scores[0], idx[0]

        sims = (self.matrix @ q.T).ravel()          # (n,) 每条切片的余弦相似度
        order = np.argsort(-sims)[:k]
        return sims[order], order


# ==============================================================================
# ★ 核心模块 8：文档切片
# ==============================================================================
def load_markdown(path: str) -> str:
    """读取 markdown。编码按 utf-8 优先，失败再退 gbk（Windows 老文件常见）。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _trim_span(text: str, s: int, e: int) -> Tuple[int, int]:
    """去掉区间首尾的空白，返回收缩后的 (start, end)。"""
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def _paragraph_spans(text: str) -> List[Tuple[int, int]]:
    """按空行切出段落区间，返回原文中的 (start, end)。"""
    bounds = [0]
    for m in re.finditer(r"\n[ \t]*\n", text):
        bounds.append(m.start())
        bounds.append(m.end())
    bounds.append(len(text))

    spans: List[Tuple[int, int]] = []
    for k in range(0, len(bounds) - 1, 2):
        s, e = _trim_span(text, bounds[k], bounds[k + 1])
        if e > s:
            spans.append((s, e))
    return spans


def _section_spans(text: str) -> List[Tuple[int, int]]:
    """
    按 markdown 标题（# / ## / ###）切出章节区间。

    为什么要在标题处强制断开：
      标题是作者写下的"语义分界"。让切片跨越标题，等于把两个主题
      塞进同一个向量，检索时两边都不够像，命中率会明显下降。
      实测中，"差错分类"这一节被硬切到一半、下一片又从上一张表格
      中间开始时，排名会排在"流程"那节后面——这就是切分方式带来的损失。
    """
    starts = [m.start() for m in re.finditer(r"(?m)^#{1,6}\s", text)]
    if not starts:
        s, e = _trim_span(text, 0, len(text))
        return [(s, e)] if e > s else []

    spans: List[Tuple[int, int]] = []
    # 第一个标题之前的正文，单独成段
    if starts[0] > 0:
        s, e = _trim_span(text, 0, starts[0])
        if e > s:
            spans.append((s, e))

    for idx, st in enumerate(starts):
        en = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        s, e = _trim_span(text, st, en)
        if e > s:
            spans.append((s, e))
    return spans


def _sentence_spans(text: str, s: int, e: int) -> List[Tuple[int, int]]:
    """超长段落按句末标点再切一次，切成不超过 size 的句子单元。"""
    spans: List[Tuple[int, int]] = []
    pattern = r"[^。！？；;\.\!\?\n]+[。！？；;\.\!\?]?"
    for m in re.finditer(pattern, text[s:e]):
        a, b = _trim_span(text, s + m.start(), s + m.end())
        if b > a:
            spans.append((a, b))
    return spans or [(s, e)]


def _is_heading_only(text: str) -> bool:
    """判断切片是否只有一行 markdown 标题、没有任何正文。"""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return len(lines) == 1 and lines[0].lstrip().startswith("#")


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
                 ) -> List[Tuple[str, int, int]]:
    """
    【核心】按 markdown 章节 + 目标字数切片，返回 (片段文本, 起始偏移, 结束偏移)。

    三层切分，优先级从高到低：
      1. 标题：章节是最可靠的语义边界，绝不跨章节硬切；
      2. 段落：章节内按空行分段，保证"一段完整进一个片"；
      3. 句子：段落本身超过 size 时才动刀，且只在句末切，不从句子中间切断。

    重叠只发生在"章节内部被迫切片"的场景：下一片从上一片结尾回退 overlap 字，
    保证被切断的那句话两头都能被检索到。章节之间不加重叠——
    加了只会把上一节的尾巴带进下一节，反而污染检索。

    偏移量直接来自原文下标，因此引用可以精确指到"第几个字"，
    人工核对时不用再猜这段具体在原文哪里。
    """
    # 统一换行符，避免 \r\n 干扰偏移计算
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs = _paragraph_spans(text)
    sections = _section_spans(text)

    chunks: List[Tuple[str, int, int]] = []

    for sec_start, sec_end in sections:
        # 收集落在本章节内的段落
        inside = [(a, b) for (a, b) in paragraphs if a >= sec_start and b <= sec_end]
        if not inside:
            inside = [(sec_start, sec_end)]

        # 短章节：整节就是一个切片，边界最干净
        if sec_end - sec_start <= size:
            chunk_text = text[sec_start:sec_end].strip()
            if chunk_text:
                chunks.append((chunk_text, sec_start, sec_end))
            continue

        # 长章节：先把超长段落拆成句子，再按 size 开窗
        units: List[Tuple[int, int]] = []
        for a, b in inside:
            if b - a <= size:
                units.append((a, b))
            else:
                units.extend(_sentence_spans(text, a, b))

        i = 0
        start: Optional[int] = None
        while i < len(units):
            if start is None:
                start = units[i][0]

            end = start
            j = i
            while j < len(units) and units[j][1] - start <= size:
                end = units[j][1]
                j += 1

            # 兜底：单个单元本身超长，硬切
            if j == i:
                end = min(start + size, sec_end)
                j = i + 1

            chunk_text = text[start:end].strip()
            # 只有标题、没有正文的切片直接丢弃：
            # 它长度极短，向量里只含几个字，余弦相似度会被偶然共现的词拉满，
            # 在检索结果里冒充高相关片段（实测确实会排到第一位）。
            if chunk_text and not _is_heading_only(chunk_text):
                chunks.append((chunk_text, start, end))

            if j >= len(units):
                break

            candidate = end - overlap
            start = candidate if candidate > start else start + 1
            i = j

    return chunks


# ==============================================================================
# ★ 核心模块 9：查询清洗
# ==============================================================================
QUERY_STOP_PHRASES = (
    "是什么意思", "什么意思", "是什么", "有哪些", "有什么", "为什么",
    "怎么样", "怎么办", "是不是", "是否", "多少", "哪些", "哪个",
    "如何", "怎么", "请问", "什么",
)


def clean_query(q: str) -> str:
    """
    去掉疑问词，只留下问题的主体词。

    为什么必须做：中文二元组分词下，「什么」「为什么」这类词在语料里遍地都是，
    问一句「长款是什么意思」会切出「是什」「什么」「么意」「意思」四个高频二元组，
    它们把真正的关键词「长款」的权重完全淹没，检索结果会跑到毫不相干的段落上。
    注意：只在查询侧清洗，文档侧不动——文档里出现这些词是正常的。
    """
    for phrase in QUERY_STOP_PHRASES:
        q = q.replace(phrase, " ")
    return q


# ==============================================================================
# ★ 核心模块 10：混合检索
# ==============================================================================
class HybridRetriever:
    """
    【核心】BM25 + 向量 的混合检索器，是"检索层"对外呈现的最终形态。

    融合方式：加权求和，但两路的量纲不同，必须分别处理：
      - 向量分是余弦相似度，天生在 [0,1]，直接用即可，不再缩放；
      - BM25 分没有上界（可能 30 也可能 0.2），按本批最大值缩放到 [0,1]。

    为什么不用 min-max 统一处理：min-max 会把最差的一条压成 0、最好的一条拉成 1，
    哪怕整库都不相关，也会造出一个 1.0 的假高分，使用者看不出这次没检索到东西。
    按最大值缩放则保留绝对强度，全库都弱时融合分自然低。

    另外加一层标题加成：切片第一行是章节标题，若命中 2 个以上查询词，
    说明整节主题相关，给最多 30% 的加权——避免正文里某个词重复出现多次
    压过标题就是答案的章节。实测这条对「差错分哪几类」这类问题影响很大。
    """

    def __init__(self, chunks: List[Tuple[str, int, int]], vectors: np.ndarray,
                 embedder: Embedder, store: VectorStore, bm25: BM25,
                 bm25_weight: float = BM25_WEIGHT):
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.store = store
        self.bm25 = bm25
        self.bm25_weight = bm25_weight

    # ------------------------------------------------------------------
    def search(self, query: str, k: int = TOP_K) -> List[Dict]:
        """返回 TopK 片段，每条带文本、偏移、以及两路各自的原始分与融合分。"""
        # 两路都必须检索全量，才能做可比的归一化
        n = len(self.chunks)
        q_clean = clean_query(query)      # 去掉疑问词，避免噪声词主导排序
        q_vec = self.embedder.encode([q_clean])[0]
        vec_scores, vec_idx = self.store.search(q_vec, k=n)

        # search 返回的是排好序的子集，需要还原成按文档顺序的满长度数组
        vec_full = np.zeros(n, dtype="float64")
        vec_full[vec_idx] = vec_scores

        bm_full = self.bm25.scores(tokenize(q_clean))

        # 向量分本身就是余弦相似度，已经落在 [0,1]，直接用，不要再做归一化。
        # 一旦用 min-max，全库都不相关时也会造出一个 1.0 的假高分。
        vec_norm = np.clip(vec_full, 0.0, 1.0)
        # BM25 分数没有上界（可能 30 也可能 0.2），按本批最大值缩放到 [0,1]
        bm_norm = _maxnorm(bm_full)

        # 标题加权：切片的第一行是章节标题，如果标题里命中 2 个以上查询词，
        # 说明这一节整体讲的就是这件事，给最多 30% 的加成。
        # 不加这个，正文里某个高频词出现五六次的段落会压过「标题就是答案」的章节。
        q_tokens = set(tokenize(q_clean))
        for i, (text, _, _) in enumerate(self.chunks):
            first_line = text.strip().split("\n", 1)[0]
            hits = sum(1 for t in q_tokens if t in first_line)
            if hits >= 2:
                bm_norm[i] *= 1.0 + 0.3 * min(hits, 3) / 3.0

        fused = self.bm25_weight * bm_norm + (1 - self.bm25_weight) * vec_norm

        # 取 TopK：稳定排序（同分时保持原始顺序，避免每次运行结果抖动）
        top_idx = np.argsort(-fused, kind="stable")[:k]

        results: List[Dict] = []
        for i in top_idx:
            text, start, end = self.chunks[int(i)]
            results.append({
                "id": int(i),
                "text": text,
                "start": start,
                "end": end,
                "score": float(fused[i]),
                "vec_score": float(vec_norm[i]),
                "bm25_score": float(bm_norm[i]),
            })
        return results


def _maxnorm(arr: np.ndarray) -> np.ndarray:
    """
    按最大值缩放到 [0,1]。

    与 min-max 的差别很关键：min-max 会把最差的一条压成 0、最好的一条拉成 1，
    哪怕全库都不相关，也会凭空造出一个 1.0 的强信号；
    按最大值缩放保留绝对强度——全库都弱时所有分数都低，
    融合分自然低，使用者一眼能看出「这次没检索到靠谱内容」。
    """
    m = float(arr.max()) if arr.size else 0.0
    if m <= 1e-9:
        return np.zeros_like(arr, dtype="float64")
    return arr / m


# ==============================================================================
# ★ 核心模块 11：提示词组装
# ==============================================================================
class PromptBuilder:
    """
    【核心】把检索结果拼成提示词。

    三条约束写死在 system 里，这是"可解释"的底线：
      1. 只依据资料回答，不许自由发挥；
      2. 资料不足要明说，不许编；
      3. 引用要标编号，便于人工回查原文。

    后续接 Agent（工具调用、工单操作）时，system 提示词会变长，
    但这个"约束 + 资料 + 问题"的三段式结构可以直接沿用。
    """

    SYSTEM = (
        "你是支付对账与差错追踪领域的业务助手。\n"
        "回答规则：\n"
        "1. 只能依据【资料】中的内容作答，不得引入资料以外的知识；\n"
        "2. 如果资料不足以回答，直接说明「资料中未提及」，不要推测；\n"
        "3. 引用资料时标注编号，例如 [1][2]；\n"
        "4. 回答简洁，先说结论，再补充依据。"
    )

    @classmethod
    def build(cls, question: str, hits: List[Dict]) -> List[Dict[str, str]]:
        """返回 OpenAI 格式的 messages。"""
        lines: List[str] = []
        for i, h in enumerate(hits, 1):
            # 截断超长片段，防止单条资料挤爆上下文（500 字 * 3 一般不会触发）
            body = h["text"][:1200]
            lines.append(f"[{i}]（第 {h['start']}–{h['end']} 字）{body}")

        user = (
            "【资料开始】\n"
            + "\n\n".join(lines)
            + "\n【资料结束】\n\n"
            + f"【问题】{question}"
        )

        return [
            {"role": "system", "content": cls.SYSTEM},
            {"role": "user", "content": user},
        ]


# ==============================================================================
# 演示层：装配、打印、REPL（这部分后续会被 Web 层替换，不必复用）
# ==============================================================================
def build_engine(file_path: str, cfg: Config, chunk_size: int = CHUNK_SIZE,
                 bm25_weight: float = BM25_WEIGHT, topk: int = TOP_K) -> Tuple[HybridRetriever, Dict]:
    """装配整条链路：读文件 → 切片 → 向量化 → 建索引 → 建检索器。"""
    t0 = time.time()

    text = load_markdown(file_path)
    if not text.strip():
        raise SystemExit(f"文件为空：{file_path}")

    chunks = split_chunks(text, size=chunk_size)
    if not chunks:
        raise SystemExit("切片结果为空，请检查文件内容")

    embedder = Embedder(cfg)
    chunk_texts = [c[0] for c in chunks]
    embedder.fit(chunk_texts)      # hash 模式：先统计 IDF；在线模式：空操作
    vectors = embedder.encode(chunk_texts)

    store = VectorStore()
    store.build(vectors)

    bm25 = BM25().fit([tokenize(c[0]) for c in chunks])

    retriever = HybridRetriever(chunks, vectors, embedder, store, bm25,
                                bm25_weight=bm25_weight)

    stats = {
        "chars": len(text),
        "chunks": len(chunks),
        "dim": int(vectors.shape[1]),
        "vector_backend": store.backend,
        "emb_provider": embedder.provider,
        "build_seconds": round(time.time() - t0, 2),
        "bm25_weight": bm25_weight,
        "topk": topk,
    }
    return retriever, stats


def print_banner(file_path: str, stats: Dict, llm: LLMClient) -> None:
    """打印启动信息。把关键参数亮出来，避免"为什么检索不准"变成玄学排查。"""
    print("=" * 72)
    print("支付对账与差错追踪系统 · 阶段 1 裸问答原型")
    print("=" * 72)
    print(f"语料文件   : {file_path}")
    print(f"语料规模   : {stats['chars']} 字 → {stats['chunks']} 个切片")
    print(f"向量维度   : {stats['dim']}（{stats['emb_provider']}）")
    print(f"向量后端   : {stats['vector_backend']}（faiss 优先，缺失自动回退 numpy）")
    print(f"大模型     : {llm.provider} {llm.cfg.llm_model}".rstrip())
    print(f"建索引耗时 : {stats['build_seconds']} 秒")
    w = stats["bm25_weight"]
    print(f"检索策略   : BM25 {w:.0%} + 向量 {1 - w:.0%}，取 Top{stats['topk']}")
    print("=" * 72)


def print_result(question: str, hits: List[Dict], answer: str, elapsed: float) -> None:
    """打印答案与引用来源。引用里带分数与偏移，方便判断"是检索错了还是模型答错了"。"""
    print(f"\n问题：{question}")
    print("-" * 72)
    print("【回答】")
    print(answer)
    print("-" * 72)
    print(f"【引用片段】（耗时 {elapsed:.2f}s）")
    for i, h in enumerate(hits, 1):
        preview = h["text"][:80].replace("\n", " ")
        print(f"  [{i}] 融合 {h['score']:.3f} | 向量 {h['vec_score']:.3f} | "
              f"BM25 {h['bm25_score']:.3f} | 第 {h['start']}–{h['end']} 字")
        print(f"      {preview}……")
    print("=" * 72)


def repl(retriever: HybridRetriever, llm: LLMClient, topk: int = TOP_K) -> None:
    """交互模式：不断读问题、检索、回答。输入 exit / quit / 空行退出。"""
    print("\n进入交互模式，直接输入问题；输入 exit 退出。\n")
    while True:
        try:
            question = input("问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            print("已退出。")
            break

        t0 = time.time()
        hits = retriever.search(question, topk)
        messages = PromptBuilder.build(question, hits)

        try:
            answer = llm.chat(messages)
        except Exception as e:      # 网络/配额/配置错误不应该让整个会话崩掉
            answer = f"[调用失败] {e}"

        print_result(question, hits, answer, time.time() - t0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="阶段 1 裸问答原型：markdown → 切片 → 混合检索 → LLM 回答",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", "-f", required=True, help="待检索的 markdown 文件路径")
    parser.add_argument("--ask", "-a", default=None, help="只问一个问题然后退出（便于脚本化测试）")
    parser.add_argument("--topk", type=int, default=TOP_K, help=f"返回片段数，默认 {TOP_K}")
    parser.add_argument("--chunk", type=int, default=CHUNK_SIZE, help=f"切片字数，默认 {CHUNK_SIZE}")
    parser.add_argument("--bm25-weight", type=float, default=BM25_WEIGHT,
                        help=f"BM25 权重（0~1），默认 {BM25_WEIGHT}；术语密集场景可调高")
    parser.add_argument("--debug", action="store_true", help="打印拼好的提示词，便于排查")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        raise SystemExit(f"文件不存在：{args.file}")

    cfg = Config.from_env()
    llm = LLMClient(cfg)

    retriever, stats = build_engine(args.file, cfg, args.chunk,
                                    args.bm25_weight, args.topk)
    print_banner(args.file, stats, llm)

    if args.ask:
        t0 = time.time()
        hits = retriever.search(args.ask, args.topk)
        messages = PromptBuilder.build(args.ask, hits)

        if args.debug:
            print("\n【调试】拼好的提示词：")
            for m in messages:
                print(f"--- {m['role']} ---")
                print(m["content"][:1500])

        try:
            answer = llm.chat(messages)
        except Exception as e:
            answer = f"[调用失败] {e}"

        print_result(args.ask, hits, answer, time.time() - t0)
        return

    repl(retriever, llm, args.topk)


if __name__ == "__main__":
    main()
