"""仓库根「默认拒绝」数据指纹——**唯一实现**（v0.45.233，数据根迁移阶段 0.3）。

conftest 的 session 总闸与 `tests/test_root_data_guard.py` 共用这一份
（`tests/` 不是包、conftest 不可 import，所以判据抽到这里；两份实现早晚漂移，
而漂移那一刻两边都是绿的——同 `_artifact_signature` fixture 的理由）。

────────────────────────────────────────────────────────────────────────
为什么是「默认拒绝」而不是再加清单
────────────────────────────────────────────────────────────────────────
`_GUARDED_PRODUCTION_ARTIFACTS` 只列了 6 项，漏了 `probability_scorecard_state/`
（v0.45.150 普查按 `BASE_DIR` 名找，账本模块用的是 `ALPHAHIVE_DIR`——按名字漏检）。
清单守卫的失效方式是**静默**的：新产物一出现就不在清单里，而没人会因此变红。
这里反过来：仓库根下**除代码以外的一切**都受闸，新产物默认就被盯住；
要豁免的是代码，而漏豁免一个代码文件的后果是**吵**（被人改了就红），不是静默。

────────────────────────────────────────────────────────────────────────
「代码」怎么界定（其余一律是数据）
────────────────────────────────────────────────────────────────────────
判据是 CLAUDE.md「这个路径指向代码还是数据」那张表：**随代码发布、由人/agent
编写**的是代码；**系统运行时写出来的**是数据。仓库根恰好两者混居——
`README.md` 与 107 份 `alpha-hive-daily-<日期>.md` 同扩展名、同目录，没有任何
机械属性能分开它们（git 跟踪也不行：报告同样被跟踪）。所以代码的认定只取
**不会误伤数据**的三条，其余全部当数据：

1. **源代码扩展名**（`CODE_EXTS`）。已核实生产代码不产出这些扩展名的文件——
   唯一例外 `weekly_optimizer` 写 `config.py` / `weight_backups/config_*.py`，
   它的测试自己 patch 了路径；新目录 `weight_backups/` 本身仍会被目录项抓到。
   ⚠️ 判据是「生产既不**写**、也不在运行时**读**它当输入」，只查写者不够（v0.45.253）：
   `.yml`/`.yaml` 曾在此列，而 `config.py` 在运行时从仓库根读 `watchlist_override.yaml` 热加载
   WATCHLIST——测试写它就是改生产扫描的标的，旁边的 `.json` 版受闸、它却不受。
   现已移出（仓库里的 yaml 只剩 `_config.yml` 与 `.github/` 下的，后者由第 2 条豁免）。
2. **代码资源目录**（`CODE_TOP_DIRS`，仅顶层）：`templates/` `prompts/`（CLAUDE.md
   那张表点名的「`__file__` 才对」的例子）与 `.github/`。
3. **仓库元文件**（`CODE_ROOT_FILES`，仅根目录）：每个 session 都会改的
   `CHANGELOG.md` / `CLAUDE.md` / `README.md` 与打包配置。刻意**不**含
   `QUICK_START.md` 之类的陈年文档——测试期间它们要是变了，本来就该红。

⚠️ 往这三处加东西之前先问：它是人写的，还是系统写的？**系统写的东西不许加豁免**，
去把写入路径接到 `PATHS.*` 上。

────────────────────────────────────────────────────────────────────────
不看的东西（与「代码还是数据」无关，是根本不属于被测代码的写入）
────────────────────────────────────────────────────────────────────────
- `SKIP_DIR_NAMES`（任意深度按名跳过整棵）：`.git`（worktree 里是文件，同名一并跳过）；
  `.claude`（生产 checkout 下挂着一串嵌套 worktree，v0.45.189）；
  **工具链自己的缓存**——`__pycache__` / `.pytest_cache` / `.ruff_cache` 等，跑测试这件事
  本身就会写；第三方依赖树 `node_modules` / `.venv`。
  ⚠️ **应用缓存不在此列**：`cache/` `data_cache/` `.factor_cache/` 等是被测代码写的，
  v0.41.3 事故正是测试把 mock 期权链写进 `cache/options_snapshot_*`、被当日扫描当真数据复用。
- 文件系统噪声：Finder 的 `.DS_Store`、FUSE 的 `.fuse_hidden*`。

────────────────────────────────────────────────────────────────────────
指纹口径
────────────────────────────────────────────────────────────────────────
文件 `(size, mtime_ns)`，理由同 conftest `_artifact_signature` 的 docstring：sqlite 以读写
模式打开会顶 mtime 而内容不变，内容哈希会把它判成无事发生。两处特例：

- **目录也记一项（只记存在）**：测试 `os.makedirs` 一个冻结路径、一个字节都没写，
  也是写穿（仓库根那个无主的空 `backups/` 就是这么来的形状）。不记目录 mtime——
  它会随被跳过的 `__pycache__` / `.DS_Store` 变。
- **SQLite 共享内存文件 `*-shm` 只记存在与大小**：WAL 库的**纯读者**也会往 -shm 写读标记
  （scratch 库实测：`mode=ro` 读一遍后 -shm 内容变、主文件与 -wal 逐字节不变）；生产 `pheromone.db-shm`
  在无人写库时 mtime 也在变（2026-09-14 实测）——**是谁读的没核实**，常驻的只读 MCP 进程只是推断
  （v0.45.253 订正：v0.45.233 把推断写成了实测）。-shm/-wal 的**出现或消失**照样会红——那才是 v0.45.150
  「测试以读写模式打开了生产库」的证据。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re

SKIP_DIR_NAMES = frozenset({
    ".git", ".claude",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", ".tox", ".nox",
    "node_modules", ".venv", "venv",
})
NOISE_FILE_NAMES = frozenset({".DS_Store"})
NOISE_FILE_PREFIXES = (".fuse_hidden",)

CODE_EXTS = frozenset({
    ".py", ".pyi", ".pyc", ".sh",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css",
    ".toml", ".cfg", ".ini",
})
CODE_TOP_DIRS = frozenset({"templates", "prompts", ".github"})
CODE_ROOT_FILES = frozenset({
    "CHANGELOG.md", "CLAUDE.md", "README.md",
    "requirements.txt", ".gitignore", ".dockerignore", "Dockerfile", "Makefile", "Procfile",
})


def excluded_reason(relpath: str) -> str | None:
    """`relpath`（相对仓库根，`/` 分隔）不受闸的原因；受闸返回 None。"""
    parts = [p for p in relpath.replace(os.sep, "/").split("/") if p]
    if not parts:
        return "root"
    for p in parts[:-1]:
        if p in SKIP_DIR_NAMES:
            return f"skip-dir:{p}"
    name = parts[-1]
    if name in SKIP_DIR_NAMES:
        return f"skip-dir:{name}"
    if parts[0] in CODE_TOP_DIRS:
        return f"code-dir:{parts[0]}"
    if name in NOISE_FILE_NAMES or name.startswith(NOISE_FILE_PREFIXES):
        return "fs-noise"
    if len(parts) == 1 and name in CODE_ROOT_FILES:
        return "code-root-file"
    if os.path.splitext(name)[1].lower() in CODE_EXTS:
        return "code-ext"
    return None


# ───────────────────────── 真实数据根（阶段 6 ⑤，v0.45.409） ─────────────────────────
#
# 阶段 5 之后生产数据在 `$ALPHA_HIVE_HOME`（~/alpha-hive-data），不在仓库根。此前那道「点名 8 个产物」的
# 总闸（`_GUARDED_PRODUCTION_ARTIFACTS`）盯的是**仓库根里同名路径**——数据搬走后那些路径不存在，
# 闸只剩「别在仓库根凭空建出同名产物」这一层，而这一层仓库根的默认拒绝早已覆盖：它成了冗余的恒真闸。
# 真正的缺口是反过来的：测试逃出隔离、写到**真实数据根**（生产数据，不可重取）没有任何东西会红。
# 这里补上，口径与仓库根闸一致（默认拒绝、`(size, mtime_ns)`、`-shm` 只记大小、目录记存在）。

#: 数据根里**常驻有写入方、且不是测试能碰到的**东西：日志（MCP / 定时任务 / 每日扫描一直在写）、备份轮转。
#: 以及 `_` 打头的元目录（`_archive` / `_migration` / `_manual_backups` 迁移与人工留档）。
#: 新增豁免前先问：它是谁写的？「测试可能写到」的东西不许豁免。
#: 「测试碰不到」不再只靠这句注释：`tests/test_root_data_guard.py::test_paths_reaching_exempt_areas_are_exactly_the_known_ones`
#: 枚举 `PATHS` 的每个属性，落在豁免区的必须恰好是那里白名单上写明「谁负责隔离」的几个。
DATA_ROOT_VOLATILE = frozenset({"logs", "db_backups"})

#: `_` 打头、但**测试碰得到**、所以照样受闸的元目录（v0.45.427）。
#: `_git_backup`：v0.45.408 起 `PATHS.data_backup_repo` 指向它，测试经 PATHS 就能写进去。2026-10-03 变异 N20 把该属性写死成
#: `~/alpha-hive-data/_git_backup`，测试夹具 `bk_src`（嵌套 git 仓库）被 `shutil.move` 搬进了生产备份仓库，
#: 10-05 每日备份把它当嵌套仓库提交进了备份历史——当时本闸按「`_` 元目录 = 测试碰不到」整个豁免它，四天没人红。
#: 它的 `.git/` 仍按 `SKIP_DIR_NAMES` 跳过（git 自己的簿记）；每日备份 Step 14 在扫描时段里写它，与闸对扫描时段的既有提醒同一回事。
DATA_ROOT_WATCHED_META = frozenset({"_git_backup"})


def data_root_excluded_reason(relpath: str) -> str | None:
    parts = [p for p in relpath.replace(os.sep, "/").split("/") if p]
    if not parts:
        return "root"
    top = parts[0]
    if top in DATA_ROOT_VOLATILE:
        return f"volatile:{top}"
    if top.startswith("_") and top not in DATA_ROOT_WATCHED_META:
        return f"meta-dir:{top}"
    for p in parts:
        if p in SKIP_DIR_NAMES:
            return f"skip-dir:{p}"
    name = parts[-1]
    if name in NOISE_FILE_NAMES or name.startswith(NOISE_FILE_PREFIXES):
        return "fs-noise"
    return None


def exempt_reachable_paths(relpaths: dict[str, str]) -> dict[str, str]:
    """`{名字: 数据根相对路径}` 中被本闸豁免的那些 → `{名字: 豁免原因}`（v0.45.427）。

    用法：喂 `PATHS` 各属性在沙箱数据根下解析出的相对路径——落在豁免区的就是「测试碰得到、闸却不看」的位置。
    数据根本身（`.`）不算。"""
    out = {}
    for name, rel in relpaths.items():
        rel = rel.replace(os.sep, "/").strip("/")
        if rel in ("", "."):
            continue
        reason = data_root_excluded_reason(rel)
        if reason is not None:
            out[name] = reason
    return out


def real_data_root(environ, home: str, repo_root: str) -> str | None:
    """测试进程**启动时**的真实数据根；没有就返回 None（本机之外的机器上它不存在）。

    必须在任何 env 隔离之前取（`_isolate_env` 会把 `ALPHA_HIVE_HOME` 改成沙箱）：`ALPHA_HIVE_HOME`
    若在调用环境里设了就用它，否则取生产默认位置 `~/alpha-hive-data`。与仓库根相同的不算（那由仓库根闸管）。
    ⚠️ 返回 None ⇒ 本闸在这台机器上**没有任何作用**——CI / 干净克隆都是这样，所以它的牙只能靠
    `tests/test_root_data_guard.py` 里合成数据根的测试证明，不能靠「这轮全套绿」证明。
    """
    cand = environ.get("ALPHA_HIVE_HOME") or os.path.join(home, "alpha-hive-data")
    cand = os.path.abspath(os.path.expanduser(cand))
    if not os.path.isdir(cand) or cand == os.path.abspath(repo_root):
        return None
    return cand


def fingerprint(root: str, excluded=None) -> dict[str, str]:
    """`root` 下全部受闸条目 → 指纹。目录记 `dir`，文件记 `size:mtime_ns`，`*-shm` 记 `shm:size`。

    `excluded`：`relpath -> 不受闸原因 | None`，默认 `excluded_reason`（仓库根的「代码」判据）；
    数据根用 `data_root_excluded_reason`。同一份遍历与口径，只换「什么不看」。"""
    excluded = excluded or excluded_reason
    out: dict[str, str] = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if excluded(rel) is None:
                keep.append(d)
                out[rel] = "dir"
        dirnames[:] = sorted(keep)
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if excluded(rel) is not None:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except OSError as e:          # 遍历期间消失：记下来，前后比对时自然算变化
                out[rel] = f"ERR:{e.errno}"
                continue
            out[rel] = f"shm:{st.st_size}" if fn.endswith("-shm") else f"{st.st_size}:{st.st_mtime_ns}"
    return out


def diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """只返回非空的类别：added / removed / changed。"""
    res = {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }
    return {k: v for k, v in res.items() if v}


def format_diff(d: dict[str, list[str]], limit: int = 25) -> str:
    lines = []
    for kind in ("added", "removed", "changed"):
        items = d.get(kind, [])
        if not items:
            continue
        lines.append(f"  {kind}（{len(items)}）：")
        lines.extend(f"    {p}" for p in items[:limit])
        if len(items) > limit:
            lines.append(f"    …另 {len(items) - limit} 项")
    return "\n".join(lines)


# ═══════════════ Alpha Bot 盘中采样：报红时说清楚是谁写的（v0.45.453）═══════════════
#
# 只影响**报错措辞**，判定照旧：有变化就红。理由：闸不认「看起来像合法写入」，只认「没有变化」——
# 一旦按路径放行，测试写穿到同一个目录就再也没人会红（「不要往豁免里加路径」）。
# 要治的是另一件事：盘中 Alpha Bot 每隔几分钟整批追加一次，跑测试 / 推 main 偶尔撞上，
# 旧报错一律说「测试写穿了生产数据」，再由 pre-push 建议 `--no-verify`——人要么被误导去查测试，
# 要么养成绕过的习惯（连 CHANGELOG 检查一起跳过）。10-09 实测还出过反方向的错判：
# 把同一批里相隔几秒的 mtime 当成「每几秒写一次」。所以归因必须逐项核实，不靠路径、不靠猜。

#: Alpha Bot 盘中快照的落盘位置：`alphabot/service.py` 的 `_intraday_dir` + `take_snapshot`
#: （`alphabot_state/intraday/<美东日期>/<标的>.jsonl`，只追加、一次写一整行）。
_ALPHABOT_DAY_RE = re.compile(r"^alphabot_state/intraday/(\d{4}-\d{2}-\d{2})$")
_ALPHABOT_FILE_RE = re.compile(r"^alphabot_state/intraday/(\d{4}-\d{2}-\d{2})/[^/]+\.jsonl$")
#: 一张快照的键 = `AlphaBotService.snapshot_from` 的产出，一个不多一个不少
#: （`tests/test_root_data_guard.py::TestAlphabotAppendClassifier::test_keys_match_the_writer` 守它不漂移）。
ALPHABOT_SNAPSHOT_KEYS = frozenset({"ts", "payload_last_trade_time", "underlying_price", "underlying_price_source",
                                    "session_live", "iv30", "views", "route"})
#: `take_snapshot` 先取时间戳、再现拉数据、最后落盘 ⇒ 会话开始之后才落盘的那一张，ts 可能早于会话开始。
ALPHABOT_TS_SLACK_BEFORE_S = 300
ALPHABOT_TS_SLACK_AFTER_S = 60
#: 归因结果挂在 pytest `config` 上的属性名：`changelog_guard._REPORT_PLUGIN` 读它，决定 pre-push 怎么提示
#: （`tests/test_changelog_guard_contract_gate.py` 守两处同名）。
VERDICT_CONFIG_ATTR = "_alpha_hive_real_data_root_verdict"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def alphabot_baseline(root: str, fp: dict[str, str], today: _dt.date) -> dict[str, str]:
    """会话开始时，近两天 Alpha Bot 盘中文件的 sha256——之后用来证明它们「只在末尾追加、原有内容没动」。

    只算日期 ≥ `today` − 1 的目录：Alpha Bot 只往当天（美东）的目录追加，美东日期不早于本机日期；
    全量算会随历史目录一天天变多、拖慢每一轮会话启动。读不了的文件不记——之后它若有变化，
    就因为「没有起点哈希」归到「其余」，照原样报成测试写的（往保守那边错）。"""
    cutoff = (today - _dt.timedelta(days=1)).isoformat()
    out: dict[str, str] = {}
    for rel, sig in fp.items():
        m = _ALPHABOT_FILE_RE.match(rel)
        if not m or m.group(1) < cutoff or sig == "dir":
            continue
        try:
            with open(os.path.join(root, rel), "rb") as f:
                out[rel] = _sha256_bytes(f.read())
        except OSError:
            continue
    return out


def _old_size(sig: str | None) -> int | None:
    if sig is None:
        return 0                                   # 新文件：原有内容为空
    size, sep, _mtime = sig.partition(":")
    return int(size) if sep and size.isdigit() else None


def _why_not_alphabot_append(root: str, rel: str, before_sig: str | None, baseline: dict[str, str],
                             lo: _dt.datetime, hi: _dt.datetime) -> tuple[str | None, int]:
    """`rel` 的变化能否归因于 Alpha Bot 追加：返回 (不能的原因 | None, 新增快照张数)。"""
    m = _ALPHABOT_FILE_RE.match(rel)
    if not m:
        return "不是 Alpha Bot 盘中文件的路径", 0
    old = _old_size(before_sig)
    if old is None:
        return f"变化前的指纹认不出大小（{before_sig}）", 0
    try:
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
    except OSError as e:
        return f"读不了（{e.strerror}）", 0
    if len(data) <= old:
        return "没有变长（不是追加）", 0
    if old and (rel not in baseline or _sha256_bytes(data[:old]) != baseline[rel]):
        return ("没有会话开始时的哈希，证明不了原有内容没动" if rel not in baseline
                else "原有内容被改写（不只是在末尾追加）"), 0
    added = data[old:]
    if not added.endswith(b"\n"):
        return "新增部分不是完整的行", 0
    try:
        lines = added.decode("utf-8").split("\n")[:-1]
    except UnicodeDecodeError:
        return "新增部分不是 UTF-8", 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            return "新增行不是 JSON", 0
        if not isinstance(rec, dict) or set(rec) != ALPHABOT_SNAPSHOT_KEYS:
            return "新增行不是 Alpha Bot 快照（键对不上）", 0
        try:
            ts = _dt.datetime.fromisoformat(rec["ts"])
        except (TypeError, ValueError):
            return "快照的 ts 不是 ISO 时间", 0
        if ts.tzinfo is None:
            return "快照的 ts 没有时区", 0
        if not lo <= ts <= hi:
            return f"快照时间 {rec['ts']} 不在本轮测试期间", 0
        if ts.date().isoformat() != m.group(1):
            return f"快照日期与目录日期 {m.group(1)} 不一致", 0
    return None, len(lines)


def explain_alphabot_appends(root: str, changes: dict[str, list[str]], before: dict[str, str],
                             baseline: dict[str, str], started: _dt.datetime | None,
                             ended: _dt.datetime) -> dict:
    """把 `diff()` 的结果分成「已核实是 Alpha Bot 盘中追加」与「其余」。**只决定报错措辞，不决定红绿。**

    一个文件归因于 Alpha Bot，必须同时满足（任何一条不满足都归「其余」，宁可报成测试写的）：
      1. 路径是 `alphabot_state/intraday/<日期>/<标的>.jsonl`；
      2. 原有内容逐字节未变：变化前那个大小之内，sha256 等于会话开始时记下的（新文件则原有为空）；
      3. 新增部分是完整的行，每行都是一张快照：键集合恰为 `ALPHABOT_SNAPSHOT_KEYS`，`ts` 带时区、
         落在 [会话开始 − 300 s, 会话结束 + 60 s]，且 `ts` 的日期就是目录日期。
    新增的日期目录只有在它里面至少有一个文件被归因时才算。删除一律归「其余」。
    返回 `{"explained": [...], "records": n, "unexplained": {类别: [...]}, "why": {Alpha Bot 路径: 不能归因的原因}}`。
    """
    explained: list[str] = []
    unexplained: dict[str, list[str]] = {}
    why: dict[str, str] = {}
    records = 0
    if started is None:                          # 没有会话开始时间：一律不归因
        return {"explained": [], "records": 0, "unexplained": dict(changes), "why": {}}
    lo = started - _dt.timedelta(seconds=ALPHABOT_TS_SLACK_BEFORE_S)
    hi = ended + _dt.timedelta(seconds=ALPHABOT_TS_SLACK_AFTER_S)
    day_dirs = []
    for kind in ("added", "changed"):
        for rel in changes.get(kind, []):
            if kind == "added" and _ALPHABOT_DAY_RE.match(rel):
                day_dirs.append(rel)
                continue
            reason, n = _why_not_alphabot_append(root, rel, before.get(rel), baseline, lo, hi)
            if reason is None:
                explained.append(rel)
                records += n
            else:
                unexplained.setdefault(kind, []).append(rel)
                if rel.startswith("alphabot_state/"):
                    why[rel] = reason
    for rel in day_dirs:
        if any(e.startswith(rel + "/") for e in explained):
            explained.append(rel)
        else:
            unexplained.setdefault("added", []).append(rel)
            why[rel] = "新建的日期目录里没有任何一项被归因"
    if changes.get("removed"):
        unexplained["removed"] = list(changes["removed"])
    return {"explained": sorted(explained), "records": records,
            "unexplained": {k: sorted(v) for k, v in unexplained.items() if v}, "why": why}
