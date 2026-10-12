"""编排器静态检查——裸 `$VAR` 紧跟非 ASCII 字符（v0.45.356 从测试文件抽出，成为唯一实现）。

为什么：macOS `/bin/bash` 3.2 在 UTF-8 locale 下把非 ASCII 字符的 UTF-8 首字节吃进变量名，
`set -u` 让整个 shell 退出（命令参数里）或把 heredoc 目标文件清成 0 字节。完整取证与
「为什么要连 heredoc 正文一起扫」见 `tests/test_orchestrator_braced_vars.py` 模块 docstring。

使用者：
* `tests/test_orchestrator_braced_vars.py`——对仓库副本与合成文本断言；
* `deploy_orchestrator.py`——部署前关卡，命中即拒绝部署（v0.45.356）；
* 命令行（v0.45.418）：`/usr/local/bin/python3 orchestrator_lint.py [FILE ...]`，缺省扫仓库编排器。
  退出码 0 无命中 / 1 有命中 / 2 有文件读不了（未检查）。此前本模块没有入口，直接跑它只是 import 一遍、
  **对任何内容都 exit 0**——v0.45.414 曾据此报「orchestrator_lint.py 通过」，那是空检查。
"""

import re
import sys
from pathlib import Path

# 裸变量名（不含 `${…}`、`$1`/`$?`/`$#` 这类特殊参数——实测它们不会吞后面的字节）
# 紧跟任意非 ASCII 字符。故意宽于「实测会中招的那些首字节」：多抓一个 `א` 无害（加花括号总是对的），
# 少抓才会漏。`(?<!\\)` 跳过转义的 `\$VAR`（那是字面量美元符，不展开）。
_BARE_BEFORE_NON_ASCII = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


# `<<DELIM` / `<<-DELIM` / `<<'DELIM'` / `<<"DELIM"` / `<<\DELIM`。`(?<!<)…(?!<)` 排除 `<<<` here-string。
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def find_unbraced(text: str) -> list[tuple[int, str, str]]:
    """返回 `(行号, 变量名, 该行)`。

    整行注释 bash 不展开，跳过；**heredoc 正文里除外**——未加引号的 heredoc 里以 `#` 开头的行照样被展开，
    所以正文内不跳过注释行。这条规则出错只会退回「不跳」（多报，安全方向），不会多吞：
    找不到终止行的 `<<` 不当 heredoc（免得一次误判让后面全文的注释规则失灵）。
    加了引号的 heredoc（`<<'X'`，bash 不展开）也一并当正文扫——保守地多报，实际 0 例。行内注释照报。
    """
    lines = text.split("\n")
    hits: list[tuple[int, str, str]] = []
    end = None                                   # 正处于 heredoc 正文时：(终止行, 是否 `<<-` 剥制表符)
    for no, line in enumerate(lines, 1):
        if end is not None:
            delim, strip = end
            if (line.lstrip("\t") if strip else line) == delim:
                end = None
                continue
        elif line.lstrip().startswith("#"):
            continue
        hits += [(no, m.group(1), line) for m in _BARE_BEFORE_NON_ASCII.finditer(line)]
        opener = _HEREDOC_OPEN.search(line) if end is None else None
        if opener:
            delim, strip = opener.group(2) or opener.group(3) or opener.group(4), bool(opener.group(1))
            if any((rest.lstrip("\t") if strip else rest) == delim for rest in lines[no:]):
                end = (delim, strip)
    return hits


def main(argv: list[str] | None = None) -> int:
    """退出码：0 无命中 / 1 有命中 / 2 有文件读不了（不存在、是目录、无权限、非 UTF-8）。

    2 优先于 1（同 grep）：有文件没扫到，就说不出「命中只有这些」。读不了的文件不跳过成 0——
    那正是本入口要治的形状（检查器不会失败 ⇒ 「通过」不携带信息）。命中进 stdout（`路径:行号: …`），
    每个文件扫了几行进 stderr：扫了个空文件 / 错文件时，「0 处命中」旁边的行数会露馅。
    """
    import argparse

    # 编排器是**代码**（随仓库发布），按 `__file__` 锚定而不是 cwd / PATHS；放在函数里，不做模块级常量
    default = Path(__file__).resolve().parent / "scripts" / "alpha-hive-orchestrator.sh"
    ap = argparse.ArgumentParser(description="找裸 `$VAR` 紧跟非 ASCII 字符（macOS bash 3.2 在 UTF-8 locale 下误解析）")
    ap.add_argument("files", nargs="*", type=Path, metavar="FILE", help=f"要扫的脚本（缺省：{str(default).replace('%', '%%')}）")
    args = ap.parse_args(argv)
    rc = 0
    for path in args.files or [default]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{path}: 读不了，未检查：{e}", file=sys.stderr)
            rc = 2
            continue
        hits = find_unbraced(text)
        for no, name, line in hits:
            print(f"{path}:{no}: ${name} 紧跟非 ASCII（改成 ${{{name}}}）：{line.strip()[:120]}")
        print(f"{path}: 扫了 {len(text.splitlines())} 行，{len(hits)} 处命中", file=sys.stderr)
        if hits:
            rc = max(rc, 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
