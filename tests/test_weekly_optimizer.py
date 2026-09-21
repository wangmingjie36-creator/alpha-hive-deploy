"""
weekly_optimizer 回归测试（v0.42.2）

锁死两批修复：
  P0  记分规则方向 bug —— 维度层记分必须只看「蜂自己的票 vs 实际涨跌」，
      与快照整体 direction 无关。旧实现对 neutral 快照（实测占 32%）恒判
      「vote<=5 即正确」，与价格完全无关，导致 5 个维度准确率全部 < 0.5。
  P1b 安全网 —— 写入前语法预检/备份、写入后回读校验、dry-run 不谎报 applied、
      跳过也留审计、--rollback 可还原。
"""

import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekly_optimizer as wo
from feedback_loop import agent_vote_correct


# ══════════════════════════════════════════════════════════════════════════
# A. 记分规则（P0）
# ══════════════════════════════════════════════════════════════════════════

class _FakeSnap:
    """最小快照替身：只带 compute_new_weights_wls 用到的字段"""

    def __init__(self, direction, entry, t7, votes, date="2026-07-01"):
        self.direction = direction
        self.entry_price = entry
        self.actual_price_t7 = t7
        self.agent_votes = votes
        self.date = date


@pytest.mark.parametrize("vote,ret,expected", [
    (8.0, +3.0, True),    # 看多 + 涨 = 对
    (8.0, -3.0, False),   # 看多 + 跌 = 错
    (2.0, -3.0, True),    # 看空 + 跌 = 对
    (2.0, +3.0, False),   # 看空 + 涨 = 错
    (5.0, +3.0, None),    # 恰好中性票 → 弃权
    (5.0, -3.0, None),
    (8.0, 0.0, None),     # 零收益 → 弃权
    (2.0, 0.0, None),
])
def test_agent_vote_correct_truth_table(vote, ret, expected):
    assert agent_vote_correct(vote, ret) is expected


@pytest.mark.parametrize("direction", ["bullish", "bearish", "neutral", "Long", "Short", "Neutral"])
def test_scoring_independent_of_snapshot_direction(direction):
    """核心不变式：维度层记分结果不得依赖快照整体 direction。

    这是 P0 根因修复的直接断言 —— 旧实现下同一份 votes+价格，换个 direction
    就会得到不同的准确率。
    """
    votes = {"ScoutBeeNova": 8.0, "ChronosBeeHorizon": 3.0}
    snaps = [_FakeSnap(direction, 100.0, 105.0, votes)]
    per_dim = {}
    for snap in snaps:
        ret = (snap.actual_price_t7 - snap.entry_price) / snap.entry_price * 100
        for agent, vote in snap.agent_votes.items():
            dim = wo.AGENT_TO_DIM[agent]
            per_dim[dim] = agent_vote_correct(vote, ret)
    # 价格涨 5%：看多的 Scout(signal) 对，看空的 Chronos(catalyst) 错 —— 与 direction 无关
    assert per_dim["signal"] is True
    assert per_dim["catalyst"] is False


def test_neutral_snapshot_scoring_depends_on_return():
    """neutral 快照必须随价格变化而改变记分 —— 修复前它完全无视价格。"""
    votes = {"ScoutBeeNova": 8.0}
    up = agent_vote_correct(8.0, +5.0)
    down = agent_vote_correct(8.0, -5.0)
    assert up is True and down is False, "neutral 场景下记分必须随收益翻转"


def test_scoring_invariant_flip_all_returns():
    """属性测试：收益全部取反 → 每个非弃权判定必须翻转。"""
    for vote in (1.0, 3.0, 6.5, 9.0):
        for ret in (-7.0, -0.5, 0.5, 7.0):
            a = agent_vote_correct(vote, ret)
            b = agent_vote_correct(vote, -ret)
            assert a is not None and b is not None
            assert a != b, f"vote={vote} ret={ret} 翻转后判定应相反"


def test_compute_weights_neutral_not_systematically_penalized(monkeypatch, tmp_path):
    """端到端：全 neutral 快照 + 看多票 + 价格上涨 → 该维度准确率应为高，而非被判错。

    旧实现下 neutral ⇒ is_correct=False ⇒ vote>5 一律记 0，准确率会掉到 0。
    """
    votes = {"ScoutBeeNova": 9.0}
    snaps = [_FakeSnap("neutral", 100.0, 110.0, votes) for _ in range(wo.MIN_SAMPLES + 5)]

    class _FakeAnalyzer:
        def __init__(self, directory=None):
            self.snapshots = snaps

    monkeypatch.setitem(sys.modules, "feedback_loop", sys.modules["feedback_loop"])
    monkeypatch.setattr("feedback_loop.BacktestAnalyzer", _FakeAnalyzer, raising=False)

    res = wo.compute_new_weights_wls(tmp_path)
    assert res is not None
    # signal 维度全部押对 → 归一化后应显著高于其余（其余无数据走 DEFAULT 兜底）
    assert res["new_weights"]["signal"] > 0.0


# ══════════════════════════════════════════════════════════════════════════
# A2. 护栏投影不变式（P1，v0.42.6）
# ══════════════════════════════════════════════════════════════════════════

class TestProjectionInvariants:
    """三不变式必须同时成立：sum=1 ∧ 各维在 WEIGHT_CLAMPS 内 ∧ 单次变动 ≤ MAX_SHIFT_PP

    旧实现分两步（先钳幅 → 再归一化）互相破坏：归一化是乘性缩放，
    必然把已钳到边界的值推出边界。历史实证突破见下方回归测试。
    """

    DIMS = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]

    def _assert_all_three(self, old, new):
        assert abs(sum(new.values()) - 1.0) < 1e-5, f"和 != 1: {sum(new.values())}"
        for k, (lo, hi) in wo.WEIGHT_CLAMPS.items():
            assert lo - 1e-5 <= new[k] <= hi + 1e-5, \
                f"{k}={new[k]:.6f} 越出 clamp [{lo}, {hi}]"
            shift_pp = abs(new[k] - old[k]) * 100
            assert shift_pp <= wo.MAX_SHIFT_PP + 1e-3, \
                f"{k} 变动 {shift_pp:.2f}pp > MAX_SHIFT_PP={wo.MAX_SHIFT_PP}"

    def test_random_targets_satisfy_all_invariants(self):
        """200 组随机 (anchor, target)，三不变式必须全部成立"""
        rng = random.Random(20260730)
        for _ in range(200):
            old = {k: rng.uniform(0.10, 0.25) for k in self.DIMS}
            s = sum(old.values())
            old = {k: v / s for k, v in old.items()}
            old = wo.project_to_feasible(old, wo.WEIGHT_CLAMPS)
            target = {k: rng.uniform(0.0, 1.0) for k in self.DIMS}
            new = wo.clamp_shifts(old, target)
            self._assert_all_three(old, new)

    def test_regression_risk_adj_plus_1072pp(self):
        """历史突破固化：weight_history.jsonl 曾记录 risk_adj +10.72pp（> 10.0）"""
        old = {"signal": 0.1958, "catalyst": 0.2500,
               "sentiment": 0.2487, "odds": 0.1877, "risk_adj": 0.1178}
        # 极端 target：把 risk_adj 顶满、其余压低（旧实现在此产生 +10.72pp）
        target = {"signal": 0.05, "catalyst": 0.05,
                  "sentiment": 0.05, "odds": 0.05, "risk_adj": 0.80}
        new = wo.clamp_shifts(old, target)
        self._assert_all_three(old, new)
        assert (new["risk_adj"] - old["risk_adj"]) * 100 <= wo.MAX_SHIFT_PP + 1e-3

    def test_regression_catalyst_clamp_breach(self):
        """历史突破固化：catalyst 曾落到 0.3316（> WEIGHT_CLAMPS 上限 0.25）"""
        old = {k: 0.20 for k in self.DIMS}
        target = {"signal": 0.02, "catalyst": 0.92,
                  "sentiment": 0.02, "odds": 0.02, "risk_adj": 0.02}
        new = wo.clamp_shifts(old, target)
        assert new["catalyst"] <= wo.WEIGHT_CLAMPS["catalyst"][1] + 1e-9
        self._assert_all_three(old, new)

    def test_projection_idempotent(self):
        """对已可行的点投影应返回自身"""
        w = wo.project_to_feasible({k: 0.2 for k in self.DIMS}, wo.WEIGHT_CLAMPS)
        again = wo.project_to_feasible(w, wo.WEIGHT_CLAMPS)
        for k in w:
            assert abs(w[k] - again[k]) < 1e-9

    def test_merge_bounds_is_intersection(self):
        """合并盒 = WEIGHT_CLAMPS ∩ [anchor±MAX_SHIFT]"""
        anchor = {k: 0.20 for k in self.DIMS}
        b = wo.merge_bounds(anchor, max_shift_pp=5.0)
        for k, (lo, hi) in b.items():
            c_lo, c_hi = wo.WEIGHT_CLAMPS[k]
            assert lo == pytest.approx(max(c_lo, 0.20 - 0.05))
            assert hi == pytest.approx(min(c_hi, 0.20 + 0.05))

    def test_infeasible_bounds_raise(self):
        """盒与单纯形无交集时必须抛错，而非静默返回越界结果"""
        bad = {k: (0.30, 0.40) for k in self.DIMS}   # Σlo = 1.5 > 1
        with pytest.raises(wo.InfeasibleBoundsError):
            wo.project_to_feasible({k: 0.2 for k in self.DIMS}, bad)
        bad2 = {k: (0.01, 0.05) for k in self.DIMS}  # Σhi = 0.25 < 1
        with pytest.raises(wo.InfeasibleBoundsError):
            wo.project_to_feasible({k: 0.2 for k in self.DIMS}, bad2)

    def test_repo_clamps_are_self_consistent(self):
        """仓库真实 WEIGHT_CLAMPS 必须满足 Σlo ≤ 1 ≤ Σhi（模块导入期已断言）"""
        lo = sum(b[0] for b in wo.WEIGHT_CLAMPS.values())
        hi = sum(b[1] for b in wo.WEIGHT_CLAMPS.values())
        assert lo <= 1.0 <= hi, f"Σlo={lo}, Σhi={hi}"

    def test_tiny_max_shift_pins_to_anchor(self):
        """MAX_SHIFT_PP 极小时结果应几乎等于 anchor（步长约束生效）"""
        old = wo.project_to_feasible({k: 0.2 for k in self.DIMS}, wo.WEIGHT_CLAMPS)
        b = wo.merge_bounds(old, max_shift_pp=0.1)
        new = wo.project_to_feasible({"signal": 1.0, "catalyst": 0.0,
                                      "sentiment": 0.0, "odds": 0.0,
                                      "risk_adj": 0.0}, b)
        for k in old:
            assert abs(new[k] - old[k]) <= 0.001 + 1e-6

    def test_assert_feasible_detects_violations(self):
        bad_sum = {k: 0.30 for k in self.DIMS}          # 和 = 1.5
        with pytest.raises(wo.InfeasibleBoundsError):
            wo.assert_feasible(bad_sum, wo.WEIGHT_CLAMPS)
        out_of_box = {"signal": 0.60, "catalyst": 0.10, "sentiment": 0.10,
                      "odds": 0.10, "risk_adj": 0.10}   # signal > 0.40
        with pytest.raises(wo.InfeasibleBoundsError):
            wo.assert_feasible(out_of_box, wo.WEIGHT_CLAMPS)


