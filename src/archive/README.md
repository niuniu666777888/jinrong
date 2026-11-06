# 问答助手会话归档

存放问答助手（平台访谈 + 本地制度问答）的**会话记录**，供追溯、统计与回归使用。

| 文件 | 内容 |
|---|---|
| `qa_sessions.json` | 结构化：6 个会话、25 轮问答，含受访人、时间、问题、回答、引用事实、来源 |
| `qa_sessions.csv` | 扁平表，一行一轮，UTF-8 BOM，Excel 可直接打开 |
| `build_archive.py` | 从 `discovery-report.json` + 补充轮次重新生成上述两个文件 |

## 数据来源

| 来源 | 轮次 | 说明 |
|---|---|---|
| 平台访谈导出（`discovery-report.json`） | 24 | 6 个岗位的项目访谈，匿名编号 `r0`–`r5` 经证据编号反查确定身份 |
| 平台补充轮次（2026-11-06） | 1 | 资金风险经理傅景，主题为审计凭证与追溯修改 |

合计 6 个会话、25 轮问答。

## 字段

```json
{
  "session_id": "s_r4",
  "project": "finance-reconciliation",
  "stage": "opportunity",
  "respondent": "傅景",
  "turns": [
    {
      "no": 7,
      "at": "2026-11-06T01:18:38.403Z",
      "question": "审计需要什么凭证，怎么追溯修改？",
      "answer": "...",
      "citations": ["f40", "f45"],
      "source": "平台补充轮次（2026-11-06）"
    }
  ]
}
```

## 与 `docs/` 的关系

- `docs/六人访谈记录.*` 是**整理后**的版本（按人成章、含要点归纳）；
- 本目录是**原始会话归档**（一问一答、保留时间戳与来源），不做二次加工；
- 两者可以互相校验：轮次数、引用的事实编号应当一致。

## 重新生成

```bash
python src/archive/build_archive.py
```

脚本只读仓库内的 `discovery-report.json` 与需求发现报告，不发起网络请求。
后续如果平台再导出新的轮次，把内容追加到 `SUPPLEMENT` 列表后重跑即可。

## 后续扩展

本地制度问答助手（`src/app`）目前不落库会话。若需要把它的问答也归档，按同样的 schema
往 `sessions` 里追加即可（`project` 填 `local-assistant`，`citations` 填命中的条款号）。
