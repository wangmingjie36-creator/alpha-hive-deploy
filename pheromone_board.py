#!/usr/bin/env python3
"""
🐝 Alpha Hive 信息素板 - 线程安全的蜂群通信系统
实时信号发布、共振检测、动态衰减
"""

import heapq as _heapq
import json as _json
import logging as _logging
from collections import deque as _deque
from dataclasses import dataclass, field
from typing import List, Dict
from threading import RLock
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from datetime import datetime

# v0.27.4: agent_memory.date 锁 PDT（与 reporter.date_str / predictions.date 口径一致），
# 避免本地时区为 CST/北京已过午夜时 date 比美股交易日多 1 天
try:
    from zoneinfo import ZoneInfo
    _PDT = ZoneInfo("America/Los_Angeles")
    def _pdt_today() -> str:
        return datetime.now(_PDT).strftime("%Y-%m-%d")
except Exception:
    def _pdt_today() -> str:
        return datetime.now().strftime("%Y-%m-%d")
import atexit

# pattern 1: discovery 消毒（防外部/LLM 注入文本流入下游 snapshot→QueenDistiller）；
# 防御式导入，模块缺失时降级为不消毒（不阻断信息素板）
try:
    import text_sanitizer as _ts
except ImportError:
    _ts = None

_log = _logging.getLogger("alpha_hive.pheromone_board")


@dataclass
class PheromoneEntry:
    """信息素信号单条记录"""
    agent_id: str          # "ScoutBeeNova", "OracleBeeEcho" 等
    ticker: str
    discovery: str         # 一句话发现摘要
    source: str            # 数据来源
    self_score: float      # 0.0~10.0
    direction: str         # "bullish" / "bearish" / "neutral"
    pheromone_strength: float = 1.0  # 初始强度 (0.0~1.0)
    support_count: int = 0
    supporting_agents: List[str] = field(default_factory=list)  # 去重：记录支持者
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: Dict = field(default_factory=dict)   # S3: 结构化键值对（替代正则解析）