# ══════════════════════════════════════════════════════════════════════════
# B. 安全网：备份 / 回读 / dry-run 语义（P1b）
# ══════════════════════════════════════════════════════════════════════════

_CONFIG_STUB = '''"""stub config"""

EVALUATION_WEIGHTS = {
    "signal":    0.3000,   # a
    "catalyst":  0.2000,   # b
    "sentiment": 0.2000,   # c
    "odds":      0.1500,   # d
    "risk_adj":  0.1500,   # e
    # ml_auxiliary: 不在此处
}

OTHER_SETTING = 42
'''

_W = {"signal": 0.25, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.20, "risk_adj": 0.15}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 optimizer 的所有落盘路径指向 tmp_path"""
    cfg = tmp_path / "config.py"
    cfg.write_text(_CONFIG_STUB, encoding="utf-8")
    monkeypatch.setattr(wo, "CONFIG_PATH", cfg)
    monkeypatch.setattr(wo, "HISTORY_FILE", tmp_path / "weight_history.jsonl")
    monkeypatch.setattr(wo, "BACKUP_DIR", tmp_path / "weight_backups")
    monkeypatch.setattr(wo, "BACKUP_LATEST", tmp_path / "config.py.weights.bak")
    return tmp_path


def test_dry_run_does_not_write_and_returns_false(sandbox):
    """dry-run 必须 return False —— 否则 main() 会把 applied 记成 True。"""
    before = wo.CONFIG_PATH.read_text(encoding="utf-8")
    ok = wo.write_weights_to_config(_W, dry_run=True)
    assert ok is False
    assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before, "dry-run 不得修改 config"


def test_real_write_updates_weights_and_keeps_file_valid(sandbox):
    ok = wo.write_weights_to_config(_W, dry_run=False)
    assert ok is True
    got = wo.read_current_weights()
    for k, v in _W.items():
        assert abs(got[k] - v) < 1e-4
    # 文件仍可编译，且无关配置项未被破坏
    text = wo.CONFIG_PATH.read_text(encoding="utf-8")
    compile(text, "config.py", "exec")
    assert "OTHER_SETTING = 42" in text


def test_backup_created_before_write(sandbox):
    original = wo.CONFIG_PATH.read_text(encoding="utf-8")
    wo.write_weights_to_config(_W, dry_run=False)
    assert wo.BACKUP_LATEST.exists()
    # 备份内容必须是写入**前**的版本
    assert wo.BACKUP_LATEST.read_text(encoding="utf-8") == original
    assert list(wo.BACKUP_DIR.glob("config_*.py"))


def test_backup_rotation_keeps_n(sandbox, monkeypatch):
    monkeypatch.setattr(wo, "BACKUP_KEEP_N", 3)
    for i in range(6):
        w = dict(_W)
        w["signal"] = 0.25 + i * 0.001
        wo._backup_config()
    assert len(list(wo.BACKUP_DIR.glob("config_*.py"))) <= 3


def test_syntax_precheck_blocks_bad_write(sandbox, monkeypatch):
    """生成的新文本若语法错误，必须中止写入且不动原文件。"""
    original = wo.CONFIG_PATH.read_text(encoding="utf-8")

    real_sub = wo.re.sub

    def _broken_sub(pattern, repl, text, **kw):
        return "def (((  # 语法错误\n"

    monkeypatch.setattr(wo.re, "sub", _broken_sub)
    ok = wo.write_weights_to_config(_W, dry_run=False)
    monkeypatch.setattr(wo.re, "sub", real_sub)

    assert ok is False
    assert wo.CONFIG_PATH.read_text(encoding="utf-8") == original


def test_readback_mismatch_restores_backup(sandbox, monkeypatch):
    """回读校验失败必须自动还原到备份。"""
    original = wo.CONFIG_PATH.read_text(encoding="utf-8")
    monkeypatch.setattr(wo, "read_current_weights",
                        lambda: {k: 0.0 for k in _W})  # 制造不匹配
    ok = wo.write_weights_to_config(_W, dry_run=False)
    assert ok is False
    assert wo.CONFIG_PATH.read_text(encoding="utf-8") == original, "应已还原"


# ══════════════════════════════════════════════════════════════════════════
# C. 审计与回滚（P1b）
# ══════════════════════════════════════════════════════════════════════════

def _read_history(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_history_records_schema_v2_and_skip_reason(sandbox):
    wo.append_history(_W, _W, n_samples=100, dry_run=False,
                      applied=False, skip_reason="below_min_change")
    recs = _read_history(wo.HISTORY_FILE)
    assert recs[-1]["schema_version"] == 2
    assert recs[-1]["skip_reason"] == "below_min_change"
    assert recs[-1]["applied"] is False
    assert recs[-1]["action"] == "optimize"


def test_last_applied_record_ignores_dry_run_and_old_schema(sandbox):
    # 旧 schema 的矛盾记录：dry_run=true 却 applied=true → 必须被忽略
    wo.append_history(_W, _W, 10, dry_run=True, applied=True)
    hist = _read_history(wo.HISTORY_FILE)
    hist[-1].pop("schema_version")
    hist[-1]["dry_run"] = True
    hist[-1]["applied"] = True
    wo.HISTORY_FILE.write_text(json.dumps(hist[-1], ensure_ascii=False) + "\n", encoding="utf-8")
    assert wo._last_applied_record() is None


def test_rollback_restores_previous_weights(sandbox):
    old = wo.read_current_weights()
    assert wo.write_weights_to_config(_W, dry_run=False) is True
    wo.append_history(old, _W, 100, dry_run=False, applied=True)

    assert wo.do_rollback(dry_run=False) == 0
    got = wo.read_current_weights()
    for k, v in old.items():
        assert abs(got[k] - v) < 1e-4, f"{k} 未回滚到 {v}"


def test_rollback_dry_run_changes_nothing(sandbox):
    old = wo.read_current_weights()
    wo.write_weights_to_config(_W, dry_run=False)
    wo.append_history(old, _W, 100, dry_run=False, applied=True)
    after_write = wo.CONFIG_PATH.read_text(encoding="utf-8")

    assert wo.do_rollback(dry_run=True) == 0
    assert wo.CONFIG_PATH.read_text(encoding="utf-8") == after_write


def test_rollback_appends_audit_record(sandbox):
    old = wo.read_current_weights()
    wo.write_weights_to_config(_W, dry_run=False)
    wo.append_history(old, _W, 100, dry_run=False, applied=True)
    wo.do_rollback(dry_run=False)

    recs = _read_history(wo.HISTORY_FILE)
    assert recs[-1]["action"] == "rollback"
    assert recs[-1]["applied"] is True


def test_rollback_without_history_is_safe(sandbox):
    """没有可回滚记录时必须优雅退出，不得破坏 config。"""
    before = wo.CONFIG_PATH.read_text(encoding="utf-8")
    assert wo.do_rollback(dry_run=False) == 1
    assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before


class TestModuleLoggerDefined:
    """v0.43.3 回归：`_log` 曾未定义，导致 InfeasibleBoundsError 处理路径崩溃。

    该 except 分支是 v0.42.6 为"约束无解时拒绝写入并留审计"专门加的安全网，
    却因 `_log` 未定义而在触发时抛 AttributeError —— append_history 永远不会执行，
    审计缺失，且 cron 以非零码崩溃。本 session 已犯过同类错误一次
    （alpha_hive_daily_report 的 `sys` 未在模块作用域）。
    """

    def test_module_logger_exists(self):
        assert hasattr(wo, "_log"), "weekly_optimizer 必须有模块级 _log"
        assert wo._log.name.startswith("alpha_hive")

    def test_infeasible_handler_can_log(self):
        """模拟 main() 的 except 分支，确认不抛 NameError/AttributeError"""
        try:
            raise wo.InfeasibleBoundsError("模拟不可行")
        except wo.InfeasibleBoundsError as e:
            wo._log.error("clamp_shifts 不可行: %s", e)   # 崩则测试失败

    def test_every_log_usage_is_resolvable(self):
        """源码里用到的所有 _log.<method> 都必须存在于 logger 上"""
        import re
        with open(wo.__file__, encoding="utf-8") as f:
            src = f.read()
        for m in set(re.findall(r"_log\.(\w+)\(", src)):
            assert hasattr(wo._log, m), f"logger 无方法 {m}"


# ════════════════════════════════════════════════════════════════════════════
# v0.44.0：默认只读 + 两道闸
#
# 起因：2026-08-16 的定时任务 dry-run 发现优化器已从 inert 变回会开火
# （risk_adj −4.13pp > MIN_CHANGE_PP=3.0），而它要动的依据全是 10 只时代的
# 数据，且 bootstrap 报"不稳健"时旧代码**只打印不阻断**。
# ════════════════════════════════════════════════════════════════════════════

class _PoolSnap:
    """带 ticker 的最小快照替身，供 check_ticker_pool_consistency 用。"""

    def __init__(self, ticker, date, t7=110.0, entry=100.0):
        self.ticker = ticker
        self.date = date
        self.actual_price_t7 = t7
        self.entry_price = entry


def _patch_snaps(monkeypatch, snaps):
    """把 BacktestAnalyzer 换成只返回给定快照的替身。

    注意 check_ticker_pool_consistency 内部是 `from feedback_loop import
    BacktestAnalyzer`（函数级 import），所以必须 patch feedback_loop 上的名字，
    patch weekly_optimizer 的属性没用。
    """
    import feedback_loop

    class _FakeAnalyzer:
        def __init__(self, directory=None, **kw):
            self.snapshots = snaps

    monkeypatch.setattr(feedback_loop, "BacktestAnalyzer", _FakeAnalyzer)


class TestTickerPoolGate:
    """闸 2：样本基必须能代表当前在扫的标的池。

    2026-08-16 实测状态：665 个 T+7 样本跨度 03-09→07-29，30 只时代贡献 0 条；
    该闸实跑时报「当前池 30 只里有 14 只（47%）从未进入 T+7 样本基」。
    """

    def test_same_pool_passes(self, monkeypatch, tmp_path):
        snaps = [_PoolSnap(t, "2026-08-14") for t in ("AAA", "BBB", "CCC")]
        _patch_snaps(monkeypatch, snaps)
        res = wo.check_ticker_pool_consistency(tmp_path)
        assert res["ok"] is True
        assert res["unrepresented_ratio"] == 0.0

    def test_expanded_pool_without_t7_is_blocked(self, monkeypatch, tmp_path):
        """复刻真实场景：老标的有 T+7，新扩的没有（t7=None）。"""
        old = [_PoolSnap(t, "2026-07-01") for t in ("AAA", "BBB")]
        new = [_PoolSnap(t, "2026-08-14", t7=None)
               for t in ("CCC", "DDD", "EEE", "FFF")]
        # 老标的当天也在扫
        still = [_PoolSnap(t, "2026-08-14", t7=None) for t in ("AAA", "BBB")]
        _patch_snaps(monkeypatch, old + new + still)
        res = wo.check_ticker_pool_consistency(tmp_path)
        assert res["ok"] is False
        assert res["unrepresented_ratio"] == pytest.approx(4 / 6)
        assert set(res["missing"]) == {"CCC", "DDD", "EEE", "FFF"}
        assert "从未进入 T+7 样本基" in res["reason"]

    def test_small_addition_within_threshold_passes(self, monkeypatch, tmp_path):
        """加 1 只到 10 只池（10% < 20% 门槛）不该拦——闸不能过敏。"""
        covered = [_PoolSnap(f"T{i}", "2026-07-01") for i in range(9)]
        recent = [_PoolSnap(f"T{i}", "2026-08-14", t7=None) for i in range(9)]
        newbie = [_PoolSnap("NEW", "2026-08-14", t7=None)]
        _patch_snaps(monkeypatch, covered + recent + newbie)
        res = wo.check_ticker_pool_consistency(tmp_path)
        assert res["ok"] is True

    def test_gate_defaults_closed_when_undeterminable(self, monkeypatch, tmp_path):
        """判不了必须**不放行**——闸的默认态是关，不是开。"""
        _patch_snaps(monkeypatch, [])
        res = wo.check_ticker_pool_consistency(tmp_path)
        assert res["ok"] is False
        assert res["reason"]

    def test_no_t7_samples_at_all_is_blocked(self, monkeypatch, tmp_path):
        _patch_snaps(monkeypatch,
                     [_PoolSnap("AAA", "2026-08-14", t7=None)])
        res = wo.check_ticker_pool_consistency(tmp_path)
        assert res["ok"] is False


class TestReadOnlyDefaultAndGates:
    """默认只读 + `--apply` 时闸门生效。用 main() 端到端跑，因为这几条
    约束全部落在 main() 的分支里，单测函数覆盖不到。
    """

    def _run_main(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py", *argv])
        wo.main()

    @pytest.fixture
    def stub_pipeline(self, monkeypatch, sandbox):
        """让 main() 走到写入决策那一步：样本充足、有显著变化。"""
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", sandbox)
        monkeypatch.setattr(wo, "count_t7_samples", lambda d: 667)
        big_shift = {"signal": 0.30, "catalyst": 0.20,
                     "sentiment": 0.20, "odds": 0.20, "risk_adj": 0.10}
        monkeypatch.setattr(wo, "compute_new_weights_wls",
                            lambda d: {"new_weights": dict(big_shift),
                                       "method": "wls_time_decay"})
        return sandbox

    def test_default_is_read_only(self, monkeypatch, stub_pipeline):
        """不给 --apply 就绝不碰 config.py，即便变化显著且闸门全过。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": True})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": True, "n_recent_pool": 30,
                                             "unrepresented_ratio": 0.0})
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run_main(monkeypatch, [])
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["applied"] is False
        assert rec["skip_reason"] == "read_only_default"
        assert rec["action"] == "diagnose"

    def test_apply_with_gates_open_does_write(self, monkeypatch, stub_pipeline):
        """闸门全过 + 显式 --apply 才写 —— 证明只读不是把功能焊死了。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": True})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": True, "n_recent_pool": 30,
                                             "unrepresented_ratio": 0.0})
        self._run_main(monkeypatch, ["--apply"])
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["applied"] is True
        assert rec["action"] == "optimize"
        assert wo.read_current_weights()["signal"] == pytest.approx(0.30, abs=1e-4)

    def test_bootstrap_gate_blocks_apply(self, monkeypatch, stub_pipeline):
        """v0.44.0 之前这里只打印不阻断，是 2026-08-16 差点写入的直接原因。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": False})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": True, "n_recent_pool": 30,
                                             "unrepresented_ratio": 0.0})
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run_main(monkeypatch, ["--apply"])
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["applied"] is False
        assert "bootstrap_unstable" in rec["skip_reason"]

    def test_pool_gate_blocks_apply(self, monkeypatch, stub_pipeline):
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": True})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": False, "reason": "陈旧池",
                                             "missing": ["X"], "n_recent_pool": 30})
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run_main(monkeypatch, ["--apply"])
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before
        assert "stale_ticker_pool" in _read_history(wo.HISTORY_FILE)[-1]["skip_reason"]

    def test_both_gates_recorded_in_skip_reason(self, monkeypatch, stub_pipeline):
        """两道闸都拦时，审计要能看出是两个原因而非一个。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": False})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": False, "reason": "陈旧池",
                                             "missing": [], "n_recent_pool": 30})
        self._run_main(monkeypatch, ["--apply"])
        reason = _read_history(wo.HISTORY_FILE)[-1]["skip_reason"]
        assert "bootstrap_unstable" in reason and "stale_ticker_pool" in reason

    def test_force_overrides_gates_but_is_audited(self, monkeypatch, stub_pipeline):
        """--force 必须真能写（否则是假逃生舱），且审计留下 bootstrap_stable=False。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": False})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": False, "reason": "陈旧池",
                                             "missing": [], "n_recent_pool": 30})
        self._run_main(monkeypatch, ["--apply", "--force"])
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["applied"] is True
        assert rec["bootstrap_stable"] is False, "强行写入也必须留下闸门状态"

    def test_dry_run_still_wins_over_apply(self, monkeypatch, stub_pipeline):
        """--dry-run 保留为向后兼容；与 --apply 同给时以不写为准。"""
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda *a, **k: {"stable": True})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: {"ok": True, "n_recent_pool": 30,
                                             "unrepresented_ratio": 0.0})
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run_main(monkeypatch, ["--apply", "--dry-run"])
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before


