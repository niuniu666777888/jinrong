#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
webapp/admin.py —— 管理后台（服务端渲染 + JSON 接口）
================================================================================
页面：登录 / 概览 / 知识库 / 提示词 / 审计
接口：/api/v1/auth/*、/api/v1/admin/*

分两类路由：
  - 页面路由返回 HTML（Jinja2），未登录直接 302 到 /login；
  - 接口路由返回 JSON，未登录/无权限走统一错误体（40101 / 40103）。
这样前端 fetch 拿到的永远是结构化的 {code, message}，不用去解析 HTML。
================================================================================
"""

from __future__ import annotations

import csv
import hashlib
import traceback
import io
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import admin_db
import auth
import db
from errors import (
    CODE_BAD_FILE_TYPE, CODE_DUPLICATE_DOC, CODE_EMPTY_PROMPT,
    CODE_FILE_TOO_LARGE, CODE_NO_SESSION, CODE_OK, CODE_PARSE_FAILED, BizError,
)

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = Path(os.environ.get("DOCS_DIR", str(BASE_DIR / "docs")))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXT = {".md": "md", ".txt": "txt", ".pdf": "pdf"}
MAX_PROMPT_CHARS = 4000
MAX_PDF_PAGES = 300

# 默认系统提示词：需求方给定，首次启动写入数据库，之后以数据库为准
DEFAULT_SYSTEM_PROMPT = (
    "你是公司制度助手，只根据提供的制度条文回答，不确定时明确说“制度中未找到”，"
    "回答末尾附引用来源，不得编造报销标准。"
)

FLAG_CATEGORIES = {
    "over_standard": "超标准报销",
    "non_business": "非因公",
    "fabricated": "虚构事项",
    "other": "其他",
}

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter()


# ==============================================================================
# 启动初始化：账号与默认提示词
# ==============================================================================
def seed() -> None:
    """
    首次启动创建两个账号和默认提示词。

    密码从环境变量取；没配就随机生成并打印到控制台。
    这里不写死密码，是因为写死的默认口令是最常见的入侵入口。
    """
    admin_db.ensure_prompt("system_default", DEFAULT_SYSTEM_PROMPT)

    if admin_db.count_users() > 0:
        return

    admin_pwd = os.environ.get("ADMIN_INIT_PASSWORD") or secrets.token_urlsafe(9)
    emp_pwd = os.environ.get("EMPLOYEE_INIT_PASSWORD") or secrets.token_urlsafe(9)

    admin_db.create_user("admin", "系统管理员", "admin",
                         auth.hash_password(admin_pwd))
    admin_db.create_user("employee01", "张伟", "employee",
                         auth.hash_password(emp_pwd))

    print("=" * 68)
    print("首次启动，已创建初始账号（请登录后立即修改密码）：")
    print(f"  管理员  admin      / {admin_pwd}")
    print(f"  员工    employee01 / {emp_pwd}")
    print("=" * 68)


# ==============================================================================
# 页面工具
# ==============================================================================
def current_user(request: Request) -> Optional[Dict[str, Any]]:
    return auth.get_current_user(request)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    """统一注入 user，模板里就不用每个页面都自己取。"""
    ctx.setdefault("user", current_user(request))
    return templates.TemplateResponse(request, name, ctx)


def guard_page(request: Request) -> Optional[RedirectResponse]:
    """
    页面守卫：未登录跳登录页，非管理员跳回聊天页。

    返回 RedirectResponse 就表示"被拦下了"，调用方直接 return 它。
    """
    user = current_user(request)
    if not user:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse("/?denied=1", status_code=302)
    return None


# ==============================================================================
# 登录页（员工与管理员共用一个页面）
# ==============================================================================
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(next or "/", status_code=302)
    return render(request, "login.html", next=next, error=None)


@router.post("/login")
async def login_submit(request: Request):
    """
    表单登录：成功后 302 跳目标页，失败回登录页显示错误。

    用表单而不是 fetch，是因为登录成功后需要整页跳转，
    fetch 还要自己处理 document.location，反而更绕。
    """
    form = await request.form()
    employee_no = str(form.get("employee_no", "")).strip()
    password = str(form.get("password", ""))
    nxt = str(form.get("next", "/") or "/")

    user = admin_db.get_user_by_no(employee_no)
    if not user or not auth.verify_password(password, user["password_hash"]):
        # 故意不区分"账号不存在"和"密码错误"，避免被用来枚举工号
        return render(request, "login.html", next=nxt, error="工号或密码不正确")

    if user["status"] != "active":
        return render(request, "login.html", next=nxt, error="账号已停用，请联系管理员")

    token = auth.create_token(user)
    admin_db.touch_login(user["id"])

    # 管理员默认进后台，员工进聊天页
    target = nxt if nxt not in ("/", "") else ("/admin" if user["role"] == "admin" else "/")
    resp = RedirectResponse(target, status_code=302)
    auth.set_auth_cookie(resp, token)
    return resp


@router.get("/logout")
async def logout_page():
    resp = RedirectResponse("/login", status_code=302)
    auth.clear_auth_cookie(resp)
    return resp


# ==============================================================================
# 页面：概览
# ==============================================================================
@router.get("/admin", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    if (redirect := guard_page(request)):
        return redirect

    today = int(datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp() * 1000)
    stats = admin_db.dashboard_stats(today)
    prompt = admin_db.get_prompt_by_name("system_default") or {}
    docs, _ = admin_db.list_docs(page=1, size=5)

    return render(request, "dashboard.html", active="dashboard",
                  stats=stats, prompt=prompt, docs=docs,
                  index=request.app.state.index_info())


# ==============================================================================
# 页面：知识库
# ==============================================================================
@router.get("/admin/kb", response_class=HTMLResponse)
async def page_kb(request: Request, keyword: str = "", page: int = 1):
    if (redirect := guard_page(request)):
        return redirect

    docs, total = admin_db.list_docs(keyword=keyword, page=page, size=20)
    return render(request, "kb.html", active="kb", docs=docs, total=total,
                  page=page, keyword=keyword, pages=max(1, (total + 19) // 20))


# ==============================================================================
# 页面：提示词
# ==============================================================================
@router.get("/admin/prompts", response_class=HTMLResponse)
async def page_prompts(request: Request):
    if (redirect := guard_page(request)):
        return redirect

    prompt = admin_db.get_prompt_by_name("system_default")
    if not prompt:
        raise BizError(CODE_NO_SESSION, "提示词模板不存在", 404)
    versions = admin_db.list_prompt_versions(prompt["id"], limit=10)
    return render(request, "prompts.html", active="prompts",
                  prompt=prompt, versions=versions)


# ==============================================================================
# 页面：审计
# ==============================================================================
@router.get("/admin/audit", response_class=HTMLResponse)
async def page_audit(request: Request, employee_no: str = "", date_from: str = "",
                     date_to: str = "", keyword: str = "", flagged: str = "",
                     page: int = 1):
    if (redirect := guard_page(request)):
        return redirect

    f = _build_filter(employee_no, date_from, date_to, keyword, flagged)
    items, total = admin_db.audit_page(f, page=page, size=20)

    return render(request, "audit.html", active="audit", items=items, total=total,
                  page=page, pages=max(1, (total + 19) // 20),
                  employee_no=employee_no, date_from=date_from, date_to=date_to,
                  keyword=keyword, flagged=flagged,
                  categories=FLAG_CATEGORIES)


@router.get("/admin/audit/{message_id}", response_class=HTMLResponse)
async def page_audit_detail(request: Request, message_id: str):
    if (redirect := guard_page(request)):
        return redirect

    item = admin_db.get_audit_item(message_id)
    if not item:
        raise BizError(CODE_NO_SESSION, "该对话记录不存在", 404)
    return render(request, "audit_detail.html", active="audit", item=item,
                  categories=FLAG_CATEGORIES)


# ==============================================================================
# 筛选条件：页面和接口共用
# ==============================================================================
def _build_filter(employee_no: str, date_from: str, date_to: str,
                  keyword: str, flagged: str) -> Dict[str, Any]:
    """
    把页面上的字符串条件转成毫秒时间戳。

    日期只到"天"，所以要补边界：起始补 00:00:00，结束补 23:59:59。
    不补的话"查 10 月 30 日"会漏掉当天下午的所有记录——这是最常见的日期筛选 bug。
    """
    f: Dict[str, Any] = {
        "employee_no": employee_no.strip(),
        "keyword": keyword.strip(),
        "flagged": flagged if flagged in ("yes", "no") else "",
    }

    def to_ms(s: str, end: bool) -> Optional[int]:
        if not s:
            return None
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        if end:
            d = d.replace(hour=23, minute=59, second=59, microsecond=999000)
        return int(d.timestamp() * 1000)

    f["from_ms"] = to_ms(date_from, end=False)
    f["to_ms"] = to_ms(date_to, end=True)
    return f


# ==============================================================================
# 接口：认证
# ==============================================================================
@router.post("/api/v1/auth/login")
async def api_login(request: Request):
    """JSON 登录，给脚本和前端用；行为与表单登录一致。"""
    body = await request.json()
    employee_no = str(body.get("employee_no", "")).strip()
    password = str(body.get("password", ""))

    user = admin_db.get_user_by_no(employee_no)
    if not user or not auth.verify_password(password, user["password_hash"]):
        raise BizError(CODE_NO_SESSION, "工号或密码不正确", 401)
    if user["status"] != "active":
        raise BizError(CODE_NO_SESSION, "账号已停用", 403)

    token = auth.create_token(user)
    admin_db.touch_login(user["id"])

    resp = JSONResponse({
        "code": CODE_OK, "message": "ok",
        "data": {"user_id": user["id"], "name": user["name"],
                 "employee_no": user["employee_no"], "role": user["role"]},
        "trace_id": "",
    })
    auth.set_auth_cookie(resp, token)
    return resp


@router.post("/api/v1/auth/logout")
async def api_logout():
    resp = JSONResponse({"code": CODE_OK, "message": "ok", "data": None})
    auth.clear_auth_cookie(resp)
    return resp


@router.get("/api/v1/auth/me")
async def api_me(request: Request):
    user = auth.require_login(request)
    return {"code": CODE_OK, "message": "ok", "data": user}


@router.post("/api/v1/auth/password")
async def api_change_password(request: Request):
    """改密后旧令牌立即失效（token_version +1），前端应跳回登录页。"""
    user = auth.require_login(request)
    body = await request.json()
    old = str(body.get("old_password", ""))
    new = str(body.get("new_password", ""))

    row = admin_db.get_user_by_id(user["id"])
    if not row or not auth.verify_password(old, row["password_hash"]):
        raise BizError(CODE_NO_SESSION, "原密码不正确", 400)
    if len(new) < 8:
        raise BizError(CODE_EMPTY_PROMPT, "新密码至少 8 位", 400)

    admin_db.change_password(user["id"], auth.hash_password(new))
    return {"code": CODE_OK, "message": "密码已修改，请重新登录", "data": None}


# ==============================================================================
# 接口：知识库
# ==============================================================================
@router.get("/api/v1/admin/kb/docs")
async def api_docs(request: Request, keyword: str = "", status: str = "",
                   page: int = 1, size: int = 20):
    auth.require_admin(request)
    docs, total = admin_db.list_docs(keyword=keyword, status=status,
                                     page=page, size=min(size, 100))
    return {"code": CODE_OK, "message": "ok",
            "data": {"items": docs, "total": total, "page": page}}


@router.post("/api/v1/admin/kb/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    """
    上传 → 解析 → 切片 → 入库 → 重建索引，全同步。

    为什么不丢后台队列：单份文档解析 + 向量化在秒级，
    同步返回能让管理员立刻看到"成功/失败"，比轮询任务状态体验好。
    超过 60 秒的大文件直接用大小上限挡掉。
    """
    user = auth.require_admin(request)
    auth.check_origin(request)

    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise BizError(CODE_BAD_FILE_TYPE,
                       f"不支持的文件类型 {ext or '（无扩展名）'}，仅支持 md / txt / pdf", 400)

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise BizError(CODE_FILE_TOO_LARGE, "文件超过 20 MB，请拆分后再上传", 413)
    if not raw.strip():
        raise BizError(CODE_PARSE_FAILED, "文件内容为空", 422)

    sha = hashlib.sha256(raw).hexdigest()
    existed = admin_db.get_doc_by_sha(sha)
    if existed:
        raise BizError(CODE_DUPLICATE_DOC,
                       f"该文档已存在：{existed['file_name']}", 409)

    text = _extract_text(raw, ext)

    doc_id = db.new_id("d")
    admin_db.create_doc(doc_id, filename, ALLOWED_EXT[ext], len(raw), sha, user["id"])

    # 原始文件落盘：出问题时能复查"到底是文件错还是解析错"
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    saved = DOCS_DIR / f"{doc_id}{ext}"
    saved.write_bytes(raw)

    try:
        chunks = _build_chunks(doc_id, filename, text)
        if not chunks:
            raise BizError(CODE_PARSE_FAILED, "解析后没有可用文本，请检查文件内容", 422)

        db.insert_chunks(chunks)
        admin_db.finish_doc(doc_id, len(chunks), "indexed")

        # 重建索引：构建完才替换，构建期间问答继续用旧索引
        request.app.state.rebuild()

    except BizError as e:
        admin_db.finish_doc(doc_id, 0, "failed", e.message)
        raise
    except Exception as e:                      # 解析器抛的任何异常都归为"解析失败"
        # 异常原文只进服务端日志与 doc.error_msg，不回给前端：
        # 它可能带有本地路径、库版本甚至 SQL 片段
        traceback.print_exc()
        admin_db.finish_doc(doc_id, 0, "failed", str(e)[:200])
        raise BizError(CODE_PARSE_FAILED, "文档处理失败，请检查文件格式后重试", 422)

    return {"code": CODE_OK, "message": "ok",
            "data": {"doc_id": doc_id, "file_name": filename, "chunks": len(chunks)}}


@router.delete("/api/v1/admin/kb/docs/{doc_id}")
async def api_delete_doc(request: Request, doc_id: str):
    """
    删除文档。历史回答里已引用的片段不受影响——
    message_source 里冗余存了文件名和摘要，正是为了扛住这种情况。
    """
    auth.require_admin(request)
    auth.check_origin(request)

    doc = admin_db.get_doc(doc_id)
    if not doc:
        raise BizError(CODE_NO_SESSION, "文档不存在或已删除", 404)

    removed = admin_db.delete_doc(doc_id)
    for suffix in ALLOWED_EXT:
        f = DOCS_DIR / f"{doc_id}{suffix}"
        if f.exists():
            f.unlink()

    request.app.state.rebuild()
    return {"code": CODE_OK, "message": "已删除",
            "data": {"removed_chunks": removed}}


@router.post("/api/v1/admin/kb/reindex")
async def api_reindex(request: Request):
    """手动全量重建，用于切片参数调整后或索引异常时。"""
    auth.require_admin(request)
    auth.check_origin(request)
    info = request.app.state.rebuild()
    return {"code": CODE_OK, "message": "重建完成", "data": info}


@router.get("/api/v1/admin/kb/docs/{doc_id}/chunks")
async def api_doc_chunks(request: Request, doc_id: str, limit: int = 50):
    auth.require_admin(request)
    if not admin_db.get_doc(doc_id):
        raise BizError(CODE_NO_SESSION, "文档不存在", 404)
    rows = admin_db.chunks_of_doc(doc_id, limit=min(limit, 100))
    return {"code": CODE_OK, "message": "ok", "data": rows}


# ==============================================================================
# 文本抽取与切片
# ==============================================================================
def _extract_text(raw: bytes, ext: str) -> str:
    """md / txt 直接解码；pdf 用 pypdf 抽文字层。"""
    if ext in (".md", ".txt"):
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # PDF
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise BizError(CODE_PARSE_FAILED,
                           f"PDF 超过 {MAX_PDF_PAGES} 页，请拆分后再上传", 422)
        if getattr(reader, "is_encrypted", False):
            raise BizError(CODE_PARSE_FAILED, "PDF 已加密，无法解析", 422)
        parts = [(p.extract_text() or "") for p in reader.pages]
    except BizError:
        raise
    except Exception as e:
        traceback.print_exc()
        raise BizError(CODE_PARSE_FAILED, "PDF 解析失败，请确认文件未加密且可正常打开", 422)

    text = "\n".join(parts).strip()
    if len(text) < 20:
        # 扫描件没有文字层，抽出来是空的——OCR 是另一个工程，一期直接拒绝
        raise BizError(CODE_PARSE_FAILED,
                       "PDF 中没有可识别的文字（可能是扫描件），请先转成文字版", 422)
    return text


def _build_chunks(doc_id: str, file_name: str, text: str) -> List[Dict[str, Any]]:
    """复用阶段 1 的切分算法，转成 chunk 表需要的结构。"""
    from qa_prototype import split_chunks

    items = []
    for ordinal, (content, start, end) in enumerate(split_chunks(text)):
        items.append({
            "chunk_id": f"c_{doc_id}_{ordinal:04d}",
            "doc_id": doc_id,
            "file_name": file_name,
            "ordinal": ordinal,
            "start": start,
            "end": end,
            "content": content,
            "content_hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
        })
    return items


# ==============================================================================
# 接口：提示词
# ==============================================================================
@router.get("/api/v1/admin/prompts")
async def api_prompts(request: Request):
    auth.require_admin(request)
    prompt = admin_db.get_prompt_by_name("system_default")
    if not prompt:
        raise BizError(CODE_NO_SESSION, "提示词模板不存在", 404)
    versions = admin_db.list_prompt_versions(prompt["id"], limit=20)
    return {"code": CODE_OK, "message": "ok",
            "data": {"prompt": prompt, "versions": versions}}


@router.put("/api/v1/admin/prompts/{template_id}")
async def api_save_prompt(request: Request, template_id: str):
    """
    保存并立即生效。

    顺序很重要：先写库（version+1），再刷内存缓存。
    反过来的话，写库失败但缓存已经变了，会出现"这通对话用了新提示词、
    但重启后又变回去"的诡异现象。
    """
    user = auth.require_admin(request)
    auth.check_origin(request)

    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        raise BizError(CODE_EMPTY_PROMPT, "提示词不能为空", 400)
    if len(content) > MAX_PROMPT_CHARS:
        raise BizError(CODE_EMPTY_PROMPT,
                       f"提示词不能超过 {MAX_PROMPT_CHARS} 字", 400)

    try:
        result = admin_db.save_prompt(template_id, content, user["id"])
    except KeyError:
        raise BizError(CODE_NO_SESSION, "模板不存在", 404)

    # 立即生效：更新内存缓存
    request.app.state.set_prompt(content, result["version"])
    return {"code": CODE_OK, "message": f"已保存并生效（第 {result['version']} 版）",
            "data": result}


@router.post("/api/v1/admin/prompts/{template_id}/rollback")
async def api_rollback_prompt(request: Request, template_id: str):
    """
    回滚到历史版本。

    注意是"把旧内容重新保存成新版本"，而不是删除后面的版本——
    回滚本身也要留痕，否则审计时说不清"第 7 版为什么和之前一样"。
    """
    user = auth.require_admin(request)
    auth.check_origin(request)

    body = await request.json()
    version = int(body.get("version", 0))

    old = admin_db.get_prompt_version(template_id, version)
    if not old:
        raise BizError(CODE_NO_SESSION, f"第 {version} 版不存在", 404)

    result = admin_db.save_prompt(template_id, old["content"], user["id"])
    request.app.state.set_prompt(old["content"], result["version"])
    return {"code": CODE_OK,
            "message": f"已回滚到第 {version} 版的内容（新版本 {result['version']}）",
            "data": result}


# ==============================================================================
# 接口：审计
# ==============================================================================
@router.get("/api/v1/admin/audit/messages")
async def api_audit(request: Request, employee_no: str = "", date_from: str = "",
                    date_to: str = "", keyword: str = "", flagged: str = "",
                    page: int = 1, size: int = 20):
    auth.require_admin(request)
    f = _build_filter(employee_no, date_from, date_to, keyword, flagged)
    items, total = admin_db.audit_page(f, page=page, size=min(size, 100))
    return {"code": CODE_OK, "message": "ok",
            "data": {"items": items, "total": total, "page": page}}


@router.get("/api/v1/admin/audit/messages/{message_id}")
async def api_audit_detail(request: Request, message_id: str):
    auth.require_admin(request)
    item = admin_db.get_audit_item(message_id)
    if not item:
        raise BizError(CODE_NO_SESSION, "对话记录不存在", 404)
    return {"code": CODE_OK, "message": "ok", "data": item}


@router.post("/api/v1/admin/audit/messages/{message_id}/flag")
async def api_flag(request: Request, message_id: str):
    """标记疑似违规。分类用固定枚举，自由文本只进 reason，这样才统计得出来。"""
    user = auth.require_admin(request)
    auth.check_origin(request)

    body = await request.json()
    category = str(body.get("category", "")).strip()
    reason = str(body.get("reason", "")).strip()[:500]

    if category not in FLAG_CATEGORIES:
        raise BizError(CODE_BAD_FILE_TYPE,
                       f"未知的分类，可选：{'、'.join(FLAG_CATEGORIES.values())}", 400)
    if not admin_db.get_audit_item(message_id):
        raise BizError(CODE_NO_SESSION, "对话记录不存在", 404)

    admin_db.flag_message(message_id, category, reason, user["id"])
    return {"code": CODE_OK, "message": "已标记", "data": None}


@router.delete("/api/v1/admin/audit/messages/{message_id}/flag")
async def api_unflag(request: Request, message_id: str):
    auth.require_admin(request)
    auth.check_origin(request)
    ok = admin_db.unflag_message(message_id)
    return {"code": CODE_OK, "message": "已取消标记" if ok else "该记录未标记",
            "data": {"removed": ok}}


@router.get("/api/v1/admin/audit/export.csv")
async def api_export(request: Request, employee_no: str = "", date_from: str = "",
                     date_to: str = "", keyword: str = "", flagged: str = ""):
    """
    导出 CSV。

    用 \ufeff 开头（BOM），否则 Excel 打开中文会乱码——
    这是导出功能最常见的问题，比"格式好不好看"重要得多。
    """
    auth.require_admin(request)
    f = _build_filter(employee_no, date_from, date_to, keyword, flagged)
    rows = admin_db.audit_for_export(f, limit=5000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "工号", "姓名", "问题", "回答", "提示词版本",
                     "标记分类", "标记原因"])
    for r in rows:
        writer.writerow([
            datetime.fromtimestamp(r["created_at"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            r.get("employee_no") or "",
            r.get("employee_name") or "",
            r.get("question") or "",
            (r.get("content") or "").replace("\n", " "),
            r.get("prompt_version") or "",
            FLAG_CATEGORIES.get(r.get("category") or "", ""),
            r.get("reason") or "",
        ])

    content = "\ufeff" + buf.getvalue()
    filename = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
