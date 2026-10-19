"""`PheromoneBoard.publish` 快路径与旧全量扫描**逐位等价**（v0.45.528）。

背景
────
旧 `publish` 每次都遍历并解析全板时间戳，n 条累计 O(n²)。生产 `MAX_ENTRIES=80` 把 n 封在 81
以内，对扫描时长无可测收益；慢的是把 `MAX_ENTRIES` 调到 4000/10000 做「与容量无关」对照的测试
（`test_get_live_signals_identical_under_any_max_entries` 本机 9.2s）。快路径只访问同标的条目，
其余条目在能证明「全量扫描也会原样保留它们」时跳过 —— 证明与不变式见 `pheromone_board.py`
`_survives_publish` 上方说明。板直接喂评分（共振 / 普查 / 定点读），所以这里要的是**状态逐位相同**，
不是「测试仍绿」。

怎么证
──────
`_RefBoard.publish` 是改动前 `publish` 的**原文**（冻结在本文件，不随生产代码变）。同一组随机动作
同时驱动参照板与生产板，每一步之后比整板状态：`_entries` 的顺序与每条的全部可变字段（强度按 repr
比，连浮点运算顺序差异都能抓到）、`_latest_by_agent`、以及「每次 publish 后 `_entries` 是新列表对象」
这一可观察性质。时钟由脚本推进（含回拨），两块板读同一个 now。

随机动作覆盖快路径的每一个退出条件：过期 / 未来 / 坏 / 带时区时间戳、强度 < MIN / NaN / 非数字、
同一对象重复 publish、外部 `_entries.extend`、`clear()`、运行中改 `MIN_STRENGTH` /
`ENTRY_MAX_AGE_S` / `MAX_ENTRIES` / `_ticker_scoped`。并断言两条路径都真被走到了（防假绿）。

⚠️ 快路径正确的前提是「已上板条目的 ticker / timestamp / pheromone_strength 只由本板改写」——
`TestNoExternalWritesToBoardOwnedFields` 用 AST 钉住生产代码里没有这类外部写。
"""
from __future__ import annotations

import ast
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heapq as _heapq  # noqa: E402

import pheromone_board as pb  # noqa: E402
from pheromone_board import PheromoneBoard, PheromoneEntry  # noqa: E402
from _repo_files import own_python_files  # noqa: E402
from tests import _ast_cache  # noqa: E402


class _RefBoard(PheromoneBoard):
    """参照实现：v0.45.527 的 `publish` 原文（持久化分支删去——两块板都不带 memory_store）。"""

    def publish(self, entry: PheromoneEntry) -> None:
        datetime = pb.datetime  # 与生产同一个（被脚本时钟替换后的）datetime
        self._validate_entry(entry)
        with self._lock:
            now = datetime.now()
            _surviving = []
            for e in self._entries:
                if e.pheromone_strength < self.MIN_STRENGTH:
                    continue
                try:
                    _age_s = (now - datetime.fromisoformat(e.timestamp)).total_seconds()
                except (ValueError, TypeError):
                    continue
                if _age_s >= self.ENTRY_MAX_AGE_S:
                    continue
                if not self._ticker_scoped or e.ticker == entry.ticker:
                    _age_m = _age_s / 60
                    if _age_m < self._fresh_minutes:
                        e.pheromone_strength -= self._fresh_decay
                    elif _age_m < 30:
                        e.pheromone_strength -= self._medium_decay
                    else:
                        e.pheromone_strength -= self._old_decay
                    if e.pheromone_strength < self.MIN_STRENGTH:
                        continue
                _surviving.append(e)
            self._entries = _surviving

            for e in self._entries:
                if e.ticker == entry.ticker and e.direction == entry.direction:
                    if entry.agent_id not in e.supporting_agents:
                        e.support_count += 1
                        e.supporting_agents.append(entry.agent_id)
                        e.pheromone_strength = min(1.0, e.pheromone_strength + 0.2)
                    break

            if not entry.supporting_agents:
                entry.supporting_agents = [entry.agent_id]
            self._entries.append(entry)
            self._latest_by_agent[(entry.ticker, entry.agent_id)] = entry
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = _heapq.nlargest(
                    self.MAX_ENTRIES, self._entries,
                    key=lambda x: (x.self_score, x.support_count, x.pheromone_strength),
                )


