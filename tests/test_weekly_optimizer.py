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
