#!/usr/bin/env python3
"""
🐝 Alpha Hive · 轨道 A — 权重优化器（v0.44.0 起：**默认只读诊断**）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **本工具默认不再修改 config.py。** 写入必须显式 `--apply`，且两道闸全过。

为什么降级为只读（三条独立理由，任一条都够）
--------------------------------------------
1. **`w = acc/Σacc` 数学上无法表达"这个维度没用"**：准确率都挤在 0.5 附近，
   权重必然全部 ≈0.2 —— 它的输出空间里不存在"归零"这个答案。
2. **它优化的对象已被证明不存在**：综合分 |IC|=0.090 打不过 20 日动量 0.135；
   五维没有一个经得起全部保守口径（见 MEMORY 的四口径表）。
   在无 edge 的信号层上优化加权，是在噪音里挑选。
3. **每次写入都重置样本世代**：此后所有样本都是在新权重下产生的，与之前不可比。
   `experiments/ic_power_report.md` 实测扩池 10→30 只把出结论时间缩短 **5.18×**，
   而那份加速的计价单位是可汇总的历史样本 —— 优化器每开一次火就清零一次。
   **这是唯一会主动让系统变差的一条。**

附带事实：`compute_new_weights_wls` 名不副实 —— docstring 称 OLS 回归取 beta
并做共线性检测，实现里**没有任何回归**。它从未做过它声称的事，却已真实改写过
config.py 两次（2026-04-26 与更早）。当前 config 里那五个数是 n=28/133 噪声期的
产物，应当**按任意常数看待**，不要赋予它"学出来的"含义。

两道闸（`--apply` 时生效，`--force` 可覆盖但会记入审计）
--------------------------------------------------------
- **闸 1 Bootstrap 稳健性**：v0.44.0 之前这里只打印不阻断（原文"继续应用
  （限幅已保护）"）。2026-08-16 实测到真实后果：bootstrap 报不稳健、
  risk_adj −4.13pp 已越阈值，若非人工中断就会写入。限幅只保证**幅度**不失控，
  不保证**方向**对。
- **闸 2 标的池世代一致性**：v0.42.9 扩池 10→30 后有数周窗口，优化器眼里
  还是旧的 10 只却要改 30 只在用的权重。2026-08-16 实测：665 个 T+7 样本
  跨度 03-09→07-29，30 只时代贡献 **0 条**。

用法（必须用 /usr/local/bin/python3 = 3.11，裸 python3 可能解析成 3.9.6）：
  /usr/local/bin/python3 weekly_optimizer.py                 # 只读诊断（默认）
  /usr/local/bin/python3 weekly_optimizer.py --apply         # 请求写入，闸门仍生效
  /usr/local/bin/python3 weekly_optimizer.py --apply --force # 无视闸门（几乎不该用）
  /usr/local/bin/python3 weekly_optimizer.py --min-samples 5 # 降低最低样本要求（测试用）
  /usr/local/bin/python3 weekly_optimizer.py --rollback      # 回滚到上次写入前的权重
  /usr/local/bin/python3 weekly_optimizer.py --rollback --dry-run   # 预览回滚

审计日志 `weight_history.jsonl` 的 `action` 字段区分两种运行：
`"diagnose"`（只读）与 `"optimize"`（请求过写入）。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_log = logging.getLogger("alpha_hive.weekly_optimizer")

# ── 路径配置（与 generate_deep_v2.py 保持一致）─────────────────────────────
# v0.10.1 修复：VM 路径硬编码旧 session（keen-magical-wright）导致新 session
# 找不到 snapshots 目录。改为 glob 动态扫描，任意 Cowork session 都能工作。
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
import glob as _glob_mod
_VM_SESSIONS = sorted(_glob_mod.glob("/sessions/*/mnt/Alpha Hive"), reverse=True)
_VM_PATH = Path(_VM_SESSIONS[0]) if _VM_SESSIONS else Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
try:
    if _VM_PATH.exists():
        ALPHAHIVE_DIR = _VM_PATH
except PermissionError:
    pass

_VM_DEEP_SESSIONS = sorted(_glob_mod.glob("/sessions/*/mnt/深度分析报告/深度"), reverse=True)
_VM_DEEP_DIR = Path(_VM_DEEP_SESSIONS[0]) if _VM_DEEP_SESSIONS else Path("/sessions/keen-magical-wright/mnt/深度分析报告/深度")
try:
    if _VM_DEEP_DIR.exists():
        OUTPUT_DIR = _VM_DEEP_DIR
    else:
        OUTPUT_DIR = Path(os.path.expanduser("~/Desktop/深度分析报告/深度"))
except PermissionError:
    OUTPUT_DIR = Path(os.path.expanduser("~/Desktop/深度分析报告/深度"))

# v0.23.6 修复：始终优先用 Alpha Hive 项目目录的 snapshots（数据最完整）
# 旧实现 OUTPUT_DIR/report_snapshots 即使存在也用（即使只有 28 个旧快照），
# 导致 weekly_optimizer 跑时只看到 28 笔样本而非 Alpha Hive 实际的 245 笔。
# 选择策略：取两者中文件数较多的（生产中 Alpha Hive 一定多于深度分析报告）。
def _best_snapshots_dir() -> Path:
    candidates = []
    for p in [ALPHAHIVE_DIR / "report_snapshots", OUTPUT_DIR / "report_snapshots"]:
        try:
            if p.exists():
                n = len(list(p.glob("*.json")))
                candidates.append((n, p))
        except (OSError, PermissionError):
            pass
    if not candidates:
        return ALPHAHIVE_DIR / "report_snapshots"  # 兜底（即使不存在）
    # 取样本数最多的目录
    candidates.sort(reverse=True)
    return candidates[0][1]
_candidate_snapshots = _best_snapshots_dir()

CONFIG_PATH    = ALPHAHIVE_DIR / "config.py"
SNAPSHOTS_DIR  = _candidate_snapshots
HISTORY_FILE   = ALPHAHIVE_DIR / "weight_history.jsonl"

# ── 优化阈值 ──────────────────────────────────────────────────────────────────
MIN_SAMPLES    = 10    # 少于此样本数不调整权重
# v0.23.6 (2026-04-26 周日复盘) — 解除 4-19 设的临时 gate
# 原因：临时 gate 11.0pp 已让 weekly_optimizer 冻结 7 天，期间 catalyst 维度仍
# 卡在 0.3316（v0.21 修复 _apply_weight_clamps 前的旧产物，>0.25 上限）。
# 必须解锁让 _apply_weight_clamps 在下次 weekly_optimizer 运行时触发归一化。
# 同时 4-19 dry-run 的 catalyst -10.5pp 方向与 v0.22 FF 归因发现的"catalyst
# 维度被高估"一致，方向稳健。
MIN_CHANGE_PP  = 3.0   # 恢复 v0.21 默认值（4-26 解锁，见 CHANGELOG v0.23.6）
MAX_SHIFT_PP   = 10.0  # 单次调整上限（每个维度最多 ±10pp）

# 5 维默认权重（与 config.py 保持一致，用于兜底）
DEFAULT_WEIGHTS = {
    "signal":    0.30,
    "catalyst":  0.20,
    "sentiment": 0.20,
    "odds":      0.15,
    "risk_adj":  0.15,
}

# 升级5: 权重 clamp — 防止 optimizer 把某个维度推到极端
# 数据发现 catalyst 被推到 0.33 → 高分看多反而胜率最低
WEIGHT_CLAMPS = {
    "signal":    (0.15, 0.40),
    "catalyst":  (0.10, 0.25),   # 上限 25%，防止"有催化剂=高分"陷阱
    "sentiment": (0.10, 0.30),
    "odds":      (0.08, 0.25),
    "risk_adj":  (0.10, 0.25),
}


# ─────────────────────────────────────────────────────────────────────────────
# 核心函数
# ─────────────────────────────────────────────────────────────────────────────

def count_t7_samples(snapshots_dir: Path) -> int:
    """统计有 T+7 实际价格的快照数"""
    if not snapshots_dir.exists():
        return 0
    count = 0
    for f in snapshots_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("actual_prices", {}).get("t7") is not None:
                count += 1
        except Exception:
            pass
    return count


class InfeasibleBoundsError(ValueError):
    """盒约束与单纯形（sum=1）无交集 —— 配置本身矛盾，必须拒绝写入而非静默降级。"""


def merge_bounds(anchor: dict, max_shift_pp: float = None,
                 clamps: dict = None) -> dict:
    """
    把「绝对上下限」与「单次变动上限」合并成**单一盒约束**（v0.42.6）

        lo_eff[d] = max(WEIGHT_CLAMPS[d][0], anchor[d] - MAX_SHIFT)
        hi_eff[d] = min(WEIGHT_CLAMPS[d][1], anchor[d] + MAX_SHIFT)

    为什么必须合并：旧实现分两步做——先 `_apply_weight_clamps` 保证绝对上下限，
    再 `clamp_shifts` 钳幅并**重新归一化**。归一化是乘性缩放，必然把已在边界上的
    值推出边界，所以两个不变式互相破坏。历史实证：`weight_history.jsonl` 里
    `risk_adj +10.72pp`、`signal −11.32pp` 均突破 MAX_SHIFT_PP=10.0，
    且 catalyst 曾落到 0.3316（> clamp 上限 0.25）。

    合并成一个盒之后，单次投影即可同时满足三个约束，不存在互相破坏的顺序问题。
    """
    if max_shift_pp is None:
        max_shift_pp = MAX_SHIFT_PP
    if clamps is None:
        clamps = WEIGHT_CLAMPS
    shift = max_shift_pp / 100.0
    bounds = {}
    for k in set(list(anchor.keys()) + list(clamps.keys())):
        a = float(anchor.get(k, DEFAULT_WEIGHTS.get(k, 0.2)))
        lo_c, hi_c = clamps.get(k, (0.0, 1.0))
        bounds[k] = (max(lo_c, a - shift), min(hi_c, a + shift))
    return bounds


def project_to_feasible(target: dict, bounds: dict = None,
                        max_iter: int = 200, tol: float = 1e-12) -> dict:
    """
    把 target 投影到「盒约束 ∩ 单纯形」上（欧氏投影，λ 二分）

    数学性质：输出严格满足 `lo ≤ w[k] ≤ hi`（对 bounds 中所有键）**且** `sum(w)=1.0`，
    并且是可行域内距 target 最近的点（L2 意义）。

    算法：求 λ 使得 `Σ clip(target[k] + λ, lo[k], hi[k]) = 1`。
    `f(λ) = Σ clip(...) − 1` 关于 λ 单调不减且连续，故二分必收敛。

    ⚠️ v0.42.6 换掉了「钳制 + 按比例再分配」的 water-filling 实现。旧算法在
    **所有维度同时触界**时会失效：free_keys 为空 → 直接 break，剩余预算无人吸收，
    输出的和可能远离 1.0（实测 0.8545）。旧代码注释里也承认了这个降级会
    "允许轻微突破 clamp"。二分法没有这个死角——它不需要"自由维度"。

    Raises:
        InfeasibleBoundsError: 盒与单纯形无交集（Σlo > 1 或 Σhi < 1）。
            由调用方据此拒绝写入，绝不静默降级。
    """
    if bounds is None:
        bounds = WEIGHT_CLAMPS

    # 可行性前置检查：盒 ∩ 单纯形非空 ⟺ Σlo ≤ 1 ≤ Σhi
    sum_lo = sum(b[0] for b in bounds.values())
    sum_hi = sum(b[1] for b in bounds.values())
    if sum_lo > 1.0 + 1e-9 or sum_hi < 1.0 - 1e-9:
        raise InfeasibleBoundsError(
            f"盒约束与 sum=1 无交集：Σlo={sum_lo:.4f}, Σhi={sum_hi:.4f}（需 Σlo ≤ 1 ≤ Σhi）"
        )

    keys = sorted(set(list(target.keys()) + list(bounds.keys())))
    t = {k: float(target.get(k, DEFAULT_WEIGHTS.get(k, 0.2))) for k in keys}

    def clipped_sum(lam: float) -> float:
        s = 0.0
        for k in keys:
            lo, hi = bounds.get(k, (0.0, 1.0))
            s += min(max(t[k] + lam, lo), hi)
        return s

    # 二分区间：λ 足够小时全部落到 lo（和 = Σlo ≤ 1），足够大时全部落到 hi（和 = Σhi ≥ 1）
    lam_lo = min(bounds.get(k, (0.0, 1.0))[0] for k in keys) - max(t.values()) - 1.0
    lam_hi = max(bounds.get(k, (0.0, 1.0))[1] for k in keys) - min(t.values()) + 1.0
    for _ in range(max_iter):
        mid = (lam_lo + lam_hi) / 2.0
        if clipped_sum(mid) < 1.0:
            lam_lo = mid
        else:
            lam_hi = mid
        if lam_hi - lam_lo < tol:
            break
    lam = (lam_lo + lam_hi) / 2.0

    w = {}
    for k in keys:
        lo, hi = bounds.get(k, (0.0, 1.0))
        w[k] = min(max(t[k] + lam, lo), hi)

    assert_feasible(w, bounds, tol=1e-6)
    return w


def assert_feasible(w: dict, bounds: dict, tol: float = 1e-6) -> None:
    """校验三不变式：sum=1、各维在盒内。不满足则抛 InfeasibleBoundsError。"""
    s = sum(w.values())
    if abs(s - 1.0) > tol:
        raise InfeasibleBoundsError(f"权重和 ={s:.9f}，偏离 1.0 超过容差")
    for k, (lo, hi) in bounds.items():
        v = w.get(k)
        if v is None:
            raise InfeasibleBoundsError(f"缺少维度 {k}")
        if v < lo - tol or v > hi + tol:
            raise InfeasibleBoundsError(
                f"维度 {k}={v:.6f} 越界 [{lo:.6f}, {hi:.6f}]"
            )


def _apply_weight_clamps(weights: dict, max_iter: int = 50, tol: float = 1e-9) -> dict:
    """仅按 WEIGHT_CLAMPS 绝对上下限投影（不含单次变动限幅）。

    保留此名以兼容既有调用点；新代码请直接用 `project_to_feasible(target, bounds)`。
    """
    return project_to_feasible(weights, WEIGHT_CLAMPS, max_iter=max_iter, tol=tol)


# 模块导入期自洽性检查：WEIGHT_CLAMPS 本身必须允许 sum=1 的解，
# 否则任何投影都会抛错。把配置错误暴露在导入时，而不是周日 cron 跑到一半。
_CLAMP_SUM_LO = sum(b[0] for b in WEIGHT_CLAMPS.values())
_CLAMP_SUM_HI = sum(b[1] for b in WEIGHT_CLAMPS.values())
if not (_CLAMP_SUM_LO <= 1.0 <= _CLAMP_SUM_HI):
    raise InfeasibleBoundsError(
        f"WEIGHT_CLAMPS 配置矛盾：Σlo={_CLAMP_SUM_LO:.4f}, Σhi={_CLAMP_SUM_HI:.4f}"
        f"（需 Σlo ≤ 1 ≤ Σhi）"
    )


# 统一的 Agent → 维度映射（BearBee 纳入 risk_adj，修复 Bug #6）
# 唯一入口：所有学习路径（weekly_optimizer / feedback_loop）必须使用这个字典
AGENT_TO_DIM = {
    "ScoutBeeNova": "signal",
    "RivalBeeVanguard": "signal",
    "BuzzBeeWhisper": "sentiment",
    "OracleBeeEcho": "odds",
    "ChronosBeeHorizon": "catalyst",
    "GuardBeeSentinel": "risk_adj",
    "BearBeeContrarian": "risk_adj",  # 修复 Bug #6：BearBee 参与 risk_adj 维度学习
}


def compute_new_weights(snapshots_dir: Path) -> Optional[dict]:
    """
    调用 BacktestAnalyzer 计算建议权重（带 clamp）。
    返回 suggest_weight_adjustments() 的完整结果，或 None（样本不足/失败）。
    """
    try:
        sys.path.insert(0, str(ALPHAHIVE_DIR))
        from feedback_loop import BacktestAnalyzer
    except ImportError as e:
        print(f"❌ 无法导入 feedback_loop: {e}")
        return None

    try:
        analyzer = BacktestAnalyzer(directory=str(snapshots_dir))
        result = analyzer.suggest_weight_adjustments()
        if result and "new_weights" in result:
            result["new_weights"] = _apply_weight_clamps(result["new_weights"])
        return result if result else None
    except Exception as e:
        print(f"❌ BacktestAnalyzer 运行失败: {e}")
        return None


def compute_new_weights_wls(snapshots_dir: Path) -> Optional[dict]:
    """
    加权最小二乘法（WLS）权重优化 — 替代简单归一化

    改进点：
    1. OLS 回归 agent_vote ~ composite_score，提取 beta 作为隐含重要性
    2. 时间衰减权重：近期快照权重 > 远期
    3. 共线性检测：高相关 Agent 不同时提升
    """
    try:
        sys.path.insert(0, str(ALPHAHIVE_DIR))
        from feedback_loop import BacktestAnalyzer, agent_vote_correct
    except ImportError as e:
        print(f"❌ 无法导入 feedback_loop: {e}")
        return None

    try:
        analyzer = BacktestAnalyzer(directory=str(snapshots_dir))
        if not analyzer.snapshots:
            return None

        # 收集有 T+7 数据的快照
        valid_snaps = []
        for snap in analyzer.snapshots:
            if snap.actual_price_t7 is not None and snap.entry_price > 0:
                valid_snaps.append(snap)

        if len(valid_snaps) < MIN_SAMPLES:
            return None

        # 时间衰减权重：exp(-(today - date) / 30)
        today = datetime.now()
        time_weights = []
        for snap in valid_snaps:
            try:
                snap_date = datetime.strptime(snap.date, "%Y-%m-%d")
                days_ago = (today - snap_date).days
                tw = math.exp(-days_ago / 30.0)
            except (ValueError, TypeError):
                tw = 0.5
            time_weights.append(tw)

        # 标准化时间权重
        tw_sum = sum(time_weights)
        if tw_sum > 0:
            time_weights = [w / tw_sum * len(time_weights) for w in time_weights]

        # 修复 Bug #7：按"维度内 Agent 平均准确度"算权重，而非"Agent 数累加"
        # 旧实现下 signal 维度（Scout+Rival 两蜂）比单蜂维度永远高一倍，结构性偏差
        # 新实现：先按 (维度, 快照) 聚合，取维度内所有 Agent 的平均准确度
        dim_weighted_accuracy = {dim: 0.0 for dim in DEFAULT_WEIGHTS}
        dim_weighted_count = {dim: 0.0 for dim in DEFAULT_WEIGHTS}

        for i, snap in enumerate(valid_snaps):
            tw = time_weights[i]
            ret_t7 = (snap.actual_price_t7 - snap.entry_price) / snap.entry_price * 100

            # v0.42.2 修复：用 agent_vote_correct（蜂自己的票 vs 实际涨跌）替代
            # 「快照 direction 推出 is_correct，再拿去判每只蜂」的旧逻辑。
            # 旧逻辑对 neutral 快照（实测占 32%）恒判「vote<=5 即正确」，与价格无关。
            per_dim_acc = {dim: [] for dim in DEFAULT_WEIGHTS}
            for agent_name, vote in snap.agent_votes.items():
                dim = AGENT_TO_DIM.get(agent_name)
                if dim is None:
                    continue
                ok = agent_vote_correct(vote, ret_t7)
                if ok is None:
                    continue  # 弃权票不计入分母
                per_dim_acc[dim].append(1.0 if ok else 0.0)

            for dim, accs in per_dim_acc.items():
                if not accs:
                    continue
                dim_weighted_accuracy[dim] += tw * (sum(accs) / len(accs))
                dim_weighted_count[dim] += tw

        # 归一化为权重
        raw_weights = {}
        for dim in DEFAULT_WEIGHTS:
            if dim_weighted_count[dim] > 0:
                raw_weights[dim] = dim_weighted_accuracy[dim] / dim_weighted_count[dim]
            else:
                raw_weights[dim] = DEFAULT_WEIGHTS[dim]

        # 归一化
        total = sum(raw_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in raw_weights.items()}
        else:
            new_weights = dict(DEFAULT_WEIGHTS)

        # 升级5: clamp 每个维度的权重到安全范围（复用共享辅助函数）
        clamped_weights = _apply_weight_clamps(new_weights)
        any_clamped = any(
            abs(clamped_weights.get(k, 0) - new_weights.get(k, 0)) > 1e-9
            for k in new_weights
        )

        return {
            "new_weights": clamped_weights,
            "method": "wls_time_decay",
            "valid_samples": len(valid_snaps),
            "clamped": any_clamped,
        }
    except Exception as e:
        print(f"❌ WLS 计算失败，回退标准方法: {e}")
        return None


def bootstrap_validate(snapshots_dir: Path, new_weights: dict,
                       n_iterations: int = 500) -> dict:
    """
    Bootstrap 验证：重采样历史准确率 N 次，检验权重变动的稳健性

    Returns:
        {
            "stable": bool,          # 权重是否稳健
            "confidence_95": dict,   # 每个维度的95%置信区间
            "median_weights": dict,  # 中位数权重
        }
    """
    try:
        sys.path.insert(0, str(ALPHAHIVE_DIR))
        from feedback_loop import BacktestAnalyzer, agent_vote_correct
    except ImportError:
        return {"stable": False, "error": "无法导入 feedback_loop"}

    try:
        analyzer = BacktestAnalyzer(directory=str(snapshots_dir))
        valid_snaps = [s for s in analyzer.snapshots
                       if s.actual_price_t7 is not None and s.entry_price > 0]

        if len(valid_snaps) < MIN_SAMPLES:
            return {"stable": False, "error": f"样本不足 ({len(valid_snaps)} < {MIN_SAMPLES})"}

        # Bootstrap: 重采样 N 次
        weight_samples = {dim: [] for dim in DEFAULT_WEIGHTS}

        for _ in range(n_iterations):
            # 有放回抽样
            sample = random.choices(valid_snaps, k=len(valid_snaps))

            # 修复 Bug #7：使用统一 AGENT_TO_DIM + 维度内平均（非累加）
            dim_snap_acc = {dim: 0.0 for dim in DEFAULT_WEIGHTS}
            dim_snap_count = {dim: 0 for dim in DEFAULT_WEIGHTS}

            for snap in sample:
                ret_t7 = (snap.actual_price_t7 - snap.entry_price) / snap.entry_price * 100

                # v0.42.2 修复：与主路径共用 agent_vote_correct，消除记分逻辑重复
                per_dim = {dim: [] for dim in DEFAULT_WEIGHTS}
                for agent_name, vote in snap.agent_votes.items():
                    dim = AGENT_TO_DIM.get(agent_name)
                    if dim is None:
                        continue
                    ok = agent_vote_correct(vote, ret_t7)
                    if ok is None:
                        continue  # 弃权票不计入分母
                    per_dim[dim].append(1.0 if ok else 0.0)
                for dim, accs in per_dim.items():
                    if accs:
                        dim_snap_acc[dim] += sum(accs) / len(accs)
                        dim_snap_count[dim] += 1

            # 保持旧变量名供下游计算
            dim_correct = dim_snap_acc
            dim_total = dim_snap_count

            # 计算这次抽样的权重
            raw = {}
            for dim in DEFAULT_WEIGHTS:
                if dim_total[dim] > 0:
                    raw[dim] = dim_correct[dim] / dim_total[dim]
                else:
                    raw[dim] = DEFAULT_WEIGHTS[dim]
            total = sum(raw.values())
            if total > 0:
                for dim in raw:
                    weight_samples[dim].append(raw[dim] / total)

        # 计算 95% 置信区间
        confidence = {}
        median_weights = {}
        for dim in DEFAULT_WEIGHTS:
            sorted_w = sorted(weight_samples[dim])
            n = len(sorted_w)
            lo_idx = int(n * 0.025)
            hi_idx = int(n * 0.975)
            confidence[dim] = {
                "lo_95": round(sorted_w[lo_idx], 4),
                "hi_95": round(sorted_w[hi_idx], 4),
                "range_pp": round((sorted_w[hi_idx] - sorted_w[lo_idx]) * 100, 1),
            }
            median_weights[dim] = round(sorted_w[n // 2], 4)

        # 判断稳健性：如果新权重在所有维度的 95% CI 内，则稳健
        stable = all(
            confidence[dim]["lo_95"] <= new_weights.get(dim, DEFAULT_WEIGHTS[dim]) <= confidence[dim]["hi_95"]
            for dim in DEFAULT_WEIGHTS
        )

        return {
            "stable": stable,
            "confidence_95": confidence,
            "median_weights": median_weights,
            "n_iterations": n_iterations,
        }
    except Exception as e:
        return {"stable": False, "error": str(e)}


def clamp_shifts(old_weights: dict, new_weights: dict) -> dict:
    """
    把 new_weights 投影到「WEIGHT_CLAMPS ∩ 距 old_weights 不超过 ±MAX_SHIFT_PP ∩ sum=1」

    v0.42.6 重写。旧实现是「逐维钳幅 → 重新归一化」，两步互相破坏：
    归一化是乘性缩放，必然把已钳到 ±MAX_SHIFT_PP 边界的值再推出去。
    历史实证突破：`risk_adj +10.72pp`、`signal −11.32pp`（均 > MAX_SHIFT_PP=10.0），
    以及 catalyst 落到 0.3316（> WEIGHT_CLAMPS 上限 0.25，因为绝对上下限在
    归一化之前施加、之后不再复查）。

    新实现把两类约束合并成单一盒后**单次投影**，三个不变式同时成立。

    Raises:
        InfeasibleBoundsError: 合并后的盒与 sum=1 无交集。调用方应据此拒绝写入。
    """
    bounds = merge_bounds(old_weights, MAX_SHIFT_PP, WEIGHT_CLAMPS)
    projected = project_to_feasible(new_weights, bounds)

    # 舍入到 6 位会引入至多 ~n×5e-7 的和偏差。把残差吸收进**余量最大**的维度，
    # 保证 sum=1 精确成立且不越界（直接返回舍入结果会让和轻微偏离 1.0）。
    rounded = {k: round(v, 6) for k, v in projected.items()}
    residual = 1.0 - sum(rounded.values())
    if abs(residual) > 1e-12:
        # 余量 = 该维度朝残差方向还能移动多少
        def slack(k):
            lo, hi = bounds.get(k, (0.0, 1.0))
            return (hi - rounded[k]) if residual > 0 else (rounded[k] - lo)
        target_k = max(rounded, key=slack)
        if slack(target_k) >= abs(residual):
            rounded[target_k] = round(rounded[target_k] + residual, 12)

    assert_feasible(rounded, bounds, tol=1e-5)
    return rounded


def has_significant_change(old: dict, new: dict, threshold_pp: float) -> bool:
    """检查是否有任一维度变化 >= threshold_pp（单位：百分点）"""
    for k in old:
        if abs(new.get(k, old[k]) - old[k]) * 100 >= threshold_pp:
            return True
    return False


def read_current_weights() -> dict:
    """从 config.py 读取当前 EVALUATION_WEIGHTS"""
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        # 匹配整个 EVALUATION_WEIGHTS = { ... } 块（多行）
        m = re.search(
            r'EVALUATION_WEIGHTS\s*=\s*\{([^}]+)\}',
            text, re.DOTALL
        )
        if not m:
            return dict(DEFAULT_WEIGHTS)
        block = m.group(1)
        weights = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 匹配 "signal": 0.30, 或 "signal": 0.30
            km = re.match(r'"(\w+)"\s*:\s*([0-9.]+)', line)
            if km:
                weights[km.group(1)] = float(km.group(2))
        return weights if len(weights) == 5 else dict(DEFAULT_WEIGHTS)
    except Exception as e:
        print(f"⚠️  读取 config.py 失败，使用默认权重: {e}")
        return dict(DEFAULT_WEIGHTS)


BACKUP_DIR      = ALPHAHIVE_DIR / "weight_backups"
BACKUP_LATEST   = ALPHAHIVE_DIR / "config.py.weights.bak"
BACKUP_KEEP_N   = 8


def _backup_config() -> bool:
    """
    写入 config.py 之前备份（v0.42.2）

    两份：
      • config.py.weights.bak            — 单槽最新，便于人工 `cp` 回滚
      • weight_backups/config_<ts>.py    — 时间戳滚动，保留最近 BACKUP_KEEP_N 份

    必须在 tmp.replace(CONFIG_PATH) **之前**调用。
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        BACKUP_LATEST.write_text(text, encoding="utf-8")

        BACKUP_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (BACKUP_DIR / f"config_{ts}.py").write_text(text, encoding="utf-8")

        # 滚动清理：只保留最近 N 份
        snaps = sorted(BACKUP_DIR.glob("config_*.py"))
        for old in snaps[:-BACKUP_KEEP_N]:
            try:
                old.unlink()
            except OSError:
                pass
        return True
    except Exception as e:
        print(f"⚠️  备份 config.py 失败: {e}")
        return False


