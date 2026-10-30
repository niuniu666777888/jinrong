# 对账差错处理 Agent（阶段 3 · 提示词 7）

一个不依赖任何 Agent 框架的轻量执行循环：模型决策 → 执行工具 → 结果回填 → 再决策，最多 5 轮。

## 为什么不用框架

场景是**单 Agent + 7 个工具 + 线性依赖**，没有多智能体协作、没有复杂状态图。
LangGraph / OpenAI Agents SDK 这类框架的价值在编排复杂度上，这里用不上，
反而会引入三层抽象（graph → node → tool）和一整套配置。

自研循环 80 行，好处是：

- 断点可控：哪里需要人工确认，就在哪停，不依赖框架的 interrupt 语义；
- 依赖为零：只用标准库，部署到内网不用带一堆包；
- 可替换：`models.py` 里的 `BaseModel` 是唯一适配点，
  以后要换 LangGraph，把循环换成图、工具注册表原样带过去即可。

## 运行

```bash
cd agent_demo
python demo_run.py            # 跑 4 个场景 + 日志表
python demo_run.py --keep-db  # 保留数据库，便于自己查 agent_log
```

接真实模型（可选，默认离线剧本）：

```bash
set LLM_API_KEY=sk-xxx
set LLM_BASE_URL=https://api.deepseek.com/v1
set LLM_MODEL=deepseek-chat
```

## 文件

| 文件 | 职责 |
|---|---|
| `db.py` | 表结构与种子数据（渠道/批次/差异/工单/规范/日志） |
| `policy.py` | 规范检索（BM25，生产替换为混合检索） |
| `tools.py` | 7 个工具：schema + 实现 + 核验凭证签名 |
| `models.py` | 决策模型适配（ScriptedModel / OpenAIModel） |
| `loop.py` | 执行循环、确认门、日志落库 |
| `demo_run.py` | 4 个场景的轨迹演示与结果核对 |

## 七个工具

| 工具 | 类型 | 说明 | 前置约束 |
|---|---|---|---|
| `search_policy` | 只读 | 检索对账口径与差错处理规范 | 无 |
| `query_batch` | 只读 | 查某渠道某结算日的批次与差异统计 | 无 |
| `list_diffs` | 只读 | 按批次/类型列差异 | 需 batch_id |
| `verify_diff` | 只读+签发 | 核验差异是否成立，通过则签发 token | 差异须 pending |
| `create_ticket` | **写** | 建差错工单 | 必须带有效 token + 人工确认 |
| `submit_ticket` | **写** | 提交复核 | 须 created 状态 + 人工确认 |
| `get_ticket_status` | 只读 | 查工单进度 | 无 |

## 三道防线

1. **提示词**：要求"先核验后建单"，属于软约束，防不住被诱导；
2. **工具契约**：写工具只收 `diff_id` 和 `token`，不收金额；金额由服务端重查；
3. **签名 + 状态机**：token 是 HMAC 签名并落库的，`create_ticket` 验签、查重、查过期、查是否已用。

结果：**"跳过核验直接建单"不是"不该做"，而是"做不到"**。

## 日志表 `agent_log`

| 字段 | 用途 |
|---|---|
| session_id / round_no | 还原一次对话的决策路径 |
| tool_name / args_json | 看模型到底传了什么 |
| ok / blocked | 区分"失败"与"被守卫拦截" |
| summary | 人可读的结果摘要，避免全 JSON 撑爆表 |
| elapsed_ms | 定位慢工具 |

典型分析：`blocked=1` 集中在哪个工具，说明提示词在该环节没写清楚；
`ok=0` 集中在哪个错误码，说明工具契约与模型理解有偏差。

## 已知边界

- 剧本模型只用于演示轨迹，不代表真实模型能力；
- 检索是 BM25，语义改写（"那另一笔呢"）检索不到；
- 确认门是同步回调，接 Web 时要改成"挂起会话 + 前端确认后恢复"。
