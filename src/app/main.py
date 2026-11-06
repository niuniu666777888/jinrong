"""FastAPI 应用：登录、检索问答（SSE）、差错处理闭环、Agent 代办。

启动即建库、灌种子、建索引；不配任何环境变量也能跑（默认两个演示账号）。
"""

import json
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, agent_loop, auth, config, db, llm, retrieval, seed
from .errors import (CODE_BAD_REQUEST, CODE_EMPTY_QUESTION, CODE_MODEL_FAIL,
                     CODE_NOT_READY, CODE_RETRIEVE_FAIL, BizError, body, ok)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE: Dict[str, Any] = {"ready": False, "policy_count": 0}


# ---------------------------------------------------------------- lifespan
def bootstrap() -> None:
    db.init_db()
    seed.seed()
    seed.seed_users()
    rows = db.q("SELECT id, clause_no, title, content FROM policy ORDER BY clause_no")
    retrieval.build(rows)
    STATE["policy_count"] = len(rows)
    STATE["ready"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    if config.IS_DEMO_SECRET:
        print("[提示] 未设置 MVP_SECRET，本次启动使用随机签名密钥；重启后登录态与核验凭证都会失效。")
    print(f"[启动] 制度条款 {STATE['policy_count']} 条 · 模型 {llm.provider_name()} · "
          f"数据库 {config.DB_PATH}")
    yield


app = FastAPI(title="支付对账与差错追踪 MVP", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------- 异常
def trace_of(request: Request) -> str:
    tid = getattr(request.state, "trace_id", "")
    if not tid:
        tid = uuid.uuid4().hex[:12]
        request.state.trace_id = tid
    return tid


@app.middleware("http")
async def attach_trace(request: Request, call_next):
    request.state.trace_id = uuid.uuid4().hex[:12]
    return await call_next(request)


@app.exception_handler(BizError)
async def on_biz_error(request: Request, exc: BizError):
    return JSONResponse(status_code=exc.http_status,
                        content=body(exc.code, exc.message, None, trace_of(request)))


@app.exception_handler(RequestValidationError)
async def on_validation(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400,
                        content=body(CODE_BAD_REQUEST, "参数格式错误", None, trace_of(request)))


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(status_code=500,
                        content=body(CODE_MODEL_FAIL, "服务内部错误，请稍后重试",
                                     None, trace_of(request)))


# ---------------------------------------------------------------- 入参
class ChatIn(BaseModel):
    question: str = Field(default="")


class LoginIn(BaseModel):
    employee_no: str = Field(default="", max_length=32)
    password: str = Field(default="", max_length=128)


class TicketIn(BaseModel):
    diff_id: str = Field(default="", max_length=32)
    token: str = Field(default="", max_length=512)
    handling: str = Field(default="", max_length=16)


class ReviewIn(BaseModel):
    approve: bool = True


class AgentIn(BaseModel):
    goal: str = Field(default="", max_length=200)


class AgentConfirmIn(BaseModel):
    approved: bool = True


def _public(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"user_id": user["id"], "employee_no": user["employee_no"],
            "name": user["name"], "role": user["role"]}


# ---------------------------------------------------------------- 页面
@app.get("/")
def index(request: Request):
    if not auth.current_user(request):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------- 认证
@app.post("/api/auth/login")
def api_login(payload: LoginIn, request: Request):
    user = auth.login(payload.employee_no, payload.password)
    response = JSONResponse(content=ok(_public(user), trace_of(request)))
    response.set_cookie(auth.COOKIE_NAME, auth.issue(user["id"]),
                        max_age=auth.TOKEN_TTL, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE, path="/")
    return response


@app.post("/api/auth/logout")
def api_logout(request: Request):
    response = JSONResponse(content=ok(None, trace_of(request)))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me")
def api_me(request: Request):
    user = auth.require_login(request)
    return ok(_public(user), trace_of(request))


# ---------------------------------------------------------------- 健康检查
@app.get("/api/health")
def health(request: Request):
    return ok({
        "status": "ready" if STATE["ready"] else "init",
        "policies": STATE["policy_count"],
        "model": llm.provider_name(),
        "db": Path(config.DB_PATH).name,
    }, trace_of(request))


# ---------------------------------------------------------------- 问答
def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(payload: ChatIn, request: Request):
    auth.require_login(request)
    if not STATE["ready"]:
        raise BizError(CODE_NOT_READY, "知识库尚未就绪", 503)

    question = (payload.question or "").strip()
    if not question:
        raise BizError(CODE_EMPTY_QUESTION, "问题不能为空", 400)
    if len(question) > 500:
        raise BizError(CODE_EMPTY_QUESTION, "问题过长，请控制在 500 字以内", 400)

    trace_id = trace_of(request)
    started = time.time()

    def stream() -> Iterator[str]:
        try:
            yield sse("meta", {"trace_id": trace_id, "ts": db.now_ms()})
            yield sse("status", {"stage": "retrieving", "text": "正在检索制度…"})
            try:
                hits = retrieval.search(question, config.TOP_K)
            except Exception:
                traceback.print_exc()
                yield sse("error", {"code": CODE_RETRIEVE_FAIL, "message": "检索失败，请重试"})
                return

            best = hits[0]["score"] if hits else 0.0
            if not hits or best < config.MIN_SCORE:
                text = ("制度中未找到与该问题直接相关的内容。"
                        "建议换个说法，或联系财务共享中心确认。")
                yield sse("sources", {"items": []})
                yield sse("delta", {"text": text})
                yield sse("done", {"elapsed_ms": int((time.time() - started) * 1000),
                                   "degraded": True})
                return

            yield sse("sources", {"items": [
                {"clause_no": h["clause_no"], "title": h["title"],
                 "score": h["score"], "snippet": h["content"][:60] + "…"}
                for h in hits
            ]})
            yield sse("status", {"stage": "generating", "text": "正在生成回答…"})

            try:
                for piece in llm.stream(question, hits):
                    yield sse("delta", {"text": piece})
            except Exception:
                traceback.print_exc()
                yield sse("error", {"code": CODE_MODEL_FAIL, "message": "模型服务不可用"})
                return

            yield sse("done", {"elapsed_ms": int((time.time() - started) * 1000),
                               "degraded": False})
        except GeneratorExit:
            raise

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/policies")
def policies(request: Request):
    auth.require_login(request)
    rows = db.q("SELECT id, clause_no, title, content FROM policy ORDER BY clause_no")
    return ok(rows, trace_of(request))


# ---------------------------------------------------------------- 对账与差错
@app.get("/api/batches")
def batches(request: Request):
    auth.require_login(request)
    return ok(agent.list_batches(), trace_of(request))


@app.get("/api/diffs")
def diffs(request: Request, batch_id: str = ""):
    auth.require_login(request)
    return ok(agent.list_diffs(batch_id), trace_of(request))


@app.post("/api/diffs/{diff_id}/verify")
def verify(diff_id: str, request: Request):
    user = auth.require_login(request)
    return ok(agent.verify_diff(diff_id, user["name"]), trace_of(request))


@app.post("/api/tickets")
def create_ticket(payload: TicketIn, request: Request):
    user = auth.require_login(request)
    return ok(agent.create_ticket(payload.diff_id, payload.token,
                                  payload.handling, user["name"]), trace_of(request))


@app.post("/api/tickets/{ticket_id}/submit")
def submit(ticket_id: str, request: Request):
    user = auth.require_login(request)
    return ok(agent.submit_ticket(ticket_id, user["name"]), trace_of(request))


@app.post("/api/tickets/{ticket_id}/review")
def review(ticket_id: str, payload: ReviewIn, request: Request):
    user = auth.require_login(request)
    auth.require_role(user, "admin")          # 复核权：只有风险岗（admin）具备
    return ok(agent.review_ticket(ticket_id, user["name"], payload.approve),
              trace_of(request))


@app.get("/api/tickets")
def tickets(request: Request):
    auth.require_login(request)
    return ok(agent.list_tickets(), trace_of(request))


# ---------------------------------------------------------------- Agent 循环
@app.post("/api/agent/run")
def agent_run(payload: AgentIn, request: Request):
    user = auth.require_login(request)
    goal = (payload.goal or "").strip()
    if not goal:
        raise BizError(CODE_BAD_REQUEST, "请输入目标", 400)
    return ok(agent_loop.start(goal, user["name"]), trace_of(request))


@app.post("/api/agent/{run_id}/confirm")
def agent_confirm(run_id: str, payload: AgentConfirmIn, request: Request):
    user = auth.require_login(request)
    return ok(agent_loop.confirm(run_id, user["name"], payload.approved),
              trace_of(request))


@app.get("/api/agent/{run_id}")
def agent_state(run_id: str, request: Request):
    auth.require_login(request)
    return ok(agent_loop.load(run_id), trace_of(request))