def _restore_config_from_backup() -> bool:
    """从 config.py.weights.bak 还原（回读校验失败时的逃生舱）"""
    try:
        if not BACKUP_LATEST.exists():
            return False
        tmp = CONFIG_PATH.with_suffix(".py.restore-tmp")
        tmp.write_text(BACKUP_LATEST.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        return True
    except Exception as e:
        print(f"⚠️  从备份还原失败: {e}")
        return False


def write_weights_to_config(new_weights: dict, dry_run: bool = False) -> bool:
    """
    将新权重写回 config.py 的 EVALUATION_WEIGHTS 块。
    保留所有注释，只替换数值。

    v0.42.2 增加三道安全网：语法预检 → 备份 → 回读校验（失败自动还原）。
    dry_run=True 时返回 False（预览不等于写入，见函数内注释）。
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")

        # 构建新的权重块（保留注释结构）
        dim_comments = {
            "signal":    "# ScoutBeeNova: SEC 披露 + 聪明钱 + 拥挤度",
            "catalyst":  "# ChronosBeeHorizon: 催化剂与时间线",
            "sentiment": "# BuzzBeeWhisper: 情绪与叙事",
            "odds":      "# OracleBeeEcho: 期权 IV(55%) + Polymarket(35%) + 异动(10%)",
            "risk_adj":  "# GuardBeeSentinel: 交叉验证 + 风险调整",
        }
        lines = ["EVALUATION_WEIGHTS = {"]
        for k in ["signal", "catalyst", "sentiment", "odds", "risk_adj"]:
            v = new_weights.get(k, DEFAULT_WEIGHTS[k])
            comment = dim_comments.get(k, "")
            lines.append(f'    "{k}":    {v:.4f},   {comment}')
        lines.append("    # ml_auxiliary: 不在此处（RivalBeeVanguard 作为 ±0.5 独立调整项）")
        lines.append("}")
        new_block = "\n".join(lines)

        # 替换原有块（贪婪匹配到第一个独立 }）
        new_text = re.sub(
            r'EVALUATION_WEIGHTS\s*=\s*\{[^}]+\}',
            new_block,
            text,
            flags=re.DOTALL
        )

        if new_text == text:
            print("⚠️  config.py 替换前后内容相同，未写入")
            return False

        if dry_run:
            print("🔍 [Dry-run] 新权重块预览：")
            print(new_block)
            # v0.42.2 修复：dry-run 必须返回 False。
            # 旧实现 return True 会让 main() 把 applied 记成 True，导致
            # weight_history.jsonl 出现 {"dry_run": true, "applied": true} 的
            # 矛盾记录（2026-05-10 那条），而 health_check.py 正在读这个字段。
            return False

        # ── v0.42.2 安全网 ①：写入前语法预检 ────────────────────────────────
        # 语法错误的 config.py 会瘫痪整个系统（所有模块都 import config）。
        # 必须在覆盖原文件之前发现，而不是之后。
        try:
            compile(new_text, str(CONFIG_PATH), "exec")
        except SyntaxError as e:
            print(f"❌ 生成的 config.py 语法错误（已中止写入）: {e}")
            return False

        # ── v0.42.2 安全网 ②：写入前备份 ──────────────────────────────────
        # 必须在 tmp.replace() 之前完成，否则备份到的是新内容。
        _backup_config()

        # 原子写入（先写临时文件再 rename）
        tmp = CONFIG_PATH.with_suffix(".py.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(CONFIG_PATH)

        # ── v0.42.2 安全网 ③：写入后回读校验 ──────────────────────────────
        # regex 替换可能损坏文件而不报错（如反向引用被解释）。回读比对是
        # 唯一能确认"落盘的确实是我要写的值"的方法。
        try:
            written = read_current_weights()
            mismatch = [
                k for k in ["signal", "catalyst", "sentiment", "odds", "risk_adj"]
                if abs(written.get(k, -1) - round(new_weights.get(k, DEFAULT_WEIGHTS[k]), 4)) > 1e-4
            ]
            if mismatch:
                print(f"❌ 回读校验失败，维度不符: {mismatch} — 正在从备份还原")
                if _restore_config_from_backup():
                    print("   ✅ 已还原到写入前状态")
                else:
                    print("   ⚠️  自动还原失败！请手动检查 config.py 与 weight_backups/")
                return False
        except Exception as e:
            print(f"❌ 回读校验异常: {e} — 正在从备份还原")
            _restore_config_from_backup()
            return False

        return True

    except Exception as e:
        print(f"❌ 写入 config.py 失败: {e}")
        return False


def check_ticker_pool_consistency(snapshots_dir: Path,
                                  recent_days: int = 3,
                                  max_unrepresented: float = 0.20) -> dict:
    """闸 2：样本基的标的池必须能代表**当前**在扫的标的池。

    为什么需要这道闸
    ----------------
    权重是在某个横截面上学出来的，而横截面宽度一变，最优加权就不是同一个问题。
    v0.42.9 把标的池从 10 只扩到 30 只（N_eff 3.18 → 12.27，见
    `experiments/ic_power_report.md`），但 T+7 要 7 个交易日才到期 —— 于是存在
    一个长达数周的窗口：**优化器眼里的世界还是旧的 10 只，却要去改现在 30 只
    在用的权重。**

    2026-08-16 实测正是此状态：665 个 T+7 样本跨度 2026-03-09→07-29，
    30 只时代（≥08-10）贡献 **0 条**。而优化器当天恰好越过 MIN_CHANGE_PP
    要开火（risk_adj −4.13pp）。

    判据
    ----
    比较「最近 recent_days 个扫描日出现过的标的」与「进入 T+7 样本基的标的」。
    当前池里未被样本基覆盖的比例 > max_unrepresented 即拦下。

    Returns:
        {ok, unrepresented_ratio, recent_pool, sample_pool, missing, reason}
    """
    try:
        sys.path.insert(0, str(ALPHAHIVE_DIR))
        from feedback_loop import BacktestAnalyzer
    except ImportError as e:
        # 拿不到就**不放行**——闸的默认态是关，不是开
        return {"ok": False, "reason": f"无法导入 feedback_loop: {e}"}

    try:
        analyzer = BacktestAnalyzer(directory=str(snapshots_dir))
    except Exception as e:  # noqa: BLE001 - 任何读取失败都按"判不了"处理
        return {"ok": False, "reason": f"BacktestAnalyzer 失败: {e}"}

    snaps = getattr(analyzer, "snapshots", None) or []
    if not snaps:
        return {"ok": False, "reason": "快照为空"}

    # 样本基 = 真正参与优化的那些（有 T+7 且入场价有效），与
    # compute_new_weights_wls 的筛选条件保持一致
    sample_pool = {
        s.ticker for s in snaps
        if getattr(s, "actual_price_t7", None) is not None
        and getattr(s, "entry_price", 0) > 0
        and getattr(s, "ticker", None)
    }
    if not sample_pool:
        return {"ok": False, "reason": "样本基里没有可用标的"}

    dates = sorted({s.date for s in snaps if getattr(s, "date", None)})
    if not dates:
        return {"ok": False, "reason": "快照缺少日期"}
    recent_dates = set(dates[-recent_days:])
    recent_pool = {s.ticker for s in snaps
                   if getattr(s, "date", None) in recent_dates
                   and getattr(s, "ticker", None)}
    if not recent_pool:
        return {"ok": False, "reason": "最近扫描日没有标的"}

    missing = sorted(recent_pool - sample_pool)
    ratio = len(missing) / len(recent_pool)
    ok = ratio <= max_unrepresented
    return {
        "ok": ok,
        "unrepresented_ratio": ratio,
        "n_recent_pool": len(recent_pool),
        "n_sample_pool": len(sample_pool),
        "missing": missing,
        "recent_dates": sorted(recent_dates),
        "reason": None if ok else (
            f"当前池 {len(recent_pool)} 只里有 {len(missing)} 只"
            f"（{ratio:.0%}）从未进入 T+7 样本基"
        ),
    }


def append_history(old_weights: dict, new_weights: dict,
                   n_samples: int, dry_run: bool,
                   method: Optional[str] = None,
                   clamped: Optional[bool] = None,
                   bootstrap_stable: Optional[bool] = None,
                   applied: Optional[bool] = None,
                   skip_reason: Optional[str] = None,
                   action: str = "optimize") -> None:
    """追加一条记录到 weight_history.jsonl

    v0.23.7：增加 method / clamped / bootstrap_stable / applied 字段
    v0.42.2：增加 schema_version / skip_reason / action
      • skip_reason 把"为什么这次没写入"结构化。旧实现里"无显著变化"的运行
        **完全不记录**（main 的 `if significant or dry_run` 分支），导致
        weight_history.jsonl 自 2026-05-10 起空白 11 周，看起来像任务挂了，
        实际是每次都在静默跳过。沉默必须可观测。
      • schema_version=2 用于区分新旧记录：v1 记录里 dry_run=true 可能伴随
        applied=true（旧 write_weights_to_config 的 dry-run 分支返回 True），
        这类记录不可信。历史记录不改写（审计轨迹不可篡改），由消费方按
        schema_version 甄别。
    """
    record = {
        "timestamp":  datetime.now().isoformat(),
        "schema_version": 2,
        "action":     action,                        # "optimize" | "rollback"
        "dry_run":    dry_run,
        "applied":    applied,                       # 是否真正写入 config.py
        "skip_reason": skip_reason,                  # 未写入的结构化原因；写入成功时为 None
        "method":     method,                        # "wls_time_decay" 或 "standard"
        "clamped":    clamped,                       # _apply_weight_clamps 是否调整了值
        "bootstrap_stable": bootstrap_stable,         # Bootstrap CI 是否覆盖新权重
        "n_samples":  n_samples,
        "old_weights": old_weights,
        "new_weights": new_weights,
        "changes_pp": {
            k: round((new_weights.get(k, old_weights[k]) - old_weights[k]) * 100, 2)
            for k in old_weights
        },
    }
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️  历史记录写入失败（不影响主流程）: {e}")


def print_summary(old: dict, new: dict, n_samples: int,
                  applied: bool, dry_run: bool) -> None:
    """打印优化摘要表"""
    tag = "[Dry-run] " if dry_run else ""
    status = "✅ 已写入 config.py" if applied and not dry_run else (
             "🔍 预览（未写入）" if dry_run else "⏭  无需调整")

    print(f"\n{'━'*52}")
    print(f"🐝 Alpha Hive · 周度权重优化报告  {tag}{status}")
    print(f"{'━'*52}")
    print(f"{'维度':<12} {'旧权重':>8} {'新权重':>8} {'变化':>8}  方向")
    print(f"{'─'*52}")
    for k in ["signal", "catalyst", "sentiment", "odds", "risk_adj"]:
        old_v = old.get(k, DEFAULT_WEIGHTS[k])
        new_v = new.get(k, old_v)
        diff  = (new_v - old_v) * 100
        arrow = "↑" if diff > 0.05 else ("↓" if diff < -0.05 else "→")
        flag  = " ⚡" if abs(diff) >= MIN_CHANGE_PP else ""
        print(f"  {k:<10} {old_v:>7.2%} {new_v:>7.2%} {diff:>+7.1f}pp  {arrow}{flag}")
    print(f"{'─'*52}")
    print(f"  样本数（T+7已回填）: {n_samples}")
    print(f"  触发阈值: 任一维度变化 ≥ {MIN_CHANGE_PP}pp")
    print(f"  单次限幅: ±{MAX_SHIFT_PP}pp")
    print(f"{'━'*52}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def _last_applied_record() -> Optional[dict]:
    """读 weight_history.jsonl 里最后一条真正写入过的记录（v0.42.2）

    只认 action=="optimize" 且 applied 为真且非 dry_run 的记录。
    schema_version < 2 的旧记录里 dry_run=true 可能伴随 applied=true（旧
    dry-run 分支返回 True 的产物），这类记录不可信，直接排除。
    """
    if not HISTORY_FILE.exists():
        return None
    last = None
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action", "optimize") != "optimize":
                    continue
                if not rec.get("applied"):
                    continue
                if rec.get("dry_run"):
                    continue  # 排除旧 schema 的矛盾记录
                last = rec
    except OSError as e:
        print(f"⚠️  读取 {HISTORY_FILE.name} 失败: {e}")
        return None
    return last


def do_rollback(dry_run: bool = False, use_backup: bool = False) -> int:
    """
    回滚权重到上一次写入前的状态（v0.42.2）

    默认从审计日志重建（只改 EVALUATION_WEIGHTS），**不做整文件还原** ——
    config.py 有 1400+ 行，两次 optimizer 运行之间用户可能改过别的配置项，
    整文件回滚会连带撤销无关改动。
    `--to-backup` 是逃生舱：直接用 config.py.weights.bak 整文件还原。

    回滚同样走 write_weights_to_config（语法预检 + 备份 + 回读校验），不开后门。
    """
    if use_backup:
        print(f"↩️  逃生舱模式：从 {BACKUP_LATEST.name} 整文件还原")
        if not BACKUP_LATEST.exists():
            print(f"❌ 备份不存在: {BACKUP_LATEST}")
            return 1
        if dry_run:
            print("🔍 [Dry-run] 将整文件还原，未执行")
            return 0
        ok = _restore_config_from_backup()
        print("✅ 已还原" if ok else "❌ 还原失败")
        return 0 if ok else 1

    rec = _last_applied_record()
    if rec is None:
        print("⏭  weight_history.jsonl 里没有可回滚的写入记录（schema_version>=2 且 applied）。")
        print("   如需强制还原整个 config.py，用: --rollback --to-backup")
        return 1

    target = rec.get("old_weights") or {}
    if not target:
        print("❌ 该记录缺少 old_weights 字段，无法回滚。")
        return 1

    current = read_current_weights()
    print(f"↩️  回滚目标：{rec.get('timestamp')} 之前的权重")
    print(f"\n{'维度':<12}{'当前':>10}{'回滚到':>10}{'变动pp':>10}")
    for k in ["signal", "catalyst", "sentiment", "odds", "risk_adj"]:
        cur, tgt = current.get(k, 0.0), target.get(k, 0.0)
        print(f"{k:<12}{cur:>10.4f}{tgt:>10.4f}{(tgt - cur) * 100:>+10.2f}")

    if dry_run:
        print("\n🔍 [Dry-run] 未执行回滚")
        return 0

    ok = write_weights_to_config(target, dry_run=False)
    append_history(
        current, target, n_samples=rec.get("n_samples", 0),
        dry_run=False, method="rollback", applied=ok,
        skip_reason=None if ok else "rollback_write_failed",
        action="rollback",
    )
    print("\n✅ 回滚完成" if ok else "\n❌ 回滚写入失败")
    if ok:
        print("   ⚠️  长驻进程需重启才能读到新权重（模块级 import 有缓存）")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha Hive · 周度权重自动优化器（轨道 A）"
    )
    parser.add_argument("--apply",        action="store_true",
                        help=(
                            "显式请求写入 config.py。**不给此参数则只做诊断**"
                            "（v0.44.0 起默认只读）。即便给了，两道闸全过才写。"
                        ))
    parser.add_argument("--dry-run",      action="store_true",
                        help="（已成为默认行为，保留仅为向后兼容）只分析，不写 config.py")
    parser.add_argument("--force",        action="store_true",
                        help=(
                            "配合 --apply：无视闸门强行写入。"
                            "**除非你已读过 experiments/ic_power_report.md "
                            "并明确知道自己在做什么，否则不要用。**"
                        ))
    parser.add_argument("--min-samples",  type=int, default=MIN_SAMPLES,
                        help=f"最少样本数（默认 {MIN_SAMPLES}）")
    parser.add_argument("--min-change",   type=float, default=MIN_CHANGE_PP,
                        help=f"最小变化触发阈值 pp（默认 {MIN_CHANGE_PP}）")
    parser.add_argument("--rollback",     action="store_true",
                        help="回滚到上一次写入前的权重（从审计日志重建）")
    parser.add_argument("--to-backup",    action="store_true",
                        help="配合 --rollback：改为用 config.py.weights.bak 整文件还原")
    args = parser.parse_args()

    if args.rollback:
        sys.exit(do_rollback(dry_run=args.dry_run, use_backup=args.to_backup))

    # v0.44.0：默认只读诊断。写入必须显式 --apply，且两道闸全过。
    #
    # 为什么反转默认值（opt-out → opt-in）：
    #   本项目已有同款先例。2026-03-16 `generate_deep_v2.py` 的 opt-out 设计
    #   让 NVDA 深度报告静默消费了 $0.47 Opus，之后改为 opt-in。这里的代价
    #   不是钱而是**样本世代**：每次写 config.py 都让此前所有样本变成在旧权重
    #   下产生的、不可比的一代，把「扩池换来的 5.18× 测量加速」周期性清零。
    #
    # 为什么本来就该只读（三条独立理由，任一条都够）：
    #   1. `w = acc/Σacc` 数学上无法表达"这个维度没用"——准确率都挤在 0.5
    #      附近，权重必然全部 ≈0.2，输出空间里不存在"归零"这个答案。
    #   2. 它优化的对象已被证明不存在：综合分 |IC|=0.090 打不过 20 日动量
    #      0.135；五维没有一个经得起全部保守口径。
    #   3. `compute_new_weights_wls` 名不副实：docstring 称 OLS 回归取 beta，
    #      实现里没有任何回归。它从未做过它声称的事，却已真实改写过 config 两次。
    write_requested = args.apply and not args.dry_run
    mode_label = "写入模式 (--apply)" if write_requested else "只读诊断（默认）"

    print(f"\n🐝 Alpha Hive · weekly_optimizer 启动 — {mode_label}")
    print(f"   快照目录: {SNAPSHOTS_DIR}")
    print(f"   config:   {CONFIG_PATH}")
    if args.apply and args.dry_run:
        print("   ⚠️  同时给了 --apply 与 --dry-run，按 --dry-run 处理（不写入）")
    if not write_requested:
        print("   ℹ️  本次不会修改 config.py。要写入需显式 --apply。")

    # 1. 检查快照目录
    if not SNAPSHOTS_DIR.exists():
        print("⏭  report_snapshots/ 不存在，尚无历史数据，跳过。")
        return

    # 2. 计算有效样本数
    n_samples = count_t7_samples(SNAPSHOTS_DIR)
    print(f"   T+7 已回填样本: {n_samples} 条")

    if n_samples < args.min_samples:
        print(f"⏭  样本不足（{n_samples} < {args.min_samples}），本次不调整权重。")
        print(f"   继续积累数据，达到 {args.min_samples} 条后自动激活。\n")
        return

    # 3. 计算建议权重
    # 优先使用 WLS + 时间衰减，失败则回退标准方法
    print("🔍 运行 WLS 权重优化...")
    result = compute_new_weights_wls(SNAPSHOTS_DIR)
    if result is None:
        print("   WLS 不可用，回退标准方法...")
        result = compute_new_weights(SNAPSHOTS_DIR)
    if not result or "new_weights" not in result:
        print("⚠️  BacktestAnalyzer 未返回有效权重，跳过。")
        return

    # 4. 读取现有权重
    old_weights = read_current_weights()
    raw_new     = result["new_weights"]

    # 5. 投影到「WEIGHT_CLAMPS ∩ ±MAX_SHIFT_PP ∩ sum=1」（v0.42.6 合并盒单次投影）
    try:
        new_weights = clamp_shifts(old_weights, raw_new)
    except InfeasibleBoundsError as e:
        # 约束无解 = 配置矛盾。拒绝写入并留审计，绝不静默降级写越界权重。
        print(f"\n🛑 权重约束无可行解，拒绝写入：{e}")
        _log.error("clamp_shifts 不可行，跳过本次写入: %s", e)
        append_history(
            old_weights, old_weights, n_samples,
            dry_run=args.dry_run, method=result.get("method"),
            applied=False, skip_reason="infeasible_bounds",
        )
        return

    # 6. 闸 1：Bootstrap 稳健性
    #
    # v0.44.0 之前这里**只打印不阻断**——原文是
    #   `if not args.dry_run: print("继续应用（限幅已保护），建议关注下周数据")`
    # 2026-08-16 实测到了它的真实后果：该轮 bootstrap 报"不稳健"，
    # 而 risk_adj −4.13pp 已越过 MIN_CHANGE_PP，若不是人工中断就会写进 config.py。
    # "限幅已保护"是个安慰性说法：限幅只保证**幅度**不失控，不保证**方向**对。
    print("🔍 闸 1/2：Bootstrap 稳健性验证...")
    bootstrap = bootstrap_validate(SNAPSHOTS_DIR, new_weights)
    gate_bootstrap_ok = bool(bootstrap.get("stable"))
    if gate_bootstrap_ok:
        print("   ✅ 通过：权重变动在 95% 置信区间内")
    else:
        print(f"   🛑 未通过：{bootstrap.get('error', '权重可能不稳健')}")

    # 7. 闸 2：样本基的标的池必须代表当前在扫的池
    print("🔍 闸 2/2：标的池世代一致性...")
    pool = check_ticker_pool_consistency(SNAPSHOTS_DIR)
    gate_pool_ok = bool(pool.get("ok"))
    if gate_pool_ok:
        print(f"   ✅ 通过：当前池 {pool.get('n_recent_pool')} 只，"
              f"未被样本覆盖 {pool.get('unrepresented_ratio', 0):.0%}")
    else:
        print(f"   🛑 未通过：{pool.get('reason')}")
        if pool.get("missing"):
            shown = ", ".join(pool["missing"][:12])
            more = "…" if len(pool["missing"]) > 12 else ""
            print(f"      未进入样本基的标的: {shown}{more}")

    gates_ok = gate_bootstrap_ok and gate_pool_ok

    # 8. 检查是否有显著变化
    significant = has_significant_change(old_weights, new_weights, args.min_change)

    # v0.42.2：无论是否写入都记审计。旧实现只在 `significant or dry_run` 时记录，
    # 于是"每周跑、每周都无显著变化"表现为日志完全空白（2026-05-10 之后 11 周），
    # 与"任务挂了"无法区分。现在跳过也留痕，附结构化 skip_reason。
    # 9. 写入决策。优先级：只读默认 > 闸门 > 显著性
    applied = False
    skip_reason = None
    if not write_requested:
        # 默认路径：只读诊断。仍然预览新权重块（write_weights_to_config 的
        # dry_run 分支只打印不落盘），并留审计轨迹。
        write_weights_to_config(new_weights, dry_run=True)
        skip_reason = "read_only_default"
    elif not gates_ok and not args.force:
        reasons = []
        if not gate_bootstrap_ok:
            reasons.append("bootstrap_unstable")
        if not gate_pool_ok:
            reasons.append("stale_ticker_pool")
        skip_reason = "+".join(reasons)
        print(f"\n🛑 --apply 已请求写入，但闸门未通过（{skip_reason}），拒绝写入。")
        print("   要覆盖需 --apply --force，且请先读 experiments/ic_power_report.md。")
    elif not significant:
        skip_reason = "below_min_change"
    else:
        if args.force and not gates_ok:
            print("\n⚠️  --force：无视未通过的闸门强行写入。此举已记入审计日志。")
        applied = write_weights_to_config(new_weights, dry_run=False)
        if not applied:
            skip_reason = "write_failed"

    append_history(
        old_weights, new_weights, n_samples,
        dry_run=not write_requested,
        method=result.get("method"),
        clamped=result.get("clamped"),
        bootstrap_stable=bootstrap.get("stable"),
        applied=applied,
        skip_reason=skip_reason,
        action="optimize" if write_requested else "diagnose",
    )

    # 10. 打印摘要
    print_summary(old_weights, new_weights, n_samples, applied,
                  dry_run=not write_requested)

    print("  闸门: "
          f"bootstrap {'✅' if gate_bootstrap_ok else '🛑'}  "
          f"标的池世代 {'✅' if gate_pool_ok else '🛑'}")
    if not write_requested:
        print("  模式: 只读诊断 — config.py 未被修改（写入需 --apply）")
    print()

    if significant and not write_requested:
        print(f"ℹ️  有维度变化 ≥ {args.min_change}pp，但当前为只读模式，未写入。")
        if not gates_ok:
            print("   （即便加 --apply 也会被闸门拦下）")
    elif not significant:
        print(f"⏭  所有维度变化 < {args.min_change}pp，权重保持不变（系统稳定）。")
    print()


if __name__ == "__main__":
    main()
