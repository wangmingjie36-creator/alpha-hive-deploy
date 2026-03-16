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
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 路径配置（与 generate_deep_v2.py 保持一致）─────────────────────────────
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
_VM_PATH = Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
if _VM_PATH.exists():
    ALPHAHIVE_DIR = _VM_PATH

_VM_DEEP_DIR = Path("/sessions/keen-magical-wright/mnt/深度分析报告/深度")
if _VM_DEEP_DIR.exists():
    OUTPUT_DIR = _VM_DEEP_DIR
else:
    OUTPUT_DIR = Path(os.path.expanduser("~/Desktop/深度分析报告/深度"))

CONFIG_PATH    = ALPHAHIVE_DIR / "config.py"
SNAPSHOTS_DIR  = OUTPUT_DIR / "report_snapshots"
HISTORY_FILE   = ALPHAHIVE_DIR / "weight_history.jsonl"

# ── 优化阈值 ──────────────────────────────────────────────────────────────────
MIN_SAMPLES    = 10    # 少于此样本数不调整权重
MIN_CHANGE_PP  = 3.0   # 权重变化绝对值 >= 3pp 才写入（防止噪声抖动）
MAX_SHIFT_PP   = 10.0  # 单次调整上限（每个维度最多 ±10pp）

# 5 维默认权重（与 config.py 保持一致，用于兜底）
DEFAULT_WEIGHTS = {
    "signal":    0.30,
    "catalyst":  0.20,
    "sentiment": 0.20,
    "odds":      0.15,
    "risk_adj":  0.15,
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


def compute_new_weights(snapshots_dir: Path) -> Optional[dict]:
    """
    调用 BacktestAnalyzer 计算建议权重。
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
        return result if result else None
    except Exception as e:
        print(f"❌ BacktestAnalyzer 运行失败: {e}")
        return None


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
    print("🔍 运行 BacktestAnalyzer...")
    result = compute_new_weights(SNAPSHOTS_DIR)
    if not result or "new_weights" not in result:
        print("⚠️  BacktestAnalyzer 未返回有效权重，跳过。")
        return

    # 4. 读取现有权重
    old_weights = read_current_weights()
    raw_new     = result["new_weights"]

    # 5. 限幅（单次最大 ±10pp）
    new_weights = clamp_shifts(old_weights, raw_new)

    # 6. 检查是否有显著变化
    significant = has_significant_change(old_weights, new_weights, args.min_change)

    applied = False
    if significant or args.dry_run:
        applied = write_weights_to_config(new_weights, dry_run=args.dry_run)
        append_history(old_weights, new_weights, n_samples, dry_run=args.dry_run)

    # 7. 打印摘要
    print_summary(old_weights, new_weights, n_samples, applied, args.dry_run)

    if not significant and not args.dry_run:
        print(f"⏭  所有维度变化 < {args.min_change}pp，权重保持不变（系统稳定）。\n")


if __name__ == "__main__":
    main()
