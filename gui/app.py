"""
Alpha Hive GUI - 主应用
AlphaHiveApp
"""

import sys
import json
import tkinter as tk
import time
import sqlite3
import os
import logging as _logging
from datetime import datetime
from threading import Thread
from pathlib import Path

from gui.animations import PixelBee, HoneycombBackground
from gui.interactions import InteractionManager
from gui.views import InfoPanel, ReportView, ChatLog

_log = _logging.getLogger("alpha_hive.app")

# 确保项目目录在 import 路径中
_PROJECT_ROOT = os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)



class AlphaHiveApp:
    CANVAS_WIDTH = 720
    CANVAS_HEIGHT = 712      # 680 + 32 收藏栏
    PANEL_WIDTH = 200
    HIVE_HEIGHT = 480        # 蜂巢区域高度
    CHAT_HEIGHT = 160        # 聊天框高度
    INPUT_HEIGHT = 40        # 输入框高度
    PRESET_HEIGHT = 32       # C1: 收藏栏高度
    FPS = 30

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Alpha Hive")
        self.root.resizable(False, True)   # D1: 允许垂直拉伸
        self.root.minsize(720, 640)
        self.root.configure(bg="#0A0A0A")

        # macOS .app 启动时强制窗口前置
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        try:
            # 激活 Python 进程（macOS 需要这一步才能显示窗口）
            os.system('''/usr/bin/osascript -e 'tell app "System Events" to set frontmost of first process whose unix id is %d to true' ''' % os.getpid())
        except OSError as e:
            _log.debug("macOS window activation failed: %s", e)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - self.CANVAS_WIDTH) // 2
        y = (screen_h - self.CANVAS_HEIGHT) // 2
        self.root.geometry(f"{self.CANVAS_WIDTH}x{self.CANVAS_HEIGHT}+{x}+{y}")
        self._last_window_height = self.CANVAS_HEIGHT  # D1: 跟踪上次高度

        self.canvas = tk.Canvas(
            self.root, width=self.CANVAS_WIDTH, height=self.CANVAS_HEIGHT,
            bg="#0D0D00", highlightthickness=0
        )
        self.canvas.pack()

        hive_w = self.CANVAS_WIDTH - self.PANEL_WIDTH
        HoneycombBackground(self.canvas, hive_w, self.HIVE_HEIGHT).draw()

        self.canvas.create_text(
            hive_w // 2, 25, text="ALPHA HIVE",
            fill="#FFB800", font=("Monaco", 18, "bold")
        )
        self.canvas.create_text(
            hive_w // 2, 45,
            text="Decentralized Investment Research Swarm",
            fill="#666600", font=("Monaco", 9)
        )

        # 蜜蜂
        self.bees = {}
        positions = [
            ("ScoutBeeNova",       90, 100),
            ("OracleBeeEcho",     270,  90),
            ("BuzzBeeWhisper",    430, 105),
            ("ChronosBeeHorizon",  80, 220),
            ("RivalBeeVanguard",  270, 230),
            ("GuardBeeSentinel",  430, 220),
            ("CodeExecutorAgent", 270, 350),
        ]
        for agent_id, bx, by in positions:
            self.bees[agent_id] = PixelBee(self.canvas, agent_id, bx, by, pixel_size=5)

        # 聊天框（在蜂巢区域下方）
        self.chat_log = ChatLog(
            self.canvas,
            x=0, y=self.HIVE_HEIGHT,
            width=hive_w, height=self.CHAT_HEIGHT
        )

        # 互动管理器（注入 chat_log 引用 + app 反向引用）
        self.interactions = InteractionManager(self.canvas, self.bees, self.chat_log)
        self.interactions._app_ref = self  # 用于更新面板数据

        # 信息面板（占满右侧全高）
        self.panel = InfoPanel(self.canvas, hive_w, 0, self.PANEL_WIDTH, self.CANVAS_HEIGHT)

        # 8 版块简报视图（覆盖蜂巢区域，按 R 切换）
        self.report_view = ReportView(self.canvas, hive_w, self.HIVE_HEIGHT)
        self.last_swarm_results = {}  # 保存最近一次扫描结果

        self.system_data = {
            "status": "IDLE", "agent_count": 7,
            "opportunities": [], "board_entries": 0,
            "memory_docs": 0, "slack": "connected",
        }

        self.running = True
        self.tick = 0

        self._start_data_refresh()

        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<space>", lambda e: self._on_space())
        self.root.bind("r", lambda e: self._toggle_report())
        self.root.bind("R", lambda e: self._toggle_report())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        # 滚动聊天框
        self.root.bind("<Up>", lambda e: self.chat_log.scroll_up())
        self.root.bind("<Down>", lambda e: self.chat_log.scroll_down())

        # A3: 鼠标滚轮
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        # B1/B2: 画布点击
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        # D1: 窗口高度变化
        self.root.bind("<Configure>", self._on_window_resize)

        # ====== 输入框（自定义 ticker 扫描）======
        input_y = self.HIVE_HEIGHT + self.CHAT_HEIGHT
        # 背景（D1: 保存 ID 供 resize 时移动）
        self._input_bg_id = self.canvas.create_rectangle(
            0, input_y, hive_w, input_y + self.INPUT_HEIGHT,
            fill="#0A0A00", outline="#333300"
        )
        # 标签
        self._input_label_id = self.canvas.create_text(
            8, input_y + self.INPUT_HEIGHT // 2,
            text="标的:", fill="#888800", font=("Monaco", 11),
            anchor="w"
        )
        # 输入框（tkinter Entry 嵌入 canvas）
        self.ticker_entry = tk.Entry(
            self.root,
            bg="#1A1A00", fg="#FFB800", insertbackground="#FFB800",
            font=("Monaco", 12), relief="flat", highlightthickness=1,
            highlightbackground="#444400", highlightcolor="#FFB800",
        )
        self._ticker_win_id = self.canvas.create_window(
            50, input_y + self.INPUT_HEIGHT // 2,
            window=self.ticker_entry,
            width=hive_w - 130, height=24,
            anchor="w"
        )
        self.ticker_entry.insert(0, self._load_last_tickers())  # C3: 加载上次标的
        # 回车键触发扫描
        self.ticker_entry.bind("<Return>", lambda e: self._on_ticker_submit())
        # 扫描按钮
        self.scan_btn = tk.Button(
            self.root, text="扫描",
            bg="#332200", fg="#FFB800", font=("Monaco", 10, "bold"),
            relief="flat", activebackground="#554400", activeforeground="#FFD700",
            command=self._on_ticker_submit,
        )
        self._scan_btn_win_id = self.canvas.create_window(
            hive_w - 45, input_y + self.INPUT_HEIGHT // 2,
            window=self.scan_btn,
            width=60, height=26,
        )

        # ====== C1: 收藏栏（常用标的预设）======
        preset_y = input_y + self.INPUT_HEIGHT
        self._preset_bg_id = self.canvas.create_rectangle(
            0, preset_y, hive_w, preset_y + self.PRESET_HEIGHT,
            fill="#050500", outline="#1A1A00"
        )
        self._preset_label_id = self.canvas.create_text(
            8, preset_y + self.PRESET_HEIGHT // 2,
            text="⭐", fill="#554400", font=("Monaco", 10), anchor="w"
        )
        self._presets = self._load_presets()
        self._preset_btns = []
        self._preset_btn_windows = []
        for i in range(3):
            label = (self._presets[i] if i < len(self._presets) else f"槽{i+1}")[:16]
            btn = tk.Button(
                self.root, text=label,
                bg="#1A1200", fg="#AA8800", font=("Monaco", 9),
                relief="flat", activebackground="#2A2000", activeforeground="#FFB800",
                command=lambda idx=i: self._on_preset_click(idx),
            )
            btn.bind("<Button-3>", lambda e, idx=i: self._on_preset_right_click(e, idx))
            wid = self.canvas.create_window(
                30 + i * 158, preset_y + self.PRESET_HEIGHT // 2,
                window=btn, width=150, height=22, anchor="w"
            )
            self._preset_btns.append(btn)
            self._preset_btn_windows.append(wid)

        # 操作提示
        self.canvas.create_text(
            hive_w // 2, self.HIVE_HEIGHT - 15,
            text="[SPACE] 扫描  |  [R] 简报  |  [C] 复制  |  [ESC] 退出",
            fill="#444400", font=("Monaco", 9)
        )

        # A1: 进度条（扫描时显示，初始隐藏）
        pb_y = self.HIVE_HEIGHT - 30
        self._pb_bg = self.canvas.create_rectangle(
            0, pb_y, hive_w, pb_y + 14, fill="#111100", outline="#333300", state="hidden"
        )
        self._pb_fill = self.canvas.create_rectangle(
            0, pb_y + 1, 0, pb_y + 13, fill="#FFB800", outline="", state="hidden"
        )
        self._pb_text = self.canvas.create_text(
            hive_w // 2, pb_y + 7, text="", fill="#000000",
            font=("Monaco", 8, "bold"), state="hidden"
        )

        # 启动时加载上次扫描结果（如有）
        self._load_last_swarm_results()

        # 欢迎消息
        self.chat_log.add("System", "Alpha Hive 桌面应用已启动", "system")
        if self.last_swarm_results:
            n = len(self.last_swarm_results)
            self.chat_log.add("System", f"已加载上次扫描数据（{n} 标的），按 [R] 查看简报", "system")
        else:
            self.chat_log.add("System", "按空格键扫描默认标的，或输入框输入自定义标的后回车", "system")

    def _on_space(self):
        """空格键：扫描默认 watchlist，或取消进行中的扫描"""
        if self.interactions.scan_phase != "idle":
            self.interactions._cancel_requested = True
            self.chat_log.add("System", "⚠ 正在取消扫描...", "alert")
            return
        if self.report_view.visible:
            self.report_view.toggle()
        self.interactions.run_scan_sequence(focus_tickers=None)

    def _toggle_report(self):
        """R 键：切换 8 版块简报视图"""
        if not self.last_swarm_results:
            self.chat_log.add("System", "暂无扫描数据。请先按空格键运行扫描。", "system")
            return
        self.report_view.toggle(self.last_swarm_results)
        if self.report_view.visible:
            # 简报模式：上下键滚动简报，C 键复制
            self.root.bind("<Up>", lambda e: self.report_view.scroll(-30))
            self.root.bind("<Down>", lambda e: self.report_view.scroll(30))
            self.root.bind("c", lambda e: self._copy_results())
            self.root.bind("C", lambda e: self._copy_results())
        else:
            # 恢复
            self.root.bind("<Up>", lambda e: self.chat_log.scroll_up())
            self.root.bind("<Down>", lambda e: self.chat_log.scroll_down())
            self.root.unbind("c")
            self.root.unbind("C")

    def _load_last_swarm_results(self):
        """启动时加载上次 .swarm_results JSON（如有）"""
        from datetime import datetime as _dt
        import glob as _glob
        try:
            today = _dt.now().strftime("%Y-%m-%d")
            pattern = os.path.join(_PROJECT_ROOT, f".swarm_results_{today}.json")
            files = _glob.glob(pattern)
            if not files:
                # 尝试最近 3 天
                for d in range(1, 4):
                    past = (_dt.now() - __import__('datetime').timedelta(days=d)).strftime("%Y-%m-%d")
                    files = _glob.glob(os.path.join(_PROJECT_ROOT, f".swarm_results_{past}.json"))
                    if files:
                        break
            if files:
                with open(files[0]) as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    self.last_swarm_results = data
                    # 也更新面板
                    opps = []
                    for t, d in sorted(data.items(), key=lambda x: x[1].get("final_score", 0) if isinstance(x[1], dict) else 0, reverse=True):
                        if isinstance(d, dict):
                            opps.append({"ticker": t, "score": d.get("final_score", 0), "direction": d.get("direction", "neutral")})
                    if opps:
                        self.system_data["opportunities"] = opps[:4]
                    first = next((d for d in data.values() if isinstance(d, dict) and d.get("dimension_scores")), None)
                    if first:
                        self.system_data["dimension_scores"] = {k: float(v) for k, v in first["dimension_scores"].items()}
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError) as e:
            _log.debug("Last swarm results load failed: %s", e)

    def _on_ticker_submit(self):
        """输入框回车或扫描按钮：扫描自定义标的，或取消进行中的扫描"""
        # A2: 扫描中点击 → 取消
        if self.interactions.scan_phase != "idle":
            self.interactions._cancel_requested = True
            self.chat_log.add("System", "⚠ 正在取消扫描...", "alert")
            return

        text = self.ticker_entry.get().strip()
        if not text:
            self.chat_log.add("System", "请输入标的代码（空格分隔，如 NVDA TSLA MSFT）", "system")
            return

        # 解析输入（支持空格、逗号、分号分隔）
        import re
        tickers = [t.strip().upper() for t in re.split(r'[,;\s]+', text) if t.strip()]

        if not tickers:
            self.chat_log.add("System", "无法解析标的代码", "system")
            return

        self._save_last_tickers(tickers)  # C3: 持久化上次标的
        self.chat_log.add("System", f"开始扫描自定义标的: {', '.join(tickers)}", "system")
        self.interactions.run_scan_sequence(focus_tickers=tickers)

    # ==================== A3: 鼠标滚轮 ====================

    def _on_mousewheel(self, event):
        """鼠标滚轮在聊天区域内滚动"""
        x, y = event.x, event.y
        hive_w = self.CANVAS_WIDTH - self.PANEL_WIDTH
        chat_y0 = self.HIVE_HEIGHT
        chat_y1 = chat_y0 + self.chat_log.height
        if 0 <= x <= hive_w and chat_y0 <= y <= chat_y1:
            if event.delta > 0:
                self.chat_log.scroll_up()
            else:
                self.chat_log.scroll_down()

    # ==================== B1/B2: 画布点击 ====================

    def _on_canvas_click(self, event):
        """画布点击：蜜蜂详情弹窗 / 面板机会跳简报"""
        x, y = event.x, event.y
        hive_w = self.CANVAS_WIDTH - self.PANEL_WIDTH

        # B2: 点击面板区域 → 检查机会列表
        if x >= hive_w:
            for y1, y2, ticker in getattr(self.panel, "opportunity_regions", []):
                if y1 <= y <= y2 and ticker:
                    self._open_report_for_ticker(ticker)
                    return
            return

        # B1: 点击蜂巢区域 → 检查是否点中某只蜜蜂
        for agent_id, bee in self.bees.items():
            if bee.home_x - 6 <= x <= bee.home_x + 46 and bee.home_y - 6 <= y <= bee.home_y + 42:
                self._show_bee_popup(bee)
                return

    def _show_bee_popup(self, bee):
        """B1: 弹出蜜蜂最近分析详情"""
        if not bee.last_analysis:
            self.chat_log.add("System", f"{bee.colors.get('label', bee.agent_id)} 尚无分析数据，请先运行扫描", "system")
            return

        popup = tk.Toplevel(self.root)
        popup.title(f"{bee.colors.get('label', bee.agent_id)}  分析详情")
        popup.configure(bg="#0A0A00")
        popup.resizable(True, True)
        popup.geometry(f"420x320+{self.root.winfo_x()+120}+{self.root.winfo_y()+80}")

        a = bee.last_analysis
        ticker = a.get("ticker", "N/A")
        score = a.get("score", 0)
        direction = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(a.get("direction", ""), a.get("direction", ""))
        c = bee.colors

        tk.Label(popup, text=f"  {c.get('label', bee.agent_id)}  ",
                 bg=c.get("body", "#FFB800"), fg="#000000",
                 font=("Monaco", 12, "bold")).pack(fill=tk.X)

        tk.Label(popup, text=f"{ticker}  {score:.1f}/10  {direction}",
                 bg="#0A0A00", fg="#FFB800", font=("Monaco", 11, "bold")).pack(pady=(6, 2))

        frame = tk.Frame(popup, bg="#0A0A00")
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        txt = tk.Text(frame, bg="#0D0D00", fg="#CCCCCC", font=("Monaco", 9),
                      wrap=tk.WORD, relief="flat", padx=6, pady=6)
        sb = tk.Scrollbar(frame, command=txt.yview, bg="#222200")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        discovery = a.get("discovery", "（无发现摘要）")
        txt.insert(tk.END, f"发现：\n{discovery}\n\n")
        source = a.get("source", "")
        if source:
            txt.insert(tk.END, f"来源：{source}\n\n")
        for k, v in a.items():
            if k not in ("discovery", "source", "ticker", "error", "direction", "score"):
                txt.insert(tk.END, f"{k}: {v}\n")
        if a.get("error"):
            txt.insert(tk.END, f"\n⚠ 错误：{a['error']}")
        txt.configure(state=tk.DISABLED)

        tk.Button(popup, text="关闭", command=popup.destroy,
                  bg="#221100", fg="#FFB800", relief="flat",
                  font=("Monaco", 10)).pack(pady=6)
        popup.transient(self.root)

    def _open_report_for_ticker(self, ticker):
        """B2: 点击面板机会 → 打开简报并跳到该 ticker"""
        if not self.last_swarm_results:
            self.chat_log.add("System", "暂无扫描数据", "system")
            return
        if not self.report_view.visible:
            self.report_view.toggle(self.last_swarm_results)
            self.root.bind("<Up>", lambda e: self.report_view.scroll(-30))
            self.root.bind("<Down>", lambda e: self.report_view.scroll(30))
            self.root.bind("c", lambda e: self._copy_results())
            self.root.bind("C", lambda e: self._copy_results())
        sorted_tickers = sorted(
            self.last_swarm_results.keys(),
            key=lambda t: self.last_swarm_results[t].get("final_score", 0),
            reverse=True
        )
        idx = sorted_tickers.index(ticker) if ticker in sorted_tickers else 0
        self.report_view.scroll_to_ticker(idx)
        self.chat_log.add("System", f"已跳转到 {ticker} 章节", "system")

    # ==================== B3: 复制结果 ====================

    def _copy_results(self):
        """B3: 复制扫描结果到系统剪贴板"""
        if not self.last_swarm_results:
            return
        lines = [f"Alpha Hive 扫描结果 {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        for ticker, data in sorted(self.last_swarm_results.items(),
                                   key=lambda x: x[1].get("final_score", 0), reverse=True):
            score = data.get("final_score", 0)
            d_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(data.get("direction", ""), "")
            tag = "【高优先】" if score >= 7.5 else ("【观察】" if score >= 6.0 else "【暂不动】")
            lines.append(f"{tag} {ticker}: {score:.1f}/10 {d_cn}")
            discovery = data.get("discovery", "")
            if discovery:
                lines.append(f"  → {discovery[:120]}")
        lines += ["", "⚠ 本报告为公开信息研究，不构成投资建议"]
        text = "\n".join(lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.chat_log.add("System", f"✓ 已复制 {len(self.last_swarm_results)} 标的结果到剪贴板", "system")

    # ==================== A1: 进度条 ====================

    def _update_progress_bar(self):
        """A1: 更新扫描进度条"""
        hive_w = self.CANVAS_WIDTH - self.PANEL_WIDTH
        prog = self.interactions.scan_progress
        is_scanning = self.interactions.scan_phase != "idle"
        state = "normal" if is_scanning else "hidden"
        self.canvas.itemconfig(self._pb_bg, state=state)
        self.canvas.itemconfig(self._pb_fill, state=state)
        self.canvas.itemconfig(self._pb_text, state=state)
        if is_scanning:
            total = max(1, prog.get("total", 1))
            current = prog.get("current", 0)
            ratio = min(1.0, current / total)
            pb_y = self.HIVE_HEIGHT - 30
            self.canvas.coords(self._pb_fill, 0, pb_y + 1, hive_w * ratio, pb_y + 13)
            phase_cn = {
                "foraging": "采集中", "resonating": "共振中",
                "distilling": "蒸馏中", "dancing": "摆尾舞", "decomposing": "分解中",
            }.get(prog.get("phase", ""), prog.get("phase", ""))
            label = f"{prog.get('ticker', '')}  {current}/{total}  {phase_cn}"
            self.canvas.itemconfig(self._pb_text, text=label)

    # ==================== C1: 收藏预设 ====================

    def _load_presets(self) -> list:
        """C1: 加载收藏标的预设"""
        try:
            p = Path(os.path.expanduser("~/.alphahive_presets.json"))
            if p.exists():
                data = json.loads(p.read_text())
                if isinstance(data, list) and data:
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return ["NVDA MSFT AAPL", "TSLA AMD QCOM", "JNJ BIIB MRNA"]

    def _save_presets(self):
        """C1: 保存收藏预设"""
        try:
            Path(os.path.expanduser("~/.alphahive_presets.json")).write_text(
                json.dumps(self._presets, ensure_ascii=False)
            )
        except OSError:
            pass

    def _on_preset_click(self, idx):
        """C1: 点击收藏按钮 → 填充输入框"""
        if idx < len(self._presets):
            self.ticker_entry.delete(0, tk.END)
            self.ticker_entry.insert(0, self._presets[idx])

    def _on_preset_right_click(self, event, idx):
        """C1: 右键收藏按钮 → 保存当前输入为新预设"""
        current = self.ticker_entry.get().strip()
        if not current:
            return
        self._presets[idx] = current
        label = current[:16]
        self._preset_btns[idx].configure(text=label)
        self._save_presets()
        self.chat_log.add("System", f"⭐ 收藏槽 {idx+1} 已保存：{current}", "system")

    # ==================== C3: 记住标的 ====================

    def _load_last_tickers(self) -> str:
        """C3: 加载上次使用的标的"""
        try:
            p = Path(os.path.expanduser("~/.alphahive_last_tickers"))
            if p.exists():
                text = p.read_text().strip()
                if text:
                    return text
        except OSError:
            pass
        return "NVDA TSLA MSFT"

    def _save_last_tickers(self, tickers):
        """C3: 持久化当前标的"""
        try:
            Path(os.path.expanduser("~/.alphahive_last_tickers")).write_text(" ".join(tickers))
        except OSError:
            pass

    # ==================== D1: 窗口拉伸响应 ====================

    def _on_window_resize(self, event):
        """D1: 窗口高度变化 → 聊天框扩展，底部控件下移"""
        if event.widget != self.root:
            return
        new_h = event.height
        if abs(new_h - self._last_window_height) < 2 or new_h < 640:
            return
        delta = new_h - self._last_window_height
        self._last_window_height = new_h
        hive_w = self.CANVAS_WIDTH - self.PANEL_WIDTH

        # 扩展聊天框
        self.chat_log.height = max(80, self.chat_log.height + delta)

        # 重新计算各行 y 坐标
        new_input_y = self.HIVE_HEIGHT + self.chat_log.height
        new_preset_y = new_input_y + self.INPUT_HEIGHT

        # 移动输入行
        self.canvas.coords(self._input_bg_id, 0, new_input_y, hive_w, new_input_y + self.INPUT_HEIGHT)
        self.canvas.coords(self._input_label_id, 8, new_input_y + self.INPUT_HEIGHT // 2)
        self.canvas.coords(self._ticker_win_id, 50, new_input_y + self.INPUT_HEIGHT // 2)
        self.canvas.coords(self._scan_btn_win_id, hive_w - 45, new_input_y + self.INPUT_HEIGHT // 2)

        # 移动收藏行
        self.canvas.coords(self._preset_bg_id, 0, new_preset_y, hive_w, new_preset_y + self.PRESET_HEIGHT)
        self.canvas.coords(self._preset_label_id, 8, new_preset_y + self.PRESET_HEIGHT // 2)
        for i, wid in enumerate(self._preset_btn_windows):
            self.canvas.coords(wid, 30 + i * 158, new_preset_y + self.PRESET_HEIGHT // 2)

        # 扩展 canvas
        self.canvas.configure(height=new_h)
        # 扩展右侧面板
        self.panel.height = new_h

    def _start_data_refresh(self):
        def refresh():
            while self.running:
                self._load_system_data()
                time.sleep(10)
        Thread(target=refresh, daemon=True).start()

    def _load_system_data(self):
        db_path = os.path.join(_PROJECT_ROOT, "pheromone.db")
        try:
            if not os.path.exists(db_path):
                return
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # 只在没有实时扫描结果时才从 DB 加载 opportunities
            # 扫描完成后 _run_real_scan 会直接写入 system_data，这里不覆盖
            if not self.system_data.get("opportunities"):
                cursor.execute("""
                    SELECT ticker, direction, self_score FROM agent_memory
                    ORDER BY created_at DESC LIMIT 10
                """)
                rows = cursor.fetchall()
                tickers = {}
                for r in rows:
                    if r[0] not in tickers:
                        tickers[r[0]] = {"ticker": r[0], "direction": r[1], "score": r[2]}
                opps = sorted(tickers.values(), key=lambda x: x["score"], reverse=True)
                if opps:
                    self.system_data["opportunities"] = opps[:4]

            cursor.execute("SELECT COUNT(*) FROM agent_memory")
            self.system_data["board_entries"] = cursor.fetchone()[0]

            # 读取历史预测记录（供历史面板使用）
            try:
                cursor.execute("""
                    SELECT date, ticker, final_score, direction,
                           return_t7, correct_t7
                    FROM predictions
                    ORDER BY date DESC, ticker
                    LIMIT 10
                """)
                pred_rows = cursor.fetchall()
                history = []
                for pr in pred_rows:
                    history.append({
                        "date": pr[0], "ticker": pr[1],
                        "final_score": pr[2], "direction": pr[3],
                        "return_t7": pr[4], "correct_t7": pr[5],
                    })
                self.system_data["prediction_history"] = history
            except sqlite3.OperationalError as e:
                _log.debug("Prediction history query failed: %s", e)

            conn.close()
        except (sqlite3.Error, OSError) as e:
            _log.debug("System data DB load failed: %s", e)
        try:
            chroma_path = os.path.join(_PROJECT_ROOT, "chroma_db")
            if os.path.exists(chroma_path):
                import chromadb
                client = chromadb.PersistentClient(path=chroma_path)
                col = client.get_or_create_collection("alpha_hive_memories")
                self.system_data["memory_docs"] = col.count()
        except (ImportError, OSError, ValueError, RuntimeError) as e:
            _log.debug("ChromaDB load failed: %s", e)
        webhook = os.path.expanduser("~/.alpha_hive_slack_webhook")
        self.system_data["slack"] = "connected" if os.path.exists(webhook) else "offline"

    def _animation_loop(self):
        if not self.running:
            return
        try:
            self.tick += 1

            # 主线程消费后台线程的 UI 操作队列
            self.interactions.flush_ui_queue()

            for bee in self.bees.values():
                bee.update()

            self.interactions.update()

            if self.tick % 30 == 0:
                self.panel.update(self.system_data, self.interactions.scan_phase)

            # A1: 进度条更新（每 5 帧）
            if self.tick % 5 == 0:
                self._update_progress_bar()

            # A2: 扫描按钮文字切换（扫描中 → 取消）
            if self.tick % 15 == 0:
                is_scanning = self.interactions.scan_phase != "idle"
                new_text = "取消" if is_scanning else "扫描"
                new_fg = "#FF6666" if is_scanning else "#FFB800"
                if self.scan_btn.cget("text") != new_text:
                    self.scan_btn.configure(text=new_text, fg=new_fg,
                                            activeforeground="#FF8888" if is_scanning else "#FFD700")

            # 每 10 帧刷新聊天框（平衡性能和实时性）
            if self.tick % 10 == 0:
                self.chat_log.draw()
        except (ValueError, TypeError, AttributeError, RuntimeError, tk.TclError) as e:
            _log.warning("AnimLoop recovered from: %s", e)
        finally:
            # 仅在 running 时重新调度，防止 root 已销毁时触发 TclError
            if self.running:
                try:
                    self._after_id = self.root.after(1000 // self.FPS, self._animation_loop)
                except tk.TclError:
                    pass

    def run(self):
        print("\n" + "=" * 50)
        print("  ALPHA HIVE Desktop")
        print("  Interactive Pixel Swarm")
        print("=" * 50)
        print("\n  [SPACE] Run full scan animation")
        print("  [ESC]   Quit\n")

        self.panel.update(self.system_data, "idle")
        self._animation_loop()
        self.root.mainloop()

    def quit(self):
        self.running = False
        # 取消待执行的动画帧，防止 root 销毁后触发 TclError
        after_id = getattr(self, "_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    AlphaHiveApp().run()
