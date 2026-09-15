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
MIN_JSON_LEAF_LEN = 12     # JSON 凭据文件里字符串叶子值的最短判定长度

# 不按扩展名白名单过滤——`SHA256SUMS` 这类无扩展名的纯文本文件曾经被
# 扩展名白名单漏扫（2026-09-15 实测：92 个文件里 19 个因扩展名不在白名单
# 里被跳过，`SHA256SUMS` 是其中之一）。改成"每个非 .git 文件都尝试当 UTF-8
# 文本读，解不出来才跳过"——判据是"读不读得进来"而不是"扩展名猜不猜得中"，
# 对本范围导出（只产生文本/校验和文件，不产生二进制）更完整也更不易漏。


def load_known_secrets(key_files: list[str] | None = None) -> list[tuple[str, str]]:
    """读本机真实密钥文件，返回 (来源文件名, 密钥字面量) 列表。

    返回值只在本进程内存里流转，调用方不得把它序列化到日志/报告。
    """
    secrets: list[tuple[str, str]] = []
    for pat in (key_files or REAL_KEY_FILES):
        p = Path(pat).expanduser()
        if not p.exists():
            continue
        name = p.name
        if p.suffix == ".json":
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            for leaf in _walk_json_strings(obj):
                leaf = leaf.strip()
                if len(leaf) >= MIN_JSON_LEAF_LEN:
                    secrets.append((name, leaf))
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            whole = text.strip()
            if len(whole) >= MIN_PLAIN_SECRET_LEN:
                secrets.append((name, whole))
            for line in text.splitlines():
                line = line.strip()
                if len(line) >= MIN_PLAIN_SECRET_LEN and line != whole:
                    secrets.append((name, line))
    # 去重（同一字面量可能被整文件匹配和逐行匹配各收一次）
    seen = set()
    out = []
    for name, val in secrets:
        key = (name, val)
        if key not in seen:
            seen.add(key)
            out.append((name, val))
    return out


def _walk_json_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json_strings(v)


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

    secrets = load_known_secrets()
    if not secrets:
        print("警告：本机一个真实密钥文件都没读到——扫描等于没扫，视为不通过。", file=sys.stderr)
        return 2

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
