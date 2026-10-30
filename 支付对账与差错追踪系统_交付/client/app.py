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

# 配色只留四个：白底、正文黑、辅助灰、主色深蓝
BG = "#ffffff"
TEXT = "#1f2937"
MUTED = "#8a94a6"
LINE = "#e8ebf0"
ACCENT = "#1f3864"


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
        super().__init__(master, bg=BG)
        self.on_success = on_success
        cfg = load_state()

        # 居中一块，四周留白；不再有标题横幅和大按钮
        card = tk.Frame(self, bg=BG)
        card.place(relx=0.5, rely=0.46, anchor="center")

        tk.Label(card, text="支付对账助手", bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei", 18, "bold")).pack(anchor="w")
        tk.Label(card, text="请登录后使用", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(4, 22))

        self.var_server = tk.StringVar(value=cfg.get("server", DEFAULT_SERVER))
        self.var_user = tk.StringVar(value=cfg.get("employee_no", ""))
        self.var_pwd = tk.StringVar()

        def field(label: str, var: tk.StringVar, show: str = ""):
            tk.Label(card, text=label, bg=BG, fg=MUTED,
                     font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(0, 3))
            e = tk.Entry(card, textvariable=var, show=show, width=30,
                         relief="flat", bg="#f5f6f8", fg=TEXT,
                         insertbackground=TEXT, font=("Microsoft YaHei", 10),
                         highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT)
            e.pack(anchor="w", ipady=6, ipadx=6, pady=(0, 12))
            e.bind("<Return>", lambda ev: self._login())
            return e

        field("服务端地址", self.var_server)
        field("工号", self.var_user)
        pwd_entry = field("密码", self.var_pwd, "*")

        self.btn = tk.Button(card, text="登录", command=self._login,
                             bg=ACCENT, fg="#ffffff", relief="flat",
                             activebackground="#2f4f7f", activeforeground="#ffffff",
                             font=("Microsoft YaHei", 10), cursor="hand2",
                             width=30, pady=6, borderwidth=0)
        self.btn.pack(anchor="w", pady=(6, 10))

        self.err = tk.Label(card, text="", bg=BG, fg="#a61b1b",
                            font=("Microsoft YaHei", 9), wraplength=260,
                            justify="left")
        self.err.pack(anchor="w")

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
        # ---------- 顶栏：只有名字和三个文字链接 ----------
        top = tk.Frame(self, bg=BG, height=44)
        top.pack(side=TOP, fill=X)
        top.pack_propagate(False)

        tk.Label(top, text="制度助手", bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei", 12, "bold")).pack(side=LEFT, padx=(18, 0))

        def text_button(text, cmd, pad=(10, 0), color=MUTED):
            lb = tk.Label(top, text=text, bg=BG, fg=color,
                          font=("Microsoft YaHei", 9), cursor="hand2")
            lb.pack(side=RIGHT, padx=pad)
            lb.bind("<Button-1>", lambda e: cmd())
            lb.bind("<Enter>", lambda e: lb.config(fg=ACCENT))
            lb.bind("<Leave>", lambda e: lb.config(fg=color))
            return lb

        text_button("退出", self._logout, pad=(18, 0))
        if self.me.get("role") == "admin":
            # 管理员多一个入口，用主色标出来，避免藏在灰色链接里
            text_button("管理", self._open_admin, color=ACCENT)
        text_button("新建", self._new_chat)

        tk.Label(top, text=f"{self.me.get('name', '')}", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side=RIGHT, padx=(0, 4))

        tk.Frame(self, bg=LINE, height=1).pack(side=TOP, fill=X)

        # ---------- 对话区 ----------
        self.text = scrolledtext.ScrolledText(
            self, wrap="word", state="disabled", relief="flat",
            borderwidth=0, highlightthickness=0, bg=BG,
            font=("Microsoft YaHei", 10), padx=22, pady=14,
            cursor="arrow",
        )
        self.text.pack(side=TOP, fill=BOTH, expand=True)

        # 角色靠对齐区分，不用颜色堆叠
        self.text.tag_configure("user", justify="right", foreground=ACCENT,
                                spacing1=18, spacing3=2, lmargin1=90, lmargin2=90)
        self.text.tag_configure("ai", justify="left", foreground=TEXT,
                                spacing1=2, spacing3=16, rmargin=60)
        self.text.tag_configure("link", foreground="#3b6ea5",
                                font=("Microsoft YaHei", 9), underline=True)
        self.text.tag_configure("status", foreground=MUTED,
                                font=("Microsoft YaHei", 9), spacing3=14)
        self.text.tag_configure("error", foreground="#a61b1b", spacing3=14)
        self.text.tag_configure("hint", foreground="#b6bdc9",
                                font=("Microsoft YaHei", 11), justify="center",
                                spacing1=90)
        self.text.tag_configure("hint2", foreground="#cdd3dc",
                                font=("Microsoft YaHei", 9), justify="center",
                                spacing3=20)

        # ---------- 输入区 ----------
        tk.Frame(self, bg=LINE, height=1).pack(side=TOP, fill=X)

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(side=TOP, fill=X, padx=18, pady=(12, 16))

        self.input = tk.Text(bottom, height=2, wrap="word", relief="flat",
                             font=("Microsoft YaHei", 10), padx=10, pady=8,
                             bg="#f5f6f8", fg=TEXT, insertbackground=TEXT,
                             highlightthickness=1, highlightbackground=LINE,
                             highlightcolor=ACCENT, borderwidth=0)
        self.input.pack(side=LEFT, fill=X, expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)

        # 发送/停止合并成一个按钮：状态由文字与颜色切换
        self.btn = tk.Button(bottom, text="发送", command=self._toggle,
                             bg=ACCENT, fg="#ffffff", relief="flat",
                             activebackground="#2f4f7f", activeforeground="#ffffff",
                             font=("Microsoft YaHei", 10), cursor="hand2",
                             width=6, pady=6, borderwidth=0)
        self.btn.pack(side=RIGHT, padx=(10, 0), fill=Y)

        self._show_empty_hint()
        self.input.focus_set()

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
        """空对话时给一句提示，第一条消息发出后清掉，不常驻。"""
        self.text.config(state="normal")
        self.text.delete("1.0", END)
        self.text.insert(END, "输入问题开始\n", "hint")
        self.text.insert(END, "例如：长款和短款有什么区别？\n", "hint2")
        self.text.config(state="disabled")
        self._hint_visible = True

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
        self._insert(question + "\n", "user")

        # 状态占位：出现即占位，收到第一个字就删掉
        self.text.config(state="normal")
        self.status_index = self.text.index("end-1c")
        self.text.insert(END, "正在检索制度…\n", "status")
        self.text.config(state="disabled")
        self.text.see(END)

        self.pending_sources = []
        self.streaming = True
        self.stop_flag = False
        self.btn.config(text="停止", bg="#e8ebf0", fg="#4b5563",
                        activebackground="#dbe0e8", activeforeground="#4b5563")

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
                        activebackground="#2f4f7f", activeforeground="#ffffff")
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
        self._show_empty_hint()
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

        self.title("支付对账助手")

        # 窗口按逻辑尺寸 × 缩放比计算，再夹到屏幕范围内，避免小本上装不下
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(900 * DPI), int(sw * 0.92))
        h = min(int(660 * DPI), int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.minsize(int(640 * DPI), int(460 * DPI))
        self.configure(bg=BG)

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
