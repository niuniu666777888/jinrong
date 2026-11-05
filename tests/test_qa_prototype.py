from qa_prototype import BM25, PromptBuilder, clean_query, split_chunks, tokenize


def test_tokenize_handles_chinese_bigrams_and_numbers():
    tokens = tokenize("联机退货 TX20261030001 金额0.01")
    assert "联机" in tokens
    assert "退货" in tokens
    assert "tx20261030001" in tokens
    assert "0.01" in tokens


def test_split_chunks_returns_real_offsets_and_no_heading_only_chunks():
    markdown = (
        "# 对账\n\n"
        "第一段讲交易和账单匹配。\n\n"
        "## 差错\n\n"
        "第二段讲长款和短款处理。\n"
    )
    chunks = split_chunks(markdown, size=30, overlap=5)
    assert chunks
    for text, start, end in chunks:
        assert text
        assert markdown[start:end].strip() == text
        # A chunk made only of a markdown heading is not useful for retrieval.
        assert not (text.strip().startswith("#") and "\n" not in text.strip())


def test_clean_query_removes_polite_prefix():
    cleaned = clean_query("请问对账差错分哪几类？")
    assert "请问" not in cleaned
    assert "对账差错" in cleaned


def test_bm25_ranks_the_matching_document_first():
    corpus = [
        tokenize("联机退货 差错 处理"),
        tokenize("手续费 合并 入账"),
        tokenize("对账 批次 差异"),
    ]
    model = BM25().fit(corpus)
    scores = model.scores(tokenize("联机退货"))
    assert scores[0] == max(scores)
    assert scores[0] > 0


def test_prompt_builder_keeps_rules_and_source_labels():
    hits = [{"text": "差错分为长款和短款", "start": 0, "end": 10}]
    messages = PromptBuilder.build("差错分几类？", hits)

    assert messages[0]["role"] == "system"
    assert "只能依据" in messages[0]["content"]
    assert "[1]" in messages[1]["content"]
    assert "差错分几类？" in messages[1]["content"]
