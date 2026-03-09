"""
Alpha Hive GUI - 视图面板
InfoPanel, ReportView, ChatLog
"""

import math
import logging as _logging
from datetime import datetime

_log = _logging.getLogger("alpha_hive.app")


# ==================== 信息面板 ====================

class InfoPanel:
    def __init__(self, canvas, x, y, width, height):
        self.canvas = canvas
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.items = []
        self.opportunity_regions = []  # B2: [(y1, y2, ticker), ...] 供点击跳转简报

    def update(self, data, scan_phase="idle"):
        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

        item = self.canvas.create_rectangle(
            self.x, self.y, self.x + self.width, self.y + self.height,
            fill="#0A0A0A", outline="#333333", width=1
        )
        self.items.append(item)

        y = self.y + 15
        self._text(self.x + self.width//2, y, "ALPHA HIVE", "#FFB800", 12, "bold", "center")

        y += 15
        self._line(y)

        # 扫描阶段指示器
        y += 18
        phase_labels = {
            "idle": ("IDLE", "#555555"),
            "decomposing": ("DECOMPOSING", "#FFB800"),
            "foraging": ("FORAGING", "#3498DB"),
            "resonating": ("RESONATING", "#E74C3C"),
            "dancing": ("WAGGLE DANCE", "#FF8C00"),
            "distilling": ("DISTILLING", "#9B59B6"),
            "done": ("COMPLETE", "#27AE60"),
        }
        phase_text, phase_color = phase_labels.get(scan_phase, ("IDLE", "#555"))
        self._text(self.x+10, y, f"Phase: {phase_text}", phase_color, 10, "bold")

        y += 18
        now = datetime.now().strftime("%H:%M:%S")
        self._text(self.x+10, y, f"Time:  {now}", "#888888", 10)

        y += 18
        self._text(self.x+10, y, f"Agents: {data.get('agent_count',7)}", "#888888", 10)

        y += 18
        self._text(self.x+10, y, f"Next:  08:00 (M-F)", "#888888", 10)

        y += 15
        self._line(y)

        y += 15
        self._text(self.x+10, y, "LATEST SCAN", "#FFB800", 10, "bold")

        self.opportunity_regions = []  # B2: 重置可点击区域
        for opp in data.get("opportunities", [])[:4]:
            y += 16
            ticker = opp.get("ticker", "???")
            score = opp.get("score", 0)
            direction = opp.get("direction", "neutral")
            sym = {"bullish": "+", "bearish": "-", "neutral": "~"}.get(direction, "?")
            clr = {"bullish": "#27AE60", "bearish": "#E74C3C", "neutral": "#7F8C8D"}.get(direction, "#888")
            # B2: 记录可点击区域（含悬浮提示标记）
            self.opportunity_regions.append((y - 8, y + 8, ticker))
            self._text(self.x+10, y, f"  {sym} {ticker:5s} {score:.1f}/10 »", clr, 10)

        y += 20
        self._line(y)
        y += 15
        self._text(self.x+10, y, "INTERACTIONS", "#FFB800", 10, "bold")

        y += 16
        self._text(self.x+10, y, f"  Board: {data.get('board_entries',0)} entries", "#888", 10)
        y += 16
        self._text(self.x+10, y, f"  Memory: {data.get('memory_docs',0)} docs", "#888", 10)
        y += 16
        slack = data.get("slack", "connected")
        self._text(self.x+10, y, f"  Slack: {slack}", "#27AE60" if slack == "connected" else "#E74C3C", 10)
        y += 16
        self._text(self.x+10, y, f"  Cron: 08:00 M-F", "#888", 10)

        # ====== 历史预测记录 ======
        history = data.get("prediction_history", [])
        if history:
            y += 20
            self._line(y)
            y += 15
            self._text(self.x+10, y, "历史预测", "#FFB800", 10, "bold")
            for h in history[:5]:
                y += 14
                ticker = h.get("ticker", "?")
                score = h.get("final_score", 0)
                date = h.get("date", "")[-5:]  # MM-DD
                ret_t7 = h.get("return_t7")
                ret_str = f"{ret_t7:+.1f}%" if ret_t7 is not None else "..."
                correct = h.get("correct_t7")
                mark = "✓" if correct == 1 else ("✗" if correct == 0 else "⏳")
                clr = "#27AE60" if correct == 1 else ("#E74C3C" if correct == 0 else "#999966")
                self._text(self.x+10, y, f"  {mark} {date} {ticker:5s} {score:.0f} {ret_str}", clr, 9)

        # ====== 自适应权重（Phase 6 反馈进化）======
        adapted_w = data.get("adapted_weights")
        if adapted_w and isinstance(adapted_w, dict):
            y += 20
            self._line(y)
            y += 15
            self._text(self.x+10, y, "自适应权重", "#FFB800", 10, "bold")
            dim_labels = {"signal": "信号", "catalyst": "催化", "sentiment": "情绪",
                         "odds": "赔率", "risk_adj": "风控"}
            default_w = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20,
                        "odds": 0.15, "risk_adj": 0.15}
            for dim_key, label in dim_labels.items():
                y += 13
                w = adapted_w.get(dim_key, default_w.get(dim_key, 0.2))
                dw = default_w.get(dim_key, 0.2)
                delta = w - dw
                delta_str = f"{delta:+.2f}" if abs(delta) > 0.005 else "="
                clr = "#27AE60" if delta > 0.01 else ("#E74C3C" if delta < -0.01 else "#888888")
                self._text(self.x+10, y, f"  {label} {w:.2f} ({delta_str})", clr, 9)

        # ====== 5 维雷达图 ======
        dim_scores = data.get("dimension_scores")
        if dim_scores and isinstance(dim_scores, dict) and any(v > 0 for v in dim_scores.values()):
            y += 20
            self._line(y)
            y += 15
            self._text(self.x + self.width // 2, y, "五维雷达", "#FFB800", 10, "bold", "center")
            y += 10
            self._draw_radar(y, dim_scores)

    def _draw_radar(self, top_y, dim_scores):
        """绘制五维雷达图（纯 Canvas 多边形）"""
        import math

        cx = self.x + self.width // 2
        cy = top_y + 75
        r_max = 55  # 最大半径

        # 5 个维度的顺序和中文标签
        dims = [
            ("signal",    "信号"),
            ("catalyst",  "催化"),
            ("sentiment", "情绪"),
            ("odds",      "赔率"),
            ("risk_adj",  "风控"),
        ]

        n = len(dims)
        angles = [math.pi / 2 + 2 * math.pi * i / n for i in range(n)]

        # 绘制背景网格（3 层同心五边形）
        for level in [0.33, 0.66, 1.0]:
            pts = []
            for angle in angles:
                px = cx + r_max * level * math.cos(angle)
                py = cy - r_max * level * math.sin(angle)
                pts.extend([px, py])
            item = self.canvas.create_polygon(
                pts, fill="", outline="#222200", width=1
            )
            self.items.append(item)

        # 绘制轴线
        for angle in angles:
            px = cx + r_max * math.cos(angle)
            py = cy - r_max * math.sin(angle)
            item = self.canvas.create_line(cx, cy, px, py, fill="#222200", width=1)
            self.items.append(item)

        # 绘制数据多边形
        data_pts = []
        for i, (dim_key, _) in enumerate(dims):
            score = dim_scores.get(dim_key, 5.0)
            ratio = min(1.0, max(0.0, score / 10.0))
            px = cx + r_max * ratio * math.cos(angles[i])
            py = cy - r_max * ratio * math.sin(angles[i])
            data_pts.extend([px, py])

        # 填充区域
        item = self.canvas.create_polygon(
            data_pts, fill="#4D3700", outline="#FFB800", width=2,
            stipple="gray25"
        )
        self.items.append(item)

        # 数据点
        for i in range(0, len(data_pts), 2):
            item = self.canvas.create_oval(
                data_pts[i] - 3, data_pts[i+1] - 3,
                data_pts[i] + 3, data_pts[i+1] + 3,
                fill="#FFB800", outline=""
            )
            self.items.append(item)

        # 标签
        label_r = r_max + 18
        for i, (dim_key, label) in enumerate(dims):
            lx = cx + label_r * math.cos(angles[i])
            ly = cy - label_r * math.sin(angles[i])
            score = dim_scores.get(dim_key, 0)
            self._text(lx, ly, f"{label}\n{score:.1f}", "#888800", 8, anchor="center")

    def _text(self, x, y, text, color, size, weight="", anchor="w"):
        font = ("Monaco", size, weight) if weight else ("Monaco", size)
        item = self.canvas.create_text(x, y, text=text, fill=color, font=font, anchor=anchor)
        self.items.append(item)

    def _line(self, y):
        item = self.canvas.create_line(
            self.x+10, y, self.x+self.width-10, y, fill="#333333"
        )
        self.items.append(item)


# ==================== 主应用 ====================

class ReportView:
    """
    8 版块结构化简报视图 — 按 R 键在蜂巢区域覆盖展示
    版块对应 CLAUDE.md：聪明钱 | 市场预期 | 情绪 | 催化剂 | 竞争格局 | 综合判断 | 跟进建议 | 数据来源
    """

    SECTION_COLORS = {
        "signal": "#FFB800", "odds": "#9B59B6", "sentiment": "#3498DB",
        "catalyst": "#27AE60", "ml_auxiliary": "#E74C3C", "risk_adj": "#7F8C8D",
        "contrarian": "#FF6B6B", "summary": "#FFD700",
    }

    AGENT_SECTION_MAP = {
        "ScoutBeeNova": ("聪明钱动向", "signal"),
        "OracleBeeEcho": ("市场隐含预期", "odds"),
        "BuzzBeeWhisper": ("情绪汇总", "sentiment"),
        "ChronosBeeHorizon": ("催化剂与时间线", "catalyst"),
        "RivalBeeVanguard": ("竞争格局 / ML", "ml_auxiliary"),
        "GuardBeeSentinel": ("综合判断与信号强度", "risk_adj"),
        "BearBeeContrarian": ("看空对冲分析", "contrarian"),
    }

    def __init__(self, canvas, width, height):
        self.canvas = canvas
        self.width = width
        self.height = height
        self.items = []
        self.visible = False
        self.swarm_data = {}
        self.scroll_y = 0
        self.content_height = 0

    def toggle(self, swarm_data=None):
        """切换显示/隐藏"""
        if swarm_data:
            self.swarm_data = swarm_data
        self.visible = not self.visible
        self.scroll_y = 0
        if self.visible:
            self.draw()
        else:
            self.clear()

    def scroll(self, delta):
        """滚动简报内容"""
        if not self.visible:
            return
        self.scroll_y = max(0, min(self.scroll_y + delta, max(0, self.content_height - self.height + 60)))
        self.draw()

    def scroll_to_ticker(self, ticker_idx):
        """B2: 滚动到指定 ticker 章节（按排序索引）"""
        # 粗估：标题约 40px，摘要约 5行×14px，每个 ticker 约 80px
        estimated_offset = 80 + ticker_idx * 80
        self.scroll_y = max(0, estimated_offset - 40)
        self.draw()

    def clear(self):
        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

    def draw(self):
        self.clear()
        if not self.swarm_data:
            return

        # 半透明背景覆盖蜂巢区域
        bg = self.canvas.create_rectangle(0, 0, self.width, self.height, fill="#0A0A0A", outline="")
        self.items.append(bg)

        y = 15 - self.scroll_y

        # 标题栏
        y = self._draw_text(self.width // 2, y, "蜂群投资简报", "#FFD700", 14, "bold", "center")
        y = self._draw_text(self.width // 2, y + 3, "[R] 返回  |  [↑↓] 滚动  |  [C] 复制", "#555500", 9, anchor="center")
        y += 8

        # 按分数排序
        sorted_tickers = sorted(
            self.swarm_data.items(),
            key=lambda x: x[1].get("final_score", 0) if isinstance(x[1], dict) else 0,
            reverse=True
        )

        # 版块 1：今日摘要
        y = self._draw_section_header(y, "一、今日摘要")
        for ticker, data in sorted_tickers[:5]:
            if not isinstance(data, dict):
                continue
            s = data.get("final_score", 0)
            d = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(data.get("direction", ""), "?")
            tag = "高优先" if s >= 7.5 else ("观察" if s >= 6.0 else "暂不动")
            clr = "#27AE60" if s >= 7.5 else ("#FFB800" if s >= 6.0 else "#888888")
            y = self._draw_text(15, y, f"  {ticker}: {s:.1f}/10 {d} [{tag}]", clr, 10)

        # 版块 2-7：每个 Agent 的详细发现
        for agent_name, (section_title, dim) in self.AGENT_SECTION_MAP.items():
            section_num = list(self.AGENT_SECTION_MAP.keys()).index(agent_name) + 2
            y += 5
            y = self._draw_section_header(y, f"{'二三四五六七八'[section_num-2]}、{section_title}")
            clr = self.SECTION_COLORS.get(dim, "#888888")

            has_content = False
            for ticker, data in sorted_tickers:
                if not isinstance(data, dict):
                    continue
                details = data.get("agent_details", {})
                agent_data = details.get(agent_name, {})
                discovery = agent_data.get("discovery", "")
                score = agent_data.get("score", 0)
                direction = agent_data.get("direction", "neutral")

                if not discovery:
                    continue
                has_content = True

                d_cn = {"bullish": "多", "bearish": "空", "neutral": "中"}.get(direction, "?")
                # 分行显示长文本
                header = f"  {ticker} ({score:.1f} {d_cn})"
                y = self._draw_text(15, y, header, clr, 10, "bold")
                # 发现摘要（截断并换行）
                for line in self._wrap_text(discovery, 48):
                    y = self._draw_text(25, y, line, "#AAAAAA", 9)

            if not has_content:
                y = self._draw_text(25, y, "暂无数据", "#555555", 9)

        # 版块 8：数据来源与免责声明
        y += 5
        y = self._draw_section_header(y, "八、数据来源与免责声明")
        y = self._draw_text(15, y, "  数据来源：SEC EDGAR / yfinance / Finviz / Reddit ApeWisdom", "#888888", 9)
        y = self._draw_text(15, y, "  期权数据：yfinance option chain (ATM ±20% 中位数 IV)", "#888888", 9)
        y = self._draw_text(15, y, "  免责声明：本报告为 AI 蜂群自动生成，不构成投资建议。", "#FF6B6B", 9)
        y = self._draw_text(15, y, "  所有交易决策需自行判断和风控。预测存在误差。", "#FF6B6B", 9)

        self.content_height = y + self.scroll_y + 20

    def _draw_section_header(self, y, title):
        """绘制版块标题（带下划线）"""
        item = self.canvas.create_line(10, y, self.width - 10, y, fill="#333300")
        self.items.append(item)
        y += 12
        y = self._draw_text(15, y, title, "#FFD700", 11, "bold")
        return y

    def _draw_text(self, x, y, text, color, size, weight="", anchor="w"):
        """绘制文字并返回下一行 y 坐标"""
        if y < -20 or y > self.height + 20:
            return y + size + 4  # 屏幕外跳过绘制但保留空间
        font = ("Monaco", size, weight) if weight else ("Monaco", size)
        item = self.canvas.create_text(x, y, text=text, fill=color, font=font, anchor=anchor)
        self.items.append(item)
        return y + size + 4

    @staticmethod
    def _wrap_text(text, max_chars):
        """简单文本换行"""
        lines = []
        # 先按 | 分段
        parts = text.split(" | ")
        current = ""
        for part in parts:
            if current and len(current) + len(part) + 3 > max_chars:
                lines.append(current)
                current = part
            else:
                current = f"{current} | {part}" if current else part
        if current:
            lines.append(current)
        return lines if lines else [text[:max_chars]]


class ChatLog:
    """实时聊天框 - 显示 Agent 之间的交流内容"""

    MAX_LINES = 50        # 最多保留消息数
    VISIBLE_LINES = 8     # 可见行数

    AGENT_COLORS = {
        "ScoutBeeNova":       "#FFB800",
        "OracleBeeEcho":      "#9B59B6",
        "BuzzBeeWhisper":     "#3498DB",
        "ChronosBeeHorizon":  "#27AE60",
        "RivalBeeVanguard":   "#E74C3C",
        "GuardBeeSentinel":   "#7F8C8D",
        "CodeExecutorAgent":  "#00CED1",
        "System":             "#FFB800",
    }

    AGENT_SHORT = {
        "ScoutBeeNova":       "Scout",
        "OracleBeeEcho":      "Oracle",
        "BuzzBeeWhisper":     "Buzz",
        "ChronosBeeHorizon":  "Chronos",
        "RivalBeeVanguard":   "Rival",
        "GuardBeeSentinel":   "Guard",
        "CodeExecutorAgent":  "Code",
        "System":             "HIVE",
    }

    def __init__(self, canvas, x, y, width, height):
        self.canvas = canvas
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.messages = []   # list of {time, sender, text, color}
        self.items = []
        self.scroll_offset = 0  # 0 = 最底部（最新消息）

    def add(self, sender, text, msg_type="chat"):
        """添加一条聊天消息"""
        now = datetime.now().strftime("%H:%M:%S")
        color = self.AGENT_COLORS.get(sender, "#888888")
        name = self.AGENT_SHORT.get(sender, sender[:6])

        # msg_type 影响前缀图标
        prefix = {
            "chat":      "",
            "signal":    "[信号] ",
            "alert":     "[警报] ",
            "resonance": "[共振] ",
            "discovery": "[发现] ",
            "dance":     "[舞蹈] ",
            "phase":     "",
            "system":    "",
        }.get(msg_type, "")

        self.messages.append({
            "time": now,
            "sender": name,
            "text": f"{prefix}{text}",
            "color": color,
            "type": msg_type,
        })

        # 保留上限
        if len(self.messages) > self.MAX_LINES:
            self.messages = self.messages[-self.MAX_LINES:]

        # 新消息时自动滚到底部
        self.scroll_offset = 0

    def scroll_up(self):
        max_scroll = max(0, len(self.messages) - self.VISIBLE_LINES)
        self.scroll_offset = min(self.scroll_offset + 1, max_scroll)

    def scroll_down(self):
        self.scroll_offset = max(0, self.scroll_offset - 1)

    def draw(self):
        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

        # 背景
        item = self.canvas.create_rectangle(
            self.x, self.y, self.x + self.width, self.y + self.height,
            fill="#080808", outline="#333333", width=1
        )
        self.items.append(item)

        # 标题栏
        bar_h = 18
        item = self.canvas.create_rectangle(
            self.x, self.y, self.x + self.width, self.y + bar_h,
            fill="#1A1200", outline="#333333", width=1
        )
        self.items.append(item)

        item = self.canvas.create_text(
            self.x + 10, self.y + bar_h // 2,
            text="蜂巢聊天室", fill="#FFB800",
            font=("Monaco", 9, "bold"), anchor="w"
        )
        self.items.append(item)

        # 消息数指示
        item = self.canvas.create_text(
            self.x + self.width - 10, self.y + bar_h // 2,
            text=f"{len(self.messages)} 条消息",
            fill="#555555", font=("Monaco", 8), anchor="e"
        )
        self.items.append(item)

        # 消息区域
        if not self.messages:
            item = self.canvas.create_text(
                self.x + self.width // 2,
                self.y + bar_h + (self.height - bar_h) // 2,
                text="等待 Agent 活动...",
                fill="#333333", font=("Monaco", 10), anchor="center"
            )
            self.items.append(item)
            return

        # 计算可见范围
        end_idx = len(self.messages) - self.scroll_offset
        start_idx = max(0, end_idx - self.VISIBLE_LINES)
        visible = self.messages[start_idx:end_idx]

        line_h = (self.height - bar_h - 8) / self.VISIBLE_LINES
        for i, msg in enumerate(visible):
            ly = self.y + bar_h + 6 + i * line_h

            # 时间戳
            item = self.canvas.create_text(
                self.x + 6, ly,
                text=msg["time"], fill="#444444",
                font=("Monaco", 8), anchor="nw"
            )
            self.items.append(item)

            # 发送者名称（彩色）
            item = self.canvas.create_text(
                self.x + 70, ly,
                text=msg["sender"], fill=msg["color"],
                font=("Monaco", 9, "bold"), anchor="nw"
            )
            self.items.append(item)

            # 消息文本（截断）
            max_text_w = self.width - 160
            text = msg["text"]
            # 粗略截断（每个字符~7px）
            max_chars = max_text_w // 7
            if len(text) > max_chars:
                text = text[:max_chars - 2] + ".."

            text_color = "#AAAAAA"
            if msg["type"] == "phase":
                text_color = "#FFB800"
            elif msg["type"] == "alert":
                text_color = "#FF6666"
            elif msg["type"] == "resonance":
                text_color = "#66FF88"
            elif msg["type"] == "discovery":
                text_color = "#FFDD66"
            elif msg["type"] == "system":
                text_color = "#FFB800"
            elif msg["type"] == "dance":
                text_color = "#FFA500"

            item = self.canvas.create_text(
                self.x + 130, ly,
                text=text, fill=text_color,
                font=("Monaco", 9), anchor="nw"
            )
            self.items.append(item)

        # 滚动指示器
        if self.scroll_offset > 0:
            item = self.canvas.create_text(
                self.x + self.width - 15, self.y + bar_h + 5,
                text="^", fill="#FFB800", font=("Monaco", 10, "bold")
            )
            self.items.append(item)

        if start_idx > 0:
            # 还有更多历史消息
            item = self.canvas.create_text(
                self.x + self.width - 15,
                self.y + self.height - 10,
                text="v", fill="#555555", font=("Monaco", 10)
            )
            self.items.append(item)