class PheromoneBoard:
    """线程安全的信息素板（蜂群通信中枢）"""

    # ⚠️ 80 这个值**按 9 只标的定的，早已不够**：`config.WATCHLIST` 现为 30 只，
    # 一轮满编 9 条/标的（8 只蜂 + CodeExecutorAgent 在 analyze 开头无条件发的
    # 5.0/neutral 占位，见 code_executor_agent.py:75）≈ **270 条**。
    #
    # 溢出淘汰键是 `nlargest(MAX_ENTRIES, key=(self_score, support_count,
    # pheromone_strength))`，即**先扔分最低的** ⇒ 缺失与被测量的量**反相关**。
    # 生产实测（2026-08-24 起 191 份）：bearish 条目丢 50.7%、bullish 只丢 12.6%，
    # 相差 4.0 倍。详见 tests/test_resonance_eviction.py 模块 docstring。
    #
    # **本值至今未动**，因为改它会一次性改变每一个板消费方；改的是**读法**：
    # `get_agent_entry`（v0.45.151，定点）、`detect_resonance`（v0.45.156，共振）、
    # `get_live_signals`（v0.45.163，普查；GuardBee 的 risk_adj 维分走它）
    # 都不依赖 `_entries`。
    #
    # 仍受截断的读法（各自待独立测量，勿搭车）：
    #   · `get_top_signals` 的**排行榜**语义本身是对的，但还剩 3 个调用方：
    #     `bear_bee.py:39/512`(n=20)、`real_data_sources.py:261`(n=10)、
    #     `base.py:85`（仅 `get_agent_entry` 的回退）。
    #   · `snapshot` —— `alpha_hive_daily_report.py:1171` 用它填
    #     `ReportSnapshot.agent_votes`（逐蜂归因 → weekly_optimizer / self_analyst /
    #     feedback_loop）。生产实测当前世代 300 份里**只有 1 份**凑齐 8 只蜂，
    #     中位 3 只；BearBee 缺席 97.3%、BuzzBee 91.0%、ScoutBee 81.0%。
    #     ⚠️ 该处是**全扫描结束后**取板，因此除溢出淘汰外还叠加了墙钟过期（当时 3600s，v0.45.518 起 `ENTRY_MAX_AGE_S`）
    #     （一轮扫描 ~56 分钟），两个成因未分离 —— 且它正确的修法多半不是换板读法，
    #     而是根本别读板：`swarm_results[ticker]['agent_details']` 里本来就有每蜂分数。
    #   · `compact_snapshot` —— `queen_distiller.py:1161` 落进 analysis JSON 的
    #     `pheromone_compact`（正是上面几次量测所用的仪器），另两处走 LLM 路径。
    MAX_ENTRIES = 80
    #: 条目「墙钟过期」线（秒）：超过它的条目视为陈货。`publish` 的存活过滤与 `get_agent_entry` / 普查视图**共用这一个数**。
    #: v0.45.518：3600 → 10800。原值恰好等于 Step 2 超时预算（3600s），于是「能跑完的扫描，条目年龄必小于过期线」
    #: 这条不变式是**碰巧**成立的；预算放宽到 180s/只（30 只 5400s）后，拖到后半段的标的，其 Guard / Bear 经 `_read_peer` 读前面
    #: 蜂的条目会得到 None，**静默按「没有这个同伴」评分**（跑完了，但质量悄悄变差）。板是每个进程新建、从空开始的（不跨轮、
    #: 不读旧条目），过期线只防同进程内的陈货，放宽不会放进昨天的数据；正常日扫描最长 ~3100s，行为不变。
    #: 与编排器预算的关系由 `tests/test_pheromone_expiry_exceeds_scan_budget.py` 焊住（过期线必须大于 Step 2 预算）。
    ENTRY_MAX_AGE_S = 10800
    DECAY_RATE = 0.1
    MIN_STRENGTH = 0.2

    # Agent → 数据维度映射（用于跨维度共振检测）
    # 真正的共振需要来自不同数据源维度的 Agent 同向，而非同一份数据的多个解读
    AGENT_DIMENSIONS: Dict[str, str] = {
        "ScoutBeeNova":      "signal",       # SEC 披露 + 聪明钱
        "OracleBeeEcho":     "odds",         # 期权 IV + 市场赔率
        "BuzzBeeWhisper":    "sentiment",    # 新闻 + Reddit 情绪
        "ChronosBeeHorizon": "catalyst",     # 催化剂与时间线
        "GuardBeeSentinel":  "risk_adj",     # 交叉验证 + 风险
        "RivalBeeVanguard":  "ml_auxiliary", # ML 预测 + 竞争格局
        "BearBeeContrarian": "contrarian",   # 看空对冲（仅在看空共振中参与维度计数）
    }

    # 不进 `detect_resonance` 的蜂（v0.45.235）。共振要的是「多源独立印证」，
    # GuardBeeSentinel 的方向是其余六只多数的复述（v0.45.209：90/90），复述多数时会给同向方
    # 凭空多出 `risk_adj` 一个维度、并抬高 consistency。实测对收益无可测影响（逐日 IC 变化
    # p 0.77–0.89、现行规则下纸面组合入场 0 变化），只是在抬分数。
    # ⚠️ 只作用于共振；`get_live_signals`（Guard 读同伴的普查视图）不受影响。
    # BearBeeContrarian 在看空共振里的计入是另一个设计决定，本版未动。
    # 守卫：tests/test_resonance_independent_sources.py
    RESONANCE_EXCLUDED_AGENTS = frozenset({"GuardBeeSentinel"})

    _BATCH_SIZE = 20  # 每 20 条刷新一次 DB（63 条 ≈ 4 次 commit，原来 63 次）

    def __init__(self, memory_store=None, session_id=None):
        self._lock = RLock()
        self._entries: List[PheromoneEntry] = []
        # (ticker, agent_id) → 该蜂本轮最新条目。**不受 MAX_ENTRIES 溢出淘汰影响**，
        # 供 `get_agent_entry` / `BeeAgent._read_peer` 做「上游蜂这轮说了什么」的定点
        # 查询。理由见 `get_agent_entry` docstring。天然有界：|标的| × |蜂|。
        self._latest_by_agent: Dict[tuple, PheromoneEntry] = {}
        # publish 快路径的索引（只是加速结构，作废即走全量扫描；见 `_survives_publish` 上方说明）
        self._idx_list = None                    # 索引对应的 _entries 列表对象；None = 作废
        self._idx_len = 0
        self._idx_min_strength = None
        self._idx_groups: Dict[str, List[PheromoneEntry]] = {}
        self._idx_ids: set = set()
        self._idx_oldest = None
        self._memory_store = memory_store
        self._session_id = session_id or "default_session"
        # Phase 2: 使用线程池替代 daemon 线程，确保退出时等待写入完成
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pheromone_db")
        self._pending_futures: _deque = _deque(maxlen=32)  # 有界 deque，防止无限增长
        self._write_buffer: List[Dict] = []  # 批量写入缓冲区
        atexit.register(self._shutdown)
        # 可配置衰减率（从 config.PHEROMONE_CONFIG 读取，ImportError 时用默认值）
        try:
            from config import PHEROMONE_CONFIG as _pcfg
            _bdr = _pcfg.get("board_decay_rates", {})
            self._fresh_minutes = _bdr.get("fresh_minutes", 5)
            self._fresh_decay = _bdr.get("fresh_decay", 0.05)
            self._medium_decay = _bdr.get("medium_decay", self.DECAY_RATE)
            self._old_decay = _bdr.get("old_decay", self.DECAY_RATE * 1.5)
            self._ticker_scoped = _pcfg.get("board_ticker_scoped_decay", True)
        except (ImportError, AttributeError):
            self._fresh_minutes = 5
            self._fresh_decay = 0.05
            self._medium_decay = self.DECAY_RATE
            self._old_decay = self.DECAY_RATE * 1.5
            self._ticker_scoped = True

    # 方案13: 输入验证 — 保护核心通信通道
    _VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}

    @staticmethod
    def _validate_entry(entry: 'PheromoneEntry') -> None:
        """
        验证并修正 PheromoneEntry 字段，防止垃圾数据污染信息素板。

        规则：
        - self_score: clamp 到 [0, 10]，NaN/非数字 → 5.0
        - direction: 必须为 bullish/bearish/neutral，其他 → "neutral"
        - pheromone_strength: clamp 到 [0, 1]，NaN → 1.0
        """
        import math
        # self_score
        try:
            s = float(entry.self_score)
            if math.isnan(s) or math.isinf(s):
                _log.warning("PheromoneBoard: self_score 为 NaN/Inf (agent=%s, ticker=%s) → 设为 5.0",
                             entry.agent_id, entry.ticker)
                entry.self_score = 5.0
            else:
                entry.self_score = max(0.0, min(10.0, s))
        except (TypeError, ValueError):
            _log.warning("PheromoneBoard: self_score 不是数字 (agent=%s, ticker=%s) → 设为 5.0",
                         entry.agent_id, entry.ticker)
            entry.self_score = 5.0

        # direction
        if entry.direction not in PheromoneBoard._VALID_DIRECTIONS:
            _log.warning("PheromoneBoard: direction '%s' 无效 (agent=%s, ticker=%s) → 设为 'neutral'",
                         entry.direction, entry.agent_id, entry.ticker)
            entry.direction = "neutral"

        # pheromone_strength
        try:
            p = float(entry.pheromone_strength)
            if math.isnan(p) or math.isinf(p):
                _log.warning("PheromoneBoard: pheromone_strength 为 NaN/Inf → 设为 0.5 中位"
                             "（旧实现给 1.0 最大值，等于奖励坏数据）")
                entry.pheromone_strength = 0.5
            else:
                entry.pheromone_strength = max(0.0, min(1.0, p))
        except (TypeError, ValueError):
            # v0.45.50：1.0 是**最大值**，不是中位数。
            # 值损坏的条目原先直接拿到板上最高影响力与最长存活期 ——
            # 越是坏数据越被优先引用。改为 0.5 中位，并留痕。
            _log.warning("PheromoneBoard: pheromone_strength 不可解析(%r) → 设为 0.5 中位"
                         "（旧实现给 1.0 最大值，等于奖励坏数据）",
                         getattr(entry, "pheromone_strength", None))
            entry.pheromone_strength = 0.5

        # pattern 3: 来源强制 —— CLAUDE.md「禁止无来源结论进入信息素板」此前仅文档、未落代码。
        # 空 / 纯空白 source → 标记 [UNSOURCED]（fail-safe：标记而非丢弃，不破坏蜂群协作）
        if not (isinstance(entry.source, str) and entry.source.strip()):
            _log.warning("PheromoneBoard: 条目缺来源 (agent=%s, ticker=%s) → 标记 [UNSOURCED]",
                         entry.agent_id, entry.ticker)
            entry.source = "[UNSOURCED]"

        # pattern 1: discovery 为外部 / LLM 文本，下游 snapshot 会喂给 QueenDistiller。
        # 最小化消毒（中和注入短语 / 控制字符，保留 '|' 分隔符与原长度）
        if _ts and isinstance(entry.discovery, str) and entry.discovery:
            entry.discovery = _ts.sanitize_external_text(
                entry.discovery, max_len=None, collapse_ws=False)

    def publish(self, entry: PheromoneEntry) -> None:
        """
        发布新发现，自动衰减旧条目

        Args:
            entry: 新的信息素条目
        """
        self._validate_entry(entry)  # 方案13: 入口验证
        with self._lock:
            # 单遍衰减 + 存活过滤；仅衰减同 ticker 条目（避免跨 ticker 误杀：TSLA 发布不应衰减 NVDA）。
            # 逐条判定只有一份实现（`_survives_publish`）；快路径只改「访问哪些条目」，见 `_ticker_group_if_rest_untouched`。
            now = datetime.now()
            group = self._ticker_group_if_rest_untouched(now, entry)
            if group is None:
                # 先作废索引再扫：扫到一半抛出时（旧代码同样抛），已衰减却没来得及丢的条目不能留在一份「有效」的索引里
                self._idx_list = None            # 下次 publish 惰性重建
                self._entries = [e for e in self._entries
                                 if self._survives_publish(e, now, entry.ticker)]
                bump_scan = self._entries
            else:
                bump_scan = self._publish_scan_group(now, entry.ticker, group)

            # 若同 ticker + direction 已有条目，增加支持数（排除同 agent 重复）
            # 快路径下 bump_scan 是同标的子序列（保持 _entries 原序），「第一条匹配」与全表扫描是同一条。
            found_resonance = False
            for e in bump_scan:
                if e.ticker == entry.ticker and e.direction == entry.direction:
                    # 仅当不同 Agent 时才增加支持
                    if entry.agent_id not in e.supporting_agents:
                        e.support_count += 1
                        e.supporting_agents.append(entry.agent_id)
                        # 强化信息素强度（但不超过 1.0）
                        e.pheromone_strength = min(1.0, e.pheromone_strength + 0.2)
                    found_resonance = True
                    break

            # 添加新条目（保持最大 MAX_ENTRIES 条）— S6: 按 (score, support, strength) 优先级截断
            if not entry.supporting_agents:
                entry.supporting_agents = [entry.agent_id]
            self._entries.append(entry)
            # 定点索引与 _entries 并行维护：后者按显著度排序并截断，前者只按
            # 身份记「最新一条」。两者用途不同，不要合并（见 get_agent_entry）。
            self._latest_by_agent[(entry.ticker, entry.agent_id)] = entry
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = _heapq.nlargest(
                    self.MAX_ENTRIES, self._entries,
                    key=lambda x: (x.self_score, x.support_count, x.pheromone_strength),
                )
                self._idx_list = None            # 截断后顺序已变，索引失效
            elif group is not None and self._idx_list is not None:
                self._index_append(entry)

            # 批量异步持久化到 DB：先缓冲，达到阈值后批量提交（减少 fsync 次数）
            if self._memory_store:
                entry_dict = {
                    'agent_id': entry.agent_id,
                    'ticker': entry.ticker,
                    'discovery': entry.discovery,
                    'source': entry.source,
                    'self_score': entry.self_score,
                    'direction': entry.direction,
                    'pheromone_strength': entry.pheromone_strength,
                    'support_count': entry.support_count,
                    'details': _json.dumps(entry.details, ensure_ascii=False, default=str) if entry.details else None,
                    'date': _pdt_today()  # v0.27.4: PDT 美股交易日
                }
                self._write_buffer.append(entry_dict)
                if len(self._write_buffer) >= self._BATCH_SIZE:
                    self._flush_writes()

    # ── publish 的存活扫描：逐条判定 + 只访问同标的条目的快路径（v0.45.528）────────────────
    #
    # 为什么要快路径：全量扫描每次 publish 都要遍历并解析**全部**条目的时间戳，n 条累计 O(n²)。
    # 生产 MAX_ENTRIES=80 把 n 封在 81 以内（一轮 ~270 次 publish ≈ 2 万次迭代，约 10ms），
    # **对生产扫描时长没有可测收益**；受益的是把 MAX_ENTRIES 调到 4000/10000 做「与容量无关」对照的
    # 测试（`test_*_identical_under_any_max_entries`）。所以快路径的唯一要求是**结果逐位相同**：
    #
    # 全量扫描对「别的标的」的条目 e 只做三件事 ——
    #   ① e.pheromone_strength < MIN_STRENGTH 则丢；② 时间戳不可解析 / 与 now 不可相减则丢；
    #   ③ 年龄 >= ENTRY_MAX_AGE_S 则丢；否则原样保留（不衰减：ticker_scoped）。
    # 快路径只在能证明这三条对**所有**别标的条目都不成立时才跳过它们：
    #   ①② 由索引的「干净」不变式保证（每条都是 float 强度且不低于建索引时的 MIN_STRENGTH、时间戳能
    #       解析成 naive datetime、无同一对象重复上板）；强度只会被本板改动：同标的衰减后低于 MIN 当场就丢；
    #       支持加成后 = min(1.0, s+0.2) ≥ MIN（MIN ≤ 1 时），MIN > 1 时校验过的新条目（≤ 1.0）入板必作废索引，
    #       故加成不需另核；新条目入板时逐条核验，不合格即作废索引。
    #   ③ 记全板最早时间戳（只降不升的下界）；年龄对时间戳单调，最早那条没过期 ⇒ 全都没过期。
    #       当场用 now 求值，不假设时钟单调。
    # 前提任何一条不满足 ⇒ 走全量扫描（逐条判定是同一个 `_survives_publish`），索引下次惰性重建。
    # 作废索引的事件：_entries 换了对象 / 长度变了（外部 extend、clear）/ MIN_STRENGTH 变了 / 溢出截断 /
    # 非 str ticker / 带时区的 now 或时间戳 / 同一对象重复 publish / ticker_scoped 关闭。
    #
    # ⚠️ 不变式的前提：**已上板条目的 ticker / timestamp / pheromone_strength 只由本板改写**
    # （同一条目对象再发到另一块板也算外部写：那块板的衰减会改它。生产每次 publish 都新建条目、每进程一块板）。
    # 全仓生产代码无此类外部写（守卫 tests/test_pheromone_publish_fast_path.py 的 AST 断言）；
    # 若将来真要改已发布条目，先改这里，别让快路径静默漏掉一次淘汰。
    # 等价性证明：同文件把旧 publish 原文冻成参照实现，随机序列逐次比对整板状态。

    def _survives_publish(self, e: PheromoneEntry, now: datetime, ticker: str) -> bool:
        """publish 对单条既有条目的存活 + 衰减判定（全量与快路径共用这一份）。"""
        # 存活检查 1: 强度过低
        if e.pheromone_strength < self.MIN_STRENGTH:
            return False
        # 存活检查 2: 时间戳解析 + 超时淘汰（ENTRY_MAX_AGE_S）
        try:
            _age_s = (now - datetime.fromisoformat(e.timestamp)).total_seconds()
        except (ValueError, TypeError):
            return False  # 时间戳解析失败 → 视为过期
        if _age_s >= self.ENTRY_MAX_AGE_S:
            return False
        # 衰减（仅同 ticker）
        if not self._ticker_scoped or e.ticker == ticker:
            _age_m = _age_s / 60
            if _age_m < self._fresh_minutes:
                e.pheromone_strength -= self._fresh_decay
            elif _age_m < 30:
                e.pheromone_strength -= self._medium_decay
            else:
                e.pheromone_strength -= self._old_decay
            # 衰减后二次检查（防止衰减到 MIN 以下）
            if e.pheromone_strength < self.MIN_STRENGTH:
                return False
        return True

    def _strength_ok(self, e: PheromoneEntry) -> bool:
        s = e.pheromone_strength
        return type(s) is float and not s < self.MIN_STRENGTH

    def _ticker_group_if_rest_untouched(self, now: datetime, entry: PheromoneEntry):
        """快路径前提全部成立时返回 entry.ticker 的同标的条目（_entries 原序子序列），否则 None。"""
        if not self._ticker_scoped or type(entry.ticker) is not str:
            return None
        try:
            if now.utcoffset() is not None:
                return None
        except (AttributeError, TypeError):
            return None
        if (self._idx_list is not self._entries
                or self._idx_len != len(self._entries)
                or self._idx_min_strength != self.MIN_STRENGTH):
            # 板已满 ⇒ 这次多半又要溢出截断、索引随即作废，重建白做：直接全量。生产 MAX_ENTRIES=80 的稳态就是这条
            if len(self._entries) >= self.MAX_ENTRIES or not self._rebuild_index():
                return None
        if id(entry) in self._idx_ids:
            return None                          # 同一对象重复上板：逐次出现的判定须走全量
        if self._idx_oldest is not None:
            try:
                if (now - self._idx_oldest).total_seconds() >= self.ENTRY_MAX_AGE_S:
                    return None                  # 至少有一条可能过期
            except (TypeError, ValueError):
                return None
        return self._idx_groups.get(entry.ticker, [])

    def _rebuild_index(self) -> bool:
        """从 _entries 重建快路径索引；任何一条不满足「干净」不变式即返回 False（索引保持作废）。"""
        self._idx_list = None
        groups: Dict[str, List[PheromoneEntry]] = {}
        ids = set()
        oldest = None
        for e in self._entries:
            # 任何异常一律只是「快路径不适用」：全量扫描是旧代码原样，该抛的在那里按旧顺序、旧消息抛
            # （例：缺 ticker 的非标准对象，旧代码先按强度丢掉它、根本不读 ticker）
            try:
                if type(e.ticker) is not str or id(e) in ids or not self._strength_ok(e):
                    return False
                ts = datetime.fromisoformat(e.timestamp)
                if ts.utcoffset() is not None:
                    return False
            except Exception:
                return False
            ids.add(id(e))
            groups.setdefault(e.ticker, []).append(e)
            if oldest is None or ts < oldest:
                oldest = ts
        self._idx_groups, self._idx_ids, self._idx_oldest = groups, ids, oldest
        self._idx_min_strength = self.MIN_STRENGTH
        self._idx_list, self._idx_len = self._entries, len(self._entries)
        return True

    def _publish_scan_group(self, now: datetime, ticker: str,
                            group: List[PheromoneEntry]) -> List[PheromoneEntry]:
        """快路径：只对同标的条目跑 `_survives_publish`，其余条目已证明会原样存活。返回同标的存活者。"""
        self._idx_list = None                    # 扫完才恢复；中途抛出则索引保持作废（理由同全量路径）
        survivors: List[PheromoneEntry] = []
        gone = set()
        clean = True
        for e in group:
            if self._survives_publish(e, now, ticker):
                survivors.append(e)
                if not self._strength_ok(e):
                    clean = False
            else:
                gone.add(id(e))
        if gone:
            self._entries = [e for e in self._entries if id(e) not in gone]
            self._idx_ids -= gone
        else:
            self._entries = self._entries.copy()  # 与全量路径一致：每次 publish 后都是新列表对象
        if survivors:
            self._idx_groups[ticker] = survivors
        else:
            self._idx_groups.pop(ticker, None)
        self._idx_list = self._entries if clean else None
        self._idx_len = len(self._entries)
        return survivors

    def _index_append(self, entry: PheromoneEntry) -> None:
        """把刚追加的新条目登记进索引；不满足「干净」不变式则作废索引（下次 publish 走全量）。"""
        try:
            ts = datetime.fromisoformat(entry.timestamp)
            naive = ts.utcoffset() is None
        except (ValueError, TypeError):
            naive = False
        if not naive or not self._strength_ok(entry):
            self._idx_list = None
            return
        self._idx_groups.setdefault(entry.ticker, []).append(entry)
        self._idx_ids.add(id(entry))
        if self._idx_oldest is None or ts < self._idx_oldest:
            self._idx_oldest = ts
        self._idx_list, self._idx_len = self._entries, len(self._entries)

    def get_top_signals(self, ticker: str = None, n: int = 5) -> List[PheromoneEntry]:
        """
        获取高强度信号，可按 ticker 过滤

        Args:
            ticker: 可选的股票过滤
            n: 返回的信号数

        Returns:
            按强度排序的信号列表
        """
        with self._lock:
            entries = [e for e in self._entries if ticker is None or e.ticker == ticker]
            return sorted(entries, key=lambda x: x.pheromone_strength, reverse=True)[:n]

    #: `get_agent_entry` 认定「陈货」的墙钟阈值，与 `publish` 里存活检查 2 用的是**同一个** `ENTRY_MAX_AGE_S` ——
    #: 两处必须一致，否则同一条目在排行榜里已过期、在定点查询里还活着。（旧属性名保留：测试与调用方按它引用。）
    _AGENT_ENTRY_MAX_AGE_S = ENTRY_MAX_AGE_S

    def get_agent_entry(self, ticker: str, agent_id: str):
        """定点取「某只蜂对某只标的本轮发布的最新条目」，取不到返回 None。

        为什么不能用 `get_top_signals(ticker, n=...)` 代替（v0.45.151）
        ---------------------------------------------------------------
        `get_top_signals` 从 `_entries` 里挑，而 `_entries` 在 `publish` 溢出时会按
        `nlargest(MAX_ENTRIES, key=(self_score, support_count, pheromone_strength))`
        截断 —— **先扔分最低的**。`MAX_ENTRIES = 80` 是按注释里那个「9 只标的」的
        年代定的，而 `config.WATCHLIST` 现为 30 只（一轮约 210 条），于是低分条目
        在同轮的下游蜂读到它之前就被挤出去了。

        后果不是「偶尔读不到」，而是**缺失与被测量的量反相关**：越是低分（例如
        ChronosBee 的「无近期催化剂」恒落 4.0）越读不到，而调用方的回落值通常比
        真值高 ⇒ 系统性单向偏斜。生产实测见 `tests/test_rival_bee_catalyst_missing.py`
        的模块 docstring（当前世代 188 份里 27 份被记成了错的等级，流向全是 C→B）。

        本方法只按身份查，不参与显著度排序，因此不受截断影响。仍保留墙钟过期
        （`_AGENT_ENTRY_MAX_AGE_S`）—— 那个判的是「这是不是上一轮的陈货」，
        是真的该拦；被 MAX_ENTRIES 挤掉则纯属容量问题，不该拦。
        """
        with self._lock:
            e = self._latest_by_agent.get((ticker, agent_id))
            if e is None:
                return None
            try:
                age_s = (datetime.now()
                         - datetime.fromisoformat(e.timestamp)).total_seconds()
            except (ValueError, TypeError):
                return None                      # 时间戳不可解析 → 视为过期
            if age_s >= self._AGENT_ENTRY_MAX_AGE_S:
                del self._latest_by_agent[(ticker, agent_id)]
                return None
            return e

    def _live_agent_entries(self, ticker: str) -> List[PheromoneEntry]:
        """该标的本轮「每只蜂最新一条」的存活视图，**不受 MAX_ENTRIES 溢出淘汰影响**。

        需持有 `_lock` 调用。过期条目就地清除，口径与 `get_agent_entry` 完全一致
        （同一个 `_AGENT_ENTRY_MAX_AGE_S`）—— 墙钟过期判的是「这是不是上一轮的
        陈货」，该拦；被容量挤掉纯属容量问题，不该拦。
        """
        now = datetime.now()
        live: List[PheromoneEntry] = []
        expired = []
        for key, e in self._latest_by_agent.items():
            if key[0] != ticker:
                continue
            try:
                age_s = (now - datetime.fromisoformat(e.timestamp)).total_seconds()
            except (ValueError, TypeError):
                expired.append(key)          # 时间戳不可解析 → 视为过期
                continue
            if age_s >= self._AGENT_ENTRY_MAX_AGE_S:
                expired.append(key)
                continue
            live.append(e)
        for key in expired:
            del self._latest_by_agent[key]
        return live

    def get_live_signals(self, ticker: str) -> List[PheromoneEntry]:
        """该标的本轮的**普查**视图：每只蜂最新一条，不受 MAX_ENTRIES 溢出淘汰影响。

        与 `get_top_signals` 的区别是**口径**，不是参数（v0.45.163）
        ------------------------------------------------------------------
        `get_top_signals` 是**排行榜**：从 `_entries` 里按 `pheromone_strength`
        取前 N。调用方若要的是「这一轮蜂群整体怎么看」（均分、方向一致性、
        看多蜂数），排行榜给不了 —— 两层都会骗它：

        ① `_entries` 溢出时按 `nlargest(MAX_ENTRIES, key=(self_score, ...))`
           截断，**先扔分最低的** ⇒ 幸存者均值**按构造**偏高，且缺失与被测量的
           量反相关（低分/看空先消失）。
        ② 即使板没溢出，`n` 仍会砍掉一部分蜂。生产实测：Guard 之前恒有 6 只蜂
           发布，而 `n=5` 的窗口 **191/191 = 100%** 装不下。

        本方法只按身份取（复用 `_latest_by_agent`），因此条数只取决于**有几只蜂
        发布过**，与 `MAX_ENTRIES` 无关。同一只蜂重复发布只算最新一条 ——
        CodeExecutorAgent 在 `analyze` 开头无条件发的 5.0/neutral 占位
        （`code_executor_agent.py:75`）因此不会被当成一票；而在排行榜口径下它
        分比真实结论高，溢出时留下的恰恰是那个**桩**（生产实测 16 例）。

        仍保留墙钟过期（`_AGENT_ENTRY_MAX_AGE_S`，口径与 `get_agent_entry`
        完全一致）—— 那判的是「这是不是上一轮的陈货」，该拦；被容量挤掉纯属
        容量问题，不该拦。

        返回按 `pheromone_strength` 降序（只为输出稳定，普查不依赖顺序）。
        量测与影响面见 `tests/test_guard_census_eviction.py` 模块 docstring。
        """
        with self._lock:
            entries = self._live_agent_entries(ticker)
            return sorted(entries, key=lambda x: x.pheromone_strength, reverse=True)

    def detect_resonance(self, ticker: str) -> Dict:
        """
        检测信号共振：同向信号来自 >= 3 个不同数据维度时才触发增强

        旧逻辑：同向 Agent 数量 >= 3（存在虚假放大：多个 Agent 基于相同 yfinance 数据）
        新逻辑：同向 Agent 覆盖 >= 3 个不同数据维度（真正的多源独立印证）

        为什么读 `_live_agent_entries` 而不是 `_entries`（v0.45.156）
        ------------------------------------------------------------
        `_entries` 溢出时按 `nlargest(MAX_ENTRIES, key=(self_score, ...))` 截断，
        **先扔分最低的**，而低分与看空/中性相关 ⇒ 共振计数、`supporting_agents`、
        `consistency`、多空对比全部单向偏斜。这不是「偶尔少数一票」：生产实测
        （当前世代 191 份）共振方向翻转 **15.2%**、`resonance_detected` 翻转
        **20.9%**，且 **20.4%** 的样本把 `consistency` 记成 **1.0（完全一致）**
        —— 板不只是丢数据，它**制造了从未存在过的一致**。
        `analysis-AMC-ml-2026-09-03.json`：9 只蜂 5 只看空，板上只剩 3 条全看多，
        于是记成 `bullish / consistency=1.0 / +15 boost`。
        证据与方法见 `tests/test_resonance_eviction.py` 模块 docstring。

        ⚠️ 同一只蜂重复发布只算最新一条（`_latest_by_agent` 按 (ticker, agent_id)
        建键）。这与旧行为有一处**有意的**差别：CodeExecutorAgent 每轮发两条
        （5.0/neutral 占位 + 真实结论），旧行为下两条都进 `consistency` 分母。
        全量对照实测：collapse 前后 `resonance_detected` / `direction` /
        `supporting_agents` / `cross_dim_count` / `confidence_boost` **0/712 不同**，
        只有 `consistency` 的分母差 1（占位条本就不是独立观点，不该稀释一致性）。

        Args:
            ticker: 标的代码

        Returns:
            共振检测结果字典，新增 cross_dim_count / resonant_dimensions 字段
        """
        with self._lock:
            ticker_entries = [e for e in self._live_agent_entries(ticker)
                              if e.agent_id not in self.RESONANCE_EXCLUDED_AGENTS]
            bullish = [e for e in ticker_entries if e.direction == "bullish"]
            bearish = [e for e in ticker_entries if e.direction == "bearish"]

            # 无条目时直接返回 neutral
            if not bullish and not bearish:
                return {
                    "resonance_detected": False,
                    "direction": "neutral",
                    "supporting_agents": 0,
                    "cross_dim_count": 0,
                    "resonant_dimensions": [],
                    "confidence_boost": 0,
                }

            # 平局时双向检查维度数，选择维度覆盖更广的方向（避免一律偏多）
            if len(bullish) == len(bearish) and len(bullish) > 0:
                _bull_dims = {self.AGENT_DIMENSIONS.get(e.agent_id, "unknown") for e in bullish} - {"unknown", "contrarian"}
                _bear_dims = {self.AGENT_DIMENSIONS.get(e.agent_id, "unknown") for e in bearish} - {"unknown"}
                dominant = "bearish" if len(_bear_dims) > len(_bull_dims) else "bullish"
            else:
                dominant = "bullish" if len(bullish) > len(bearish) else "bearish"
            dominant_entries = bullish if dominant == "bullish" else bearish

            # 统计同向 Agent 覆盖的不同数据维度数
            unique_dims = {
                self.AGENT_DIMENSIONS.get(e.agent_id, "unknown")
                for e in dominant_entries
            } - {"unknown"}
            # BearBee contrarian 维度仅在看空共振中计入
            # （看多时排除：BearBee 返回 bullish 仅表示"无看空证据"，非独立看多维度）
            if dominant == "bullish":
                unique_dims -= {"contrarian"}

            cross_dim_count = len(unique_dims)

            # 一致性：优势方向条目数 / 该标的全部条目数（含中性）
            # 中性 Agent 表示"无明确信号"，应纳入分母以降低稀释后的一致性
            consistency = len(dominant_entries) / len(ticker_entries) if ticker_entries else 0.0

            # 触发条件（双重门槛）：
            #   1. 跨维度：≥3 个不同数据维度同向（多源独立印证）
            #   2. 一致性：优势方向票数 > 全部 Agent 票数的 50%
            # 背景：5 Agent / 5 维度时，任意 3 票就可覆盖 3 维 → 原单一条件 10/10 全触发；
            #       加入一致性后，3/7 = 43% < 50% 不触发（信号分散的标的正确返回 N）
            resonance_detected = cross_dim_count >= 3 and consistency > 0.5

            return {
                "resonance_detected": resonance_detected,
                "direction": dominant,
                "supporting_agents": len(dominant_entries),
                "cross_dim_count": cross_dim_count,
                "consistency": round(consistency, 3),
                "resonant_dimensions": sorted(unique_dims),
                "confidence_boost": min(cross_dim_count * 5, 20) if resonance_detected else 0,
            }

    def snapshot(self) -> List[Dict]:
        """
        返回完整板快照（用于 QueenDistiller）

        Returns:
            信息素板的完整记录快照
        """
        with self._lock:
            return [
                {
                    "agent_id": e.agent_id,
                    "ticker": e.ticker,
                    "discovery": e.discovery,
                    "source": e.source,
                    "self_score": e.self_score,
                    "direction": e.direction,
                    "pheromone_strength": round(e.pheromone_strength, 3),
                    "support_count": e.support_count,
                    "timestamp": e.timestamp,
                    "details": e.details,
                }
                for e in self._entries
            ]

    # D1: details 中对 LLM 决策最有价值的字段白名单（避免传全量）
    _DETAIL_KEYS = {
        "pc_ratio", "iv_rank", "iv_skew", "gex",                    # OracleBee
        "sentiment_score", "reddit_momentum",                        # BuzzBee
        "catalyst_count", "nearest_days", "analyst_upside_pct",      # ChronosBee
        "ml_probability", "expected_7d",                             # RivalBee
        "consistency", "conflict_type", "resonance_detected",        # GuardBee
        "bear_score", "signal_count",                                # BearBee
    }

    def compact_snapshot(self, ticker: str = None) -> List[Dict]:
        """
        紧凑快照：核心字段 + 精选 details，供 LLM 决策

        相比 snapshot() 减少 ~50% 数据量：
        - 去掉 discovery（大文本）、timestamp、source
        - 保留评分、方向、支持数
        - D1: 新增 details 白名单字段（仅数值/布尔，每条约 +40 token）
        """
        with self._lock:
            entries = self._entries
            if ticker:
                entries = [e for e in entries if e.ticker == ticker]
            result = []
            for e in entries:
                item = {
                    "a": e.agent_id[:8],  # 缩写 agent_id
                    "t": e.ticker,
                    "d": {"bullish": "+", "bearish": "-", "neutral": "0"}.get(e.direction, "?"),
                    "s": round(e.self_score, 1),
                    "p": round(e.pheromone_strength, 2),
                    "c": e.support_count,
                }
                # D1: 附加精选 details（白名单过滤，仅有值时添加）
                if e.details:
                    detail_compact = {
                        k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in e.details.items()
                        if k in self._DETAIL_KEYS and v is not None
                    }
                    if detail_compact:
                        item["x"] = detail_compact  # "x" = extra details
                result.append(item)
            return result

    def get_entry_count(self) -> int:
        """获取当前板上的条目数"""
        with self._lock:
            return len(self._entries)

    def _flush_writes(self) -> None:
        """将缓冲区中的条目批量写入 DB（需持有 _lock 调用）"""
        if not self._write_buffer or not self._memory_store:
            return
        batch = list(self._write_buffer)
        self._write_buffer.clear()
        # 清理已完成的 futures + 检查失败
        _still = _deque(maxlen=32)
        for f in self._pending_futures:
            if not f.done():
                _still.append(f)
            elif f.exception() is not None:
                _log.warning("PheromoneBoard 异步写入失败: %s", f.exception())
        self._pending_futures = _still
        # 提交批量写入（通过 callback 确保失败时写入 fallback，防止数据丢失）
        try:
            future = self._executor.submit(
                self._memory_store.save_agent_memories_batch, batch, self._session_id
            )

            def _on_done(f, _batch=batch):
                if f.exception() is not None:
                    _log.warning("PheromoneBoard 异步批量写入失败: %s", f.exception())
                    self._save_fallback_batch(_batch)

            future.add_done_callback(_on_done)
            self._pending_futures.append(future)
        except RuntimeError:
            _log.debug("PheromoneBoard executor shut down, fallback to sync")
            self._save_fallback_batch(batch)

    def flush_writes(self) -> None:
        """公开接口：强制刷新缓冲区（扫描结束时调用）"""
        with self._lock:
            self._flush_writes()

    def _save_fallback(self, entry_dict: dict) -> None:
        """异步写入失败时保存到 fallback JSON 文件，防止数据丢失"""
        self._save_fallback_batch([entry_dict])

    def _save_fallback_batch(self, entries: list) -> None:
        """批量 fallback 写入。

        v0.45.260（数据根迁移阶段 2）：此前落盘位置是 `Path(__file__).parent /
        "pheromone_fallback.jsonl"`——不读 `ALPHA_HIVE_HOME`。这条路径本该和
        `pheromone.db` 同级受管：全仓 grep 确认从未被读回，丢了就是唯一副本丢了
        （"不可重取数据"）。改读 `PATHS.home`。
        """
        from hive_logger import PATHS
        try:
            fb_path = PATHS.home / "pheromone_fallback.jsonl"
            with open(fb_path, "a", encoding="utf-8") as f:
                for entry_dict in entries:
                    f.write(_json.dumps(entry_dict, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            _log.error("PheromoneBoard fallback 写入也失败: %s", e)

    def _shutdown(self) -> None:
        """atexit 处理器：刷新缓冲 + 等待异步写入完成 + 关闭线程池"""
        # 刷新残余缓冲 + 在 lock 内复制 pending（防止与 publish 并发修改 _pending_futures）
        with self._lock:
            self._flush_writes()
            pending = [f for f in self._pending_futures if not f.done()]
        # 在 lock 外等待（callback 可能需要 lock 进行 fallback 写入）
        if pending:
            _log.debug("PheromoneBoard shutdown: 等待 %d 个异步写入完成…", len(pending))
            done, not_done = _futures_wait(pending, timeout=5)
            if not_done:
                _log.warning(
                    "PheromoneBoard shutdown: %d 个异步写入超时未完成（已放弃）",
                    len(not_done),
                )
                for f in not_done:
                    f.cancel()
            for f in done:
                try:
                    f.result(timeout=0)
                except Exception as exc:
                    _log.warning("PheromoneBoard shutdown 写入失败: %s", exc)
        self._executor.shutdown(wait=True)

    def clear(self) -> None:
        """清空信息素板"""
        with self._lock:
            self._entries.clear()
            self._latest_by_agent.clear()
            self._idx_list = None
            self._idx_groups, self._idx_ids = {}, set()
