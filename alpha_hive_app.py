#!/usr/bin/env python3
"""
Alpha Hive Desktop - 像素蜂群动画桌面应用
实时显示 7 个 Agent 的工作状态 + 互动通信
"""

import sys
import json
import tkinter as tk
import math
import time
import random
import sqlite3
import os
from datetime import datetime
from threading import Thread, Lock
from pathlib import Path
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging as _logging

_log = _logging.getLogger("alpha_hive.app")

# 确保项目目录在 import 路径中
_PROJECT_ROOT = os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


# ==================== 蜂群消息（Agent 间通信） ====================

class BeeMessage:
    """蜜蜂之间传递的消息气泡"""

    def __init__(self, canvas, sender, receiver, msg_type="signal", color="#FFD700"):
        self.canvas = canvas
        self.sender = sender
        self.receiver = receiver
        self.msg_type = msg_type  # signal / alert / resonance / question
        self.color = color
        self.progress = 0.0      # 0.0 (发送端) → 1.0 (接收端)
        self.speed = random.uniform(0.015, 0.03)
        self.alive = True
        self.items = []
        self.trail_items = []

    def update(self):
        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

        if not self.alive:
            return

        self.progress += self.speed
        if self.progress >= 1.0:
            self.alive = False
            # 到达接收者时产生接收效果
            self.receiver._on_receive_message(self)
            return

        # 计算当前位置（贝塞尔曲线）
        sx = self.sender.x + 12
        sy = self.sender.y + 10 + self.sender.bob_offset
        ex = self.receiver.x + 12
        ey = self.receiver.y + 10 + self.receiver.bob_offset

        # 控制点（弧形路径）
        mx = (sx + ex) / 2
        my = min(sy, ey) - 40 - random.uniform(-3, 3)

        t = self.progress
        x = (1-t)**2 * sx + 2*(1-t)*t * mx + t**2 * ex
        y = (1-t)**2 * sy + 2*(1-t)*t * my + t**2 * ey

        # 绘制消息气泡
        size = 4
        if self.msg_type == "resonance":
            size = 6
            # 共振用双圈
            item = self.canvas.create_oval(
                x-size-2, y-size-2, x+size+2, y+size+2,
                outline=self.color, width=1
            )
            self.items.append(item)
        elif self.msg_type == "alert":
            size = 5
            # 告警用菱形
            pts = [x, y-size, x+size, y, x, y+size, x-size, y]
            item = self.canvas.create_polygon(pts, fill=self.color, outline="")
            self.items.append(item)
            return

        item = self.canvas.create_oval(
            x-size, y-size, x+size, y+size,
            fill=self.color, outline=""
        )
        self.items.append(item)

        # 尾迹（小粒子）
        if random.random() < 0.4:
            trail_size = 2
            trail = self.canvas.create_oval(
                x-trail_size, y-trail_size, x+trail_size, y+trail_size,
                fill=self.color, outline=""
            )
            self.trail_items.append({"item": trail, "life": 12})

    def cleanup_trails(self):
        new_trails = []
        for t in self.trail_items:
            t["life"] -= 1
            if t["life"] <= 0:
                self.canvas.delete(t["item"])
            else:
                new_trails.append(t)
        self.trail_items = new_trails


# ==================== 信息素连线（共振可视化） ====================

class ResonanceLine:
    """两只蜜蜂之间的共振连线"""

    def __init__(self, canvas, bee_a, bee_b, strength=1.0):
        self.canvas = canvas
        self.bee_a = bee_a
        self.bee_b = bee_b
        self.strength = strength
        self.life = 120  # 持续帧数
        self.items = []
        self.pulse_phase = 0

    @property
    def alive(self):
        return self.life > 0

    def update(self):
        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

        self.life -= 1
        if self.life <= 0:
            return

        self.pulse_phase += 0.15
        alpha_factor = min(1.0, self.life / 30.0)

        ax = self.bee_a.x + 12
        ay = self.bee_a.y + 10 + self.bee_a.bob_offset
        bx = self.bee_b.x + 12
        by = self.bee_b.y + 10 + self.bee_b.bob_offset

        # 脉冲宽度
        width = max(1, int(2 * alpha_factor * (1 + 0.5 * math.sin(self.pulse_phase))))

        # 颜色随强度变化
        colors = ["#332200", "#554400", "#776600", "#FFB800"]
        color_idx = min(len(colors)-1, int(self.strength * (len(colors)-1)))
        color = colors[color_idx]

        item = self.canvas.create_line(
            ax, ay, bx, by,
            fill=color, width=width, dash=(4, 4)
        )
        self.items.append(item)

        # 中点标记
        mx = (ax + bx) / 2
        my = (ay + by) / 2
        pulse_size = 3 + int(2 * math.sin(self.pulse_phase))
        item = self.canvas.create_oval(
            mx - pulse_size, my - pulse_size,
            mx + pulse_size, my + pulse_size,
            fill="#FFB800", outline=""
        )
        self.items.append(item)


# ==================== 像素蜜蜂精灵 ====================

