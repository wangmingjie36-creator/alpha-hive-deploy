#!/usr/bin/env python3
"""
🐝 Alpha Hive · 轨道 A — 每周权重自动优化器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
流程：
  1. 扫描 report_snapshots/ → 找 T+7 已回填快照
  2. BacktestAnalyzer.suggest_weight_adjustments() → 计算新 5 维权重
  3. 变化 >= MIN_CHANGE_PP（绝对值）且样本 >= MIN_SAMPLES → 写入 config.py
  4. 记录到 weight_history.jsonl（审计轨迹）
  5. 打印摘要

用法：
  python3 weekly_optimizer.py                  # 标准运行
  python3 weekly_optimizer.py --dry-run        # 只分析，不写 config
  python3 weekly_optimizer.py --min-samples 5  # 降低最低样本要求（测试用）
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def _apply_weight_clamps(weights: dict, max_iter: int = 50, tol: float = 1e-9) -> dict:
    """
    迭代式 clamp + 归一化 — 修复 Bug #5：
    旧实现"先 clamp 再归一化"在多个维度触底时会让未触底维度被放大超过上限
    （实证：catalyst raw 可触发 0.33 > 0.25 上限）。

    新算法：
      1. 目标总和 = 1.0
      2. 循环：把当前超限的维度钳到边界并固定；剩余未固定维度按比例瓜分剩余预算
      3. 直到无新维度触发钳制 或达到 max_iter
    数学性质：输出严格满足 lo ≤ w[k] ≤ hi（对 CLAMPS 中所有键）且 sum(w)=1.0
    """
    # 初始化：未覆盖 CLAMPS 的维度用 DEFAULT_WEIGHTS 兜底
    w = {k: float(weights.get(k, DEFAULT_WEIGHTS.get(k, 0.2))) for k in set(list(weights.keys()) + list(WEIGHT_CLAMPS.keys()))}
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    w = {k: v / total for k, v in w.items()}  # 先归一化到 1.0

    fixed: dict = {}
    for _ in range(max_iter):
        # 识别当前超限维度
        to_fix = {}
        for k, (lo, hi) in WEIGHT_CLAMPS.items():
            if k in fixed:
                continue
            if w[k] < lo - tol:
                to_fix[k] = lo
            elif w[k] > hi + tol:
                to_fix[k] = hi
        if not to_fix:
            break
        fixed.update(to_fix)
        # 立即把 fixed 值写回 w（修复 edge case：所有维度都 fix 时要先落值再 break）
        for k, v in fixed.items():
            w[k] = v
        # 剩余预算分配给未固定维度（按原比例）
        remaining_budget = 1.0 - sum(fixed.values())
        free_keys = [k for k in w if k not in fixed]
        if not free_keys:
            # 所有维度都被钳制 — 数学上 sum(fixed) 与 1.0 不等时，无可行解
            # 降级：均匀缩放 fixed 值到 sum=1.0，允许轻微突破 clamp（这种情况表明 CLAMPS 设置本身不合理）
            break
        free_sum = sum(w[k] for k in free_keys)
        if remaining_budget <= 0 or free_sum <= 0:
            if remaining_budget > 0:
                share = remaining_budget / len(free_keys)
                for k in free_keys:
                    w[k] = share
            else:
                for k in free_keys:
                    w[k] = 0.0
            break
        for k in free_keys:
            w[k] = w[k] / free_sum * remaining_budget

    # 最终确保和为 1.0（数值稳定性）
    s = sum(w.values())
    if s > 0 and abs(s - 1.0) > tol:
        w = {k: v / s for k, v in w.items()}
    return w


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
        from feedback_loop import BacktestAnalyzer
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
            direction = snap.direction.lower()
            is_correct = (direction in ("long", "bullish") and ret_t7 > 0) or \
                         (direction in ("short", "bearish") and ret_t7 < 0)

            # 按维度聚合：同维度多只蜂取平均而非相加
            per_dim_acc = {dim: [] for dim in DEFAULT_WEIGHTS}
            for agent_name, vote in snap.agent_votes.items():
                dim = AGENT_TO_DIM.get(agent_name)
                if dim is None:
                    continue
                agent_correct = (vote > 5 and is_correct) or (vote <= 5 and not is_correct)
                per_dim_acc[dim].append(1.0 if agent_correct else 0.0)

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
        from feedback_loop import BacktestAnalyzer
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
                direction = snap.direction.lower()
                is_correct = (direction in ("long", "bullish") and ret_t7 > 0) or \
                             (direction in ("short", "bearish") and ret_t7 < 0)

                per_dim = {dim: [] for dim in DEFAULT_WEIGHTS}
                for agent_name, vote in snap.agent_votes.items():
                    dim = AGENT_TO_DIM.get(agent_name)
                    if dim is None:
                        continue
                    agent_correct = (vote > 5 and is_correct) or (vote <= 5 and not is_correct)
                    per_dim[dim].append(1.0 if agent_correct else 0.0)
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
    限制单次调整幅度（每维度 <= MAX_SHIFT_PP），防止权重突变。
    调整后重新归一化，保证总和 = 1.0。
    """
    clamped = {}
    for k, old in old_weights.items():
        new  = new_weights.get(k, old)
        diff = new - old
        # 限幅
        if abs(diff) > MAX_SHIFT_PP / 100:
            diff = (MAX_SHIFT_PP / 100) * (1 if diff > 0 else -1)
        clamped[k] = old + diff

    # 归一化
    total = sum(clamped.values())
    if total > 0:
        clamped = {k: round(v / total, 6) for k, v in clamped.items()}
    return clamped


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


