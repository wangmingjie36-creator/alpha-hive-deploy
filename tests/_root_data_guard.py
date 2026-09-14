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
- **SQLite 共享内存文件 `*-shm` 只记存在与大小**：WAL 库的**纯读者**也会往 -shm 写读标记、
  顶它的 mtime（2026-09-14 实测：生产 `pheromone.db-shm` 在无人写库时 mtime 仍在变，
  来自常驻的只读 MCP 进程）。-shm/-wal 的**出现或消失**照样会红——那才是 v0.45.150
  「测试以读写模式打开了生产库」的证据。
"""
from __future__ import annotations

import os

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
    ".toml", ".cfg", ".ini", ".yml", ".yaml",
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


def fingerprint(root: str) -> dict[str, str]:
    """仓库根下全部受闸条目 → 指纹。目录记 `dir`，文件记 `size:mtime_ns`，`*-shm` 记 `shm:size`。"""
    out: dict[str, str] = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if excluded_reason(rel) is None:
                keep.append(d)
                out[rel] = "dir"
        dirnames[:] = sorted(keep)
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if excluded_reason(rel) is not None:
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
