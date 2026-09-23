"""阶段 3.3 —— 导出后、提交前的密钥字面量扫描。

用户决定"数据仓库不加密"，所以每次提交进 `_git_backup`（本地裸仓库的工作区）
之前，必须拿本机**真实**密钥文件逐字面扫一遍导出产物，命中就拒绝提交
——不是拒绝推送，是提交这一步就要拦，防止密钥进了 git 历史就算之后删了也在
历史里（同「公开历史清不清」那节的教训）。

真实密钥文件清单（`ls -1a ~ | grep -iE '(alpha_hive|anthropic).*\\.(key|token)|
credentials'` 加上未被该正则命中但同族的 `_webhook`/`_pending.json`/`_bot_token`,
2026-09-15 现场核对）：见 `REAL_KEY_FILES`。

⚠️ 硬规矩：本模块任何时候都不打印密钥值本身——只报告"扫到/没扫到"、
命中的是导出产物里的哪个文件、对应哪个凭据文件名。

JSON 凭据叶子过滤（v0.45.307 修，二次检查发现）：旧逻辑把凭据 JSON 里任何
≥12 字符的字符串叶子都当密钥候选——`.alpha_hive_gmail_credentials.json`
这类标准 Google OAuth client_secret 文件里，`auth_uri`/`token_uri`/
`auth_provider_x509_cert_url` 三个字段本身就是**公开** URL，
`redirect_uris[0]`（`http://localhost`，16 字符）同理。这些公开字符串一旦
被当"密钥"收进比对表，会在任何提到过 localhost/google OAuth 端点的无关
文件里"命中"，且一旦某次导出产物含有这类字符串，之后每天的备份都会被
永久拦停（append-only 表里出现一次就一直在）——fail-closed 所以不危险，
但等于备份停摆，且报警文案会让人误以为真的泄密。
`_is_candidate_json_secret` 现在：① 无条件跳过 `http://`/`https://` 开头的
叶子；② 键名含 `_SENSITIVE_JSON_KEY_MARKERS` 之一（更像"这就是密钥"）用
`MIN_JSON_LEAF_LEN`（12）的低门槛；③ 其余叶子退回 `MIN_JSON_LEAF_LEN_GENERIC`
（20）的高门槛，压低短小通用字符串的误报面。

扫描守卫（v0.45.307 补，二次检查发现）：`load_known_secrets_with_diagnostics`
额外返回"磁盘上存在、但读取/解析失败"与"存在、可读、却一个密钥字面量都没
贡献"的文件名清单——`run_backup.run()` 拿这份诊断做"扫描等于没扫就拒绝"
的守卫（该模块此前只在 `main()` CLI 入口有这道守卫，生产实际走的
`run()` 完全没有，见 CHANGELOG）。`load_known_secrets` 保留原签名/原返回值
作为薄包装，不破坏既有直接调用方。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REAL_KEY_FILES: list[str] = [
    "~/.alpha_hive_av_key",
    "~/.alpha_hive_finnhub_key",
    "~/.alpha_hive_fred_key",
    "~/.alpha_hive_github_token",
    "~/.alpha_hive_gmail_credentials.json",
    "~/.alpha_hive_slack_bot_token",
    "~/.alpha_hive_slack_pending.json",
    "~/.alpha_hive_slack_webhook",
    "~/.alpha_hive_tiingo_key",
    "~/.alpha_hive_twelvedata_key",
    "~/.anthropic_api_key",
]

MIN_PLAIN_SECRET_LEN = 8   # 单值凭据文件（token/key）逐行的最短判定长度
MIN_JSON_LEAF_LEN = 12     # JSON 凭据文件里"键名像密钥"的叶子值的最短判定长度
MIN_JSON_LEAF_LEN_GENERIC = 20  # 键名不像密钥时的更高门槛（见 _is_candidate_json_secret）

# 键名含这些词之一才按 MIN_JSON_LEAF_LEN（更低门槛）收——如 client_secret/
# access_token/api_key/password/private_key。不含时退回 MIN_JSON_LEAF_LEN_GENERIC。
_SENSITIVE_JSON_KEY_MARKERS = ("secret", "token", "key", "password", "private_key")

# 不按扩展名白名单过滤——`SHA256SUMS` 这类无扩展名的纯文本文件曾经被
# 扩展名白名单漏扫（2026-09-15 实测：92 个文件里 19 个因扩展名不在白名单
# 里被跳过，`SHA256SUMS` 是其中之一）。改成"每个非 .git 文件都尝试当 UTF-8
# 文本读，解不出来才跳过"——判据是"读不读得进来"而不是"扩展名猜不猜得中"，
# 对本范围导出（只产生文本/校验和文件，不产生二进制）更完整也更不易漏。


def load_known_secrets(key_files: list[str] | None = None) -> list[tuple[str, str]]:
    """读本机真实密钥文件，返回 (来源文件名, 密钥字面量) 列表。

    薄包装：只要密钥列表、不关心诊断信息的既有调用方（`scan_secrets.main()`、
    直接调用本函数的测试）用这个签名不变的版本；`run_backup.run()` 的扫描
    守卫需要诊断信息，改用 `load_known_secrets_with_diagnostics`。
    返回值只在本进程内存里流转，调用方不得把它序列化到日志/报告。
    """
    secrets, _diag = load_known_secrets_with_diagnostics(key_files)
    return secrets


def load_known_secrets_with_diagnostics(
    key_files: list[str] | None = None,
) -> tuple[list[tuple[str, str]], dict]:
    """读本机真实密钥文件，返回 (密钥列表, 诊断字典)。

    诊断字典（均只含文件名，不含任何密钥值/内容，可安全写进 status.json）：
    - `existing_files`：候选清单里在磁盘上真实存在的文件名
    - `files_unreadable`：存在但读取/解析失败的文件名（非 UTF-8 / 非法 JSON）
    - `files_with_zero_secrets`：存在、可读，但过滤后一个密钥字面量都没贡献
      的文件名（比如整份 JSON 里的字符串叶子全是公开 URL 或太短）
    `run_backup.run()` 拿这份诊断做"扫描等于没扫就拒绝"的守卫：secrets 为空，
    或磁盘上存在的候选文件里有任何一个落进后两类，都不能悄悄放行提交。
    """
    secrets: list[tuple[str, str]] = []
    existing: set[str] = set()
    unreadable: set[str] = set()
    contributed: dict[str, int] = {}

    for pat in (key_files or REAL_KEY_FILES):
        p = Path(pat).expanduser()
        if not p.exists():
            continue
        name = p.name
        existing.add(name)
        contributed.setdefault(name, 0)
        if p.suffix == ".json":
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                unreadable.add(name)
                continue
            for leaf_key, leaf in _walk_json_strings(obj):
                leaf = leaf.strip()
                if not _is_candidate_json_secret(leaf_key, leaf):
                    continue
                secrets.append((name, leaf))
                contributed[name] += 1
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                unreadable.add(name)
                continue
            whole = text.strip()
            if len(whole) >= MIN_PLAIN_SECRET_LEN:
                secrets.append((name, whole))
                contributed[name] += 1
            for line in text.splitlines():
                line = line.strip()
                if len(line) >= MIN_PLAIN_SECRET_LEN and line != whole:
                    secrets.append((name, line))
                    contributed[name] += 1

    # 去重（同一字面量可能被整文件匹配和逐行匹配各收一次）——去重不影响
    # contributed 计数用于诊断的语义："贡献过 ≥1 个"，不需要精确到去重后的数量。
    seen = set()
    out = []
    for name, val in secrets:
        key = (name, val)
        if key not in seen:
            seen.add(key)
            out.append((name, val))

    diagnostics = {
        "existing_files": sorted(existing),
        "files_unreadable": sorted(unreadable),
        "files_with_zero_secrets": sorted(
            n for n in existing if n not in unreadable and contributed.get(n, 0) == 0
        ),
    }
    return out, diagnostics


def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _is_candidate_json_secret(key: str | None, value: str) -> bool:
    """JSON 字符串叶子是否该被当密钥候选。

    见模块 docstring「JSON 凭据叶子过滤」一节：先无条件滤掉公开 URL，
    再按键名是否"像密钥"分两级门槛。
    """
    if not value or _looks_like_url(value):
        return False
    if key and any(marker in key.lower() for marker in _SENSITIVE_JSON_KEY_MARKERS):
        return len(value) >= MIN_JSON_LEAF_LEN
    return len(value) >= MIN_JSON_LEAF_LEN_GENERIC


def _walk_json_strings(obj, _key=None):
    """深度优先遍历 JSON 值，产出 (所在键名, 字符串叶子) 对。

    列表元素没有自己的键名，沿用其容器（父 dict 的那个键）的 `_key`——
    如 `redirect_uris: ["http://localhost"]` 里，列表元素的 key 仍是
    `"redirect_uris"`，供 `_is_candidate_json_secret` 判断键名语义。
    """
    if isinstance(obj, str):
        yield _key, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_json_strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json_strings(v, _key)


def scan_directory(root: Path, secrets: list[tuple[str, str]]) -> dict:
    """扫 `root` 下所有文本文件是否含 `secrets` 里的任一字面量。

    返回 `{"hits": [...], "files_scanned": N, "files_skipped_unreadable": [...]}`，
    `hits` 里只有「文件相对路径 + 命中的凭据文件名」，绝不含密钥值本身。
    """
    root = Path(root)
    hits = []
    files_scanned = 0
    skipped = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if ".git" in p.relative_to(root).parts:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(str(p.relative_to(root)))
            continue
        files_scanned += 1
        for cred_name, secret in secrets:
            if secret and secret in content:
                hits.append({"file": str(p.relative_to(root)), "credential_source": cred_name})
    return {"hits": hits, "files_scanned": files_scanned, "files_skipped_unreadable": skipped}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="阶段 3.3 密钥字面量扫描（导出后、提交前）")
    ap.add_argument("--dir", required=True, help="要扫描的导出产物目录")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    secrets, diag = load_known_secrets_with_diagnostics()
    if not secrets:
        print("警告：本机一个真实密钥文件都没读到——扫描等于没扫，视为不通过。", file=sys.stderr)
        return 2
    if diag["files_unreadable"] or diag["files_with_zero_secrets"]:
        print(f"⚠️ 部分已存在的凭据文件未能贡献可比对的密钥字面量——"
              f"读取/解析失败：{diag['files_unreadable'] or '无'}；"
              f"贡献为零：{diag['files_with_zero_secrets'] or '无'}", file=sys.stderr)

    result = scan_directory(Path(args.dir), secrets)
    result["known_secret_sources"] = sorted({name for name, _ in secrets})
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {result['files_scanned']} 个文本文件，"
              f"密钥来源 {len(result['known_secret_sources'])} 个"
              f"（{', '.join(result['known_secret_sources'])}）")
        if result["files_skipped_unreadable"]:
            print(f"⚠️ 跳过（非 UTF-8，未扫到）：{result['files_skipped_unreadable']}")
        if result["hits"]:
            print(f"❌ 命中 {len(result['hits'])} 处，拒绝提交：")
            for h in result["hits"]:
                print(f"   {h['file']}  <=  凭据文件 {h['credential_source']}")
        else:
            print("✅ 未命中任何已知密钥字面量。")
    return 1 if result["hits"] else 0


if __name__ == "__main__":
    sys.exit(main())
