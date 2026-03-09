"""
Alpha Hive GUI - 互动管理器
InteractionManager
"""

import os
import time
import random
import logging as _logging
from threading import Thread
from queue import Queue, Empty

import tkinter as tk

from gui.animations import BeeMessage, ResonanceLine, PixelBee
from gui.monitor import LiveMonitor
from gui.views import ChatLog

_log = _logging.getLogger("alpha_hive.app")


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


