"""ML 模型退化观测点 + 模型版本快照（v0.45.145）

治的缺陷：**ML 模型退化成常数函数时，全线没有任何东西会变红。**

2026-09-04 全部 12 份 `analysis-*-ml-*.json` 的
`ml_prediction.prediction.probability` **逐位相同** = `0.5899693787928219`，
报告照常印「ML 预测 59.0%」，退出码 0，日志正常。
2026-08-28 同样：12/14 份恒为 `0.14901620144018954`（另 2 只因
`iv_rank`/`put_call_ratio` 为 None 走了别的路径）。

同期输入并不相同——09-04 的 `iv_rank` 跨 0→98.61、`put_call_ratio` 0.26→1.23、
`momentum_5d` −3.45→+7.76；用**当前**模型重放同一批输入得 7 个不同值，
生产当天只有 1 个 ⇒ 当天的模型确实是常数函数。

## 为什么 `probability_scorecard.py` 覆盖不了这件事

记分卡记的是「准不准」（Brier / 命中率）。**零区分度的常数预测校准误差可以很小**：
09-04 那个 59.0% 离真实基准率 56.8% 只差 2.2pp，记分卡看不出异常。
**校准 ≠ 区分度**，两件事必须分开观测。

## 判据（两条，均为硬错误）

设 `n` = 当日有数值 probability 的报告数，`distinct` = 其中不同取值的个数。

1. `n > 1 且 distinct == 1` → `constant`：模型是常数函数。
2. `n >= 8 且 distinct <= 2` → `near_constant`：09-04 只被第 1 条抓到，
   **08-28（distinct=2）会从第 1 条底下漏过去**。只写第 1 条就是重犯
   「修一支漏一支」。

### 阈值不是拍脑袋的（判据：定阈值前先数生产数据里历史命中几次）

全量 77 个扫描日实测：

| 时期 | distinct 分布 |
|---|---|
| 当前世代（08-10 起，30 只池）健康日 | **9 ~ 21**（n=11~30） |
| 已知事故 08-28 | 2（n=14） |
| 已知事故 09-04 | 1（n=12） |

当前世代健康日与事故日之间是 **2 vs 9** 的空隙，`distinct <= 2` 落在正中间，
零误报。旧世代（2026 年 3~4 月，10 只池）另有 17 天会命中——那段时期模型本来
就长期退化，不是误报。

⚠️ **合法的「精确相同」是存在的，不要把它当退化**：09-02 有 3 只共享
`0.4805419596430596`，但那天 12 份输入**各不相同**（树模型叶子离散化所致）。
所以判据取 `distinct`（唯一值个数）而不是「有没有并列」。

⚠️ `n >= 8` 这道地板是为了挡住小样本误报：n=3 时两只并列就是 distinct=2，
而并列在生产上确实合法。第 1 条规则不设地板（`n > 1` 即可），因为
「全体逐位相同」在任何 n 下都不可能是叶子离散化的巧合。

## 阈值是编译期常数，**不从历史池化**（所以不需要世代边界）

本模块只读**当日**产物，两个阈值（`distinct == 1`、`n>=8 且 distinct<=2`）
是写死的常数。上面那张 77 天的表是**离线选常数时**用的一次性证据，
运行时不会去读历史 `ml_probability`。

⇒ 因此 `probability_scorecard._ML_ESTIMATOR_GENERATIONS` 的世代边界
（`2026-09-07 → v0.45.146+v0.45.147`）**不影响本模块**。
若将来有人把阈值改成「从历史学出来的基线」，那一刻必须补世代边界，
否则新旧口径会被静默混算 —— 这是本注释存在的唯一理由。

顺带：v0.45.146/147 让服务端特征接上真值后，唯一值总数从 308 升到 462
（并发 session 实测，未由本模块复核）。方向上只会**加宽**健康日与事故日
之间那道空隙，不会让阈值变松。

## 复现 2026-09-04 需要当时那份模型

生产 `ml_model.json` 已于 v0.45.152 从 `pheromone.db` 重训恢复
（`n=497 / acc 71.63 / oos 44.35`），**已不是 09-04 当时那份**。
当时那份被污染的模型保留在
`ml_model.corrupted-2026-09-07T0130.json`（`n=30 / acc 96.67 / oos None`），
污染前健康副本在 `ml_model_cache.pre-restore-2026-09-07.json`。
两者都被 `.gitignore` 的 `ml_model*.json` 忽略（实测确认本模块的
`!ml_model_history/*.json` 反向规则只作用于该目录内，未误放行它们）。

⚠️ **判模型真伪不能用 accuracy** —— 夹具模型 96.67 比真模型 71.63 更好看。
可用信号是 `oos_accuracy is None` + `n_samples_seen` 明显偏低，
本模块的 `manifest.jsonl` 两个字段都记了。

## 为什么读磁盘而不是读内存

当日 `analysis-*-ml-*.json` 有**两个**生产者：

- `alpha_hive_daily_report._generate_ml_reports`（Step 2，每天 12 份，主力）
- `generate_ml_report.main()`（Step 3 补跑，只补 Step 2 漏掉的那几只）

读磁盘让两条路径共用同一份判据，且**补跑那几只会和 Step 2 的合在一起算**
（否则补跑 2 只时 n=2，判据形同虚设）。

## 退出码（沿用 Step 10/11/12 约定）

- 0 = 健康
- 1 = **要人动手**：模型退化成常数 / 准常数
- 3 = 无法判定（当日报告数 < 2，或全都没有数值 probability）
- 2 由编排器 `run_step` 占用（脚本不存在），本模块不使用

## 模型版本快照

09-04 那次**无法事后归因**，因为 `ml_model.json` / `ml_model_cache.json` 被
原地覆盖、没有任何版本留存。`snapshot_model_file()` 在每次 `save_model` 之后
留一份 `ml_model_history/{stem}-YYYY-MM-DD.json`，并往
`ml_model_history/manifest.jsonl` 追加一行元数据（sha256 / 样本数 / 训练精度），
下次退化时可以直接把当天的模型捞出来重放。

⚠️ `.gitignore` 有 `ml_model*.json`，会**连快照一起吞掉**（实测
`git check-ignore` 命中）。本 PR 同时加了 `!ml_model_history/*.json` 反向规则、
并把 `ml_model_history/` 加进 `REPORT_ARTIFACT_PATHS` 与 `_ARTIFACT_PREFIXES`
两张表——见 [[alpha-hive-silent-degradation]] 的 v0.45.111 教训：
新产物目录不做齐这几件事，就会每天被改、每天被自动提交跳过、永远挂在工作区。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

#: 第 1 条判据的最小标的数（「全体逐位相同」在任何 n>1 下都不可能是巧合）
CONSTANT_MIN_N = 2

#: 第 2 条判据的地板。
NEAR_CONSTANT_MIN_N = 8

#: 第 2 条判据的门槛，**按 n 分段**。见 `near_constant_max_distinct()`。
#: (n 下界, 该档允许的最大 distinct)，按 n 下界降序匹配第一个命中的。
NEAR_CONSTANT_BANDS = ((20, 4), (NEAR_CONSTANT_MIN_N, 2))

#: 兼容名：小扫描日那一档的门槛。外部若要引用，请改用
#: `near_constant_max_distinct(n)` —— 单一常数正是 v0.45.154 要治的问题。
NEAR_CONSTANT_MAX_DISTINCT = NEAR_CONSTANT_BANDS[-1][1]


def near_constant_max_distinct(n: int) -> Optional[int]:
    """该扫描规模下，distinct 低到多少算「准常数」。n 不够判则返回 None。

    **为什么必须按 n 分段**（v0.45.154）：原判据是单一绝对数 `distinct <= 2`，
    于是**扫描日越大、闸越松**——方向与诊断力恰好相反。
    n=12 时 distinct<=2 才红（相对健康下界 9 是 4.5 倍塌缩）；
    而 n=30 时同样要 distinct<=2，相对该档健康下界 **13** 已是 6.5 倍塌缩，
    中间的 3、4 全部放行。实测：n=30 / distinct=3 在旧判据下是 `ok`。

    **为什么不换成比值 `distinct/n`**：健康日的比值带是 **[0.43, 0.82]**
    （n=11/distinct=9 → 0.82；n=30/distinct=13 → 0.43），比绝对数
    2-vs-9 的空隙**更窄、更易误报**。且换量纲＝换了一个估计量，
    标定表必须整体重标（同 v0.45.144「闸门比的是两个不同估计量」）。

    ## 标定（77 个扫描日实测，n>=8 的那些）

    | n 档 | 当前世代健康日 distinct | 门槛 | 余量 |
    |---|---|---|---|
    | [8, 20)  | 最小 **9**（n=11/12/16 各有一天取到 9） | <= 2 | 4.5× |
    | [20, ∞)  | 最小 **13**（另有 13 / 20 / 21） | <= 4 | 3.25× |

    两档在当前世代均**零误报**，三个已知事故（06-25 / 08-28 / 09-04）全中，
    且 n=30/distinct=3 与 n=30/distinct=4 都被抓住。

    ⚠️ **`n>=20` 那一档只有 4 天样本**（08-25/26/27 的 n=30 与 09-01 的 n=24）。
    门槛取 4 是刻意的保守选择：它离该档实测下界 13 还有 3.25 倍，
    不靠「13 附近」这个尚未站稳的估计。**要把门槛往上抬，先攒够 n>=20 的样本**
    ——扫描池 08-25 才扩到 30 只，几周后再谈。
    """
    if n < NEAR_CONSTANT_MIN_N:
        return None
    for lo, cap in NEAR_CONSTANT_BANDS:
        if n >= lo:
            return cap
    return None

#: 模型快照目录名（相对 report_dir）
HISTORY_DIRNAME = "ml_model_history"
MANIFEST_NAME = "manifest.jsonl"

#: 每个 stem 保留的带日期快照份数。180 × (19KB + 61KB) ≈ 14MB。
RETAIN_SNAPSHOTS = 180

#: 「输入侧本身也塌了」的判据：不同输入向量数 × 2 <= 有效标的数。
#: 只影响 `describe()` 的措辞（该往哪查），**不影响 verdict 与退出码**——
#: 见 `DegeneracyVerdict.inputs_also_collapsed` 的说明。
INPUT_COLLAPSE_RATIO = 2

#: 关掉快照的环境变量。测试里必须关掉——见 `_snapshot_disabled` 的说明。
SNAPSHOT_DISABLE_ENV = "ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE"

#: 快照文件名形状，删旧文件时用它把「我们写的」与「别人放的」分开
_SNAPSHOT_RE = re.compile(r"^(?P<stem>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.json$")

_ANALYSIS_RE = re.compile(r"^analysis-(?P<ticker>.+)-ml-(?P<date>\d{4}-\d{2}-\d{2})\.json$")


class MLModelDegenerateError(RuntimeError):
    """当日 ML 概率退化成常数（或准常数）。

    ⚠️ 抛它之前先确认调用点接得住：`_generate_ml_reports` 的调用点只兜
    `(OSError, ValueError, KeyError, TypeError)`，`RuntimeError` 会穿透并连带
    杀掉 index.html 生成与 gh-pages 部署（v0.43.17 同款事故）。那条路径用
    `enforce_day(..., raise_on_degenerate=False)` + 非零退出码，不要抛。
    """


@dataclass(frozen=True)
class DegeneracyVerdict:
    """一天的判定结果。

    `n_files` 与 `n_numeric` 都要留着：只报 `n_numeric` 的话，
    「12 份里 10 份根本没有 probability」会被渲染成「只有 2 份报告」
    ——即 v0.45.114「跳过缺失项＝把缺失渲染成不存在」。
    """

    date: str
    verdict: str                       # "ok" | "constant" | "near_constant" | "undetermined"
    n_files: int
    n_numeric: int
    distinct: int
    modal_count: int
    modal_value: Optional[float]
    tickers_without_probability: List[str] = field(default_factory=list)
    snapshot_present: bool = False
    #: 当日有几份报告根本没有 `swarm_results`（蜂群没跑 / 没接上）。
    n_without_swarm_results: int = 0
    #: 当日有几个**不同的**输入向量。⚠️ 只统计 JSON 里记录的 `ml_prediction.input`，
    #: 那是 **5/12 维的展示子集**（catalyst_quality / crowding_score /
    #: market_sentiment / momentum_5d / volatility），不含 iv_rank、put_call_ratio 等。
    #: 所以它**只能用来加一句提示，绝不能用来放行**：真实 12 维有差异而这 5 维
    #: 恰好相同的日子，据它放行就会把真退化判成「输入塌了」——漏报比误报危险。
    distinct_inputs: Optional[int] = None

    @property
    def is_degenerate(self) -> bool:
        return self.verdict in ("constant", "near_constant")

    @property
    def inputs_also_collapsed(self) -> bool:
        """输入侧本身是否也塌了 —— **只用于措辞，不参与判定**。

        2026-06-25：9 份 probability 逐位相同，但当日 **9/9 份无
        `swarm_results`**，记录的输入只有 2 个不同向量（其余四槽全是兜底常数
        crowding 50.0 / sentiment 0.0 / momentum 0.0 / volatility 5.0）。
        那天真正该查的是「蜂群没跑」，不是「模型退化」——闸若照旧说
        「拿当日模型快照重放」，就是把人指向错的地方
        （同 v0.45.126：降级消息别替失败原因下结论）。

        ⚠️ **刻意不据此放行**，三条理由：
        1. 记录的 `input` 只有 5/12 维，据它放行会漏掉真模型退化；
        2. 那天全站印出 9 份相同的「ML 预测 51.7%」，报告一样是误导性的，
           本就该红；
        3. 「当日全无 swarm_results」并不必然塌成常数——06-30(10 份)、
           07-02(3 份) 同样全缺，distinct 分别是 5 和 3，闸压根没点亮它们。
           拿它当排除条件是个**过宽**的闸。

        ⚠️ 另：**不要改用 `unreliable` / `input_features_missing` 做判据**。
        实测 06-25 那 9 份里这两个键的值都是 `None`（那天的 schema 里没有它们），
        照此实现出来的排除逻辑会在**它唯一要排除的那一天**永不触发
        ——即 v0.45.108「闸门自己恒假」。
        """
        if self.distinct_inputs is None or self.n_numeric <= 1:
            return False
        return self.distinct_inputs * INPUT_COLLAPSE_RATIO <= self.n_numeric

    @property
    def exit_code(self) -> int:
        if self.is_degenerate:
            return 1
        if self.verdict == "undetermined":
            return 3
        return 0

    def describe(self) -> str:
        head = {
            "constant": "🚨 ML 模型退化成常数函数",
            "near_constant": "🚨 ML 模型准常数（%d 只标的那一档的门槛是唯一值 ≤ %s）" % (
                self.n_numeric, near_constant_max_distinct(self.n_numeric)),
            "ok": "ML 概率分布正常",
            "undetermined": "ML 概率无法判定",
        }[self.verdict]
        parts = [
            f"{head}：{self.date} 共 {self.n_files} 份报告，"
            f"其中 {self.n_numeric} 份有数值 probability，唯一值 {self.distinct} 个"
        ]
        if self.modal_value is not None and self.modal_count > 1:
            parts.append(
                f"最大同值组 {self.modal_count}/{self.n_numeric} 份 = {self.modal_value!r}"
            )
        if self.tickers_without_probability:
            parts.append(
                "无数值 probability 的标的：" + ", ".join(sorted(self.tickers_without_probability))
            )
        if self.distinct_inputs is not None:
            parts.append(f"记录的输入向量 {self.distinct_inputs} 个不同（仅 5/12 维）")
        if self.n_without_swarm_results:
            parts.append(f"无 swarm_results 的报告 {self.n_without_swarm_results}/{self.n_files} 份")
        parts.append(
            "当日模型快照：" + ("已留存" if self.snapshot_present
                              else f"缺失（{HISTORY_DIRNAME}/ 里没有当日文件；"
                                   "命中磁盘缓存、当天未训练时属正常）")
        )
        if self.is_degenerate:
            parts.append(
                "这不是记分卡能发现的问题——常数预测的校准误差可以很小"
                "（09-04 那次 59.0% 离基准率只差 2.2pp）"
            )
            if self.inputs_also_collapsed:
                # 别替失败原因下结论：输入侧也塌了的日子，先查上游。
                parts.append(
                    "⚠️ **输入侧本身也塌了**（不同输入向量 "
                    f"{self.distinct_inputs} 个 / 有效标的 {self.n_numeric} 只"
                    + (f"，其中 {self.n_without_swarm_results} 份无 swarm_results"
                       if self.n_without_swarm_results else "")
                    + "）。**先查蜂群/上游取数有没有跑**，不要先查模型——"
                    "模型拿到的本来就是同一批输入，输出相同不构成退化证据。"
                    f"（仍判为硬错误：全站印出 {self.n_numeric} 份相同的预测，"
                    "报告一样是误导性的。）"
                )
            else:
                parts.append(
                    f"输入是有区分度的 ⇒ 恒定来自模型。归因材料在 {HISTORY_DIRNAME}/，"
                    "拿当日快照重放当日输入即可复现"
                )
        return "；".join(parts)


# ---------------------------------------------------------------------------
# 纯判定谓词（不碰磁盘，供 TestGuardsHaveTeeth 式的成对测试直接调用）
# ---------------------------------------------------------------------------
def evaluate_probabilities(
    values: Sequence[float],
    *,
    date: str = "",
    n_files: Optional[int] = None,
    tickers_without_probability: Optional[Sequence[str]] = None,
    snapshot_present: bool = False,
    n_without_swarm_results: int = 0,
    distinct_inputs: Optional[int] = None,
) -> DegeneracyVerdict:
    """对一批 probability 下判定。纯函数，判据全部在这里，没有第二份实现。

    ⚠️ `bool` 是 `int` 的子类，`True`/`False` 会被 `float()` 悄悄收成 1.0/0.0。
    仓库同类守卫（5 处）一律显式排除 bool，这里照抄那一句，不自己发明。
    """
    nums: List[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        f = float(v)
        if f != f:            # NaN：不参与去重（NaN != NaN 会把 distinct 撑大，掩盖退化）
            continue
        nums.append(f)

    n_numeric = len(nums)
    counts = Counter(nums)
    distinct = len(counts)
    modal_value, modal_count = (counts.most_common(1)[0] if counts else (None, 0))

    if n_numeric < CONSTANT_MIN_N:
        verdict = "undetermined"
    elif distinct == 1:
        verdict = "constant"
    elif (_cap := near_constant_max_distinct(n_numeric)) is not None and distinct <= _cap:
        verdict = "near_constant"
    else:
        verdict = "ok"

    return DegeneracyVerdict(
        date=date,
        verdict=verdict,
        n_files=n_files if n_files is not None else n_numeric,
        n_numeric=n_numeric,
        distinct=distinct,
        modal_count=modal_count,
        modal_value=modal_value,
        tickers_without_probability=list(tickers_without_probability or []),
        snapshot_present=snapshot_present,
        n_without_swarm_results=n_without_swarm_results,
        distinct_inputs=distinct_inputs,
    )


# ---------------------------------------------------------------------------
# 磁盘读取
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DayRecord:
    """当日一份 `analysis-*-ml-*.json` 里与本闸相关的那几样东西。"""

    ticker: str
    probability: Optional[float]
    #: `ml_prediction.input` 的规范化签名（仅 5/12 维，见 `distinct_inputs` 说明）
    input_signature: Optional[str]
    #: `swarm_results` 是否存在且非空。**跨 schema 世代都可用**——
    #: 不像 `unreliable` / `input_features_missing`，那两个在 2026-06-25 是 `None`。
    has_swarm_results: bool


def read_day_records(report_dir, date_str: str) -> Tuple[List[DayRecord], List[str]]:
    """读当日全部 `analysis-*-ml-{date}.json`。

    返回 `([DayRecord], [读不动的文件名])`。
    读不动的文件单独列出、不静默丢弃——「少一份」和「这份坏了」必须可区分。
    """
    d = Path(report_dir)
    out: List[DayRecord] = []
    unreadable: List[str] = []
    for path in sorted(d.glob(f"analysis-*-ml-{date_str}.json")):
        m = _ANALYSIS_RE.match(path.name)
        if not m:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            unreadable.append(path.name)
            continue
        mlp = payload.get("ml_prediction") or {}
        raw = (mlp.get("prediction") or {}).get("probability")
        inp = mlp.get("input")
        out.append(DayRecord(
            ticker=m.group("ticker"),
            probability=(raw if isinstance(raw, (int, float))
                         and not isinstance(raw, bool) else None),
            input_signature=(json.dumps(inp, sort_keys=True, default=str)
                             if isinstance(inp, dict) and inp else None),
            has_swarm_results=bool(payload.get("swarm_results")),
        ))
    return out, unreadable


def read_day_probabilities(
    report_dir, date_str: str
) -> Tuple[Dict[str, Optional[float]], List[str]]:
    """`read_day_records` 的薄封装，只要 `{ticker: probability|None}`。"""
    records, unreadable = read_day_records(report_dir, date_str)
    return {r.ticker: r.probability for r in records}, unreadable


def _snapshot_exists(report_dir, date_str: str) -> bool:
    hist = Path(report_dir) / HISTORY_DIRNAME
    if not hist.is_dir():
        return False
    return any(hist.glob(f"*-{date_str}.json"))


def check_day(report_dir, date_str: str) -> DegeneracyVerdict:
    """读当日产物并给出判定。不抛、不改任何东西。"""
    records, unreadable = read_day_records(report_dir, date_str)
    missing = [r.ticker for r in records if r.probability is None]
    missing.extend(f"<读取失败:{name}>" for name in unreadable)
    sigs = {r.input_signature for r in records if r.input_signature is not None}
    return evaluate_probabilities(
        [r.probability for r in records if r.probability is not None],
        date=date_str,
        n_files=len(records) + len(unreadable),
        tickers_without_probability=missing,
        snapshot_present=_snapshot_exists(report_dir, date_str),
        n_without_swarm_results=sum(1 for r in records if not r.has_swarm_results),
        distinct_inputs=(len(sigs) if sigs else None),
    )


def enforce_day(
    report_dir,
    date_str: str,
    *,
    raise_on_degenerate: bool = True,
    logger: Optional[logging.Logger] = None,
) -> DegeneracyVerdict:
    """判定 + 落日志（+ 可选抛错）。

    `raise_on_degenerate=False` 供 `_generate_ml_reports` 使用：那个调用点
    只兜 `(OSError, ValueError, KeyError, TypeError)`，抛 `RuntimeError`
    会穿透并连带杀掉 index.html 与 gh-pages 部署。那条路径靠调用方把判决
    挂到 `report` 上、由 `__main__` 出非零退出码来「变红」。

    ⚠️ 本函数**不发任何 Slack**（CLAUDE.md「Slack 通知精简规则」：
    Bot 只发 LLM 模式确认与日报推送成功两类）。观测点＝日志 + 退出码。
    """
    lg = logger or _log
    verdict = check_day(report_dir, date_str)
    if verdict.is_degenerate:
        lg.error(verdict.describe())
    elif verdict.verdict == "undetermined":
        lg.warning(verdict.describe())
    else:
        lg.info(verdict.describe())
    if verdict.is_degenerate and raise_on_degenerate:
        raise MLModelDegenerateError(verdict.describe())
    return verdict


# ---------------------------------------------------------------------------
# 模型版本快照
# ---------------------------------------------------------------------------
def _snapshot_disabled() -> bool:
    """测试跑出来的模型**绝不能**进快照目录。

    ⚠️ 这不是假想风险，是实测：`HGBModel.save_model(filename="ml_model.json")`
    的默认值是**相对路径**，而 `tests/` 里约 12 处 `svc.train_model()` 不传
    tmp 路径 ⇒ 在主 checkout 跑 pytest 就会往 cwd 写 `ml_model.json`。
    实测单跑
    `tests/test_ml_real_training.py::TestTrainModelIntegration::test_train_model_prefers_real_data`
    → 仓库根凭空出现 `ml_model.json`，且本模块会顺手把它快照进
    `ml_model_history/`。而该目录是 **git 跟踪 + 在自动提交白名单里的** ——
    夹具模型会被当成生产模型提交推送。

    **那比没有快照更糟**：把「没有归因材料」换成了「有一份长得很真的假证据」。
    （相对路径默认值本身是另一条独立缺陷，已另挂任务，不在本版改动面内。）

    两层：显式设了环境变量就听它（`0`/`false`/空 = 开启，其余 = 关闭）；
    没设时，**在 pytest 里默认关闭**。本模块自己的测试用
    `monkeypatch.setenv(..., "0")` 显式开启，从而真的走生产那条路径。
    """
    v = os.environ.get(SNAPSHOT_DISABLE_ENV)
    if v is not None:
        return v.strip().lower() not in ("", "0", "false", "no")
    return "PYTEST_CURRENT_TEST" in os.environ


def _prune_snapshots(hist: Path, stem: str, retain: int) -> None:
    """只删形如 `{stem}-YYYY-MM-DD.json` 的自产文件，保留最新 `retain` 份。"""
    if retain <= 0:
        return
    mine = []
    for p in hist.glob(f"{stem}-*.json"):
        m = _SNAPSHOT_RE.match(p.name)
        if m and m.group("stem") == stem:
            mine.append((m.group("date"), p))
    for _date, p in sorted(mine, reverse=True)[retain:]:
        try:
            p.unlink()
        except OSError as e:
            _log.warning("清理旧模型快照失败 %s: %s", p.name, e)


def snapshot_model_file(
    model_path,
    *,
    date_str: Optional[str] = None,
    history_dir=None,
    retain: int = RETAIN_SNAPSHOTS,
) -> Optional[Path]:
    """把刚保存的模型留一份带日期的副本，并往 manifest.jsonl 追加一行。

    返回快照路径；未写（内容与当日已有快照相同）或失败时返回 None。

    失败**不抛**：保住模型本身比留快照重要。但失败会 `_log.error` 而非 debug，
    且当日 manifest 缺行 / 快照缺失会由 `DegeneracyVerdict.describe()`
    在每日那条日志里说出来——这就是「这个失败，下游怎么知道」的答案。

    ⚠️ 快照缺失**不构成硬错误**：命中磁盘缓存的那天根本不会调 `save_model`，
    当天没有快照是正常的。

    ⚠️ pytest 里默认不写（见 `_snapshot_disabled`）——测试夹具模型混进
    这个目录会被自动提交成「生产模型」，是有误导性的假证据。
    """
    if _snapshot_disabled():
        return None
    try:
        src = Path(model_path)
        if not src.is_file():
            return None
        raw = src.read_bytes()
        hist = Path(history_dir) if history_dir is not None else src.parent / HISTORY_DIRNAME
        hist.mkdir(parents=True, exist_ok=True)

        if date_str is None:
            try:
                from hive_logger import pdt_today
                date_str = pdt_today()
            except Exception:
                date_str = _dt.date.today().isoformat()

        digest = hashlib.sha256(raw).hexdigest()
        dest = hist / f"{src.stem}-{date_str}.json"

        meta = {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                meta = {
                    k: parsed.get(k)
                    for k in ("model_type", "is_trained", "n_samples_seen",
                              "training_accuracy", "oos_accuracy")
                }
        except (UnicodeDecodeError, json.JSONDecodeError):
            meta = {"parse_error": True}

        line = {
            "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "date": date_str,
            "source": src.name,
            "snapshot": dest.name,
            "sha256": digest,
            "bytes": len(raw),
            **meta,
        }
        with open(hist / MANIFEST_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

        # 同日重复保存：内容没变就不重写（省 churn），变了就覆盖成最新一次。
        # manifest 上面已经记了每一次的 sha256，日内演化仍可追。
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
            return None

        dest.write_bytes(raw)
        _prune_snapshots(hist, src.stem, retain)
        return dest
    except OSError as e:
        _log.error("🚨 模型快照写入失败（%s）：%s——下次退化将无法事后归因",
                   getattr(model_path, "name", model_path), e)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ML 概率常数退化检查（退出码 0=健康 / 1=要人动手 / 3=无法判定）"
    )
    parser.add_argument("--date", help="扫描日 YYYY-MM-DD，默认 PDT 今天")
    parser.add_argument("--dir", default=None, help="报告目录，默认 PATHS.home")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非人读文本")
    args = parser.parse_args(argv)

    if args.dir:
        report_dir = Path(args.dir)
    else:
        from hive_logger import PATHS
        report_dir = PATHS.home

    if args.date:
        date_str = args.date
    else:
        from hive_logger import pdt_today
        date_str = pdt_today()

    verdict = check_day(report_dir, date_str)
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(verdict), ensure_ascii=False, indent=2))
    else:
        print(verdict.describe())
    return verdict.exit_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
