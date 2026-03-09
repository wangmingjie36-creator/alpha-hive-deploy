"""
Alpha Hive GUI - 动画组件
BeeMessage, ResonanceLine, PixelBee, HoneycombBackground
"""

import math
import random

import tkinter as tk


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


