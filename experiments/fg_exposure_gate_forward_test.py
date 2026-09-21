#!/usr/bin/env python3
"""F&G 组合层敞口控制门 前瞻检验（预注册，v0.45.262，2026-09-15）

问题
----
`BuzzBeeWhisper` 把 F&G 当逐标的情绪信号用（当天全池同一常数加进逐标的连续分数，
再过类别阈值判方向）已实测证据不支持——顺周期（生产现状）明显是"顺周期/去掉/逆周期"
三种处理里最差的，去掉和逆周期几乎没有差距，且全部差距远未过检验（15 周，
本仓功效标准要 ~25 周，见 [[alpha-hive-power-analysis]]）。更根本的问题是结构性的：
F&G 是当天全池唯一一个常数，加到逐标的连续分数上不改变横截面排序（秩相关对同一常数
平移不变），它唯一的效应来自"谁卡在类别阈值附近被推过还是推不过"——本质是往阈值分类
里注入噪音，跟顺/逆周期无关。CNN 官方把 Fear & Greed Index 设计成大盘/组合择时工具，
不是逐标的选股信号——用错了层次。完整推导见 auto-memory `alpha-hive-fear-greed-dead-wire.md`。

`paper_portfolio.CONFIG["fg_exposure_gate"]`（v0.45.262 新增，默认 `enabled=False`）把
F&G 挪到它真正适用的层次：组合层的仓位敞口控制——极度贪婪日新开多头减仓、极度恐惧日
新开空头减仓（只收紧顺势方向，不给逆势方向加仓），完全不触碰任何标的的评分或方向判定。

为什么是前瞻而不是直接改
------------------------
这个假设至今**一次都没有真实运行过**（`enabled` 从落地起就是 `False`），没有任何样本内
证据可用——不像共振加成那次还有半年生产数据可供事后分析（v0.45.242）。唯一诚实的验证
方式是让它先在沙盒里跑一段真实前瞻期，而不是凭理论推导直接把 `enabled` 改成 `True`。

────────────────────────────────────────────────────────────────────
预注册（2026-09-15 写死，早于任何前瞻样本；改动须在 CHANGELOG 标「事后」并写理由）
────────────────────────────────────────────────────────────────────
样本  前瞻窗口 = [FORWARD_START, 今天)（`FORWARD_START` = 登记次日 2026-09-16）内，
      `paper_portfolio` 真实产出过 `report_snapshots/*.json` 的交易日
      （`paper_portfolio._all_snapshot_dates()`）。
变体  A（baseline）= `run_replay({}, ...)`，即当前生产默认配置（`fg_exposure_gate.enabled=False`）；
      B（treatment）= `run_replay(FG_GATE_TEST_CONFIG, ...)`，`enabled=True`，其余参数逐字节等于
      `paper_portfolio.CONFIG["fg_exposure_gate"]` 落地时写的默认值（`extreme_fear=25` /
      `extreme_greed=75` / `long_size_mult=0.5` / `short_size_mult=0.5`）——测的是"把默认参数
      打开会怎样"，不是另找一组参数调优（调参本身需要更多证据支撑，不在这次范围内）。
      两者都用**真实** `paper_portfolio.run_replay`，各自独立沙盒 state_dir，不碰生产状态。
自证  （前提，不满足即"无法判定" exit 3）A（baseline）重放出的"窗口内新开仓位"集合
      （`(ticker, entry_date, direction, round(size_usd,2))` 四元组）与生产**实际记录**的
      `paper_portfolio_state/`（`closed_trades.jsonl` ∪ 仍在场的 `positions.jsonl`，
      entry_date 落在窗口内的那些）重合比例 >= SELFPROOF_MIN_RATE=0.95。
      ⇒ 若评分链、`paper_portfolio.CONFIG` 其它参数、或本身的重放机制在期间被改动，
      这里会红，而不是静默算出一个没意义的数。
      【事后修订 v0.45.297，2026-09-21 —— 改的是自证的**前提**，不是判定规则】
      原设计里 A/B 沙箱从**空状态**（本金 5 万、无持仓、无历史平仓）起跑，而生产在窗口首日
      已带着累计已实现盈亏、在场持仓与历史平仓记录。仓位基数 `nav = cash + Σ size_usd`
      是**状态**，所以空沙箱重放的 `size_usd` 与生产逐分对不上：首次拿到真实前瞻样本
      （09-16~09-18，3 个快照日）就报「精确 0/8」，而决策层 (ticker, date, direction) 实为 8/8。
      这是自证的**设计缺陷**，不是评分链变动——播种后前瞻窗口 8/8、历史窗口 5/5 与 13/13，
      故意改 A 的配置则全部变红（数据见 CHANGELOG v0.45.297）。
      修订 = A/B 两个沙箱都从「生产在窗口首日前一交易日收盘」的状态起跑：
      `experiments/fg_gate_forward_seed/` 冻结了 meta / positions / closed_trades 三个文件
      （逐字节取自 `SEED_SOURCE.json` 记录的提交，附 sha256）。`equity_curve.jsonl` 刻意不播——
      它只写不读，播进去会让窗口前的周也进统计量。
      **未动**：窗口、变体 A/B、统计量、检视点（15/30）、α、盲化、SELFPROOF_MIN_RATE、四元组键。
      修订时前瞻样本仅 3 个快照日，且从未计算或查看任何效应量，修订不受结果影响。
统计量 两个变体各自的 `equity_curve.jsonl` 按 ISO 周取"本周首个交易日 NAV"，相邻两个
      取值点间的百分比变化即该周收益率；ΔNAV_pct = B周收益率 − A周收益率。
      非极端 F&G 日两个变体的仓位应逐笔相同 ⇒ 多数周 ΔNAV_pct=0——这是**预期正常**，
      不是异常，会稀释均值、需要更多周才能检出效应，不代表实现有问题。
      对周序列做单侧 t 检验（H1：均值 > 0，即敞口门让组合更好）。不用横截面 IC——
      敞口门改的是仓位大小、不改排序，横截面秩相关在这里没有意义。
检视  成组序贯两次，按**时间顺序最先攒到的**前 N 个合格周计算：
        中期  N=15  单侧 p < 0.02   → 确认
        终期  N=30  单侧 p < 0.045  → 确认；否则 → 未确认（结案）
      两次合计单侧 α ≈ 0.06。**N 高于共振加成那次（10/20）是明确写出的估计，不是算出来的**：
      极度恐惧/贪婪日历史约占 15% 交易日（CNN 141 天 21 天 <25，[[alpha-hive-fear-greed-dead-wire]]），
      远比共振加成"天天都有效应"稀疏，多数周 delta=0 会拉长检出所需的周数——没有做严格功效分析
      （本身就没有先验效应量可供估计），这里选的是"比共振加成更保守"的直觉调整，如实记录，
      不假装精确。
盲化  未到检视点时只报进度（合格周数、自证率），**不输出任何效应量**；中期未过线同样
      只报"未过中期界，继续"。
结论  确认 → 评估把 `fg_exposure_gate.enabled` 改 `True`（这会改变纸面组合的真实交易，
      需要 paper_portfolio 自己的 `run_replay` 对比新旧参数、用户拍板，不是本脚本自动执行）。
      未确认 → 保持默认关闭，登记结案，勿再拿这轮样本内证据提议打开。
附带  （不参与判定）敞口门实际调整过的仓位笔数与调整方向分布、这些笔已平仓部分的
      已实现盈亏对比（仅描述，非统计检验）。

样本内复核：`--insample` 在 `FORWARD_START` 之前的历史窗口上跑同一套 A/B 重放
（此时 A 的自证对象是"整个历史"而非"前瞻窗口"）。**它就是生成假设的那份数据，
只供复核报告数字与检查代码机制，不能拿来确认**——F&G 敞口门此前从未真正运行过，
这不像共振加成还有"半年生产分数"可供样本内探索，这里的 `--insample` 更多是自证脚本
本身没写错，而不是提前偷看效应。

用法
----
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py            # 前瞻（默认）
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --json
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --insample
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --rehearse 2026-09-09 2026-09-16
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --build-seed   # 一次性；拒绝覆盖

`--rehearse SINCE BEFORE`（v0.45.297）：自证演练。对任一窗口，从 git 现取「窗口首日前」的生产状态播种，
**只重放 A**、报复现率。不跑 B、不算周度差、不出任何统计量，因此没有泄漏效应量的风险。
任何人改了 `paper_portfolio` 或评分链之后，一条命令即可确认「A 还能不能复现生产」——这正是
本检验预注册前缺失的那个「先见过绿」的动作。需要完整 git 历史（浅克隆会明确报错，不会给假结果）。
⚠️ 在没有 `pheromone.db` 的 worktree 里跑，波动率查找会全部退回 `tier_fallback`，得到
「决策层对、金额层错」的假红——请在主 checkout 或设 `ALPHA_HIVE_DB_PATH` 指向真库。

种子核验（独立于本脚本）：`git show <SEED_SOURCE.json 里的 commit>:paper_portfolio_state/<文件> | shasum -a 256`
应与清单里该文件的 sha256 一致。

承载物：`ic_rerun_readiness.py` 每周被只读诊断任务调用，其摘要行会带上本检验的进度
（`status_line`），攒够周数时显著提示来跑本脚本。

退出码：0 = 已到检视点并给出结论（`--rehearse`：自证通过）/ 1 = 未就绪（正常）/ 3 = 无法判定
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

from ic_diagnostics import basic_stats, normal_two_sided_p  # noqa: E402

# ── 预注册常量（改动 = 事后，须在 CHANGELOG 说明；测试钉住）──────────────────
FORWARD_START = "2026-09-16"
LOOKS: Tuple[Tuple[int, float, str], ...] = ((15, 0.02, "中期"), (30, 0.045, "终期"))
SELFPROOF_MIN_RATE = 0.95
FG_GATE_TEST_CONFIG: Dict = {
    "fg_exposure_gate": {
        "enabled": True,
        "extreme_fear": 25,
        "extreme_greed": 75,
        "long_size_mult": 0.5,
        "short_size_mult": 0.5,
    },
}

STALE_DAYS = 21  # 登记后这么多天仍无前瞻样本 ⇒ 不是"还在攒"，是扫描停了或路径错了

# ── 窗口起点状态的种子（v0.45.297 事后修订，见文件头「自证」段）──────────────────
SEED_DIRNAME = "fg_gate_forward_seed"
SEED_MANIFEST_NAME = "SEED_SOURCE.json"
# 只播这三个。`equity_curve.jsonl` 只写不读，播进去会让窗口前的周也进统计量。
SEED_STATE_FILES: Tuple[str, ...] = ("meta.json", "positions.jsonl", "closed_trades.jsonl")
SEED_GIT_STATE_DIR = "paper_portfolio_state"  # 状态文件在 git 里的仓库相对路径（数据根迁移阶段 5 之前）
_SANDBOX_STATE_FILES: Tuple[str, ...] = SEED_STATE_FILES + ("equity_curve.jsonl",)


class SeedError(Exception):
    """窗口起点种子不可用（缺文件 / 校验和不符 / 起点日期不对 / 状态含非有限值 / git 历史取不到）。

    调用方**必须**把它变成「无法判定」，不许退回空沙箱——那正是 v0.45.297 修掉的设计缺陷：
    空沙箱起点会让自证在真实数据上必红，且文案还把原因指向别处。
    """


# ── 种子：加载 / 校验 / 从 git 生成 ────────────────────────────────────────────
def _seed_dir(seed_dir: Optional[Path] = None) -> Path:
    """冻结种子目录。**调用时求值**；`__file__` 锚点——它是随预注册协议发布的代码同址资源
    （同 `templates/`、`prompts/`），不是运行时数据，改成 `PATHS.home` 反而会在测试把 HOME
    指向 tmp 后找不到文件。"""
    return Path(seed_dir) if seed_dir is not None else Path(__file__).resolve().parent / SEED_DIRNAME


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _jsonl_rows(blob: bytes, name: str) -> List[Dict]:
    rows = []
    for i, line in enumerate(blob.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as e:
            raise SeedError(f"{name} 第 {i} 行不是合法 JSON：{e}") from e
    return rows


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _validate_seed_state(files: Dict[str, bytes], *, before: str) -> Dict:
    """种子内容自洽性检查；返回摘要。`before` = 窗口首日，种子的 `last_run_date` 必须严格早于它。

    这里挡的是「静默地把坏状态当起点」：`cash` 非有限（v0.45.97 的 NaN 事故让 08-28~09-02 的
    状态里 cash 就是 NaN）、持仓记录构造不出 `Position`、起点晚于窗口……任何一项都会让整个
    窗口的每个仓位继承同一个错，且看起来只是「复现率低」。
    """
    missing = [n for n in SEED_STATE_FILES if n not in files]
    if missing:
        raise SeedError(f"种子缺文件：{missing}")
    try:
        meta = json.loads(files["meta.json"])
    except ValueError as e:
        raise SeedError(f"meta.json 不是合法 JSON：{e}") from e
    lrd = meta.get("last_run_date") if isinstance(meta, dict) else None
    try:
        dt.date.fromisoformat(lrd)
    except (TypeError, ValueError):
        raise SeedError(f"meta.json 的 last_run_date={lrd!r} 不是 YYYY-MM-DD") from None
    if not lrd < before:
        raise SeedError(f"种子 last_run_date={lrd} 不早于窗口首日 {before}——起点必须是窗口开始「之前」的状态")
    if not _finite(meta.get("cash")):
        raise SeedError(f"种子 cash={meta.get('cash')!r} 非有限值——坏状态不能当起点（v0.45.97 的 NaN 事故形状）")

    import paper_portfolio as pp
    positions = _jsonl_rows(files["positions.jsonl"], "positions.jsonl")
    for r in positions:
        try:
            pp.Position(**r)
        except TypeError as e:
            raise SeedError(f"positions.jsonl 里有构造不出 Position 的记录（{r.get('ticker')}）：{e}") from e
        if not all(_finite(r.get(k)) for k in ("size_usd", "entry_price", "shares")):
            raise SeedError(f"positions.jsonl 里 {r.get('ticker')} 的 size_usd/entry_price/shares 含非有限值")
        if not str(r.get("entry_date", "")) <= lrd:
            raise SeedError(f"positions.jsonl 里 {r.get('ticker')} 的 entry_date={r.get('entry_date')} 晚于种子 last_run_date={lrd}")
    closed = _jsonl_rows(files["closed_trades.jsonl"], "closed_trades.jsonl")
    for r in closed:
        if not _finite(r.get("pnl_usd")):
            raise SeedError(f"closed_trades.jsonl 里 {r.get('ticker')} 的 pnl_usd 非有限值")
    return {"last_run_date": lrd, "cash": float(meta["cash"]),
            "n_positions": len(positions), "n_closed": len(closed)}


def load_seed(seed_dir: Optional[Path] = None, *, forward_start: str = FORWARD_START) -> Dict[str, bytes]:
    """加载并校验冻结种子。任何不符抛 `SeedError`——**不静默降级**。

    校验链：清单存在 → 清单的 `window_start` 等于 `forward_start`（改了起点没重建种子会红）→
    三个文件齐全且 sha256 与清单一致 → 内容自洽（见 `_validate_seed_state`）→
    清单的 `seed_last_run_date` 与 meta.json 里的一致。
    """
    d = _seed_dir(seed_dir)
    manifest_path = d / SEED_MANIFEST_NAME
    if not manifest_path.is_file():
        raise SeedError(f"种子清单不存在：{manifest_path}（种子没提交进仓库？）")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SeedError(f"种子清单不是合法 JSON：{e}") from e
    if manifest.get("window_start") != forward_start:
        raise SeedError(f"种子对应的窗口起点 {manifest.get('window_start')!r} ≠ FORWARD_START={forward_start!r}"
                        "——改了起点却没重建种子")
    files: Dict[str, bytes] = {}
    for name in SEED_STATE_FILES:
        p = d / name
        if not p.is_file():
            raise SeedError(f"种子缺文件：{p}")
        blob = p.read_bytes()
        want = ((manifest.get("files") or {}).get(name) or {}).get("sha256")
        if _sha256(blob) != want:
            raise SeedError(f"{name} 的 sha256 与清单不符（种子被改动过？）：实际 {_sha256(blob)[:16]}… ≠ 清单 {str(want)[:16]}…")
        files[name] = blob
    summary = _validate_seed_state(files, before=forward_start)
    if summary["last_run_date"] != manifest.get("seed_last_run_date"):
        raise SeedError(f"清单 seed_last_run_date={manifest.get('seed_last_run_date')!r} 与 meta.json 的 "
                        f"{summary['last_run_date']!r} 不一致")
    return files


def _git(repo: Path, *args: str) -> bytes:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SeedError(f"git 不可用（{type(e).__name__}: {e}）") from e
    if r.returncode != 0:
        raise SeedError(f"git {' '.join(args)} 失败（exit {r.returncode}）：{r.stderr.decode(errors='replace').strip()[:300]}")
    return r.stdout


def build_seed_from_git(since: str, repo_root: Optional[Path] = None) -> Tuple[Dict[str, bytes], Dict]:
    """从 git 历史取「生产处理窗口首日 `since` 之前」的状态；返回 (三个文件的原始字节, 清单)。

    定位规则：按时间顺序遍历改动过 `meta.json` 的提交，找**第一个** `last_run_date >= since` 的提交
    （生产在此首次处理了窗口内的日期），种子取它的**前一个**提交。
    ⚠️ 不能写成「`last_run_date < since` 的最新提交」——那会被窗口之后的回滚/还原提交带偏
    （状态倒退到更早日期的提交在 newest-first 遍历里会被先命中）。

    只信完整历史：浅克隆里最老的可见提交可能已是窗口之后，会把「历史被截断」误判成
    「窗口前没有状态」，故浅克隆直接报错。`--diff-filter=AM` 排除「已删除」的提交
    （数据根迁移阶段 5 会 `git rm --cached`），否则 `git show` 在删除提交上会失败。
    """
    if repo_root is None:
        from hive_logger import PATHS
        repo_root = PATHS.git_repo_root
    repo = Path(repo_root)
    if _git(repo, "rev-parse", "--is-shallow-repository").decode().strip() == "true":
        raise SeedError(f"{repo} 是浅克隆，git 历史不完整，取不到窗口起点状态（先 `git fetch --unshallow`）")
    rel_meta = f"{SEED_GIT_STATE_DIR}/meta.json"
    commits = _git(repo, "log", "--reverse", "--diff-filter=AM", "--format=%H", "--", rel_meta).decode().split()
    if not commits:
        raise SeedError(f"git 历史里没有 {rel_meta} 的任何提交")
    prev: Optional[str] = None
    crossing: Optional[str] = None
    for h in commits:
        try:
            lrd = json.loads(_git(repo, "show", f"{h}:{rel_meta}")).get("last_run_date")
        except (ValueError, AttributeError):
            continue  # 该提交的 meta.json 不可解析：不当作任何一侧的锚点；被选中的种子之后还会整体校验
        if isinstance(lrd, str) and lrd >= since:
            crossing = h
            break
        if isinstance(lrd, str):
            prev = h
    if crossing is None:
        raise SeedError(f"git 历史里没有 last_run_date >= {since} 的状态提交——窗口内生产尚未运行，无从确定种子")
    if prev is None:
        raise SeedError(f"窗口首日 {since} 之前生产没有任何状态提交——起点就是空状态，不需要（也没法）播种")
    files = {n: _git(repo, "show", f"{prev}:{SEED_GIT_STATE_DIR}/{n}") for n in SEED_STATE_FILES}
    summary = _validate_seed_state(files, before=since)
    info = _git(repo, "log", "-1", "--format=%cI%x09%s", prev).decode().strip().split("\t", 1)
    manifest = {
        "schema": 1,
        "purpose": "F&G 敞口门前瞻检验（预注册）的窗口起点状态种子；v0.45.297 事后修订引入，见脚本文件头「自证」段",
        "window_start": since,
        "seed_last_run_date": summary["last_run_date"],
        "source": {"commit": prev, "commit_iso": info[0], "subject": info[1] if len(info) > 1 else "",
                   "git_path": SEED_GIT_STATE_DIR},
        "files": {n: {"sha256": _sha256(b), "bytes": len(b)} for n, b in files.items()},
        "verify": f"git show {prev}:{SEED_GIT_STATE_DIR}/<文件> | shasum -a 256",
    }
    return files, manifest


def write_seed_dir(out_dir: Path, files: Dict[str, bytes], manifest: Dict) -> None:
    """把种子写成冻结目录。**拒绝覆盖**已有内容——冻结的意义就是它不会被悄悄换掉。"""
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} 已有内容，拒绝覆盖（冻结种子不许悄悄替换；确需重建请先人工核对并删除）")
    out.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (out / name).write_bytes(blob)
    (out / SEED_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _apply_seed(state_dir: Path, seed: Dict[str, bytes]) -> None:
    """把种子写进一个**全新**的沙箱目录。`run_replay` 对已存在的 state_dir 是「续跑」语义，
    往有状态的目录里再播种会与它混在一起，所以非空即拒。"""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    clash = [n for n in _SANDBOX_STATE_FILES if (state_dir / n).exists()]
    if clash:
        raise FileExistsError(f"沙箱 {state_dir} 已有状态文件 {clash}，播种会与「已存在则续跑」混在一起")
    unknown = [n for n in seed if n not in SEED_STATE_FILES]
    if unknown:
        raise ValueError(f"种子里有不许播的文件 {unknown}（只播 {list(SEED_STATE_FILES)}；"
                         "equity_curve.jsonl 只写不读，播进去会让窗口前的周也进统计量）")
    for name, blob in seed.items():
        (state_dir / name).write_bytes(blob)


# ── 重放（全部调真实 paper_portfolio 代码）─────────────────────────────────────
def _replay_variant(config_overrides: Dict, state_dir: Path, dates: List[str],
                    seed: Optional[Dict[str, bytes]] = None) -> Dict:
    """跑一个变体，返回 `run_replay` 原样结果 + 该沙盒里最终仍持有的仓位。

    `seed`：生产在窗口起点的状态（`{文件名: 原始字节}`）。`None`/`{}` = 空起点。
    """
    import paper_portfolio as pp
    if seed:
        _apply_seed(state_dir, seed)
    result = pp.run_replay(config_overrides, state_dir, dates=dates)
    result["open_positions"] = pp._load_jsonl(state_dir / "positions.jsonl")
    return result


def _entry_key(t: Dict) -> Tuple[str, str, str, float]:
    """建仓市值：`Position`（仍在场）直接有 `size_usd`；`ClosedTrade`（已平仓）没有这个字段，
    只能用 `shares × entry_price` 反推——两者对同一笔仓位应给出同一个数（下单时 `shares =
    size_usd / entry_price` 就是这么算的），敞口门测的正是这个建仓时刻的市值差异。"""
    size = t["size_usd"] if "size_usd" in t else t["shares"] * t["entry_price"]
    return (t["ticker"], t["entry_date"], t["direction"], round(float(size), 2))


def _entries_in_window(closed: List[Dict], open_positions: List[Dict],
                       since: str, before: str) -> set:
    """(ticker, entry_date, direction, size_usd) 四元组集合——窗口内所有"曾经开过"的仓位，
    不管现在是已平仓还是还在场（两个来源合并，覆盖"平仓时点晚于检查时点"这一类）。"""
    out = set()
    for t in closed + open_positions:
        ed = t.get("entry_date", "")
        if since <= ed < before:
            out.add(_entry_key(t))
    return out


def _real_recorded_entries(since: str, before: str) -> set:
    """生产**实际记录**的 `paper_portfolio_state/`——自证的比对基准，只读，不碰。"""
    import paper_portfolio as pp
    closed = pp._load_jsonl(pp.CLOSED_FILE)
    open_positions = pp._load_jsonl(pp.POSITIONS_FILE)
    return _entries_in_window(closed, open_positions, since, before)


def _selfproof_stats(real: set, a_entries: set) -> Dict:
    """自证的两层复现：**精确**（四元组含 `size_usd`，判定用）与**决策**（三元组，仅诊断用）。

    两层分开报是为了一眼看出是哪一层对不上：决策层对、精确层错 ⇒ 开哪只/什么方向都复现了，
    错的是仓位金额（起点状态/波动率来源/仓位参数）；决策层也错 ⇒ 评分链或入场规则变了。
    v0.45.297 之前只报精确层，「0/8」把这两种情形混成一个数，文案还把原因指错了方向。
    """
    total = len(real)
    exact = len(real & a_entries)
    decision = len({k[:3] for k in real} & {k[:3] for k in a_entries})
    return {"total": total, "exact": exact, "decision": decision,
            "rate": exact / total if total else None,
            "decision_rate": decision / total if total else None}


def _selfproof_failure_reason(sp: Dict) -> str:
    head = f"A（baseline）重放复现生产记录仅 {sp['exact']}/{sp['total']}（< {SELFPROOF_MIN_RATE:.0%}）"
    if sp["decision_rate"] is not None and sp["decision_rate"] >= SELFPROOF_MIN_RATE:
        return (f"{head}，但决策层（标的/日期/方向）复现 {sp['decision']}/{sp['total']}——开哪只、开哪个方向"
                "都对得上，对不上的是仓位金额。先查：种子是否对应窗口起点、波动率来源（pheromone.db）"
                "是否与生产一致、组合层仓位/出场参数是否被改动")
    return (f"{head}，决策层（标的/日期/方向）也仅复现 {sp['decision']}/{sp['total']}——评分链/入场规则/"
            "组合层配置已被改动，或重放机制本身有问题，本检验前提不成立")


def _weekly_nav_returns(equity: List[Dict]) -> Dict[Tuple[int, int], float]:
    """NAV 序列 → 每 ISO 周的收益率（%）：本周首个交易日 NAV 相对上一个"首个交易日"取值点
    的变化。相邻两个取值点之间可能不足严格 7 天（数据有缺口时），这是近似，不强求精确周边界。
    """
    by_day = {e["date"]: e["nav"] for e in equity if e.get("nav") is not None}
    first_of_week: "collections.OrderedDict" = collections.OrderedDict()
    for d in sorted(by_day):
        wk = dt.date.fromisoformat(d).isocalendar()[:2]
        if wk not in first_of_week:
            first_of_week[wk] = by_day[d]
    weeks = list(first_of_week)
    out = {}
    for i in range(1, len(weeks)):
        prev, cur = first_of_week[weeks[i - 1]], first_of_week[weeks[i]]
        if prev and prev > 0:
            out[weeks[i]] = (cur / prev - 1) * 100.0
    return out


# ── 统计（复用 ic_diagnostics 的规范实现，只补一层单侧转换）────────────────────
def one_sided_greater_than_zero(values: List[float]) -> Dict:
    """H1: 均值 > 0。全部相同（含全 0——多数周没有极端 F&G 触发时的正常形态）时
    `se=0`，`ic_diagnostics.basic_stats` 给的 t 是 nan，这里按符号显式判 p。"""
    m, se, t, n = basic_stats(values)
    if n < 2:
        return {"n": n, "mean": m, "t": None, "p": 1.0}
    if se == 0 or not (t == t):  # se==0（全同值）或 t 非有限
        return {"n": n, "mean": m, "t": None, "p": 0.0 if m > 0 else 1.0}
    p_two = normal_two_sided_p(t)
    return {"n": n, "mean": m, "t": t, "p": (p_two / 2 if t > 0 else 1 - p_two / 2)}


def weekly_deltas(a_equity: List[Dict], b_equity: List[Dict]) -> List[Dict]:
    ra, rb = _weekly_nav_returns(a_equity), _weekly_nav_returns(b_equity)
    common = sorted(set(ra) & set(rb))
    return [{"week": f"{wk[0]}-W{wk[1]:02d}", "ret_a": ra[wk], "ret_b": rb[wk],
             "delta": rb[wk] - ra[wk]} for wk in common]


def decide(weeks: List[Dict], looks=LOOKS) -> Dict:
    """成组序贯判定。**未到结论时返回值里不含任何效应量**（盲化在数据结构上，不在打印上）。"""
    k = len(weeks)
    for n_req, alpha, label in looks:
        if k < n_req:
            break
        st = one_sided_greater_than_zero([w["delta"] for w in weeks[:n_req]])
        if st["p"] < alpha:
            return {"status": "confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
        if (n_req, alpha, label) == looks[-1]:
            return {"status": "not_confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
    passed = [lab for n_req, _, lab in looks if k >= n_req]
    nxt = next(n_req for n_req, _, _ in looks if k < n_req)
    return {"status": "not_ready", "weeks": k, "next_look_at": nxt,
            "looks_passed_without_verdict": passed}


# ── 编排 ──────────────────────────────────────────────────────────────────────
def _adjusted_trades_summary(a_closed: List[Dict], b_closed: List[Dict],
                             since: str, before: str) -> Dict:
    """附带统计（不参与判定）：哪些笔的仓位大小被敞口门改了，改完已平仓的那部分盈亏对比。"""
    a_by_key = {(t["ticker"], t["entry_date"]): t for t in a_closed if since <= t["entry_date"] < before}
    b_by_key = {(t["ticker"], t["entry_date"]): t for t in b_closed if since <= t["entry_date"] < before}
    # "被调整"的判据：同一笔（ticker, entry_date）的建仓市值（shares × entry_price）在
    # 两个变体里不同——直接比 shares 也行（同一 entry_price 下等价），用市值更直观。
    adjusted = [(k, a_by_key[k], b_by_key[k]) for k in a_by_key
               if k in b_by_key and round(a_by_key[k]["shares"] * a_by_key[k]["entry_price"], 2)
               != round(b_by_key[k]["shares"] * b_by_key[k]["entry_price"], 2)]
    return {
        "adjusted_closed_trades": len(adjusted),
        "pnl_sum_a": round(sum(t[1]["pnl_usd"] for t in adjusted), 2),
        "pnl_sum_b": round(sum(t[2]["pnl_usd"] for t in adjusted), 2),
    }


def evaluate(dates: List[str], since: str, before: str, sandbox_root: Path,
            *, insample: bool = False, seed: Optional[Dict[str, bytes]] = None) -> Dict:
    """`seed`：生产在窗口起点的状态（见 `load_seed`）。**前瞻模式必传**——没有它就是 v0.45.297 之前的
    设计缺陷（空沙箱起点，自证必红）；`{}` 是显式的「生产当时也是空状态」，只给合成测试用。
    样本内模式从 `bootstrap_date` 起，生产当时本来就是空状态，**不许**传种子。"""
    if not insample and seed is None:
        raise ValueError("前瞻模式必须显式传 seed（生产窗口起点状态，见 load_seed）：空沙箱起点会让自证在真实数据上必红")
    if insample and seed:
        raise ValueError("样本内模式从 bootstrap_date 起，生产当时是空状态，不许传 seed")
    a_dir, b_dir = sandbox_root / "A_baseline", sandbox_root / "B_treatment"
    a = _replay_variant({}, a_dir, dates, seed=seed)
    b = _replay_variant(FG_GATE_TEST_CONFIG, b_dir, dates, seed=seed)

    out: Dict = {"mode": "insample" if insample else "forward", "n_dates": len(dates)}
    if seed and "meta.json" in seed:
        out["seed_last_run_date"] = json.loads(seed["meta.json"]).get("last_run_date")

    if insample:
        # 样本内没有"生产实际记录"可比——自证换成检查机制本身没写错：
        # A（enabled=False）与"完全不传 fg_exposure_gate 覆盖"必须逐笔相同
        # （这正是 Part 1 落地时的核心前提，这里顺带在真实前瞻脚本路径上再核一遍）。
        a_entries = _entries_in_window(a["closed"], a["open_positions"], since, before)
        a_again = _replay_variant({"fg_exposure_gate": {"enabled": False}}, sandbox_root / "A_check", dates)
        a_again_entries = _entries_in_window(a_again["closed"], a_again["open_positions"], since, before)
        mechanism_ok = a_entries == a_again_entries
        out["mechanism_selfcheck_ok"] = mechanism_ok
        if not mechanism_ok:
            return {**out, "status": "cannot_judge",
                    "reason": "机制自检失败：baseline(默认配置) 与显式 enabled=False 重放结果不一致，"
                             "重放机制本身有问题，不是效应"}
    else:
        real_entries = _real_recorded_entries(since, before)
        a_entries = _entries_in_window(a["closed"], a["open_positions"], since, before)
        sp = _selfproof_stats(real_entries, a_entries)
        out["selfproof"] = {"real_entries": sp["total"], "reproduced": sp["exact"],
                            "decision_reproduced": sp["decision"]}
        out["selfproof_rate"] = sp["rate"]
        out["selfproof_decision_rate"] = sp["decision_rate"]
        if sp["total"] == 0:
            return {**out, "status": "not_ready", "weeks": 0, "next_look_at": LOOKS[0][0],
                    "looks_passed_without_verdict": [],
                    "reason": f"窗口内生产还没有任何真实开仓记录（{since}~{before}）"}
        if sp["rate"] is None or sp["rate"] < SELFPROOF_MIN_RATE:
            return {**out, "status": "cannot_judge", "reason": _selfproof_failure_reason(sp)}

    weeks = weekly_deltas(a["equity"], b["equity"])
    out["weeks_available"] = len(weeks)
    out["adjusted_trades"] = _adjusted_trades_summary(a["closed"], b["closed"], since, before)

    if insample:  # 样本内不盲化、不走检视：它是生成假设/自检机制的数据
        return {**out, "status": "insample",
                "stats_all_weeks": one_sided_greater_than_zero([w["delta"] for w in weeks]) if weeks else None,
                "weekly_deltas": weeks}

    verdict = decide(weeks)
    out.update(verdict)
    return out


def _snapshot_dates_in_window(since: str, before: str) -> List[str]:
    import paper_portfolio as pp
    return sorted(d for d in pp._all_snapshot_dates()
                  if pp.CONFIG["bootstrap_date"] <= d and since <= d < before)


def run(insample: bool = False, today: Optional[str] = None) -> Dict:
    import paper_portfolio as pp

    if not any(pp.SNAPSHOT_DIR.glob("analysis-*-ml-*.json")) and not any(pp.SNAPSHOT_DIR.glob("*_*.json")):
        return {"status": "cannot_judge", "reason": f"{pp.SNAPSHOT_DIR} 下没有任何历史快照（路径错了？）"}

    today_d = today or dt.date.today().isoformat()
    since, before = (pp.CONFIG["bootstrap_date"], FORWARD_START) if insample else (FORWARD_START, today_d)
    dates = _snapshot_dates_in_window(since, before)

    if not insample and not dates:
        days = (dt.date.fromisoformat(today_d) - dt.date.fromisoformat(FORWARD_START)).days
        res = {"status": "not_ready", "weeks": 0, "next_look_at": LOOKS[0][0],
              "looks_passed_without_verdict": [], "mode": "forward", "n_dates": 0,
              "reason": f"尚无 {FORWARD_START} 起的前瞻样本"}
        if days > STALE_DAYS:
            res["stale"] = True
            res["reason"] = f"登记后 {days} 天仍无前瞻样本 —— 扫描停了，或快照不在 {pp.SNAPSHOT_DIR}"
        return res

    seed: Optional[Dict[str, bytes]] = None
    if not insample:
        try:
            seed = load_seed()
        except SeedError as e:
            # 绝不退回空沙箱：那会让自证在真实数据上必红，且原因会被读成「评分链被改」。
            return {"status": "cannot_judge", "mode": "forward", "n_dates": len(dates),
                    "reason": f"窗口起点种子不可用：{e}"}

    with tempfile.TemporaryDirectory(prefix="fg_gate_fwd_") as tmp:
        return evaluate(dates, since, before, Path(tmp), insample=insample, seed=seed)


def rehearse(since: str, before: str, repo_root: Optional[Path] = None) -> Dict:
    """自证演练：任一窗口，从 git 现取「窗口首日前」的生产状态播种，**只重放 A**，报复现率。

    不跑 B、不算周度差、不出任何统计量——只回答「A 还能不能复现生产」，所以没有泄漏效应量的风险。
    这是本检验预注册前缺失的「先见过绿」的动作（v0.45.297）：自证若从没在真实数据上绿过，
    它红的时候你就分不清是被测对象坏了还是自证自己坏了。
    """
    out: Dict = {"mode": "rehearse", "since": since, "before": before}
    try:
        seed, manifest = build_seed_from_git(since, repo_root)
    except SeedError as e:
        return {**out, "status": "cannot_judge", "reason": f"无法取窗口起点状态：{e}"}
    out["seed_commit"] = manifest["source"]["commit"]
    out["seed_last_run_date"] = manifest["seed_last_run_date"]
    dates = _snapshot_dates_in_window(since, before)
    out["n_dates"] = len(dates)
    if not dates:
        return {**out, "status": "not_ready", "reason": f"窗口 [{since}, {before}) 内没有快照日"}
    real = _real_recorded_entries(since, before)
    if not real:
        return {**out, "status": "not_ready", "reason": f"窗口 [{since}, {before}) 内生产没有真实开仓记录，无从比对"}
    with tempfile.TemporaryDirectory(prefix="fg_gate_rehearse_") as tmp:
        a = _replay_variant({}, Path(tmp) / "A_baseline", dates, seed=seed)
    sp = _selfproof_stats(real, _entries_in_window(a["closed"], a["open_positions"], since, before))
    out["selfproof"] = {"real_entries": sp["total"], "reproduced": sp["exact"], "decision_reproduced": sp["decision"]}
    out["selfproof_rate"] = sp["rate"]
    out["selfproof_decision_rate"] = sp["decision_rate"]
    if sp["rate"] < SELFPROOF_MIN_RATE:
        return {**out, "status": "cannot_judge", "reason": _selfproof_failure_reason(sp)}
    return {**out, "status": "rehearsal_ok"}


def status_line(res: Dict) -> str:
    """一行进度，供 `ic_rerun_readiness` 摘要行引用。不含效应量。"""
    s = res.get("status")
    if s == "cannot_judge":
        return f"⚠️ F&G 敞口门前瞻检验无法判定：{res.get('reason')}"
    if s == "not_ready":
        extra = f"（中期未过界，继续攒到 {res['next_look_at']} 周）" if res.get("looks_passed_without_verdict") else ""
        rate = res.get("selfproof_rate")
        proof = f"，自证 {rate:.0%}" if rate is not None else ""
        why = f"（{res['reason']}）" if res.get("reason") else ""
        icon = "⚠️" if res.get("stale") else "⏳"
        return f"{icon} F&G 敞口门前瞻检验：{res.get('weeks', 0)}/{res['next_look_at']} 个合格周{extra}{proof}{why}"
    if s in ("confirmed", "not_confirmed"):
        return (f"🔔 F&G 敞口门前瞻检验已到{res['look']}检视点 —— 跑 "
                "`/usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py` 看结论"
                "（需人判断，勿自动改 CONFIG）")
    return f"⚠️ F&G 敞口门前瞻检验状态未知：{s}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="F&G 组合层敞口控制门前瞻检验（预注册）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--today", help="覆盖今天的日期（测试用，YYYY-MM-DD）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--insample", action="store_true", help="样本内复核（生成假设/自检机制，不能用来确认）")
    mode.add_argument("--rehearse", nargs=2, metavar=("SINCE", "BEFORE"),
                      help="自证演练：对窗口 [SINCE, BEFORE) 只重放 A、报复现率（不跑 B、不出统计量）")
    mode.add_argument("--build-seed", action="store_true",
                      help="从 git 历史生成冻结种子（一次性；拒绝覆盖已有种子）")
    args = ap.parse_args(argv)

    if args.build_seed:
        try:
            files, manifest = build_seed_from_git(FORWARD_START)
            write_seed_dir(_seed_dir(), files, manifest)
        except (SeedError, FileExistsError) as e:
            print(f"❌ 生成种子失败：{e}", file=sys.stderr)
            return 3
        print(f"✅ 已冻结种子 → {_seed_dir()}\n   来源提交 {manifest['source']['commit']}"
              f"（last_run_date={manifest['seed_last_run_date']}）")
        return 0

    res = rehearse(*args.rehearse) if args.rehearse else run(insample=args.insample, today=args.today)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(res)
    return {"confirmed": 0, "not_confirmed": 0, "insample": 0, "rehearsal_ok": 0,
            "not_ready": 1}.get(res.get("status"), 3)


def _print_human(res: Dict) -> None:
    print("━" * 72)
    print(f"🐝 F&G 敞口门前瞻检验（{res.get('mode', '?')}）")
    print("━" * 72)
    if res.get("mode") == "rehearse":
        sp = res.get("selfproof") or {}
        print(f"  演练窗口 [{res.get('since')}, {res.get('before')})｜播种提交 {str(res.get('seed_commit'))[:8]}"
              f"（last_run_date={res.get('seed_last_run_date')}）｜日期数 {res.get('n_dates')}")
        if sp:
            print(f"  精确复现 {sp['reproduced']}/{sp['real_entries']}｜决策层复现 "
                  f"{sp['decision_reproduced']}/{sp['real_entries']}（判定只看精确层，阈值 {SELFPROOF_MIN_RATE:.0%}）")
        print("  结论：" + ("✅ 自证通过 —— A 能逐分复现生产" if res.get("status") == "rehearsal_ok"
                         else f"❌ {res.get('status')}：{res.get('reason')}"))
        return
    print(f"  日期数 {res.get('n_dates')}｜自证 {res.get('selfproof')}｜可用周数 {res.get('weeks_available')}")
    s = res.get("status")
    if s in ("confirmed", "not_confirmed"):
        st = res["stats"]
        print(f"  {res['look']}检视（前 {st['n']} 个合格周，单侧 α={res['alpha']}）："
              f"周均 ΔNAV% {st['mean']:+.4f}  t={st['t'] if st['t'] is None else round(st['t'], 2)}  p={st['p']:.4f}")
        print(f"  附带：被调整并已平仓的笔数 {res['adjusted_trades']['adjusted_closed_trades']}｜"
              f"这些笔的已实现盈亏 A={res['adjusted_trades']['pnl_sum_a']:+.2f} "
              f"vs B={res['adjusted_trades']['pnl_sum_b']:+.2f}")
        print("  结论：" + ("✅ 确认 —— 评估把 CONFIG['fg_exposure_gate']['enabled'] 改 True（用户拍板，非自动）"
                         if s == "confirmed" else "❌ 未确认 —— 保持默认关闭，登记结案"))
    elif s == "insample":
        st = res.get("stats_all_weeks")
        if st:
            print(f"  样本内（仅复核，不能确认）：{res['weeks_available']} 周，"
                  f"周均 ΔNAV% {st['mean']:+.4f} 单侧 p={st['p']:.4f}")
        print(f"  机制自检：{'✅ 通过' if res.get('mechanism_selfcheck_ok') else '❌ 失败'}")
    else:
        print("  " + status_line(res))


if __name__ == "__main__":
    sys.exit(main())