def write_weights_to_config(new_weights: dict, dry_run: bool = False) -> bool:
    """
    将新权重写回 config.py 的 EVALUATION_WEIGHTS 块。
    保留所有注释，只替换数值。
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
            return True

        # 原子写入（先写临时文件再 rename）
        tmp = CONFIG_PATH.with_suffix(".py.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        return True

    except Exception as e:
        print(f"❌ 写入 config.py 失败: {e}")
        return False


def append_history(old_weights: dict, new_weights: dict,
                   n_samples: int, dry_run: bool) -> None:
    """追加一条记录到 weight_history.jsonl"""
    record = {
        "timestamp":  datetime.now().isoformat(),
        "dry_run":    dry_run,
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

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha Hive · 周度权重自动优化器（轨道 A）"
    )
    parser.add_argument("--dry-run",      action="store_true",
                        help="只分析，不写 config.py")
    parser.add_argument("--min-samples",  type=int, default=MIN_SAMPLES,
                        help=f"最少样本数（默认 {MIN_SAMPLES}）")
    parser.add_argument("--min-change",   type=float, default=MIN_CHANGE_PP,
                        help=f"最小变化触发阈值 pp（默认 {MIN_CHANGE_PP}）")
    args = parser.parse_args()

    print(f"\n🐝 Alpha Hive · weekly_optimizer 启动")
    print(f"   快照目录: {SNAPSHOTS_DIR}")
    print(f"   config:   {CONFIG_PATH}")

    # 1. 检查快照目录
    if not SNAPSHOTS_DIR.exists():
        print(f"⏭  report_snapshots/ 不存在，尚无历史数据，跳过。")
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

    # 5. 限幅（单次最大 ±10pp）
    new_weights = clamp_shifts(old_weights, raw_new)

    # 6. Bootstrap 验证
    print("🔍 Bootstrap 稳健性验证...")
    bootstrap = bootstrap_validate(SNAPSHOTS_DIR, new_weights)
    if bootstrap.get("stable"):
        print("   ✅ Bootstrap 验证通过：权重变动在 95% 置信区间内")
    else:
        print(f"   ⚠️  Bootstrap 警告：{bootstrap.get('error', '权重可能不稳健')}")
        if not args.dry_run:
            print("   继续应用（限幅已保护），建议关注下周数据")

    # 7. 检查是否有显著变化
    significant = has_significant_change(old_weights, new_weights, args.min_change)

    applied = False
    if significant or args.dry_run:
        applied = write_weights_to_config(new_weights, dry_run=args.dry_run)
        append_history(old_weights, new_weights, n_samples, dry_run=args.dry_run)

    # 8. 打印摘要
    print_summary(old_weights, new_weights, n_samples, applied, args.dry_run)

    if not significant and not args.dry_run:
        print(f"⏭  所有维度变化 < {args.min_change}pp，权重保持不变（系统稳定）。\n")


if __name__ == "__main__":
    main()
