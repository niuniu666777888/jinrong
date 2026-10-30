# 员工问答入口（阶段 2 · 提示词 4）

FastAPI + 原生 JS 的单页问答应用。制度检索复用阶段 1 的 `qa_prototype.py`，
不重复实现切片、BM25、向量检索。

## 目录

```
webapp/
├── app.py              FastAPI 应用：路由、SSE、错误处理、知识库装配
├── db.py               SQLite：会话 / 消息 / 引用 / 切片
├── llm.py              流式大模型调用（继承阶段 1 的 LLMClient）
├── static/index.html   聊天页面（打字机、引用、错误重试）
├── docs/               知识库（markdown，启动时自动切片入库）
├── requirements.txt
└── app.db              SQLite 数据文件（首次启动自动创建）
```

`app.py` 通过 `sys.path` 引用上一层目录的 `qa_prototype.py`，所以**不要单独移动 webapp 目录**。

## 启动

```bash
cd webapp
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000

默认走 mock 模式（不用配 Key 也能跑通全链路，回答是抽取式摘录）。
接真实模型：

```bash
set LLM_PROVIDER=deepseek
set LLM_API_KEY=sk-xxx
set LLM_MODEL=deepseek-chat
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/chat` | 提问，返回 SSE |
| GET | `/api/v1/sessions/{sid}/messages` | 拉历史（刷新恢复） |
| POST | `/api/v1/sessions` | 新建会话 |
| GET | `/api/v1/sources/{chunk_id}` | 查看条文原文 |
| GET | `/api/v1/health` | 健康检查 |

统一响应：`{code, message, data, trace_id}`，`code=0` 为成功，非 0 时 `message` 是可直接展示的中文。

错误码：

| code | HTTP | 含义 |
|---|---|---|
| 10400 | 400 | 参数格式错误 |
| 10401 | 400 | 问题为空或超过 500 字 |
| 10404 | 404 | 会话/条文不存在 |
| 10406 | 409 | 单会话消息数已达上限 |
| 10500 | 500 | 检索失败 |
| 10501 | 502 | 模型调用失败 |
| 10502 | 503 | 知识库未就绪 |

### SSE 事件顺序

```
meta → status(retrieving) → sources → status(generating) → delta × N → done
                                                       ↘ error（可替代 done）
```

`error` 只会在响应头已经发出之后使用；参数错误一律走 HTTP 4xx + JSON。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOCS_DIR` | `./docs` | 知识库目录，扫 `*.md` |
| `DB_PATH` | `./app.db` | SQLite 文件 |
| `TOP_K` | 3 | 检索片段数 |
| `HISTORY_TURNS` | 6 | 上下文轮数（1 轮 = 问 + 答） |
| `MAX_MESSAGES` | 200 | 单会话消息上限 |
| `MIN_SCORE` | 0.30 | 检索分数低于此值直接兜底，不调模型 |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` | mock | 与阶段 1 共用 |

## 已知边界

- **单进程**：检索索引常驻内存，多 worker 会各建一份（逻辑正确但内存翻倍）。
- **匿名会话**：`session_id` 由前端生成并存在 localStorage，谁拿到 ID 谁能看历史。
  接 SSO 后把 `session.employee_id` 填上，并在 `get_messages` 前加归属校验。
- **mock 模式不代表能力**：hash 向量与抽取式回答只用于联调，
  生产环境必须换真实 embedding 与模型。
- **未做 query 改写**：多轮里"那短款呢"这类指代，检索仍按原句进行，
  上下文只用于生成。二期在检索前加一轮改写即可，接口无需变更。