class _Clock(datetime):
    """脚本时钟：两块板读同一个 now；fromisoformat 照旧（继承）。"""
    current = datetime(2026, 10, 16, 9, 30, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.replace(tzinfo=tz)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(pb, "datetime", _Clock)
    _Clock.current = datetime(2026, 10, 16, 9, 30, 0)
    return _Clock


@pytest.fixture
def path_counter(monkeypatch):
    """数快路径 / 全量路径各走了几次（只计数，不改行为）。"""
    counts = {"fast": 0, "slow": 0}
    orig = PheromoneBoard._ticker_group_if_rest_untouched

    def counting(self, now, entry):
        g = orig(self, now, entry)
        if type(self) is PheromoneBoard:
            counts["fast" if g is not None else "slow"] += 1
        return g

    monkeypatch.setattr(PheromoneBoard, "_ticker_group_if_rest_untouched", counting)
    return counts


def _state_of(e):
    return (e.agent_id, e.ticker, e.direction, repr(e.self_score), repr(e.pheromone_strength),
            e.support_count, tuple(e.supporting_agents), e.timestamp, e.discovery, e.source)


def _board_state(b):
    return ([_state_of(e) for e in b._entries],
            sorted((repr(k), _state_of(v)) for k, v in b._latest_by_agent.items()))


# ── 随机动作 ─────────────────────────────────────────────────────────────

_AGENTS = ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon",
           "RivalBeeVanguard", "GuardBeeSentinel", "BearBeeContrarian", "CodeExecutorAgent"]


def _rand_spec(rng: random.Random, now: datetime, tickers):
    r = rng.random()
    if r < 0.55:
        ts = None                                            # 缺省：当下
    elif r < 0.80:
        ts = (now - timedelta(seconds=rng.uniform(0, 4.5 * 3600))).isoformat()
    elif r < 0.86:
        ts = (now + timedelta(seconds=rng.uniform(0, 900))).isoformat()   # 未来
    elif r < 0.91:
        ts = "not-a-date"
    elif r < 0.95:
        ts = (now - timedelta(seconds=rng.uniform(0, 600))).isoformat() + "+00:00"  # 带时区
    else:
        ts = rng.choice([12345, ""])                         # 非字符串 / 空串：坏值
    strength = rng.choice([1.0, 1.0, 1.0, 0.9, 0.5, 0.3, 0.21, 0.1, 0.0,
                           float("nan"), "0.7", "junk", 1.7, -0.2, 1])
    score = rng.choice([rng.uniform(0, 10), rng.uniform(0, 10), 5.0, 5.0, 7.0,
                        float("nan"), "8.1", None, 12.0])
    direction = rng.choice(["bullish", "bearish", "neutral", "bullish", "bearish", "sideways"])
    ticker = rng.choice(tickers) if rng.random() < 0.8 else f"U{rng.randrange(10**6)}"
    return dict(agent_id=rng.choice(_AGENTS), ticker=ticker, discovery="d|x", source=rng.choice(["s", ""]),
                self_score=score, direction=direction, pheromone_strength=strength, ts=ts)


def _make(spec):
    e = PheromoneEntry(agent_id=spec["agent_id"], ticker=spec["ticker"], discovery=spec["discovery"],
                       source=spec["source"], self_score=spec["self_score"], direction=spec["direction"],
                       pheromone_strength=spec["pheromone_strength"])
    if spec["ts"] is not None:
        e.timestamp = spec["ts"]
    return e


def _run_scenario(seed: int, steps: int, clock):
    rng = random.Random(seed)
    ref, new = _RefBoard(), PheromoneBoard()
    tickers = [f"T{i}" for i in range(rng.choice([1, 2, 3, 5, 8]))]
    max_entries = rng.choice([3, 8, 25, 80, 10000])
    published = []                                           # [(ref_entry, new_entry)]
    raised = [0]

    def set_both(attr, val):
        setattr(ref, attr, val)
        setattr(new, attr, val)

    set_both("MAX_ENTRIES", max_entries)
    try:
        for step in range(steps):
            a = rng.random()
            if a < 0.70:
                spec = _rand_spec(rng, clock.current, tickers)
                pair = (_make(spec), _make(spec))
            elif a < 0.74 and published:
                pair = rng.choice(published)                 # 同一对象重复 publish
            elif a < 0.86:
                dt = rng.choice([0, 0, 1, 30, 120, 600, 1800, 5400, -120])
                clock.current = clock.current + timedelta(seconds=dt)
                pair = None
            elif a < 0.88:
                spec = _rand_spec(rng, clock.current, tickers)
                if rng.random() < 0.9:                       # 绕过校验入板：多数给 float（含 < MIN / NaN），少数给坏值
                    spec["pheromone_strength"] = rng.choice([1.0, 0.5, 0.1, float("nan"), 1.7])
                ra, na = _make(spec), _make(spec)
                ref._entries.extend([ra])                     # 外部直接改 _entries（测试里有人这么做）
                new._entries.extend([na])
                pair = None
            elif a < 0.89:
                ref.clear()
                new.clear()
                pair = None
            elif a < 0.91:
                set_both("MIN_STRENGTH", rng.choice([0.2, 0.2, 0.05, 0.35, 1.05]))
                pair = None
            elif a < 0.93:
                set_both("ENTRY_MAX_AGE_S", rng.choice([10800, 10800, 3600, 900]))
                pair = None
            elif a < 0.95:
                set_both("MAX_ENTRIES", rng.choice([3, 8, 25, 80, 10000]))
                pair = None
            elif a < 0.96:
                set_both("_ticker_scoped", rng.random() < 0.7)
                pair = None
            else:
                pair = None

            if pair is not None:
                prev_ref, prev_new = ref._entries, new._entries
                outcome = []
                for b, e in ((ref, pair[0]), (new, pair[1])):
                    try:
                        b.publish(e)
                        outcome.append(None)
                    except Exception as exc:              # 旧实现会抛的（如外部塞进非数字强度），新实现须同样抛
                        outcome.append((type(exc), str(exc)))
                assert outcome[0] == outcome[1], f"seed={seed} step={step}: 异常不同 {outcome}"
                raised[0] += outcome[0] is not None
                published.append(pair)
                assert (ref._entries is prev_ref) == (new._entries is prev_new), \
                    f"seed={seed} step={step}: _entries 列表对象是否换新，两条实现不一致"
            got, want = _board_state(new), _board_state(ref)
            assert got == want, f"seed={seed} step={step}: 整板状态与旧全量扫描不同"
    finally:
        ref.clear()
        new.clear()
    return raised[0]