class PixelBee:
    """像素蜜蜂 Agent（含互动能力）"""

    AGENT_COLORS = {
        "ScoutBeeNova":       {"body": "#FFB800", "wing": "#FFF4CC", "eye": "#1A1A1A", "accent": "#FF8C00", "label": "Scout"},
        "OracleBeeEcho":      {"body": "#9B59B6", "wing": "#E8D5F5", "eye": "#1A1A1A", "accent": "#8E44AD", "label": "Oracle"},
        "BuzzBeeWhisper":     {"body": "#3498DB", "wing": "#D6EAF8", "eye": "#1A1A1A", "accent": "#2980B9", "label": "Buzz"},
        "ChronosBeeHorizon":  {"body": "#27AE60", "wing": "#D5F5E3", "eye": "#1A1A1A", "accent": "#1E8449", "label": "Chronos"},
        "RivalBeeVanguard":   {"body": "#E74C3C", "wing": "#FADBD8", "eye": "#1A1A1A", "accent": "#C0392B", "label": "Rival"},
        "GuardBeeSentinel":   {"body": "#7F8C8D", "wing": "#D5DBDB", "eye": "#1A1A1A", "accent": "#566573", "label": "Guard"},
        "CodeExecutorAgent":  {"body": "#00CED1", "wing": "#E0FFFF", "eye": "#1A1A1A", "accent": "#008B8B", "label": "Code"},
    }

    def __init__(self, canvas, agent_id, home_x, home_y, pixel_size=5):
        self.canvas = canvas
        self.agent_id = agent_id
        self.colors = self.AGENT_COLORS.get(agent_id, self.AGENT_COLORS["ScoutBeeNova"])
        self.home_x = home_x
        self.home_y = home_y
        self.x = home_x
        self.y = home_y
        self.ps = pixel_size
        self.state = "idle"
        self.frame = 0
        self.items = []
        self.particles = []
        self.score = 0.0
        self.direction = ""
        self.bob_offset = 0
        self.facing_right = True

        # 互动状态
        self.attention_target = None   # 正在关注的蜜蜂
        self.dancing = False           # 正在跳摆尾舞
        self.dance_ticker = ""         # 舞蹈传递的标的
        self.excited = False           # 被激动（收到消息）
        self.excited_timer = 0
        self.gathering = False         # 正在聚集
        self.gather_x = 0
        self.gather_y = 0
        self.speech_bubble = ""        # 头顶气泡文字
        self.speech_timer = 0
        self.last_analysis = {}        # B1: 存储最近一次分析结果（供点击弹窗使用）

    def set_state(self, state, score=0.0, direction=""):
        self.state = state
        self.score = score
        self.direction = direction

    def say(self, text, duration=60):
        """头顶气泡说话"""
        self.speech_bubble = text
        self.speech_timer = duration

    def start_dance(self, ticker, score):
        """开始摆尾舞（发现高价值信号）"""
        self.dancing = True
        self.dance_ticker = ticker
        self.score = score
        self.state = "dancing"

    def stop_dance(self):
        self.dancing = False
        self.state = "idle"

    def look_at(self, other_bee):
        """转向看另一只蜜蜂"""
        self.attention_target = other_bee
        self.facing_right = other_bee.x > self.x

    def gather_to(self, gx, gy):
        """向指定位置聚集"""
        self.gathering = True
        self.gather_x = gx
        self.gather_y = gy

    def return_home(self):
        """返回原位"""
        self.gathering = False
        self.attention_target = None
        self.dancing = False

    def _on_receive_message(self, message):
        """收到消息时的反应"""
        self.excited = True
        self.excited_timer = 30

        if message.msg_type == "resonance":
            self.say("!", 40)
            self._spawn_particle("spark")
        elif message.msg_type == "alert":
            self.say("!!", 50)
        elif message.msg_type == "signal":
            self.say("?", 30)

    def update(self):
        self.frame += 1

        for item in self.items:
            self.canvas.delete(item)
        self.items.clear()

        self._update_particles()

        # 聚集移动
        if self.gathering:
            dx = self.gather_x - self.x
            dy = self.gather_y - self.y
            dist = math.sqrt(dx*dx + dy*dy)
            if dist > 3:
                self.x += dx * 0.05
                self.y += dy * 0.05
                self.facing_right = dx > 0
        elif self.state == "idle" and not self.dancing:
            # 缓慢回到原位
            dx = self.home_x - self.x
            dy = self.home_y - self.y
            if abs(dx) > 2 or abs(dy) > 2:
                self.x += dx * 0.03
                self.y += dy * 0.03

        # 兴奋计时器
        if self.excited_timer > 0:
            self.excited_timer -= 1
            if self.excited_timer == 0:
                self.excited = False

        # 根据状态绘制
        if self.state == "dancing":
            self._draw_dancing_bee()
        elif self.state == "sleeping":
            self._draw_sleeping_bee()
        elif self.state == "working":
            self._draw_working_bee()
        elif self.state == "publishing":
            self._draw_publishing_bee()
        else:
            self._draw_idle_bee()

        # 兴奋效果（闪烁轮廓）
        if self.excited:
            self._draw_excited_ring()

        # 气泡文字
        if self.speech_timer > 0:
            self._draw_speech_bubble()
            self.speech_timer -= 1

        # 名称标签
        self._draw_label()

    def _px(self, gx, gy, color):
        if not self.facing_right:
            gx = 5 - gx  # 水平翻转
        x = self.x + gx * self.ps
        y = self.y + gy * self.ps + self.bob_offset
        item = self.canvas.create_rectangle(
            x, y, x + self.ps, y + self.ps,
            fill=color, outline="", width=0
        )
        self.items.append(item)

    def _draw_bee_body(self):
        c = self.colors
        body = [
            (2,0),(3,0),
            (1,1),(2,1),(3,1),(4,1),
            (0,2),(1,2),(2,2),(3,2),(4,2),(5,2),
            (1,3),(2,3),(3,3),(4,3),
            (2,4),(3,4),
        ]
        stripe = [(0,2),(2,2),(4,2),(1,3),(3,3)]
        for gx, gy in body:
            color = "#1A1A1A" if (gx, gy) in stripe else c["body"]
            self._px(gx, gy, color)
        self._px(2, 1, c["eye"])
        self._px(4, 1, c["eye"])
        self._px(1, -1, c["accent"])
        self._px(4, -1, c["accent"])

    def _draw_wings(self, flap_phase=0):
        c = self.colors
        if flap_phase % 2 == 0:
            self._px(-1, -1, c["wing"])
            self._px(-1, 0, c["wing"])
            self._px(6, -1, c["wing"])
            self._px(6, 0, c["wing"])
        else:
            self._px(-1, 1, c["wing"])
            self._px(-1, 2, c["wing"])
            self._px(6, 1, c["wing"])
            self._px(6, 2, c["wing"])

    def _draw_idle_bee(self):
        self.bob_offset = int(math.sin(self.frame * 0.08) * 3)
        self._draw_bee_body()
        self._draw_wings(self.frame // 18)

    def _draw_working_bee(self):
        self.bob_offset = int(math.sin(self.frame * 0.25) * 5)
        if self.frame % 8 == 0:
            self.x += random.randint(-4, 4)
            self.y += random.randint(-3, 3)
        self._draw_bee_body()
        self._draw_wings(self.frame // 4)
        if self.frame % 6 == 0:
            self._spawn_particle("spark")

    def _draw_publishing_bee(self):
        self.bob_offset = int(math.sin(self.frame * 0.15) * 2)
        self._draw_bee_body()
        self._draw_wings(self.frame // 6)
        if self.frame % 4 == 0:
            self._spawn_particle("glow")

    def _draw_sleeping_bee(self):
        self.bob_offset = 0
        self._draw_bee_body()
        if self.frame % 40 == 0:
            self._spawn_particle("zzz")

    def _draw_dancing_bee(self):
        """摆尾舞：蜜蜂发现好信号时的 8 字形舞蹈"""
        dance_speed = 0.15
        t = self.frame * dance_speed
        # 8 字形轨迹
        dx = math.sin(t) * 15
        dy = math.sin(t * 2) * 8
        self.x = self.home_x + dx
        self.y = self.home_y + dy
        self.facing_right = math.cos(t) > 0
        self.bob_offset = int(math.sin(self.frame * 0.3) * 3)
        self._draw_bee_body()
        self._draw_wings(self.frame // 3)  # 快速扇翅
        if self.frame % 5 == 0:
            self._spawn_particle("spark")
            self._spawn_particle("glow")

    def _draw_excited_ring(self):
        """兴奋时的闪烁光圈"""
        cx = self.x + 3 * self.ps
        cy = self.y + 2 * self.ps + self.bob_offset
        r = 18 + int(3 * math.sin(self.frame * 0.5))
        item = self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=self.colors["accent"], width=2, dash=(3, 3)
        )
        self.items.append(item)

    def _draw_speech_bubble(self):
        """头顶气泡"""
        cx = self.x + 3 * self.ps
        cy = self.y - 15 + self.bob_offset
        text = self.speech_bubble

        # 气泡背景
        tw = len(text) * 7 + 10
        item = self.canvas.create_rectangle(
            cx - tw//2, cy - 10, cx + tw//2, cy + 8,
            fill="#222200", outline=self.colors["accent"], width=1
        )
        self.items.append(item)

        # 小三角
        item = self.canvas.create_polygon(
            cx - 3, cy + 8, cx + 3, cy + 8, cx, cy + 14,
            fill="#222200", outline=self.colors["accent"]
        )
        self.items.append(item)

        # 文字
        item = self.canvas.create_text(
            cx, cy - 1, text=text,
            fill=self.colors["accent"], font=("Monaco", 9, "bold")
        )
        self.items.append(item)

    def _draw_label(self):
        label = self.colors["label"]
        x = self.x + 3 * self.ps
        y = self.y + 7 * self.ps + self.bob_offset
        score_text = f" {self.score:.1f}" if self.score > 0 else ""
        item = self.canvas.create_text(
            x, y, text=f"{label}{score_text}",
            fill=self.colors["accent"],
            font=("Monaco", 9, "bold"), anchor="center"
        )
        self.items.append(item)

    def _spawn_particle(self, ptype):
        px = self.x + 3 * self.ps + random.randint(-10, 10)
        py = self.y + self.bob_offset + random.randint(-10, 5)
        if ptype == "spark":
            self.particles.append({
                "x": px, "y": py, "type": "spark",
                "color": random.choice(["#FFD700", "#FFA500", "#FF6347"]),
                "life": 15, "vx": random.uniform(-1.5, 1.5), "vy": -1.5
            })
        elif ptype == "glow":
            self.particles.append({
                "x": px, "y": py, "type": "glow",
                "color": self.colors["accent"],
                "life": 20, "vx": random.uniform(-2, 2), "vy": random.uniform(-2, 0)
            })
        elif ptype == "zzz":
            self.particles.append({
                "x": px + 20, "y": py - 5, "type": "zzz",
                "color": "#666666", "life": 45, "vx": 0.3, "vy": -0.8
            })

    def _update_particles(self):
        new = []
        for p in self.particles:
            p["life"] -= 1
            if p["life"] <= 0:
                continue
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            if p["type"] == "zzz":
                sz = max(1, p["life"] // 12)
                item = self.canvas.create_text(
                    p["x"], p["y"], text="z",
                    fill=p["color"], font=("Monaco", 7 + sz)
                )
            else:
                sz = max(1, p["life"] // 5)
                item = self.canvas.create_oval(
                    p["x"]-sz, p["y"]-sz, p["x"]+sz, p["y"]+sz,
                    fill=p["color"], outline=""
                )
            self.items.append(item)
            new.append(p)
        self.particles = new


# ==================== 实时监控引擎 ====================

class LiveMonitor:
    """
    真实数据实时监控 - 后台线程定期拉取 yfinance 数据
    检测价格异动、成交量异动、波动率变化、催化剂倒计时
    """

    # 监控前 5 个 WATCHLIST 标的
    MONITOR_TICKERS = ["NVDA", "TSLA", "MSFT", "AMD", "QCOM"]

    # 阈值
    PRICE_ALERT_PCT = 1.5      # 价格变动 >1.5% 触发警报
    VOLUME_ALERT_RATIO = 1.5   # 量比 >1.5 触发警报
    REFRESH_INTERVAL = 30      # 基础刷新间隔（秒）

    def __init__(self):
        self.running = False
        self._cache = {}        # {ticker: {price, prev_price, volume_ratio, ...}}
        self._callbacks = []    # [(agent_id, msg, msg_type, bee_action)]
        self._lock = __import__("threading").Lock()
        self._last_catalyst_check = 0

    def start(self):
        self.running = True
        Thread(target=self._monitor_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def pop_events(self):
        """主线程调用：取出所有待处理事件"""
        with self._lock:
            events = list(self._callbacks)
            self._callbacks.clear()
        return events

    def _emit(self, agent_id, msg, msg_type="discovery", bee_action=None):
        """推送事件到队列"""
        with self._lock:
            self._callbacks.append((agent_id, msg, msg_type, bee_action))

    def _monitor_loop(self):
        """后台循环"""
        import time as _time
        _time.sleep(3)  # 启动延迟

        cycle = 0
        while self.running:
            try:
                cycle += 1

                # 每次随机选 1-2 个标的拉取（避免并发请求过多）
                tickers = random.sample(self.MONITOR_TICKERS, min(2, len(self.MONITOR_TICKERS)))

                for ticker in tickers:
                    self._check_ticker(ticker)

                # 每 5 分钟检查催化剂倒计时
                now = _time.time()
                if now - self._last_catalyst_check > 300:
                    self._check_catalysts()
                    self._last_catalyst_check = now

            except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
                _log.debug("DataFeed cycle %d error: %s", cycle, e)

            # 30-45 秒随机间隔（避免完全规律的请求）
            _time.sleep(self.REFRESH_INTERVAL + random.randint(0, 15))

    def _check_ticker(self, ticker):
        """检查单个标的的价格和成交量"""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist.empty or len(hist) < 2:
                return

            current_price = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            change_pct = (current_price / prev_close - 1) * 100

            # 成交量
            current_vol = float(hist["Volume"].iloc[-1])
            avg_vol = float(hist["Volume"].mean())
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

            # 5 日动量
            if len(hist) >= 5:
                mom_5d = (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
            else:
                mom_5d = change_pct

            prev = self._cache.get(ticker, {})
            self._cache[ticker] = {
                "price": current_price,
                "change_pct": change_pct,
                "volume_ratio": vol_ratio,
                "momentum_5d": mom_5d,
            }

            # === 价格异动检测 ===
            if abs(change_pct) >= self.PRICE_ALERT_PCT:
                dir_word = "涨" if change_pct > 0 else "跌"
                emoji_dir = "看多" if change_pct > 0 else "看空"
                self._emit(
                    "ScoutBeeNova",
                    f"{ticker} 价格异动！{dir_word} {change_pct:+.2f}%，现价 ${current_price:.2f}",
                    "alert",
                    {"state": "working", "score": min(10, 5 + abs(change_pct)), "say": f"{ticker} {dir_word}!"}
                )

                # Oracle 跟进评论
                self._emit(
                    "OracleBeeEcho",
                    f"{ticker} {dir_word} {abs(change_pct):.1f}%，关注期权隐含波动率变化",
                    "chat"
                )

            # === 成交量异动检测 ===
            if vol_ratio >= self.VOLUME_ALERT_RATIO:
                self._emit(
                    "BuzzBeeWhisper",
                    f"{ticker} 成交量异动！量比 {vol_ratio:.1f}x（{vol_ratio:.0%} 于 5 日均量）",
                    "alert",
                    {"state": "working", "say": f"{ticker} 量!"}
                )

            # === 常规价格播报（无异动时也偶尔播报）===
            elif not prev:  # 首次加载
                self._emit(
                    "ScoutBeeNova",
                    f"{ticker} ${current_price:.2f}（{change_pct:+.2f}%）| 量比 {vol_ratio:.1f}x | 5日 {mom_5d:+.1f}%",
                    "discovery",
                    {"state": "publishing", "score": 5 + change_pct * 0.3}
                )

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Ticker check failed for %s: %s", ticker, e)

    def _check_catalysts(self):
        """检查催化剂倒计时"""
        try:
            import yfinance as yf
            from datetime import datetime

            for ticker in self.MONITOR_TICKERS[:3]:  # 只查前 3 个
                try:
                    t = yf.Ticker(ticker)
                    cal = t.calendar
                    if cal is None:
                        continue

                    if isinstance(cal, dict):
                        cal_dict = cal
                    elif hasattr(cal, 'to_dict'):
                        cal_dict = cal.to_dict()
                    else:
                        continue

                    earnings = cal_dict.get("Earnings Date", [])
                    if isinstance(earnings, list):
                        for ed in earnings:
                            if hasattr(ed, 'strftime'):
                                date_str = ed.strftime("%Y-%m-%d")
                                days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                                if 0 <= days_until <= 14:
                                    urgency = "紧急" if days_until <= 3 else "注意"
                                    self._emit(
                                        "ChronosBeeHorizon",
                                        f"[{urgency}] {ticker} 财报还有 {days_until} 天（{date_str}）",
                                        "alert" if days_until <= 3 else "discovery",
                                        {"state": "working", "say": f"{days_until}天!"} if days_until <= 3 else None
                                    )
                                    break
                except (ConnectionError, TimeoutError, ValueError, KeyError, AttributeError) as e:
                    _log.debug("Catalyst check failed for %s: %s", ticker, e)
        except (ImportError, ConnectionError, TimeoutError, OSError) as e:
            _log.debug("Catalyst check unavailable: %s", e)


# ==================== 蜂巢背景 ====================

class HoneycombBackground:
    def __init__(self, canvas, width, height):
        self.canvas = canvas
        self.width = width
        self.height = height

    def draw(self):
        hex_size = 25
        colors = ["#1A1200", "#1F1600", "#241A00"]
        for row in range(-1, self.height // (hex_size * 2) + 2):
            for col in range(-1, self.width // (hex_size * 2) + 2):
                offset_x = (hex_size * 1.5) if row % 2 == 1 else 0
                cx = col * hex_size * 3 + offset_x + hex_size
                cy = row * hex_size * 1.7 + hex_size
                color = random.choice(colors)
                points = []
                for i in range(6):
                    angle = math.pi / 3 * i - math.pi / 6
                    points.extend([cx + (hex_size-2)*math.cos(angle),
                                   cy + (hex_size-2)*math.sin(angle)])
                self.canvas.create_polygon(points, fill=color, outline="#2A2000", width=1)


# ==================== 互动管理器 ====================

class InteractionManager:
    """管理蜜蜂之间的所有互动行为"""

    def __init__(self, canvas, bees, chat_log=None):
        self.canvas = canvas
        self.bees = bees  # dict {agent_id: PixelBee}
        self.chat_log = chat_log  # ChatLog 引用
        self.messages = []        # 活跃的消息
        self.resonance_lines = [] # 共振连线
        self.tick = 0
        self.scan_phase = "idle"  # idle / foraging / resonating / distilling / done

        # 线程安全 UI 操作队列：后台线程只往队列推操作，主线程消费
        self._ui_queue = Queue()

        # A2: 取消扫描标志
        self._cancel_requested = False
        # A1: 扫描进度状态
        self.scan_progress = {"current": 0, "total": 0, "ticker": "", "phase": ""}
        # 启动实时监控引擎
        self.monitor = LiveMonitor()
        self.monitor.start()

    def _enqueue(self, action, *args, **kwargs):
        """线程安全：将 UI 操作放入队列，由主线程消费"""
        self._ui_queue.put((action, args, kwargs))

    def flush_ui_queue(self):
        """主线程调用：批量执行队列中的 UI 操作（每帧最多处理 20 条防卡顿）"""
        for _ in range(20):
            try:
                action, args, kwargs = self._ui_queue.get_nowait()
            except Empty:
                break
            try:
                action(*args, **kwargs)
            except (ValueError, TypeError, AttributeError, RuntimeError, tk.TclError) as e:
                _log.warning("UI queue action failed: %s", e)

    def _log(self, sender, text, msg_type="chat"):
        """记录到聊天框（线程安全）"""
        if self.chat_log:
            self.chat_log.add(sender, text, msg_type)

    def update(self):
        self.tick += 1

        # 更新消息
        new_msgs = []
        for msg in self.messages:
            msg.update()
            msg.cleanup_trails()
            if msg.alive:
                new_msgs.append(msg)
            else:
                # 清理残留
                for t in msg.trail_items:
                    self.canvas.delete(t["item"])
        self.messages = new_msgs

        # 更新共振线
        self.resonance_lines = [r for r in self.resonance_lines if r.alive]
        for line in self.resonance_lines:
            line.update()

        # 消费实时监控事件
        if self.scan_phase == "idle":
            for agent_id, msg, msg_type, bee_action in self.monitor.pop_events():
                self._log(agent_id, msg, msg_type)
                bee = self.bees.get(agent_id)
                if bee and bee_action:
                    if "state" in bee_action:
                        bee.set_state(bee_action["state"], score=bee_action.get("score", 0))
                    if "say" in bee_action:
                        bee.say(bee_action["say"], 50)
                # 警报类事件触发消息动画
                if msg_type == "alert":
                    other_ids = [a for a in self.bees if a != agent_id]
                    targets = random.sample(other_ids, min(2, len(other_ids)))
                    for tid in targets:
                        self.send_message(agent_id, tid, "alert")

        # 空闲时随机互动
        if self.scan_phase == "idle" and self.tick % 90 == 0:
            self._random_idle_interaction()

    def send_message(self, sender_id, receiver_id, msg_type="signal", log_text=None):
        """发送一条消息"""
        sender = self.bees.get(sender_id)
        receiver = self.bees.get(receiver_id)
        if not sender or not receiver:
            return

        color_map = {
            "signal": "#FFD700",
            "alert": "#FF4444",
            "resonance": "#00FF88",
            "question": "#88BBFF",
        }
        color = color_map.get(msg_type, "#FFD700")
        msg = BeeMessage(self.canvas, sender, receiver, msg_type, color)
        self.messages.append(msg)

        # 记录到聊天框
        if log_text:
            r_name = ChatLog.AGENT_SHORT.get(receiver_id, receiver_id[:6])
            self._log(sender_id, f"-> {r_name}: {log_text}", msg_type)

    def broadcast(self, sender_id, msg_type="signal", log_text=None):
        """广播消息给所有其他蜜蜂"""
        if log_text:
            self._log(sender_id, f"[Broadcast] {log_text}", msg_type)
        for agent_id in self.bees:
            if agent_id != sender_id:
                self.send_message(sender_id, agent_id, msg_type)

    def create_resonance(self, bee_id_a, bee_id_b, strength=0.8, ticker=""):
        """创建共振连线"""
        a = self.bees.get(bee_id_a)
        b = self.bees.get(bee_id_b)
        if a and b:
            line = ResonanceLine(self.canvas, a, b, strength)
            self.resonance_lines.append(line)
            name_a = ChatLog.AGENT_SHORT.get(bee_id_a, bee_id_a[:6])
            name_b = ChatLog.AGENT_SHORT.get(bee_id_b, bee_id_b[:6])
            info = f" {ticker}" if ticker else ""
            self._log(bee_id_a, f"{name_a} <-> {name_b} 共振{info}（强度={strength:.1f}）", "resonance")

    def start_waggle_dance(self, dancer_id, ticker, score):
        """开始摆尾舞 + 周围蜜蜂观看"""
        dancer = self.bees.get(dancer_id)
        if not dancer:
            return

        dancer.start_dance(ticker, score)
        dancer.say(f"{ticker} {score:.1f}", 90)
        self._log(dancer_id, f"摆尾舞！{ticker} 评分={score:.1f} - 发现高价值信号", "dance")

        # 附近蜜蜂转向观看
        for aid, bee in self.bees.items():
            if aid != dancer_id:
                bee.look_at(dancer)
                bee.say("?", 40)

    def gather_all(self, cx, cy):
        """所有蜜蜂向中心聚集（蒸馏阶段）"""
        for bee in self.bees.values():
            # 给每只蜜蜂一个略微偏移的聚集点
            ox = random.randint(-30, 30)
            oy = random.randint(-25, 25)
            bee.gather_to(cx + ox, cy + oy)

    def disperse_all(self):
        """所有蜜蜂散开回原位"""
        for bee in self.bees.values():
            bee.return_home()
            bee.set_state("idle")

    def run_scan_sequence(self, focus_tickers=None):
        """运行真实蜂群扫描 - 连接后端 Agent 系统"""

        if self.scan_phase != "idle":
            self._log("System", "扫描正在进行中，请等待完成", "alert")
            return

        def real_scan():
            try:
                self._run_real_scan(focus_tickers)
            except (ImportError, ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError) as e:
                _log.error("Scan failed: %s", e, exc_info=True)
                self._enqueue(self._log, "System", f"扫描出错：{str(e)[:80]}", "alert")
                self._enqueue(self.disperse_all)
                self.scan_phase = "idle"

        thread = Thread(target=real_scan, daemon=True)
        thread.start()

    def _run_real_scan(self, focus_tickers=None):
        """真实扫描：直接调用 AlphaHiveDailyReporter.run_swarm_scan()
        确保 App / CLI / GitHub 三端数据完全一致：
          - 相同 7 个 Agent（含 BearBeeContrarian 看空对冲蜂）
          - 相同 prefetch / VectorMemory / QueenDistiller 逻辑
          - 扫描完成后自动保存报告 + git push 到 GitHub
        """

        # ---- 导入完整日报引擎 ----
        try:
            from alpha_hive_daily_report import AlphaHiveDailyReporter
            from config import WATCHLIST
        except ImportError as e:
            self._enqueue(self._log, "System", f"日报引擎导入失败：{e}", "alert")
            self.scan_phase = "idle"
            return

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        self.scan_progress = {"current": 0, "total": len(targets), "ticker": "", "phase": "foraging"}

        # ===== 阶段 1：任务分解 + 动画准备 =====
        self.scan_phase = "decomposing"
        self._enqueue(self._log, "System", "--- Alpha Hive 完整蜂群引擎启动 ---", "phase")
        self._enqueue(self._log, "System", f"模式：7 Agent（含 BearBeeContrarian 看空蜂）| 标的：{len(targets)} 个", "system")
        self._enqueue(self._log, "ScoutBeeNova", f"目标：{', '.join(targets)}", "system")

        agent_readymap = {
            "ScoutBeeNova":      "拉取 SEC 披露和机构持仓",
            "OracleBeeEcho":     "拉取期权链和 IV 数据",
            "BuzzBeeWhisper":    "扫描 X/Reddit 情绪",
            "ChronosBeeHorizon": "检查催化剂和财报日历",
            "RivalBeeVanguard":  "分析竞争格局 + ML 预测",
            "GuardBeeSentinel":  "待命，准备交叉验证",
            "CodeExecutorAgent": "代码执行分析就绪",
        }
        for name, msg in agent_readymap.items():
            bee = self.bees.get(name)
            if bee:
                self._enqueue(bee.set_state, "working")
                self._enqueue(bee.say, "就绪", 40)
            self._enqueue(self._log, name, msg, "chat")
            time.sleep(0.04)

        self._enqueue(self.broadcast, "ScoutBeeNova", "signal", "全员出动！调用完整蜂群引擎")
        time.sleep(0.5)

        # ===== 阶段 2：实例化报告引擎 =====
        try:
            reporter = AlphaHiveDailyReporter()
        except Exception as e:
            self._enqueue(self._log, "System", f"日报引擎初始化失败：{e}", "alert")
            self._enqueue(self.disperse_all)
            self.scan_phase = "idle"
            self.scan_progress = {"current": 0, "total": 0, "ticker": "", "phase": ""}
            return

        # ===== 阶段 3：进度回调（每完成一个 ticker 触发动画）=====
        all_swarm_results = {}
        bee_ids = list(self.bees.keys())

        def on_ticker_done(idx, total, ticker, distilled):
            """run_swarm_scan 每完成一个 ticker 时回调，同步更新 UI 动画"""
            all_swarm_results[ticker] = distilled
            self.scan_progress = {"current": idx, "total": total, "ticker": ticker, "phase": "distilling"}

            final_score = distilled.get("final_score", 0)
            direction = distilled.get("direction", "neutral")
            dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)
            resonance = distilled.get("resonance", {})
            res_tag = "共振✅" if resonance.get("resonance_detected") else "无共振"
            breakdown = distilled.get("agent_breakdown", {})

            # 更新每只蜜蜂状态（来自 agent_details）
            for agent_name, agent_data in distilled.get("agent_details", {}).items():
                bee = self.bees.get(agent_name)
                if bee:
                    score = agent_data.get("score", 0)
                    self._enqueue(bee.set_state, "publishing", score)
                    self._enqueue(bee.say, f"{ticker} {score:.1f}", 50)
                    bee.last_analysis = dict(agent_data)  # B1: 供点击弹窗

            # 聊天日志：结果摘要
            self._enqueue(self._log, "System",
                f"[{idx}/{total}] {ticker}：{final_score:.1f}/10 {dir_cn} | {res_tag} "
                f"(多{breakdown.get('bullish',0)}/空{breakdown.get('bearish',0)}/中{breakdown.get('neutral',0)})",
                "alert")

            # 共振可视化
            if resonance.get("resonance_detected"):
                supporting = resonance.get("supporting_agents", 0)
                boost = resonance.get("confidence_boost", 0)
                self._enqueue(self._log, "GuardBeeSentinel",
                    f"{ticker} 共振！{supporting} Agent 同向{dir_cn}，置信+{boost}%", "resonance")
                # 画共振连线（随机取 3 对）
                agents_list = list(self.bees.keys())
                for i in range(min(3, len(agents_list))):
                    for j in range(i+1, min(4, len(agents_list))):
                        self._enqueue(self.create_resonance, agents_list[i], agents_list[j], 0.85, ticker)
                self._enqueue(self.broadcast, "GuardBeeSentinel", "resonance",
                    f"{ticker} 共振 - {supporting} Agent 同向{dir_cn}")

            # D2: 高分音效 + 摆尾舞
            if final_score >= 7.5:
                os.system("afplay /System/Library/Sounds/Glass.aiff &")
            if final_score >= 7.0:
                self.scan_phase = "dancing"
                self._enqueue(self._log, "System", f"--- {ticker} 高分！摆尾舞 ---", "phase")
                self._enqueue(self.start_waggle_dance, "ScoutBeeNova", ticker, final_score)
                time.sleep(1.2)
                scout = self.bees.get("ScoutBeeNova")
                if scout:
                    self._enqueue(scout.stop_dance)
                self.scan_phase = "foraging"

            time.sleep(0.2)

        # ===== 阶段 4：执行完整蜂群扫描（委托给 AlphaHiveDailyReporter）=====
        self.scan_phase = "foraging"
        self._enqueue(self._log, "System", "--- 阶段 2-4：7 Agent 并行觅食→共振→蒸馏 ---", "phase")
        scan_start = time.time()

        try:
            report = reporter.run_swarm_scan(
                focus_tickers=focus_tickers,
                progress_callback=on_ticker_done,
            )
        except Exception as e:
            self._enqueue(self._log, "System", f"蜂群扫描出错：{str(e)[:80]}", "alert")
            self._enqueue(self.disperse_all)
            self.scan_phase = "idle"
            self.scan_progress = {"current": 0, "total": 0, "ticker": "", "phase": ""}
            return

        # A2: 取消检测（扫描完成后检查）
        if self._cancel_requested:
            self._cancel_requested = False
            self._enqueue(self._log, "System", "⚠ 扫描已取消", "alert")
            self._enqueue(self.disperse_all)
            self.scan_phase = "idle"
            self.scan_progress = {"current": 0, "total": 0, "ticker": "", "phase": ""}
            return

        elapsed = time.time() - scan_start

        # ===== 阶段 5：最终蒸馏汇总 =====
        self.scan_phase = "distilling"
        self._enqueue(self._log, "System", "--- 阶段 5：最终蒸馏汇总 ---", "phase")
        self._enqueue(self.gather_all, 250, 200)
        self._enqueue(self._log, "System", "女王蒸馏蜂汇总完成，共振计数结束", "system")
        time.sleep(1.2)

        # 结果排序 + 摘要输出
        self._enqueue(self._log, "System", "─── 蜂群简报 ───", "phase")
        for ticker, data in sorted(all_swarm_results.items(), key=lambda x: x[1].get("final_score", 0), reverse=True):
            s = data.get("final_score", 0)
            d_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(data.get("direction", ""), "中性")
            tag = "高优先" if s >= 7.5 else ("观察" if s >= 6.0 else "暂不动")
            res = "共振✅" if data.get("resonance", {}).get("resonance_detected") else ""
            self._enqueue(self._log, "System", f"【{ticker}】{s:.1f}/10 {d_cn} [{tag}] {res}", "alert")
            for bee in self.bees.values():
                self._enqueue(bee.set_state, "publishing", s)
                self._enqueue(bee.say, f"{s:.1f}!", 25)
            time.sleep(0.2)

        self._enqueue(self._log, "System", "─── 简报结束 ───", "phase")
        self._enqueue(self._log, "System", f"耗时 {elapsed:.1f}s | {len(targets)} 标的 | 按 [R] 查看完整简报", "system")

        # 全员共振庆祝动画
        all_ids = list(self.bees.keys())
        for i in range(len(all_ids)):
            for j in range(i + 1, len(all_ids)):
                if random.random() < 0.3:
                    self._enqueue(self.create_resonance, all_ids[i], all_ids[j], 1.0)
        time.sleep(1.2)

        # ===== 阶段 6：保存报告 + 推送 GitHub（保持三端一致）=====
        self._enqueue(self._log, "System", "--- 阶段 6：保存报告 + GitHub 同步 ---", "phase")
        try:
            reporter.save_report(report)
            self._enqueue(self._log, "System", "报告文件已保存（MD/JSON/X线程）", "system")
        except Exception as e:
            self._enqueue(self._log, "System", f"报告保存失败：{str(e)[:60]}", "alert")

        try:
            reporter.auto_commit_and_notify(report)
            self._enqueue(self._log, "System", "✅ GitHub 推送完成，网站已同步", "system")
        except Exception as e:
            self._enqueue(self._log, "System", f"GitHub 推送失败：{str(e)[:60]}", "alert")

        # 更新面板数据
        has_ref = hasattr(self, '_app_ref') and self._app_ref
        opps = []
        top_dims = None
        for ticker, data in sorted(all_swarm_results.items(), key=lambda x: x[1].get("final_score", 0), reverse=True):
            opps.append({"ticker": ticker, "score": data.get("final_score", 0), "direction": data.get("direction", "neutral")})
            if top_dims is None and data.get("dimension_scores"):
                top_dims = {k: float(v) for k, v in data["dimension_scores"].items()}
        if opps and has_ref:
            self._app_ref.system_data["opportunities"] = opps[:4]
            if top_dims:
                self._app_ref.system_data["dimension_scores"] = top_dims
            self._app_ref.last_swarm_results = dict(all_swarm_results)

        # 更新历史预测面板
        try:
            from backtester import Backtester
            bt = Backtester()
            if has_ref:
                preds = bt.store.get_all_predictions(days=7)
                self._app_ref.system_data["prediction_history"] = preds[:5]
                adapted_w = Backtester.load_adapted_weights()
                if adapted_w:
                    self._app_ref.system_data["adapted_weights"] = adapted_w
        except (ImportError, OSError, ValueError, KeyError, AttributeError):
            pass

        # 散开 + 恢复 idle
        for bee in self.bees.values():
            self._enqueue(bee.say, "完成", 40)
        time.sleep(0.8)
        self._enqueue(self.disperse_all)
        for bee in self.bees.values():
            self._enqueue(bee.set_state, "idle")
        self._enqueue(self._log, "System", "全员返回待命，下次扫描：08:00（周一至周五）", "system")
        # C2: macOS 系统通知（扫描完成）
        try:
            best = max(all_swarm_results.items(), key=lambda x: x[1].get("final_score", 0))
            b_ticker, b_data = best
            b_score = b_data.get("final_score", 0)
            b_dir = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(b_data.get("direction", ""), "")
            notif = f"最高：{b_ticker} {b_score:.1f}/10 {b_dir}"
            os.system(f'osascript -e \'display notification "{notif}" with title "Alpha Hive 扫描完成" sound name "Glass"\' &')
        except (ValueError, OSError):
            pass
        # A1: 重置进度
        self.scan_progress = {"current": 0, "total": 0, "ticker": "", "phase": ""}
        self.scan_phase = "idle"

    def _random_idle_interaction(self):
        """空闲时随机互动"""
        ids = list(self.bees.keys())
        action = random.choice(["chat", "chat", "nap", "look", "signal", "signal"])

        # 随机闲聊内容（中文）
        idle_chats = {
            "ScoutBeeNova": [
                "正在检查盘后 SEC 披露...",
                "今天有没有异常的 Form 4 活动？",
                "内部人交易模式有点意思",
                "监控暗池资金流向中",
                "13F 季度报告快出了，关注大机构调仓",
            ],
            "OracleBeeEcho": [
                "VIX 在悄悄爬升，注意风险",
                "科技股期权出现异常活动",
                "Put/Call 比在变化，有东西在酝酿",
                "隐含波动率曲面偏斜明显",
                "期权市场定价有分歧，值得深挖",
            ],
            "BuzzBeeWhisper": [
                "X 热搜：AI 芯片短缺叙事升温",
                "金融推特今天情绪在转变",
                "发现新的大 V 在发布 alpha...",
                "散户情绪偏多但在衰减",
                "Reddit 和 X 的叙事出现分歧",
            ],
            "ChronosBeeHorizon": [
                "FOMC 会议还有 12 天，注意仓位",
                "财报季下周开始，准备好了",
                "催化剂日历已更新，下周很关键",
                "GDP 数据周四公布，盯紧宏观",
                "CPI 数据即将发布，通胀预期在升温",
            ],
            "RivalBeeVanguard": [
                "半导体行业竞争格局在变化",
                "新产品发布可能改变格局",
                "市场份额数据刚出来了",
                "关注定价压力趋势",
                "竞品对标分析发现新动态",
            ],
            "GuardBeeSentinel": [
                "全系统正常，持续监控异常",
                "正在核查数据完整性...",
                "风险指标在正常范围内",
                "对最近信号进行验证扫描中",
                "检查信息素板一致性，暂无冲突",
            ],
            "CodeExecutorAgent": [
                "用最新数据回测动量模型中",
                "ML 模型已重新训练，准确率稳定",
                "统计套利扫描正在运行",
                "量化信号表现稳定",
                "因子模型更新完毕，等待新数据",
            ],
        }

        if action == "chat":
            # 两只蜂聊天
            a, b = random.sample(ids, 2)
            self.bees[a].look_at(self.bees[b])
            self.bees[b].look_at(self.bees[a])
            self.send_message(a, b, "question")
            self.bees[a].say("...", 35)
            msg = random.choice(idle_chats.get(a, ["..."]))
            self._log(a, msg, "chat")

        elif action == "nap":
            # 一只蜂打瞌睡
            napper = random.choice(ids)
            self.bees[napper].set_state("sleeping")
            self._log(napper, "小憩一下... zzZ", "chat")

        elif action == "look":
            # 两只蜂互看
            a, b = random.sample(ids, 2)
            self.bees[a].look_at(self.bees[b])
            b_name = ChatLog.AGENT_SHORT.get(b, b[:6])
            self._log(a, f"看看 {b_name} 在忙什么", "chat")

        elif action == "signal":
            # 一只蜂发信号给另一只
            a, b = random.sample(ids, 2)
            self.send_message(a, b, "signal")
            self.bees[a].say("!", 25)
            msg = random.choice(idle_chats.get(a, ["收到!"]))
            b_name = ChatLog.AGENT_SHORT.get(b, b[:6])
            self._log(a, f"-> {b_name}: {msg}", "signal")


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
