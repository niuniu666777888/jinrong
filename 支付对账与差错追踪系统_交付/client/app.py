#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
client/app.py —— 桌面客户端（tkinter，极简版）
================================================================================
设计原则：一屏之内只看得到三样东西——对话、输入框、一个按钮。

    去掉了：彩色气泡、角色前缀、三个顶部按钮、独立状态栏、按钮堆叠。
    保留的：流式回答、来源可点、停止生成。

具体做法：
  · 角色不用标签区分，用对齐方式：我问的靠右，回答靠左；
  · 发送与停止合并成一个按钮，状态切换而不是并排两个；
  · 状态提示写在对话流里（"正在检索制度…"），出现即占位、取消即删除；
  · 来源折成一行"查看 3 条来源"，点开才是列表，不占版面。

线程模型与之前一致：网络在子线程，事件进 queue，主线程 after() 渲染。
tkinter 的控件只能在主线程操作，这条不改。
================================================================================
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
import webbrowser
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, TOP, X, Y
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext
import tkinter as tk

from api import ApiError, ChatClient

STATE_FILE = Path(__file__).with_name("client_state.json")
DEFAULT_SERVER = "http://127.0.0.1:8000"


def _enable_dpi_awareness() -> float:
    """
    让 tkinter 按物理像素渲染。

    不声明 DPI 感知时，Windows 会把整个窗口当位图放大到 150%/200%，
    结果是"窗口很大、字很糊"——自绘客户端最容易被忽略的观感问题。
    返回缩放比，供字号与窗口尺寸换算。
    """
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)      # 1 = 系统级 DPI 感知
        return windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


DPI = _enable_dpi_awareness()

# 界面配色：浅灰蓝背景 + 白色卡片 + 深蓝主色
APP_BG = "#f3f6fb"
BG = "#ffffff"
SURFACE_ALT = "#f7f9fd"
TEXT = "#111827"
MUTED = "#6b7280"
LINE = "#e4e9f2"
ACCENT = "#1f3864"
ACCENT_HOVER = "#2c4f86"
ACCENT_SOFT = "#eaf1fb"
ERROR = "#b42318"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