class TestEquivalentToFullScan:

    def test_random_sequences_match_reference(self, clock, path_counter, caplog):
        caplog.set_level(logging.ERROR, logger="alpha_hive.pheromone_board")  # 校验告警刷屏只拖慢，不是被测对象
        raised = sum(_run_scenario(seed, 300, clock) for seed in range(80))
        assert raised > 0, "随机序列没覆盖到「旧实现会抛」的分支"
        # 防假绿：两条路径都必须被大量走到，否则「等价」只是因为快路径从没启用。
        # 种子固定 ⇒ 计数确定（实测快 4268 / 全量 13552）：小 MAX_ENTRIES 场景每次 publish 都截断、板满不重建索引，
        # 全量占多数是设计如此（生产稳态即全量）；快路径的大头在 MAX 10000 场景与下面的灌洪测试。
        assert path_counter["fast"] > 3000, path_counter
        assert path_counter["slow"] > 1000, path_counter

    def test_flood_shape_matches_reference(self, clock, path_counter):
        """`*_identical_under_any_max_entries` 的形状：大 MAX_ENTRIES、千条不同标的 + 同标的一轮。"""
        ref, new = _RefBoard(), PheromoneBoard()
        for b in (ref, new):
            b.MAX_ENTRIES = 600
        try:
            rng = random.Random(7)
            for i in range(640):
                tk = "TEST" if i % 40 == 0 else f"FLOOD{i}"
                d = rng.choice(["bullish", "bearish", "neutral"])
                s = rng.uniform(0, 10)
                for b in (ref, new):
                    b.publish(PheromoneEntry(agent_id=_AGENTS[i % 8], ticker=tk, discovery="x",
                                             source="s", self_score=s, direction=d))
                clock.current += timedelta(seconds=rng.choice([0, 1, 5]))
                assert _board_state(new) == _board_state(ref), f"第 {i} 次 publish 后不同"
            assert new.get_live_signals("TEST") and \
                [_state_of(e) for e in new.get_live_signals("TEST")] == \
                [_state_of(e) for e in ref.get_live_signals("TEST")]
            assert new.detect_resonance("TEST") == ref.detect_resonance("TEST")
        finally:
            ref.clear()
            new.clear()
        assert path_counter["fast"] >= 600, path_counter

    def test_expiry_still_happens_on_fast_path_boards(self, clock):
        """快路径必须照样淘汰过期的**别标的**条目（只靠同标的扫描会漏掉它）。"""
        b = PheromoneBoard()
        old = PheromoneEntry(agent_id="A", ticker="OLD", discovery="x", source="s",
                             self_score=5.0, direction="bullish")
        b.publish(old)
        b.publish(PheromoneEntry(agent_id="B", ticker="X1", discovery="x", source="s",
                                 self_score=5.0, direction="bullish"))
        clock.current += timedelta(seconds=PheromoneBoard.ENTRY_MAX_AGE_S)
        b.publish(PheromoneEntry(agent_id="C", ticker="X2", discovery="x", source="s",
                                 self_score=5.0, direction="bullish"))
        assert [e.ticker for e in b._entries] == ["X2"]
        b.clear()