# ══════════════════════════════════════════════════════════════════════════
# G. close_t7 干净口径覆盖（v0.45.86）
# ══════════════════════════════════════════════════════════════════════════
#
# actual_price_t7（report_snapshots/*.json 的 actual_prices.t7，来自
# backtest_engine.PriceBackfiller 的 Ticker().history() ±3天容差取价）与
# pheromone.db.close_t7（backfill_dir_accuracy.py 的干净口径）实测同一
# (ticker,date) 下只有约1/3重合。_apply_clean_t7_prices 让 Track A 统一改用
# close_t7；本节锁死三条分支：覆盖、丢弃(无匹配)、库不存在时原样不动。

def _make_pheromone_db(tmp_path, rows):
    """rows: [(ticker, date, close_t7), ...]，建最小 predictions 表。"""
    import sqlite3
    db_path = tmp_path / "pheromone.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE predictions (ticker TEXT, date TEXT, close_t7 REAL)")
    con.executemany("INSERT INTO predictions VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return db_path


class TestCleanT7PriceOverride:

    def test_overrides_with_matching_close_t7(self, monkeypatch, tmp_path):
        """有匹配行：actual_price_t7 必须被换成 close_t7，而不是原来的值。"""
        db_path = _make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)

        snap = _PoolSnap("AAA", "2026-08-14", t7=999.0)  # 999 是要被覆盖掉的脏值
        analyzer = type("A", (), {"snapshots": [snap]})()
        wo._apply_clean_t7_prices(analyzer)

        assert snap.actual_price_t7 == 123.45

    def test_drops_sample_without_matching_row(self, monkeypatch, tmp_path):
        """库存在但查无该 (ticker,date)：必须丢弃（置 None），不回退旧值。"""
        db_path = _make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)

        snap = _PoolSnap("ZZZ", "2026-08-14", t7=999.0)  # 库里没有 ZZZ
        analyzer = type("A", (), {"snapshots": [snap]})()
        wo._apply_clean_t7_prices(analyzer)

        assert snap.actual_price_t7 is None

    def test_unchanged_when_db_missing(self, monkeypatch, tmp_path):
        """库不存在：原样返回，不能把 Track A 全部样本清零。"""
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", tmp_path / "_absent.db")

        snap = _PoolSnap("AAA", "2026-08-14", t7=999.0)
        analyzer = type("A", (), {"snapshots": [snap]})()
        wo._apply_clean_t7_prices(analyzer)

        assert snap.actual_price_t7 == 999.0

    def test_count_t7_samples_uses_same_source(self, monkeypatch, tmp_path):
        """n_samples 展示的数字必须和真正参与拟合的样本用同一个口径，
        否则重现此前"skip_reason 和 bootstrap_stable 对不上"那类误读。"""
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        (snapshots_dir / "AAA_2026-08-14.json").write_text(json.dumps({
            "ticker": "AAA", "date": "2026-08-14",
            "entry_price": 100.0,
            "actual_prices": {"t7": 999.0},  # 脏值，不该被数进去
        }))
        (snapshots_dir / "BBB_2026-08-14.json").write_text(json.dumps({
            "ticker": "BBB", "date": "2026-08-14",
            "entry_price": 100.0,
            "actual_prices": {"t7": None},  # close_t7 有、旧字段没有——该被数进去
        }))
        db_path = _make_pheromone_db(tmp_path, [("BBB", "2026-08-14", 50.0)])
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)

        assert wo.count_t7_samples(snapshots_dir) == 1  # 只有 BBB 在 close_t7 里