# ==============================================================================
# 登录页
# ==============================================================================
class LoginFrame(tk.Frame):
    def __init__(self, master, on_success):
        super().__init__(master, bg=APP_BG)
        self.on_success = on_success
        cfg = load_state()

        # 居中卡片：白底 + 细边框 + 品牌标识
        outer = tk.Frame(self, bg=LINE)
        outer.place(relx=0.5, rely=0.48, anchor="center")
        card = tk.Frame(outer, bg=BG)
        card.pack(padx=1, pady=1)

        inner = tk.Frame(card, bg=BG)
        inner.pack(padx=34, pady=30)

        head = tk.Frame(inner, bg=BG)
        head.pack(anchor="w", pady=(0, 20))

        logo = tk.Canvas(head, width=42, height=42, bg=BG,
                         highlightthickness=0, bd=0)
        logo.pack(side=LEFT)
        logo.create_oval(2, 2, 40, 40, fill=ACCENT, outline="")
        logo.create_text(21, 21, text="金", fill="#ffffff",
                         font=("Microsoft YaHei", 15, "bold"))

        titles = tk.Frame(head, bg=BG)
        titles.pack(side=LEFT, padx=(12, 0))
        tk.Label(titles, text="支付对账助手", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei", 16, "bold")).pack(anchor="w")
        tk.Label(titles, text="制度问答 · 差错处理", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(2, 0))

        self.var_server = tk.StringVar(value=cfg.get("server", DEFAULT_SERVER))
        self.var_user = tk.StringVar(value=cfg.get("employee_no", ""))
        self.var_pwd = tk.StringVar()

        def field(label: str, var: tk.StringVar, show: str = ""):
            tk.Label(inner, text=label, bg=BG, fg=MUTED,
                     font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(0, 4))
            wrap = tk.Frame(inner, bg=SURFACE_ALT, highlightbackground=LINE,
                            highlightthickness=1)
            wrap.pack(fill=X, pady=(0, 12))
            e = tk.Entry(wrap, textvariable=var, show=show, width=30,
                         relief="flat", bg=SURFACE_ALT, fg=TEXT,
                         insertbackground=ACCENT,
                         font=("Microsoft YaHei", 11),
                         highlightthickness=0, borderwidth=0)
            e.pack(fill=X, ipady=7, padx=10)
            e.bind("<FocusIn>",
                   lambda ev: wrap.config(highlightbackground=ACCENT))
            e.bind("<FocusOut>",
                   lambda ev: wrap.config(highlightbackground=LINE))
            e.bind("<Return>", lambda ev: self._login())
            return e

        field("服务端地址", self.var_server)
        field("工号", self.var_user)
        pwd_entry = field("密码", self.var_pwd, "*")

        self.btn = tk.Button(inner, text="登录", command=self._login,
                             bg=ACCENT, fg="#ffffff", relief="flat",
                             activebackground=ACCENT_HOVER,
                             activeforeground="#ffffff",
                             font=("Microsoft YaHei", 10, "bold"),
                             cursor="hand2", pady=7, borderwidth=0)
        self.btn.pack(fill=X, pady=(4, 8))
        self.btn.bind("<Enter>", lambda e: self.btn.config(bg=ACCENT_HOVER))
        self.btn.bind("<Leave>", lambda e: self.btn.config(bg=ACCENT))

        self.err = tk.Label(inner, text="", bg=BG, fg=ERROR,
                            font=("Microsoft YaHei", 9), wraplength=300,
                            justify="left")
        self.err.pack(anchor="w")

        tk.Label(inner, text="默认服务端 http://127.0.0.1:8000", bg=BG,
                 fg="#9aa4b2", font=("Microsoft YaHei", 8)).pack(
                     anchor="w", pady=(14, 0))

        pwd_entry.focus_set()

    def _login(self) -> None:
        server = self.var_server.get().strip().rstrip("/")
        user = self.var_user.get().strip()
        pwd = self.var_pwd.get()

        if not server or not user or not pwd:
            self.err.config(text="请填写服务端地址、工号和密码")
            return

        self.btn.config(state="disabled", text="登录中…")
        self.err.config(text="")

        def work():
            client = ChatClient(server)
            try:
                me = client.login(user, pwd)
            except ApiError as e:
                self.after(0, lambda: self._fail(e.message))
                return
            except Exception as e:                      # noqa: BLE001
                self.after(0, lambda: self._fail(f"登录失败：{e}"))
                return

            save_state({"server": server, "employee_no": user})
            self.after(0, lambda: self.on_success(client, me))

        threading.Thread(target=work, daemon=True).start()

    def _fail(self, msg: str) -> None:
        self.btn.config(state="normal", text="登录")
        self.err.config(text=msg)


# ==============================================================================
# 聊天页
# ==============================================================================
class ChatFrame(tk.Frame):
    def __init__(self, master, client: ChatClient, me: dict, on_logout):
        super().__init__(master, bg=BG)
        self.client = client
        self.me = me
        self.on_logout = on_logout

        self.session_id = "s_" + uuid.uuid4().hex[:12]
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.streaming = False
        self.stop_flag = False
        self.pending_sources: list[dict] = []
        self.status_index: str | None = None     # 状态文本在文本域里的位置

        self._build()
        self.after(60, self._drain)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.configure(bg=APP_BG)

        # ---------- 顶栏：品牌 + 用户 + 操作 ----------
        header = tk.Frame(self, bg=BG)
        header.pack(side=TOP, fill=X)

        head = tk.Frame(header, bg=BG)
        head.pack(fill=X, padx=18, pady=10)

        logo = tk.Canvas(head, width=30, height=30, bg=BG,
                         highlightthickness=0, bd=0)
        logo.pack(side=LEFT)
        logo.create_oval(2, 2, 28, 28, fill=ACCENT, outline="")
        logo.create_text(15, 15, text="金", fill="#ffffff",
                         font=("Microsoft YaHei", 10, "bold"))

        titles = tk.Frame(head, bg=BG)
        titles.pack(side=LEFT, padx=(9, 0))
        tk.Label(titles, text="制度助手", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei", 12, "bold")).pack(anchor="w")
        tk.Label(titles, text="支付对账知识库", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(anchor="w")

        actions = tk.Frame(head, bg=BG)
        actions.pack(side=RIGHT)

        def action(text, cmd, color=MUTED):
            b = tk.Button(actions, text=text, command=cmd, bg=BG, fg=color,
                          relief="flat", activebackground=SURFACE_ALT,
                          activeforeground=ACCENT,
                          font=("Microsoft YaHei", 9), cursor="hand2",
                          padx=9, pady=4, borderwidth=0)
            b.pack(side=RIGHT, padx=(2, 0))
            b.bind("<Enter>", lambda e: b.config(bg=SURFACE_ALT))
            b.bind("<Leave>", lambda e: b.config(bg=BG))
            return b

        action("退出", self._logout)
        if self.me.get("role") == "admin":
            action("管理", self._open_admin, ACCENT)
        action("新建", self._new_chat)

        role = "管理员" if self.me.get("role") == "admin" else "员工"
        tk.Label(actions, text=f"{self.me.get('name', '')} · {role}", bg=BG,
                 fg=MUTED, font=("Microsoft YaHei", 8)).pack(
                     side=RIGHT, padx=(0, 14))

        tk.Frame(self, bg=LINE, height=1).pack(side=TOP, fill=X)

        # ---------- 对话卡片 ----------
        body = tk.Frame(self, bg=BG, highlightbackground=LINE,
                        highlightthickness=1)
        body.pack(side=TOP, fill=BOTH, expand=True, padx=14, pady=(12, 8))

        self.text = scrolledtext.ScrolledText(
            body, wrap="word", state="disabled", relief="flat",
            borderwidth=0, highlightthickness=0, bg=BG,
            font=("Microsoft YaHei", 10), padx=26, pady=16,
            cursor="arrow",
        )
        self.text.pack(side=TOP, fill=BOTH, expand=True)

        # 角色靠对齐和浅色底区分，不用重色块堆叠
        self.text.tag_configure("role_user", justify="right", foreground=MUTED,
                                font=("Microsoft YaHei", 8),
                                lmargin1=170, lmargin2=170, rmargin=26,
                                spacing1=16, spacing3=3)
        self.text.tag_configure("user", justify="right", foreground=TEXT,
                                background="#eef3fb",
                                font=("Microsoft YaHei", 10),
                                lmargin1=170, lmargin2=170, rmargin=26,
                                spacing1=2, spacing3=10)
        self.text.tag_configure("role_ai", justify="left", foreground=MUTED,
                                font=("Microsoft YaHei", 8),
                                lmargin1=26, lmargin2=26, spacing1=8,
                                spacing3=3)
        self.text.tag_configure("ai", justify="left", foreground=TEXT,
                                font=("Microsoft YaHei", 10),
                                lmargin1=26, lmargin2=26, rmargin=110,
                                spacing1=2, spacing3=16)
        self.text.tag_configure("link", foreground="#3b6ea5",
                                font=("Microsoft YaHei", 9), underline=True)
        self.text.tag_configure("status", foreground=MUTED,
                                font=("Microsoft YaHei", 9, "italic"),
                                lmargin1=26, lmargin2=26, spacing3=12)
        self.text.tag_configure("error", foreground=ERROR,
                                background="#fef2f2",
                                lmargin1=26, lmargin2=26, rmargin=90,
                                spacing3=12)
        self.text.tag_configure("hint", foreground="#b6bdc9",
                                font=("Microsoft YaHei", 15, "bold"),
                                justify="center", spacing1=90)
        self.text.tag_configure("hint2", foreground="#9aa4b2",
                                font=("Microsoft YaHei", 9),
                                justify="center", spacing3=14)
        self.text.tag_configure("suggest", foreground=ACCENT,
                                font=("Microsoft YaHei", 9), underline=True,
                                justify="center", spacing1=4, spacing3=3)

        # ---------- 输入卡片 ----------
        bottom = tk.Frame(self, bg=APP_BG)
        bottom.pack(side=TOP, fill=X, padx=14, pady=(0, 14))

        row = tk.Frame(bottom, bg=APP_BG)
        row.pack(fill=X)

        input_card = tk.Frame(row, bg=BG, highlightbackground=LINE,
                              highlightthickness=1)
        input_card.pack(side=LEFT, fill=X, expand=True)

        self.input = tk.Text(input_card, height=2, wrap="word", relief="flat",
                             font=("Microsoft YaHei", 10), padx=12, pady=8,
                             bg=SURFACE_ALT, fg=TEXT, insertbackground=ACCENT,
                             highlightthickness=0, borderwidth=0)
        self.input.pack(fill=BOTH, expand=True, padx=1, pady=1)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)
        self.input.bind("<KeyRelease>", lambda e: self._update_count())
        self.input.bind("<FocusIn>",
                        lambda e: input_card.config(highlightbackground=ACCENT))
        self.input.bind("<FocusOut>",
                        lambda e: input_card.config(highlightbackground=LINE))

        self.btn = tk.Button(row, text="发送", command=self._toggle,
                             bg=ACCENT, fg="#ffffff", relief="flat",
                             activebackground=ACCENT_HOVER,
                             activeforeground="#ffffff",
                             font=("Microsoft YaHei", 10, "bold"),
                             cursor="hand2", width=7, pady=8, borderwidth=0)
        self.btn.pack(side=RIGHT, padx=(10, 0), fill=Y)
        self.btn.bind("<Enter>", lambda e: self.btn.config(
            bg=LINE if self.streaming else ACCENT_HOVER))
        self.btn.bind("<Leave>", lambda e: self.btn.config(
            bg=SURFACE_ALT if self.streaming else ACCENT))

        meta = tk.Frame(bottom, bg=APP_BG)
        meta.pack(fill=X, pady=(5, 0))
        tk.Label(meta, text="Enter 发送 · Shift+Enter 换行", bg=APP_BG,
                 fg="#9aa4b2", font=("Microsoft YaHei", 8)).pack(side=LEFT)
        self.count_label = tk.Label(meta, text="0 / 500", bg=APP_BG,
                                    fg="#9aa4b2",
                                    font=("Microsoft YaHei", 8))
        self.count_label.pack(side=RIGHT)

        self._show_empty_hint()
        self.input.focus_set()

    def _update_count(self) -> None:
        n = len(self.input.get("1.0", END).strip())
        self.count_label.config(text=f"{n} / 500",
                                fg=ERROR if n > 500 else "#9aa4b2")

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _insert(self, content: str, tag) -> None:
        self.text.config(state="normal")
        self.text.insert(END, content, tag)
        self.text.config(state="disabled")
        self.text.see(END)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _show_empty_hint(self) -> None:
        """空对话时给一句提示和几个可点击的示例问题。"""
        self.text.config(state="normal")
        self.text.delete("1.0", END)
        self.text.insert(END, "有什么可以帮你？\n", "hint")
        self.text.insert(END, "从制度里找答案，并给出原文引用。\n", "hint2")
        for question in (
            "长款和短款有什么区别？",
            "跨日退款怎么匹配？",
            "差错工单怎么处理？",
        ):
            tag = f"hintq_{uuid.uuid4().hex[:6]}"
            self.text.insert(END, f"{question}\n", ("suggest", tag))
            self.text.tag_bind(tag, "<Button-1>",
                               lambda e, q=question: self._fill_input(q))
            self.text.tag_bind(tag, "<Enter>",
                               lambda e: self.text.config(cursor="hand2"))
            self.text.tag_bind(tag, "<Leave>",
                               lambda e: self.text.config(cursor="arrow"))
        self.text.config(state="disabled")
        self._hint_visible = True

    def _fill_input(self, question: str) -> None:
        self.input.delete("1.0", END)
        self.input.insert("1.0", question)
        self.input.focus_set()
        self._update_count()

    def _clear_hint(self) -> None:
        if getattr(self, "_hint_visible", False):
            self.text.config(state="normal")
            self.text.delete("1.0", END)
            self.text.config(state="disabled")
            self._hint_visible = False

    def _on_enter(self, event):
        if event.state & 0x0001:      # Shift 按下 = 换行
            return None
        self._send()
        return "break"

    def _toggle(self) -> None:
        if self.streaming:
            self._stop()
        else:
            self._send()

    def _send(self) -> None:
        if self.streaming:
            return
        self._clear_hint()
        question = self.input.get("1.0", END).strip()
        if not question:
            return
        if len(question) > 500:
            messagebox.showwarning("提示", "问题不能超过 500 字")
            return

        self.input.delete("1.0", END)
        self._update_count()
        self._insert("你\n", "role_user")
        self._insert(question + "\n", "user")
        self._ai_labeled = False

        # 状态占位：出现即占位，收到第一个字就删掉
        self.text.config(state="normal")
        self.status_index = self.text.index("end-1c")
        self.text.insert(END, "正在检索制度…\n", "status")
        self.text.config(state="disabled")
        self.text.see(END)

        self.pending_sources = []
        self.streaming = True
        self.stop_flag = False
        self.btn.config(text="停止", bg=SURFACE_ALT, fg=MUTED,
                        activebackground=LINE, activeforeground=TEXT)

        threading.Thread(target=self._worker, args=(question,), daemon=True).start()

    def _worker(self, question: str) -> None:
        """后台线程：只读网络，把事件塞队列，绝不碰控件。"""
        try:
            for evt in self.client.chat(self.session_id, question):
                self.queue.put(evt)
                if self.stop_flag:
                    break
            self.queue.put({"type": "_end"})
        except ApiError as e:
            self.queue.put({"type": "error", "code": e.code, "message": e.message})
            self.queue.put({"type": "_end"})
        except Exception as e:                          # noqa: BLE001
            self.queue.put({"type": "error", "code": -1, "message": str(e)})
            self.queue.put({"type": "_end"})

    def _drain(self) -> None:
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _clear_status(self) -> None:
        """删掉状态占位文本。只删一次，避免把已经生成的回答删了。"""
        if self.status_index is None:
            return
        self.text.config(state="normal")
        self.text.delete(self.status_index, f"{self.status_index} lineend +1c")
        self.text.config(state="disabled")
        self.status_index = None

    def _handle(self, evt: dict) -> None:
        kind = evt.get("type")

        if kind == "status":
            # 检索阶段更新文案；生成阶段不再显示，保持干净
            if self.status_index is not None:
                self.text.config(state="normal")
                self.text.delete(self.status_index, f"{self.status_index} lineend")
                self.text.insert(self.status_index, evt.get("text", ""), "status")
                self.text.config(state="disabled")

        elif kind == "sources":
            self.pending_sources = evt.get("items") or []

        elif kind == "delta":
            self._clear_status()
            if not getattr(self, "_ai_labeled", False):
                self._insert("制度助手\n", "role_ai")
                self._ai_labeled = True
            self.text.config(state="normal")
            self.text.insert(END, evt.get("text", ""), "ai")
            self.text.config(state="disabled")
            self.text.see(END)

        elif kind == "done":
            self._append_sources_link()
            self._insert("\n", "ai")
            if evt.get("degraded"):
                self._insert("（未检索到相关制度或额度已用完）\n", "status")

        elif kind == "error":
            self._clear_status()
            self._insert(f"[{evt.get('code')}] {evt.get('message')}\n", "error")

        elif kind == "_end":
            self._finish()

    def _finish(self) -> None:
        if self.stop_flag:
            self._insert("（已停止）\n", "status")
        self._clear_status()
        self.streaming = False
        self.btn.config(text="发送", bg=ACCENT, fg="#ffffff",
                        activebackground=ACCENT_HOVER, activeforeground="#ffffff")
        self.input.focus_set()

    # ------------------------------------------------------------------
    # 来源：默认只占一行，点开才展开
    # ------------------------------------------------------------------
    def _append_sources_link(self) -> None:
        if not self.pending_sources:
            return
        n = len(self.pending_sources)
        tag = f"src_{uuid.uuid4().hex[:6]}"
        self.text.config(state="normal")
        self.text.insert(END, f"\n查看 {n} 条来源\n", ("link", tag))
        self.text.config(state="disabled")
        self.text.tag_bind(tag, "<Button-1>", lambda e: self._show_sources())
        self.text.tag_bind(tag, "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind(tag, "<Leave>", lambda e: self.text.config(cursor="arrow"))

    def _show_sources(self) -> None:
        """来源列表独立小窗，看完即关，不打断对话。"""
        win = tk.Toplevel(self, bg=BG)
        win.title("来源")
        win.geometry("640x420")
        win.transient(self)

        box = tk.Text(win, wrap="word", relief="flat", bg=BG, fg=TEXT,
                      font=("Microsoft YaHei", 9), padx=16, pady=12,
                      borderwidth=0, highlightthickness=0)
        box.pack(fill=BOTH, expand=True)

        for i, s in enumerate(self.pending_sources, 1):
            box.insert(END, f"[{i}] {s['file_name']}\n", ("h",))
            box.insert(END, f"第 {s['start']}–{s['end']} 字 · 匹配度 {s['score']}\n", ("m",))
            box.insert(END, s["snippet"] + "\n\n", ("b",))
            tag = f"open_{i}"
            box.insert(END, "查看原文\n\n", ("l", tag))
            box.tag_bind(tag, "<Button-1>",
                         lambda e, cid=s["chunk_id"]: self._open_chunk(cid))

        box.tag_configure("h", font=("Microsoft YaHei", 9, "bold"), foreground=ACCENT,
                          spacing1=8)
        box.tag_configure("m", foreground=MUTED, font=("Microsoft YaHei", 8))
        box.tag_configure("b", foreground=TEXT, spacing3=2)
        box.tag_configure("l", foreground="#3b6ea5", underline=True,
                          font=("Microsoft YaHei", 8))
        box.config(state="disabled")

    def _open_chunk(self, chunk_id: str) -> None:
        try:
            data = self.client.fetch_source(chunk_id)
        except ApiError as e:
            messagebox.showerror("读取失败", e.message)
            return

        win = tk.Toplevel(self, bg=BG)
        win.title(f"{data.get('file_name', '')} · 第 {data.get('start')}–{data.get('end')} 字")
        win.geometry("720x520")
        box = scrolledtext.ScrolledText(
            win, wrap="word", relief="flat", bg=BG, fg=TEXT,
            font=("Microsoft YaHei", 10), padx=18, pady=14,
            borderwidth=0, highlightthickness=0,
        )
        box.pack(fill=BOTH, expand=True)
        box.insert("1.0", data.get("content", ""))
        box.config(state="disabled")

    # ------------------------------------------------------------------
    def _new_chat(self) -> None:
        if self.streaming:
            if not messagebox.askyesno("提示", "正在生成回答，确定新建对话？"):
                return
            self._stop()
        self.session_id = "s_" + uuid.uuid4().hex[:12]
        self.status_index = None
        self._ai_labeled = False
        self._show_empty_hint()
        self.input.delete("1.0", END)
        self._update_count()
        self.input.focus_set()

    def _open_admin(self) -> None:
        """在客户端内打开管理面板，而不是跳浏览器。"""
        from admin_ui import AdminWindow
        AdminWindow(self, self.client)

    def _logout(self) -> None:
        if self.streaming:
            self._stop()
        self.client.logout()
        self.on_logout()

    def _stop(self) -> None:
        self.stop_flag = True


# ==============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()

        # 声明 DPI 感知后，Tk 仍按 96dpi 算字号，所以手动把缩放比告诉它，
        # 否则字会显得特别小（高分屏上尤其明显）
        try:
            self.tk.call("tk", "scaling", 96.0 * DPI / 72.0)
        except Exception:
            pass

        self.title("支付对账助手 · 制度问答")

        # 窗口按逻辑尺寸 × 缩放比计算，再夹到屏幕范围内，避免小本上装不下
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(900 * DPI), int(sw * 0.92))
        h = min(int(700 * DPI), int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.minsize(int(640 * DPI), int(500 * DPI))
        self.configure(bg=APP_BG)

        tkfont.nametofont("TkDefaultFont").configure(family="Microsoft YaHei", size=10)

        self.current = None
        self._show_login()

    def _swap(self, frame) -> None:
        if self.current is not None:
            self.current.destroy()
        self.current = frame
        frame.pack(fill=BOTH, expand=True)

    def _show_login(self) -> None:
        self._swap(LoginFrame(self, on_success=self._on_login))

    def _on_login(self, client: ChatClient, me: dict) -> None:
        self._swap(ChatFrame(self, client, me, on_logout=self._show_login))


if __name__ == "__main__":
    App().mainloop()