# ── 不变式前提：生产代码不在板外改写已上板条目的这三个字段 ─────────────────────

_BOARD_OWNED = frozenset({"ticker", "timestamp", "pheromone_strength"})
_OWNER = "pheromone_board.py"


def _external_writes(source: str):
    """非 self/cls 目标上对 _BOARD_OWNED 属性的赋值（含增量赋值、解包赋值）。"""
    hits = []
    for node in ast.walk(_ast_cache.parse(source)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for t in targets:
            for a in ast.walk(t):
                if (isinstance(a, ast.Attribute) and isinstance(a.ctx, ast.Store)
                        and a.attr in _BOARD_OWNED
                        and not (isinstance(a.value, ast.Name) and a.value.id in ("self", "cls"))):
                    hits.append((node.lineno, ast.unparse(t)))
    return hits


def _scan_external_writes(root, files=None):
    """→ (违规 [(相对路径, 行号, 语句)], 扫到的文件数, 读 / 解析失败 ["路径: 原因"])。"""
    root = Path(root)
    if files is None:
        files, _ = own_python_files(root)
    bad, scanned, unreadable = [], 0, []
    for p in files:
        rel = p.relative_to(root)
        if rel.parts[0] == "tests" or rel.as_posix() == _OWNER:
            continue
        try:
            hits = _external_writes(_ast_cache.read_text(p))
        except (OSError, UnicodeDecodeError, SyntaxError) as e:
            unreadable.append(f"{rel.as_posix()}: {type(e).__name__}")
            continue
        scanned += 1
        bad += [(rel.as_posix(), ln, s) for ln, s in hits]
    return bad, scanned, unreadable


class TestNoExternalWritesToBoardOwnedFields:

    def test_production_code_has_none(self):
        """快路径跳过别标的条目的依据是「它们的 ticker / timestamp / 强度只有本板会改」。

        `self.ticker = ...`（别的类给自己的字段赋值）不算；`snap.ticker = ...` 一律算 ——
        宁可误报一个快照类也不放过一次对已上板条目的外部写。命中了先看是不是信息素条目：
        是 ⇒ 快路径的前提破了，改 `pheromone_board.py`；不是 ⇒ 换个字段名或在此登记理由。
        动态 `setattr(obj, name, ...)` 静态看不出写的是哪个字段，本守卫管不到（现存 3 处都在快照 / 时钟工具上）。
        """
        bad, scanned, unreadable = _scan_external_writes(_ROOT)
        # v0.45.532：成对断言。此前读不了 / 解析不了的文件 `continue` 掉、也不数扫了几个——
        # 枚举返回空表或违规文件恰好解析失败时本条恒绿（实测两种都绿）。
        assert scanned > 100, f"只扫到 {scanned} 个生产文件 —— 枚举坏了，「无外部写」等于没查"
        assert not unreadable, f"这些生产文件读不了 / 解析不了，本守卫看不见它们：{unreadable}"
        assert not bad, f"板外改写信息素条目自有字段（破坏 publish 快路径前提）: {bad}"

    def test_empty_enumeration_is_red(self, monkeypatch):
        monkeypatch.setattr(sys.modules[__name__], "own_python_files", lambda root: ([], "git"))
        with pytest.raises(AssertionError, match="只扫到 0 个"):
            self.test_production_code_has_none()

    def test_unparsable_violator_is_reported_not_skipped(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("e.pheromone_strength = 0\ndef (:\n", encoding="utf-8")
        g = tmp_path / "big5.py"
        g.write_bytes("e.ticker = '中'\n".encode("big5"))
        bad, scanned, unreadable = _scan_external_writes(tmp_path, files=[f, g])
        assert scanned == 0 and bad == []
        assert sorted(u.split(":")[0] for u in unreadable) == ["bad.py", "big5.py"], unreadable

    def test_guard_has_teeth(self):
        src = ("def f(board, e, es):\n"
               "    e.pheromone_strength = 0.0\n"
               "    es[0].ticker += 'X'\n"
               "    a, b.timestamp = 1, 'x'\n"
               "    self.ticker = 'ok'\n")
        assert [ln for ln, _ in _external_writes(src)] == [2, 3, 4]


def test_reference_is_still_the_old_algorithm():
    """参照实现若被顺手「同步」成新代码，等价测试就成了自己比自己。钉住它不调用任何快路径方法。"""
    import inspect
    src = inspect.getsource(_RefBoard.publish)
    for name in ("_survives_publish", "_ticker_group_if_rest_untouched", "_publish_scan_group",
                 "_index_append", "_rebuild_index"):
        assert name not in src
    assert "for e in self._entries:" in src