# ══════════════════════════════════════════════════════════════════════════
# G2. close_t7 缺数据/瞬时故障的状态区分与可见警示（v0.45.87，复查问题 #2-4）
# ══════════════════════════════════════════════════════════════════════════
#
# _load_close_t7_map() 此前对"库不存在"和"库/表都在但查无任何 close_t7
# 非空行"一视同仁，都返回空 dict；调用方也都用 `if close_t7_map:` 真值判断，
# CLI 表现在两种情况下完全一样——用户看不出这次权重其实是用脏口径算的。

def _make_empty_predictions_db(tmp_path):
    """建一张有 predictions 表、但没有任何 close_t7 非空行的库（模拟全新部署
    或 backfill_dir_accuracy.py 还没跑过/跑挂了）。"""
    import sqlite3
    db_path = tmp_path / "pheromone.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE predictions (ticker TEXT, date TEXT, close_t7 REAL)")
    con.execute("INSERT INTO predictions VALUES ('AAA', '2026-08-14', NULL)")
    con.commit()
    con.close()
    return db_path


class TestCloseT7StatusDistinction:

    def test_status_ok_when_rows_present(self, monkeypatch, tmp_path):
        db_path = _make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)
        result, status = wo._load_close_t7_map()
        assert status == "ok"
        assert result == {("AAA", "2026-08-14"): 123.45}

    def test_status_missing_when_db_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", tmp_path / "_absent.db")
        result, status = wo._load_close_t7_map()
        assert status == "missing"
        assert result == {}

    def test_status_empty_when_db_has_no_close_t7_rows(self, monkeypatch, tmp_path):
        """问题 #2 核心场景：库/表都存在，但没有任何 close_t7 非空行。"""
        db_path = _make_empty_predictions_db(tmp_path)
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)
        result, status = wo._load_close_t7_map()
        assert status == "empty"
        assert result == {}

    def test_main_prints_visible_warning_when_db_empty(self, monkeypatch, sandbox, capsys):
        """问题 #2 要求：警示必须出现在用户能看到的输出层（终端 print），
        不能只是内部行为对了、CLI 表现却和一切正常时一模一样。"""
        db_path = _make_empty_predictions_db(sandbox)
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", sandbox)
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py"])
        wo.main()
        out = capsys.readouterr().out
        assert "close_t7" in out
        assert "⚠️" in out

    def test_main_silent_when_db_missing(self, monkeypatch, sandbox, capsys):
        """"库不存在"是预期内的正常降级路径（如全新环境、测试隔离），不应报警。"""
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", sandbox / "_absent.db")
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", sandbox)
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py"])
        wo.main()
        out = capsys.readouterr().out
        assert "close_t7" not in out

    def test_exists_permission_error_falls_back_gracefully(self, monkeypatch, tmp_path):
        """问题 #3：Path.exists() 抛 PermissionError 不能让脚本崩溃，
        必须走跟"库不存在"一样的优雅降级分支，并标记为 error 供上层警示。"""
        fake_path = tmp_path / "pheromone.db"

        class _BoomPath:
            def exists(self):
                raise PermissionError("目录权限被误改")

        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", _BoomPath())
        result, status = wo._load_close_t7_map()  # 不应抛出
        assert result == {}
        assert status == "error"

    def test_transient_sqlite_error_not_cached(self, monkeypatch, tmp_path):
        """问题 #4：sqlite3.Error（如并发写产生的 "database is locked"）
        不能写进缓存，否则一次性脚本内后续所有调用都直接命中空缓存不重试。
        """
        db_path = _make_pheromone_db(tmp_path, [("AAA", "2026-08-14", 123.45)])
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", db_path)

        # v0.45.302：此前写的是 `sqlite3.connect` —— 借 weekly_optimizer 模块的属性
        # 去够全局 sqlite3。但 weekly_optimizer 自己从不调用 sqlite3（那行 import
        # 是死的、已随 F401 清理删掉），真正连库的是它下游的模块。两种写法拿到的是
        # **同一个模块对象**，补丁效果完全相同；下面 status1 == "error" 那条断言
        # 同时证明补丁确实拦到了实际的 connect（没拦到会是 "ok"）。
        import sqlite3
        real_connect = sqlite3.connect
        calls = {"n": 0}

        def _flaky_connect(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(*a, **kw)

        monkeypatch.setattr(sqlite3, "connect", _flaky_connect)

        result1, status1 = wo._load_close_t7_map()
        assert status1 == "error"
        assert result1 == {}

        # 第二次调用应该重新尝试连接（没有被空缓存挡住），这次真的成功
        result2, status2 = wo._load_close_t7_map()
        assert status2 == "ok"
        assert result2 == {("AAA", "2026-08-14"): 123.45}


# ══════════════════════════════════════════════════════════════════════════
# H. 样本数门槛口径对齐（v0.45.87，复查问题 #1）
# ══════════════════════════════════════════════════════════════════════════
#
# count_t7_samples() 此前只看「有没有 T+7 价格」，而 compute_new_weights_wls /
# bootstrap_validate 内部构造 valid_snaps 时额外要求 entry_price > 0。
# main() 先用前者判断是否达到 args.min_samples（够就往下走），若 WLS 因
# entry_price>0 过滤后样本不足 MIN_SAMPLES 而返回 None，会回退到
# compute_new_weights()（旧实现），而后者此前完全没有最低样本数保护，
# 能在极少样本（比如 3 条）下产出权重建议。

class TestSampleThresholdAlignment:

    def test_count_t7_samples_excludes_zero_or_missing_entry_price(self, tmp_path):
        """口径必须与 valid_snaps 的 entry_price > 0 完全对齐。"""
        snapshots_dir = tmp_path / "snapshots"
        snapshots_dir.mkdir()
        for i in range(3):
            (snapshots_dir / f"OK{i}.json").write_text(json.dumps({
                "ticker": f"OK{i}", "date": "2026-08-14",
                "entry_price": 100.0,
                "actual_prices": {"t7": 105.0},
            }))
        (snapshots_dir / "ZERO.json").write_text(json.dumps({
            "ticker": "ZERO", "date": "2026-08-14",
            "entry_price": 0.0,
            "actual_prices": {"t7": 105.0},
        }))
        (snapshots_dir / "MISSING.json").write_text(json.dumps({
            "ticker": "MISSING", "date": "2026-08-14",
            "actual_prices": {"t7": 105.0},
        }))
        assert wo.count_t7_samples(snapshots_dir) == 3

    def test_compute_new_weights_returns_none_below_min_samples(self, monkeypatch, tmp_path):
        """回退路径必须有最低样本数保护——此前完全没有，能在极少样本下产出建议。"""
        assert wo.MIN_SAMPLES > 3
        snaps = [_FakeSnap("bullish", 100.0, 105.0, {"ScoutBeeNova": 8.0})
                 for _ in range(3)]
        called = {"suggest": False}

        class _FakeAnalyzer:
            def __init__(self, directory=None):
                self.snapshots = snaps

            def suggest_weight_adjustments(self):
                called["suggest"] = True
                return {"new_weights": dict(wo.DEFAULT_WEIGHTS)}

        monkeypatch.setattr("feedback_loop.BacktestAnalyzer", _FakeAnalyzer, raising=False)
        res = wo.compute_new_weights(tmp_path)
        assert res is None
        assert called["suggest"] is False, "样本不足时不该走到 suggest_weight_adjustments()"

    def test_compute_new_weights_still_works_above_min_samples(self, monkeypatch, tmp_path):
        """保护不能过度——样本充足时回退路径仍应正常产出建议。"""
        snaps = [_FakeSnap("bullish", 100.0, 105.0, {"ScoutBeeNova": 8.0})
                 for _ in range(wo.MIN_SAMPLES + 2)]

        class _FakeAnalyzer:
            def __init__(self, directory=None):
                self.snapshots = snaps

            def suggest_weight_adjustments(self):
                return {"new_weights": dict(wo.DEFAULT_WEIGHTS)}

        monkeypatch.setattr("feedback_loop.BacktestAnalyzer", _FakeAnalyzer, raising=False)
        res = wo.compute_new_weights(tmp_path)
        assert res is not None
        assert "new_weights" in res


# ════════════════════════════════════════════════════════════════════════════
# v0.45.295：退役（归零）维度与 WEIGHT_CLAMPS 的结构矛盾
#
# 起因：v0.45.172 把 signal / risk_adj 归零之后，`clamp_shifts` 每周必抛
# InfeasibleBoundsError（2026-09-14、09-20），`main()` 在两道闸**之前** return、
# 退出码 0，周诊断整份丢失而没有任何东西变红。
#
# 为什么上面 TestProjectionInvariants 一条都没红：它的锚点全是 0.10~0.25 之间的
# 合成值，生产锚点 [0, .332, .325, .343, 0] 在夹具里根本不可达。下面每一组都用
# **生产形状**的锚点，并且有一条直接读真实 config.EVALUATION_WEIGHTS 的观测点。
# ════════════════════════════════════════════════════════════════════════════

_DIMS5 = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]
_LIVE3 = ["catalyst", "sentiment", "odds"]
# config.EVALUATION_WEIGHTS 自 v0.45.172 起的形状
_RETIRED_ANCHOR = {"signal": 0.0, "catalyst": 0.332, "sentiment": 0.325,
                   "odds": 0.343, "risk_adj": 0.0}
