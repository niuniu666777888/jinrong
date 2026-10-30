#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
client/admin_ui.py —— 客户端内的管理后台
================================================================================
三个页签：知识库 / 提示词 / 审计。

为什么不继续"跳浏览器打开 /admin"：
  管理员大多在客户端里工作，跳浏览器会丢登录态、丢上下文。
  既然 JSON 接口都现成，直接在客户端里做一套更顺。

统一约定：
  · 所有网络请求走 _async()，跑在子线程，回到主线程再碰控件；
  · 失败一律弹中文提示，不把异常原文丢给管理员；
  · 每次操作完成刷新数据，而不是靠本地猜测状态。
================================================================================
"""

from __future__ import annotations

import threading
import webbrowser
from tkinter import BOTH, END, LEFT, RIGHT, TOP, X, Y
from tkinter import filedialog, messagebox
from tkinter import ttk
import tkinter as tk

from api import ApiError, ChatClient

BG = "#ffffff"
TEXT = "#1f2937"
MUTED = "#8a94a6"
LINE = "#e8ebf0"
ACCENT = "#1f3864"

FLAG_LABELS = {
    "over_standard": "超标准报销",
    "non_business": "非因公",
    "fabricated": "虚构事项",
    "other": "其他",
}
FLAG_BY_LABEL = {v: k for k, v in FLAG_LABELS.items()}


class AdminWindow(tk.Toplevel):
    """独立窗口，不遮挡对话；关掉不影响主窗口。"""

    def __init__(self, master, client: ChatClient):
        super().__init__(master)
        self.client = client

        self.title("管理后台 · 支付对账助手")
        self.geometry("1080x700")
        self.minsize(900, 560)
        self.configure(bg=BG)
        self.transient(master)

        style = ttk.Style(self)
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=26,
                        background=BG, fieldbackground=BG)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"))

        nb = ttk.Notebook(self)
        self.nb = nb
        nb.pack(fill=BOTH, expand=True, padx=12, pady=(12, 4))

        self.tab_kb = tk.Frame(nb, bg=BG)
        self.tab_prompt = tk.Frame(nb, bg=BG)
        self.tab_audit = tk.Frame(nb, bg=BG)
        nb.add(self.tab_kb, text="  知识库  ")
        nb.add(self.tab_prompt, text="  提示词  ")
        nb.add(self.tab_audit, text="  对话审计  ")

        self._build_kb()
        self._build_prompt()
        self._build_audit()

        self._audit_loaded = False
        # 切到审计页时才查询：不点不看，省一次请求
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)

        self.after(120, self.refresh_kb)
        self.after(160, self.load_prompt)

    def _on_tab(self, _event=None) -> None:
        """切到某个页签时按需加载，避免开窗就打三个请求。"""
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            return
        if idx == 2 and not self._audit_loaded:
            self._audit_loaded = True
            self.refresh_audit()

    # ------------------------------------------------------------------
    # 通用：后台线程执行 + 回主线程渲染
    # ------------------------------------------------------------------
    def _async(self, fn, done) -> None:
        def worker():
            result, err = None, None
            try:
                result = fn()
            except ApiError as e:
                err = e.message
            except Exception as e:                      # noqa: BLE001
                err = str(e)
            self.after(0, lambda: done(result, err))

        threading.Thread(target=worker, daemon=True).start()

    # ==================================================================
    # 页签一：知识库
    # ==================================================================
    def _build_kb(self) -> None:
        bar = tk.Frame(self.tab_kb, bg=BG)
        bar.pack(fill=X, pady=(10, 6))

        for text, cmd in (
            ("上传文档", self.upload_doc),
            ("删除选中", self.delete_doc),
            ("重建索引", self.reindex),
            ("刷新", self.refresh_kb),
        ):
            tk.Button(bar, text=text, command=cmd, relief="flat",
                      bg="#f2f4f8" if text != "上传文档" else ACCENT,
                      fg=TEXT if text != "上传文档" else "#ffffff",
                      activebackground="#e6eaf1", activeforeground=TEXT,
                      font=("Microsoft YaHei", 9), padx=12, pady=4,
                      cursor="hand2", borderwidth=0).pack(side=LEFT, padx=(0, 8))

        self.kb_status = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.kb_status, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side=RIGHT)

        cols = ("file", "type", "chunks", "status", "uploader", "time")
        heads = ("文件名", "类型", "切片", "状态", "上传人", "时间")
        widths = (380, 60, 70, 90, 110, 150)

        wrap = tk.Frame(self.tab_kb, bg=BG)
        wrap.pack(fill=BOTH, expand=True)
        self.kb_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                    selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.kb_tree.heading(c, text=h)
            self.kb_tree.column(c, width=w, anchor="w" if c == "file" else "center")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.kb_tree.yview)
        self.kb_tree.configure(yscrollcommand=vs.set)
        self.kb_tree.pack(side=LEFT, fill=BOTH, expand=True)
        vs.pack(side=RIGHT, fill=Y)

        tk.Label(self.tab_kb,
                 text="上传后自动切片并重建索引，几秒内可被检索到。支持 md / txt / pdf。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(6, 0))

        self._docs: dict[str, dict] = {}

    def refresh_kb(self) -> None:
        self.kb_status.set("加载中…")

        def done(data, err):
            if err:
                self.kb_status.set("")
                messagebox.showerror("加载失败", err, parent=self)
                return

            self.kb_tree.delete(*self.kb_tree.get_children())
            self._docs.clear()
            for d in (data or {}).get("items", []):
                status = {"indexed": "已索引", "failed": "失败"}.get(d["status"], d["status"])
                iid = self.kb_tree.insert("", END, values=(
                    d["file_name"], d["file_type"], d["chunk_count"], status,
                    d.get("uploader_name") or "系统导入",
                    _fmt_ts(d.get("created_at")),
                ))
                self._docs[iid] = d
            self.kb_status.set(f"共 {(data or {}).get('total', 0)} 份")

        self._async(lambda: self.client.list_docs(), done)

    def upload_doc(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择制度文档",
            filetypes=[("制度文档", "*.md *.txt *.pdf"), ("全部文件", "*.*")],
        )
        if not path:
            return

        self.kb_status.set("上传中…")

        def done(data, err):
            if err:
                self.kb_status.set("")
                messagebox.showerror("上传失败", err, parent=self)
                return
            self.kb_status.set(f"已入库：{data.get('chunks')} 个切片")
            self.refresh_kb()

        self._async(lambda: self.client.upload_doc(path), done)

    def delete_doc(self) -> None:
        sel = self.kb_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一份文档", parent=self)
            return

        doc = self._docs[sel[0]]
        if not messagebox.askyesno(
                "确认删除",
                f"删除《{doc['file_name']}》？\n切片会一并移除，已引用它的历史回答不受影响。",
                parent=self):
            return

        def done(data, err):
            if err:
                messagebox.showerror("删除失败", err, parent=self)
                return
            self.kb_status.set("已删除")
            self.refresh_kb()

        self._async(lambda: self.client.delete_doc(doc["id"]), done)

    def reindex(self) -> None:
        self.kb_status.set("重建中…")

        def done(data, err):
            if err:
                self.kb_status.set("")
                messagebox.showerror("重建失败", err, parent=self)
                return
            self.kb_status.set(f"重建完成：{data.get('chunks', 0)} 个切片")
            self.refresh_kb()

        self._async(self.client.reindex, done)

    # ==================================================================
    # 页签二：提示词
    # ==================================================================
    def _build_prompt(self) -> None:
        head = tk.Frame(self.tab_prompt, bg=BG)
        head.pack(fill=X, pady=(10, 6))

        self.prompt_info = tk.StringVar(value="加载中…")
        tk.Label(head, textvariable=self.prompt_info, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side=LEFT)

        tk.Button(head, text="保存并生效", command=self.save_prompt, relief="flat",
                  bg=ACCENT, fg="#ffffff", activebackground="#2f4f7f",
                  activeforeground="#ffffff", font=("Microsoft YaHei", 9),
                  padx=14, pady=4, cursor="hand2", borderwidth=0).pack(side=RIGHT)

        tk.Label(self.tab_prompt,
                 text="这段文字作为 system 消息发给模型，决定它怎么答。"
                      "资料片段由系统拼接，不受这里影响。保存后下一条提问立即生效。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei", 8),
                 justify="left", wraplength=900).pack(anchor="w", pady=(0, 8))

        self.prompt_text = tk.Text(self.tab_prompt, wrap="word", relief="flat",
                                   font=("Microsoft YaHei", 10), padx=12, pady=10,
                                   bg="#f8f9fb", fg=TEXT, borderwidth=0,
                                   highlightthickness=1, highlightbackground=LINE,
                                   highlightcolor=ACCENT)
        self.prompt_text.pack(fill=BOTH, expand=True)

        self._prompt_id = ""

    def load_prompt(self) -> None:
        def done(data, err):
            if err:
                self.prompt_info.set("加载失败")
                messagebox.showerror("加载失败", err, parent=self)
                return
            p = (data or {}).get("prompt") or {}
            self._prompt_id = p.get("id", "")
            self.prompt_info.set(f"当前第 {p.get('version')} 版")
            self.prompt_text.delete("1.0", END)
            self.prompt_text.insert("1.0", p.get("content", ""))

        self._async(self.client.get_prompt, done)

    def save_prompt(self) -> None:
        content = self.prompt_text.get("1.0", END).strip()
        if not content:
            messagebox.showwarning("提示", "提示词不能为空", parent=self)
            return
        if len(content) > 4000:
            messagebox.showwarning("提示", "提示词不能超过 4000 字", parent=self)
            return

        def done(data, err):
            if err:
                messagebox.showerror("保存失败", err, parent=self)
                return
            messagebox.showinfo("已生效", f"第 {data.get('version')} 版已保存并生效",
                                parent=self)
            self.load_prompt()

        self._async(lambda: self.client.save_prompt(self._prompt_id, content), done)

    # ==================================================================
    # 页签三：审计
    # ==================================================================
    def _build_audit(self) -> None:
        bar = tk.Frame(self.tab_audit, bg=BG)
        bar.pack(fill=X, pady=(10, 6))

        self.var_emp = tk.StringVar()
        self.var_from = tk.StringVar()
        self.var_to = tk.StringVar()
        self.var_kw = tk.StringVar()
        self.var_flag = tk.StringVar(value="全部")

        def field(label, var, width):
            tk.Label(bar, text=label, bg=BG, fg=MUTED,
                     font=("Microsoft YaHei", 8)).pack(side=LEFT, padx=(0, 2))
            tk.Entry(bar, textvariable=var, width=width, relief="flat",
                     bg="#f5f6f8", fg=TEXT, font=("Microsoft YaHei", 9),
                     highlightthickness=1, highlightbackground=LINE,
                     insertbackground=TEXT).pack(side=LEFT, padx=(0, 10), ipady=3)

        field("工号", self.var_emp, 12)
        field("起", self.var_from, 10)
        field("止", self.var_to, 10)
        field("关键词", self.var_kw, 14)

        ttk.Combobox(bar, textvariable=self.var_flag, width=8, state="readonly",
                     values=["全部", "仅已标记", "仅未标记"]).pack(side=LEFT, padx=(0, 10))

        tk.Button(bar, text="查询", command=self.refresh_audit, relief="flat",
                  bg=ACCENT, fg="#ffffff", activebackground="#2f4f7f",
                  activeforeground="#ffffff", font=("Microsoft YaHei", 9),
                  padx=14, pady=4, cursor="hand2", borderwidth=0).pack(side=LEFT)

        self.audit_status = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.audit_status, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side=RIGHT)

        cols = ("time", "emp", "question", "flag")
        heads = ("时间", "员工", "问题", "标记")
        widths = (170, 170, 520, 90)

        wrap = tk.Frame(self.tab_audit, bg=BG)
        wrap.pack(fill=BOTH, expand=True)
        self.audit_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                       selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.audit_tree.heading(c, text=h)
            self.audit_tree.column(c, width=w, anchor="w" if c == "question" else "center")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.audit_tree.yview)
        self.audit_tree.configure(yscrollcommand=vs.set)
        self.audit_tree.pack(side=LEFT, fill=BOTH, expand=True)
        vs.pack(side=RIGHT, fill=Y)
        self.audit_tree.bind("<Double-1>", lambda e: self.open_detail())

        bottom = tk.Frame(self.tab_audit, bg=BG)
        bottom.pack(fill=X, pady=(6, 0))
        tk.Button(bottom, text="查看 / 标记", command=self.open_detail, relief="flat",
                  bg="#f2f4f8", fg=TEXT, font=("Microsoft YaHei", 9),
                  padx=12, pady=3, cursor="hand2", borderwidth=0).pack(side=LEFT)
        tk.Label(bottom, text="双击行也可打开", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(side=LEFT, padx=8)

        self._audit_items: dict[str, dict] = {}

    def refresh_audit(self) -> None:
        flag_map = {"全部": "", "仅已标记": "yes", "仅未标记": "no"}
        self.audit_status.set("查询中…")

        def done(data, err):
            if err:
                self.audit_status.set("")
                messagebox.showerror("查询失败", err, parent=self)
                return
            self.audit_tree.delete(*self.audit_tree.get_children())
            self._audit_items.clear()
            for it in (data or {}).get("items", []):
                tag = FLAG_LABELS.get(it.get("category") or "", "")
                iid = self.audit_tree.insert("", END, values=(
                    _fmt_ts(it.get("created_at")),
                    f"{it.get('employee_name') or '匿名'} {it.get('employee_no') or ''}",
                    (it.get("question") or "")[:80],
                    tag or "—",
                ))
                self._audit_items[iid] = it
            self.audit_status.set(f"共 {(data or {}).get('total', 0)} 条")

        self._async(lambda: self.client.audit_list(
            employee_no=self.var_emp.get().strip(),
            date_from=self.var_from.get().strip(),
            date_to=self.var_to.get().strip(),
            keyword=self.var_kw.get().strip(),
            flagged=flag_map.get(self.var_flag.get(), ""),
        ), done)

    def open_detail(self) -> None:
        sel = self.audit_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条记录", parent=self)
            return
        item = self._audit_items[sel[0]]

        def done(data, err):
            if err:
                messagebox.showerror("加载失败", err, parent=self)
                return
            DetailDialog(self, self.client, data, on_changed=self.refresh_audit)

        self._async(lambda: self.client.audit_detail(item["id"]), done)


# ==============================================================================
# 单条对话详情 + 标记
# ==============================================================================
class DetailDialog(tk.Toplevel):
    def __init__(self, master, client: ChatClient, item: dict, on_changed=None):
        super().__init__(master)
        self.client = client
        self.item = item
        self.on_changed = on_changed

        self.title("对话详情")
        self.geometry("820x640")
        self.configure(bg=BG)
        self.transient(master)

        head = tk.Frame(self, bg=BG)
        head.pack(fill=X, padx=16, pady=(14, 6))
        tk.Label(head, text=f"{item.get('employee_name') or '匿名'} "
                            f"（{item.get('employee_no') or '迁移前'}） · "
                            f"{_fmt_ts(item.get('created_at'))}",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei", 9)).pack(side=LEFT)

        body = tk.Text(self, wrap="word", relief="flat", bg="#f8f9fb", fg=TEXT,
                       font=("Microsoft YaHei", 9), padx=14, pady=12,
                       borderwidth=0, highlightthickness=0)
        body.pack(fill=BOTH, expand=True, padx=16)

        body.insert(END, "问题\n", ("h",))
        body.insert(END, (item.get("question") or "（无）") + "\n\n", ("b",))
        body.insert(END, "回答\n", ("h",))
        body.insert(END, (item.get("content") or "") + "\n\n", ("b",))

        if item.get("sources"):
            body.insert(END, f"引用条文（{len(item['sources'])}）\n", ("h",))
            for s in item["sources"]:
                body.insert(END, f"· {s['file_name']} 第 {s['start']}–{s['end']} 字 "
                                 f"（匹配度 {s['score']}）\n", ("m",))
        body.tag_configure("h", font=("Microsoft YaHei", 9, "bold"), foreground=ACCENT,
                           spacing1=6, spacing3=4)
        body.tag_configure("b", spacing3=8)
        body.tag_configure("m", foreground=MUTED)
        body.config(state="disabled")

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill=X, padx=16, pady=12)

        tk.Label(foot, text="分类", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side=LEFT)
        self.var_cat = tk.StringVar(value=FLAG_LABELS.get(item.get("category") or "", ""))
        ttk.Combobox(foot, textvariable=self.var_cat, width=10, state="readonly",
                     values=list(FLAG_LABELS.values())).pack(side=LEFT, padx=6)

        self.var_reason = tk.StringVar(value=item.get("reason") or "")
        tk.Entry(foot, textvariable=self.var_reason, width=36, relief="flat",
                 bg="#f5f6f8", fg=TEXT, font=("Microsoft YaHei", 9),
                 highlightthickness=1, highlightbackground=LINE,
                 insertbackground=TEXT).pack(side=LEFT, padx=6, ipady=4)

        tk.Button(foot, text="标记", command=self.do_flag, relief="flat",
                  bg=ACCENT, fg="#ffffff", activebackground="#2f4f7f",
                  activeforeground="#ffffff", font=("Microsoft YaHei", 9),
                  padx=12, pady=4, cursor="hand2", borderwidth=0).pack(side=LEFT, padx=4)

        if item.get("category"):
            tk.Button(foot, text="取消标记", command=self.do_unflag, relief="flat",
                      bg="#f2f4f8", fg=TEXT, font=("Microsoft YaHei", 9),
                      padx=12, pady=4, cursor="hand2", borderwidth=0).pack(side=LEFT)

    def do_flag(self) -> None:
        label = self.var_cat.get()
        if label not in FLAG_BY_LABEL:
            messagebox.showwarning("提示", "请选择标记分类", parent=self)
            return

        def done(_data, err):
            if err:
                messagebox.showerror("标记失败", err, parent=self)
                return
            if self.on_changed:
                self.on_changed()
            self.destroy()

        self.client_flag(FLAG_BY_LABEL[label], self.var_reason.get().strip(), done)

    def client_flag(self, category: str, reason: str, done) -> None:
        def work():
            try:
                self.client.flag(self.item["id"], category, reason)
                err = None
            except ApiError as e:
                err = e.message
            except Exception as e:                      # noqa: BLE001
                err = str(e)
            self.after(0, lambda: done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def do_unflag(self) -> None:
        def work():
            try:
                self.client.unflag(self.item["id"])
                err = None
            except ApiError as e:
                err = e.message
            except Exception as e:                      # noqa: BLE001
                err = str(e)
            self.after(0, lambda: self._after_unflag(err))

        threading.Thread(target=work, daemon=True).start()

    def _after_unflag(self, err) -> None:
        if err:
            messagebox.showerror("取消失败", err, parent=self)
            return
        if self.on_changed:
            self.on_changed()
        self.destroy()


def _fmt_ts(ms) -> str:
    """毫秒时间戳 → 本地可读时间。"""
    if not ms:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
