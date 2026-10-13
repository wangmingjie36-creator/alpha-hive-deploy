"""Yahoo Finance 期权 IV 的求解失败值识别（v0.45.482）。

Yahoo 的 `impliedVolatility` 是二分法求出来的：每个值都是「m / 2ⁿ + 一个 1e-6 量级的正偏移」
（本地 chain 缓存 + 快照 9500 个非 CBOE 值全部如此，深度 n 集中在 10–13）。求解器在市场价低于模型价
下界时一路减半走到底，留下的是一条**减半阶梯**：1、½、¼、⅛、1/16 … 2⁻ⁿ（+偏移），最后是地板 1e-05。

这些不是观测。2026-10-13 用同一 OCC 合约对照 CBOE 自己的 IV（8 只标的、2839 个合约，盘前）：
  - 1e-05：992 个，与 CBOE 相差 > 10 个波动率点的占 100%
  - 减半阶梯：1785 个，相差 > 10 点的占 99–100%（NVDA：Yahoo 0.0625 / 0.125 vs CBOE 0.29–0.34）
  - 阶梯之外的值可以是真的（NVDA 0.7656 vs CBOE 0.765；0.8438 vs 0.8538）
对 ATM 合约取中位数 ×100 就会得到正好 50 / 25 / 12.5 / 6.25——它们落在 5%–150% 的有效区间里，
此前被当成当日真实观测：标 real、写 last_valid_iv 缓存、写自攒 IV 历史、进 signal_archive。

只用于 **Yahoo** 来源的 IV。CBOE 的 IV 是 4 位小数（0.2500 这样的整值是真观测），不要拿它过这个判据。
"""
import math

# 阶梯偏移：实测 0.500005 / 0.250007 / 0.125009 / 0.062509 / 0.031259 / 0.015635，均为正、≤ 1e-5 量级
_MAX_OFFSET = 2e-5
_MIN_OFFSET = 1e-9
# 1 … 2⁻²⁰。地板 1e-05 不必单列：它 = 2⁻¹⁷ + 2.4e-6，落在阶梯的偏移窗口里（单列的判断分支变异下存活，v0.45.482）
_LADDER = tuple(2.0 ** -n for n in range(0, 21))


def is_placeholder_iv(v) -> bool:
    """`v`（小数，0.25 = 25%）是 Yahoo 求解失败留下的值 ⇒ True。非数 / 非有限 / ≤ 0 ⇒ False（那是另一种缺失，调用方各自处理）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(x) or x <= 0:
        return False
    # 偏移严格为正：实测 2840 个阶梯命中（10-13 对照 2827 + 本地缓存 13）偏移全部 > 0，从没有正好落在 2⁻ⁿ 上的。
    # 正好 0.25 / 0.5 这样的整值不判失败（测试夹具常用；真有 Yahoo 给出整值也不该据此冤枉它）。
    return any(_MIN_OFFSET < x - step <= _MAX_OFFSET for step in _LADDER)