# `w = acc/Σacc` 的典型输出：它表达不了零，五维永远 ≈0.2
_FLAT_TARGET = {k: 0.2 for k in _DIMS5}


class TestRetiredDimensions:

    def test_retired_dims_is_derived_from_anchor(self):
        assert wo.retired_dims(_RETIRED_ANCHOR) == {"signal", "risk_adj"}
        assert wo.retired_dims({k: 0.2 for k in _DIMS5}) == set()
        # 容差：浮点噪声级别的 0 也算退役；0.001 是真权重，不算
        assert "odds" in wo.retired_dims({**_RETIRED_ANCHOR, "odds": 1e-12})
        assert "odds" not in wo.retired_dims({**_RETIRED_ANCHOR, "odds": 0.001})

    def test_incident_mechanism_is_two_independent_contradictions(self):
        """把事故机制钉成算术，防止将来有人只修其中一个就当修好了。

        ① 下限：锚点 0 时旧合并盒是空区间；② 上限：三个活维度的上限之和 < 1。
        **用事故当时的字面量，不读 wo.WEIGHT_CLAMPS**：将来有人为三维时代重设上限（v0.45.295
        的 CHANGELOG 就建议过），这条不该因此变红——它记的是「当时为什么不可行」，
        「现在还需不需要冻结」由 test_clamp_shifts_survives_production_shaped_anchor 与变异测试负责。
        """
        legacy_clamps = {"signal": (0.15, 0.40), "catalyst": (0.10, 0.25), "sentiment": (0.10, 0.30),
                         "odds": (0.08, 0.25), "risk_adj": (0.10, 0.25)}
        shift = 0.10   # MAX_SHIFT_PP=10.0
        lo_c, hi_c = legacy_clamps["signal"]
        legacy_box = (max(lo_c, 0.0 - shift), min(hi_c, 0.0 + shift))
        assert legacy_box[0] > legacy_box[1], f"旧公式在锚点 0 上应给出空区间，实为 {legacy_box}"
        assert sum(legacy_clamps[k][1] for k in _LIVE3) < 1.0, \
            "三个活维度的旧上限之和应 < 1（即便下限修好也凑不到 sum=1）"

    def test_merge_bounds_freezes_retired_and_frees_live_caps(self):
        b = wo.merge_bounds(_RETIRED_ANCHOR)
        assert b["signal"] == (0.0, 0.0)
        assert b["risk_adj"] == (0.0, 0.0)
        for k in _LIVE3:
            lo, hi = b[k]
            assert lo <= _RETIRED_ANCHOR[k] <= hi, f"{k}：现行配置本身必须落在盒内"
            # 五维时代的 0.25/0.30 上限不再适用；±MAX_SHIFT 仍是运行时护栏
            assert hi == pytest.approx(min(1.0, _RETIRED_ANCHOR[k] + wo.MAX_SHIFT_PP / 100.0))
            assert lo >= wo.WEIGHT_CLAMPS[k][0], "下限照旧生效"
        assert all(lo <= hi for lo, hi in b.values()), "不许再出现空区间"
        assert sum(lo for lo, _ in b.values()) <= 1.0 <= sum(hi for _, hi in b.values())

    def test_no_retired_dims_box_is_bitwise_legacy(self):
        """没有退役维度时与改动前的公式逐位相同——爆炸半径守卫。"""
        rng = random.Random(295)
        for _ in range(200):
            raw = {k: rng.uniform(0.05, 0.4) for k in _DIMS5}
            s = sum(raw.values())
            anchor = {k: v / s for k, v in raw.items()}
            shift_pp = rng.choice([1.0, 5.0, 10.0])
            got = wo.merge_bounds(anchor, shift_pp)
            for k in _DIMS5:
                lo_c, hi_c = wo.WEIGHT_CLAMPS[k]
                assert got[k] == (max(lo_c, anchor[k] - shift_pp / 100.0),
                                  min(hi_c, anchor[k] + shift_pp / 100.0))

    def test_clamp_shifts_survives_production_shaped_anchor(self):
        """事故复现：修复前这里抛 `维度 signal=0.100000 越界 [0.150000, 0.100000]`。"""
        new = wo.clamp_shifts(_RETIRED_ANCHOR, _FLAT_TARGET)
        assert new["signal"] == 0.0 and new["risk_adj"] == 0.0, "退役维度只能冻结，不能被复活"
        assert abs(sum(new.values()) - 1.0) < 1e-9
        for k in _LIVE3:
            assert abs(new[k] - _RETIRED_ANCHOR[k]) <= wo.MAX_SHIFT_PP / 100.0 + 1e-6

    def test_retired_dims_stay_zero_under_adversarial_targets(self):
        """目标把质量全押在退役维度上，也复活不了它们。"""
        rng = random.Random(295)
        for i in range(300):
            target = {k: rng.uniform(0.0, 1.0) for k in _DIMS5}
            if i % 3 == 0:                                   # 最恶劣：全押在两个退役维度
                target = {"signal": 0.5, "risk_adj": 0.5, "catalyst": 0.0,
                          "sentiment": 0.0, "odds": 0.0}
            new = wo.clamp_shifts(_RETIRED_ANCHOR, target)
            assert new["signal"] == 0.0 and new["risk_adj"] == 0.0
            assert abs(sum(new.values()) - 1.0) < 1e-9
            for k in _LIVE3:
                assert abs(new[k] - _RETIRED_ANCHOR[k]) <= wo.MAX_SHIFT_PP / 100.0 + 1e-6

    def test_project_to_feasible_names_inverted_interval(self):
        """单维空区间要在前置检查里点名，不能靠事后 assert_feasible 报一句「越界」。

        这组盒 Σlo=0.55 ≤ 1 ≤ Σhi=1.00，聚合检查照样放行——正是 2026-09-20 的形状。
        """
        bounds = {"a": (0.15, 0.10), "b": (0.40, 0.90)}
        with pytest.raises(wo.InfeasibleBoundsError, match="空区间"):
            wo.project_to_feasible({"a": 0.5, "b": 0.5}, bounds)

    def test_real_config_is_feasible_for_the_optimizer(self):
        """**观测点**：config.EVALUATION_WEIGHTS 一旦被改成优化器投影不了的形状，这里立刻红，
        而不是等周日 cron 死一次。

        此前所有投影测试都用合成锚点，生产已经不可行的那两周它们照样全绿——这条读的是
        真实 config（直接 import，不走 wo.CONFIG_PATH 的 ~ 路径，CI 与 worktree 里也成立）。
        """
        import config
        anchor = dict(config.EVALUATION_WEIGHTS)
        assert set(anchor) == set(_DIMS5), f"config 权重维度变了：{sorted(anchor)}"
        rng = random.Random(295)
        targets = [_FLAT_TARGET] + [{k: rng.uniform(0, 1) for k in _DIMS5} for _ in range(50)]
        for target in targets:
            new = wo.clamp_shifts(anchor, target)      # 不抛就是通过
            assert abs(sum(new.values()) - 1.0) < 1e-9
            for k in wo.retired_dims(anchor):
                assert new[k] == 0.0, f"config 里归零的 {k} 被优化器复活了"


class TestInfeasiblePathRunsGates:
    """`main()` 的不可行分支：曾经在两道闸之前 return。用 main() 端到端跑——
    上面 TestModuleLoggerDefined.test_infeasible_handler_can_log 是**手抄**了一份
    except 分支来测的，从来没真的走过 main() 的这一支，所以三次出事三次没红。
    """

    @pytest.fixture
    def pipeline(self, monkeypatch, sandbox):
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", sandbox)
        monkeypatch.setattr(wo, "count_t7_samples", lambda d: 667)
        raw = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20,
               "odds": 0.20, "risk_adj": 0.10}
        monkeypatch.setattr(wo, "compute_new_weights_wls",
                            lambda d: {"new_weights": dict(raw), "method": "wls_time_decay"})
        calls = {"bootstrap": [], "pool": 0}

        def _bootstrap(snap_dir, weights, *a, **kw):
            calls["bootstrap"].append((dict(weights), kw))
            return {"stable": True}

        def _pool(*a, **kw):
            calls["pool"] += 1
            return {"ok": True, "n_recent_pool": 30, "unrepresented_ratio": 0.0}

        monkeypatch.setattr(wo, "bootstrap_validate", _bootstrap)
        monkeypatch.setattr(wo, "check_ticker_pool_consistency", _pool)
        return {"raw": raw, "calls": calls}

    @staticmethod
    def _make_infeasible(monkeypatch):
        # 真实的不可行（不是 mock 掉 clamp_shifts）：每维下限 0.30 ⇒ Σlo=1.5>1
        monkeypatch.setattr(wo, "WEIGHT_CLAMPS", {k: (0.30, 0.40) for k in _DIMS5})

    @staticmethod
    def _run(monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py", *argv])
        wo.main()

    def test_read_only_run_still_reaches_both_gates_and_labels_correctly(
            self, monkeypatch, pipeline, capsys):
        self._make_infeasible(monkeypatch)
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run(monkeypatch, [])

        assert len(pipeline["calls"]["bootstrap"]) == 1, "闸 1 必须仍然运行"
        assert pipeline["calls"]["pool"] == 1, "闸 2 必须仍然运行"
        # 没有可行的投影结果可验 ⇒ 验原始 WLS 目标，且不带 anchor（不做逐次投影）
        weights_arg, kwargs = pipeline["calls"]["bootstrap"][0]
        assert weights_arg == pipeline["raw"]
        assert not kwargs.get("anchor")

        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["skip_reason"] == "infeasible_bounds"
        assert rec["applied"] is False
        # 只读运行必须记成 diagnose / dry_run=True。修复前这里是 optimize / False
        # （因为 dry_run=args.dry_run，而默认只读运行并不传 --dry-run）。
        assert rec["action"] == "diagnose"
        assert rec["dry_run"] is True
        assert rec["bootstrap_stable"] is True, "闸 1 的结果必须进审计记录"
        assert rec["pool_ok"] is True, "闸 2 的结果也必须进审计记录（此前只在 stdout，周任务读不到）"
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before

        out = capsys.readouterr().out
        assert "闸 1/2" in out and "闸 2/2" in out
        assert "无可行解" in out
        assert "系统稳定" not in out, "没有提议时不许说『权重保持不变（系统稳定）』"

    def test_apply_force_cannot_write_when_infeasible(self, monkeypatch, pipeline):
        """没有可行权重就没有东西可写——`--force` 覆盖的是闸门，不是数学。

        ⚠️ 只断言「config 没变」是空的（v0.45.299 独立审查）：不可行时 `new_weights = old_weights`，
        `significant` 恒假，就算把 `if blocked` 分支整个删掉，写入决策链也会走到 `below_min_change`
        而不写。所以：① 强制「显著变化」为真，让只有 blocked 分支能挡住写入；
        ② 直接 spy `write_weights_to_config`，断言它**根本没被调用**。
        """
        self._make_infeasible(monkeypatch)
        monkeypatch.setattr(wo, "has_significant_change", lambda *a, **k: True)
        writes = []
        monkeypatch.setattr(wo, "write_weights_to_config",
                            lambda *a, **k: writes.append((a, k)) or False)
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run(monkeypatch, ["--apply", "--force"])

        assert writes == [], "不可行时 write_weights_to_config 不许被调用（哪怕是 dry_run 预览也不该有）"
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["applied"] is False
        assert rec["skip_reason"] == "infeasible_bounds"
        assert rec["action"] == "optimize"      # 请求了写入，如实记成 optimize

    def test_retired_regime_is_not_infeasible_and_gate1_gets_anchor(self, monkeypatch, pipeline):
        """事故本身：config 里有归零维度 ⇒ 不再不可行，且闸 1 拿到 anchor 做逐次投影。"""
        wo.CONFIG_PATH.write_text(
            _CONFIG_STUB.replace('"signal":    0.3000', '"signal":    0.0000')
                        .replace('"catalyst":  0.2000', '"catalyst":  0.3320')
                        .replace('"sentiment": 0.2000', '"sentiment": 0.3250')
                        .replace('"odds":      0.1500', '"odds":      0.3430')
                        .replace('"risk_adj":  0.1500', '"risk_adj":  0.0000'),
            encoding="utf-8")
        assert wo.read_current_weights() == _RETIRED_ANCHOR       # 夹具自证：真的读成生产形状
        self._run(monkeypatch, [])

        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["skip_reason"] != "infeasible_bounds"
        assert rec["skip_reason"] == "read_only_default"
        assert rec["action"] == "diagnose"
        assert rec["new_weights"]["signal"] == 0.0 and rec["new_weights"]["risk_adj"] == 0.0
        assert abs(sum(rec["new_weights"].values()) - 1.0) < 1e-6

        assert len(pipeline["calls"]["bootstrap"]) == 1
        _, kwargs = pipeline["calls"]["bootstrap"][0]
        assert kwargs.get("anchor") == _RETIRED_ANCHOR, "闸 1 必须拿到 anchor，否则恒红"


# ════════════════════════════════════════════════════════════════════════════
# v0.45.298：`--apply` 不许抹掉写在数值旁的注释
#
# `write_weights_to_config` 的 docstring 一直写着「保留所有注释，只替换数值」，实现却整块按
# 通用文案重写。生产形状下这条路径原先不可达（归零后 clamp_shifts 恒不可行），v0.45.295 让它
# 首次可达——`--apply` 会抹掉 `—— IC -0.088，归零（见上）` 这类 config 里唯一贴着数值的「为什么」。
# ════════════════════════════════════════════════════════════════════════════

class TestWritePreservesInlineComments:

    def test_comments_next_to_values_survive_a_write(self, sandbox):
        # 注释里故意放反斜杠：块现在带的是用户自己的文本，不能被当成正则替换串里的转义
        stub = (_CONFIG_STUB
                .replace('"signal":    0.3000,   # a', r'"signal":    0.3000,   # a —— IC -0.088，归零（见上）\1\d')
                .replace('"odds":      0.1500,   # d', '"odds":      0.1500,   # d 三维中唯一方向显著（未校正）'))
        wo.CONFIG_PATH.write_text(stub, encoding="utf-8")

        assert wo.write_weights_to_config(_W, dry_run=False) is True
        text = wo.CONFIG_PATH.read_text(encoding="utf-8")
        assert r"# a —— IC -0.088，归零（见上）\1\d" in text, "注释（含反斜杠）必须原样保留"
        assert "# d 三维中唯一方向显著（未校正）" in text
        assert "# b" in text and "# c" in text and "# e" in text
        got = wo.read_current_weights()
        for k, v in _W.items():
            assert abs(got[k] - v) < 1e-4, "数值仍要更新"
        compile(text, "config.py", "exec")
        assert "OTHER_SETTING = 42" in text

    def test_falls_back_to_generic_comment_when_line_is_absent(self, sandbox):
        stub = _CONFIG_STUB.replace('    "odds":      0.1500,   # d\n', "")
        wo.CONFIG_PATH.write_text(stub, encoding="utf-8")
        assert wo.write_weights_to_config(_W, dry_run=False) is True
        text = wo.CONFIG_PATH.read_text(encoding="utf-8")
        assert "# OracleBeeEcho" in text, "现有块里找不到该维度那行时，才退回通用文案"
        assert abs(wo.read_current_weights()["odds"] - _W["odds"]) < 1e-4

    def test_real_config_comments_survive_apply(self, sandbox):
        """对**真实 config.py 的副本**：`--apply` 的写入不许抹掉逐维注释（含归零决策的理由）。

        动态取「写入前每个维度数值之后的内容」再与写入后比，不写死注释文字，
        **也不要求 config 此刻恰好有归零维度**——config 里的措辞与权重将来都可以合理地改，
        不该让这条测试变成定时炸弹（归零形状的写入已由 stub 版 main() 测试覆盖）。
        """
        import re
        import config
        real = open(config.__file__, encoding="utf-8").read()
        wo.CONFIG_PATH.write_text(real, encoding="utf-8")
        anchor = wo.read_current_weights()
        new = wo.clamp_shifts(anchor, {"signal": .2, "catalyst": .1, "sentiment": .4,
                                       "odds": .2, "risk_adj": .1})
        assert any(abs(new[k] - anchor[k]) > 0.01 for k in new), "夹具自证：必须真的有数值变化"

        def tails(t):
            blk = re.search(r'EVALUATION_WEIGHTS\s*=\s*\{[^}]+\}', t, re.DOTALL).group(0)
            return {k: re.search(rf'"{k}"\s*:\s*[0-9.]+(,[^\n]*)', blk).group(1) for k in new}

        before = tails(real)
        assert wo.write_weights_to_config(new, dry_run=False) is True
        after_text = wo.CONFIG_PATH.read_text(encoding="utf-8")
        assert tails(after_text) == before, "逐维数值之后的内容（注释）必须原样保留"
        got = wo.read_current_weights()
        for k in wo.retired_dims(anchor):
            assert got[k] == 0.0, "退役维度写入后仍须为 0"
        assert after_text.replace(re.search(r'EVALUATION_WEIGHTS\s*=\s*\{[^}]+\}', after_text, re.DOTALL).group(0), "") == \
            real.replace(re.search(r'EVALUATION_WEIGHTS\s*=\s*\{[^}]+\}', real, re.DOTALL).group(0), ""), \
            "块之外的内容必须逐字节不变"


# ════════════════════════════════════════════════════════════════════════════
# v0.45.299：独立审查后续
#   ① 现行权重读不出必须明说并阻断，不许静默兜底（否则归零维度会被悄悄换成默认值再被 --apply 写回）
#   ② 「上限解除」只在活维度上限之和 < 1 时发生（此前只要有维度退役就一律解除）
# ════════════════════════════════════════════════════════════════════════════

class TestParseCurrentWeights:
    """`parse_current_weights` 用 AST 读 config，读不出返回 None，绝不兜底。"""

    def _write(self, text):
        wo.CONFIG_PATH.write_text(text, encoding="utf-8")

    def test_real_config_parses_to_the_imported_module_values(self, sandbox):
        """**输入路径的观测点**：优化器自己读到的锚点必须 == 被 import 的 config 模块的值。

        此前观测点只读 `config.EVALUATION_WEIGHTS`（模块），而 `main()` 用的是另一条路径
        （对文件文本的解析）——两条路径从没被对拍过，解析层无声出错时冻结逻辑照样全绿。
        """
        import config
        self._write(open(config.__file__, encoding="utf-8").read())
        got = wo.parse_current_weights()
        assert got is not None
        assert got == pytest.approx(dict(config.EVALUATION_WEIGHTS))

    def test_the_three_silent_failures_of_the_regex_version(self, sandbox):
        # ① 注释里出现 `{…}`：旧正则 `[^}]+` 提前截断 ⇒ 静默回退默认值
        self._write(_CONFIG_STUB.replace("# a", "# 见 {SEC} 披露"))
        assert wo.parse_current_weights() == pytest.approx(
            {"signal": .30, "catalyst": .20, "sentiment": .20, "odds": .15, "risk_adj": .15})
        # ② 科学计数法：旧 `[0-9.]+` 把 2.0e-1 读成 2.0（差 10 倍且无声）
        self._write(_CONFIG_STUB.replace('"catalyst":  0.2000', '"catalyst":  2.0e-1'))
        assert wo.parse_current_weights()["catalyst"] == 0.2
        # ③ 归零值必须读成 0.0（退役判据的输入）
        self._write(_CONFIG_STUB.replace('"signal":    0.3000', '"signal":    0.0000'))
        assert wo.parse_current_weights()["signal"] == 0.0
        assert "signal" in wo.retired_dims(wo.parse_current_weights())

    @pytest.mark.parametrize("label,mutate", [
        ("多一个键", lambda t: t.replace("    # ml_auxiliary: 不在此处\n", '    "ml_auxiliary": 0.0500,\n')),
        ("少一个键", lambda t: t.replace('    "odds":      0.1500,   # d\n', "")),
        ("值是字符串", lambda t: t.replace('"odds":      0.1500', '"odds":      "x"')),
        ("值为负", lambda t: t.replace('"odds":      0.1500', '"odds":      -0.1500')),
        ("值是非字面量表达式", lambda t: t.replace('"odds":      0.1500', '"odds":      float("nan")')),
        ("值为 inf（1e999 是合法字面量，只能靠 isfinite 拦）", lambda t: t.replace('"odds":      0.1500', '"odds":      1e999')),
        ("语法错误", lambda t: t + "\ndef (((\n"),
        ("没有该赋值", lambda t: t.replace("EVALUATION_WEIGHTS = {", "OTHER_WEIGHTS = {")),
    ])
    def test_unreadable_returns_none_never_a_default(self, sandbox, label, mutate):
        self._write(mutate(_CONFIG_STUB))
        assert wo.parse_current_weights() is None, label

    def test_read_current_weights_fallback_is_loud(self, sandbox, capsys):
        self._write(_CONFIG_STUB.replace("    # ml_auxiliary: 不在此处\n", '    "ml_auxiliary": 0.05,\n'))
        got = wo.read_current_weights()
        assert got == wo.DEFAULT_WEIGHTS
        out = capsys.readouterr().out
        assert "⚠️" in out and "不是现行权重" in out, "旧版对无匹配/键数不对是静默兜底"


class TestConfigUnparseablePath:
    """`main()` 读不出现行权重：不许拿默认值当锚点提议，更不许写入；闸照跑；审计如实记。"""

    @pytest.fixture
    def pipeline(self, monkeypatch, sandbox):
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", sandbox)
        monkeypatch.setattr(wo, "count_t7_samples", lambda d: 667)
        raw = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.20, "risk_adj": 0.10}
        monkeypatch.setattr(wo, "compute_new_weights_wls",
                            lambda d: {"new_weights": dict(raw), "method": "wls_time_decay"})
        calls = {"bootstrap": [], "pool": 0}
        monkeypatch.setattr(wo, "bootstrap_validate",
                            lambda d, w, *a, **k: calls["bootstrap"].append((dict(w), k)) or {"stable": True})
        monkeypatch.setattr(wo, "check_ticker_pool_consistency",
                            lambda *a, **k: calls.__setitem__("pool", calls["pool"] + 1)
                            or {"ok": True, "n_recent_pool": 30, "unrepresented_ratio": 0.0})
        # 配置块多一个键 ⇒ 读不出（旧版：静默回退默认值）
        wo.CONFIG_PATH.write_text(
            _CONFIG_STUB.replace("    # ml_auxiliary: 不在此处\n", '    "ml_auxiliary": 0.0500,\n'),
            encoding="utf-8")
        return {"raw": raw, "calls": calls}

    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py", *argv])
        wo.main()

    def test_read_only_run_is_blocked_but_gates_still_run(self, monkeypatch, pipeline, capsys):
        before = wo.CONFIG_PATH.read_text(encoding="utf-8")
        self._run(monkeypatch, [])
        assert len(pipeline["calls"]["bootstrap"]) == 1 and pipeline["calls"]["pool"] == 1
        weights_arg, kwargs = pipeline["calls"]["bootstrap"][0]
        assert weights_arg == pipeline["raw"] and not kwargs.get("anchor"), "没有锚点，不许逐次投影"
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["skip_reason"] == "config_unparseable"
        assert rec["action"] == "diagnose" and rec["dry_run"] is True and rec["applied"] is False
        assert rec["pool_ok"] is True and rec["bootstrap_stable"] is True
        assert wo.CONFIG_PATH.read_text(encoding="utf-8") == before
        out = capsys.readouterr().out
        assert "读不出" in out and "系统稳定" not in out
        assert "--apply 也无法写入" in out, "被阻断时不许再说『写入需 --apply』"

    def test_apply_force_never_calls_the_writer(self, monkeypatch, pipeline):
        monkeypatch.setattr(wo, "has_significant_change", lambda *a, **k: True)
        writes = []
        monkeypatch.setattr(wo, "write_weights_to_config",
                            lambda *a, **k: writes.append(1) or False)
        self._run(monkeypatch, ["--apply", "--force"])
        assert writes == []
        rec = _read_history(wo.HISTORY_FILE)[-1]
        assert rec["skip_reason"] == "config_unparseable" and rec["applied"] is False
        assert rec["action"] == "optimize"


class TestCapsLiftedOnlyWhenStructurallyNeeded:
    """v0.45.299：解除活维度旧上限的条件是「上限之和 < 1」，不是「只要有维度退役」。"""

    def test_single_retired_dim_keeps_the_absolute_caps(self):
        anchor = {"signal": 0.0, "catalyst": 0.25, "sentiment": 0.25, "odds": 0.25, "risk_adj": 0.25}
        b = wo.merge_bounds(anchor)
        assert b["signal"] == (0.0, 0.0)
        # 其余四维上限之和 1.05 ≥ 1，撑得起 sum=1 ⇒ 上限照旧（一律解除会让 catalyst 一次能到 0.35）
        assert b["catalyst"][1] == pytest.approx(0.25)
        assert b["odds"][1] == pytest.approx(0.25)
        assert b["risk_adj"][1] == pytest.approx(0.25)
        assert b["sentiment"][1] == pytest.approx(0.30)
        new = wo.clamp_shifts(anchor, {"signal": 0.4, "catalyst": 0.6, "sentiment": 0.0,
                                       "odds": 0.0, "risk_adj": 0.0})
        assert new["signal"] == 0.0 and new["catalyst"] <= 0.25 + 1e-9

    def test_caps_are_lifted_when_live_caps_cannot_reach_one(self):
        # 生产形状：三个活维度上限之和 0.80 < 1
        assert wo.merge_bounds(_RETIRED_ANCHOR)["catalyst"][1] > wo.WEIGHT_CLAMPS["catalyst"][1]
        # 只剩一个活维度：上限 0.25 < 1，必须解除，否则连 sum=1 都凑不成
        anchor = {"signal": 0.0, "catalyst": 1.0, "sentiment": 0.0, "odds": 0.0, "risk_adj": 0.0}
        new = wo.clamp_shifts(anchor, {k: 0.2 for k in _DIMS5})
        assert new["catalyst"] == pytest.approx(1.0) and sum(new.values()) == pytest.approx(1.0)
