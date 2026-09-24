"""Slack 发送白名单：只有两个模块许发起 Slack 发送（v0.45.339）。

CLAUDE.md「Slack 通知精简规则」：Bot **只发两类消息** ——
  ① 扫描前 LLM 模式确认（`pre_scan_notify.py`）
  ② 富文本日报推送（`push_report_to_slack.py`）
其余（扫描开始/完成、SLO、权重更新、数据质量降级、逐标的高分/低分预警、扫描失败）
**只进本地日志**。

────────────────────────────────────────────────────────────────────────
为什么需要一条会红的守卫
────────────────────────────────────────────────────────────────────────
规则写在 CLAUDE.md 里已经很久，生产照样在发。2026-09-23 按发送者逐条归因生产日志
（`logs/alpha_hive.log{,.1,.2,.3}`，日志无日期戳，按行号区间切）：

* `real_data_sources._try_src_slack_alert`（数据源降级）与
  `sentiment._check_sentiment_spike`（逐标的情绪突变：CRM / DELL×2 / VKTX / RKLB / MU）
  在 v0.45.131 修好测试隔离**之后**仍有真实私信 —— 两处都用
  `if getattr(n, "enabled", False)` 当闸，而 `enabled = bool(user_token) or webhook_alive`，
  本机有 Bot Token ⇒ **恒真**；Bot 不在 #alpha-hive ⇒ `not_in_channel` ⇒ 降级成私信用户。
  那道闸挡的是「没配置」，挡不住「不该发」。
* `resilience.CircuitBreaker.record_failure` 在**持锁**时发告警，熔断的若恰是
  `slack_breaker` 本身就**自死锁**（见本文件 `TestBreakerDoesNotDeadlock`）。
* 其余三处（EDGAR RSS 降级、`--check-earnings` 财报通知、`alert_manager --dispatch`）
  生产日志里没有成功发送记录，但代码路径是通的。

「新 `new` 一个通知器、`if enabled` 就发」这个形状在六个模块里各自长出来过 ——
逐处修只是重置计时器，所以守的是**谁在调用发送**，不是某一处调用。

────────────────────────────────────────────────────────────────────────
三组测试，各管一件事
────────────────────────────────────────────────────────────────────────
1. `TestOnlyWhitelistedModulesSend` —— 静态：AST 扫本仓每个非测试 .py。
   **双向**：白名单外不许有发送（怕它变大）；白名单内每个模块必须**真被扫到**在发送
   （怕它变小 —— 既防名单过期，也是探针在真代码上的正对照：扫描器瞎了这条先红）。
2. `TestScannerHasTeeth` —— 合成源码 + 真文件注入变异：每种检测规则至少一个会红的
   样本、一个不该红的样本。**没有这一组，上一组的全绿证明不了任何事。**
3. `TestForbiddenPathsStayLogOnly` / `TestBreakerDoesNotDeadlock` —— 行为：把 Slack
   **故意武装起来**（假 token、记录器 session），再走每条被禁的路径，断言
   ① 零发送尝试 ② 原来那行日志还在（信息只换出口，不丢）。
   先自证武装真的生效（`test_fixture_really_arms_slack`），否则「没发」和
   「根本发不出去」长得一模一样。

⚠️ 本文件**不发任何 Slack 消息**：静态部分只读源码；行为部分的 `get_session` 换成
记录器，`_check_webhook_alive` 换成常量（掐掉构造时那次 HEAD），token 是假串。
conftest 的 `_block_slack` 仍在底下兜底（它的记录器会被本文件的覆盖，但 ①② 两道闸不变）。

⚠️ 已知盲区：本仓之外的编排器 `~/.claude/scripts/alpha-hive-orchestrator.sh` 扫不到。
2026-09-23 人工核对：它只调 `pre_scan_notify.py` / `push_report_to_slack.py` /
不带 `--dispatch` 的 `alert_manager.py`，没有 curl 到 Slack 的行。
"""
from __future__ import annotations

import ast
import logging
import threading
import types
from pathlib import Path

import pytest

from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: 唯一允许**发起** Slack 发送的模块（相对仓库根）→ 它发的是规则里哪一类。
ALLOWED_SENDERS = {
    "pre_scan_notify.py": "① 扫描前 LLM 模式确认（Bot 私信 + 轮询回复）",
    "push_report_to_slack.py": "② 富文本日报推送",
}

#: 发送器库：模块 → 类名。**类体内部**的调用是实现（`send_risk_alert` 调
#: `_send_slack_message`），不算发起；同文件里类体之外（CLI 入口、模块级代码）照样算。
SENDER_LIBS = {
    "slack_report_notifier.py": "SlackReportNotifier",
    "slack_notifier.py": "SlackNotifier",
}

#: 许出现 Slack 凭证名（token / webhook 文件名、secret 键）却不发送的模块：
#: `config.py` 是 secret 名 → 文件路径的登记表，`scan_secrets.py` 是泄密扫描的清单。
CREDENTIAL_REGISTRIES = {"config.py", "data_backup/scan_secrets.py"}

#: 按方法名匹配会误伤的名字。发送方法里只要有一个是这种名字，该发送器类就改用
#: 「类名出现即算」（例：`SlackNotifier.send` —— `sock.send` 满仓都是）。
_GENERIC_METHOD_NAMES = {"send", "post", "get", "put", "run", "call", "emit", "notify", "write"}

#: 出现在网络调用参数里即算「手搓发送」的 Slack 端点片段。
_SLACK_ENDPOINT_MARKERS = ("slack.com/api/chat.", "hooks.slack.com")
_NET_CALL_NAMES = {"post", "get", "put", "patch", "request", "urlopen", "Request"}

#: 凭证形状：读到它们的代码就能自己拼出一个发送器。
_CREDENTIAL_MARKERS = (".alpha_hive_slack_", "SLACK_BOT_TOKEN", "SLACK_USER_TOKEN",
                       "SLACK_WEBHOOK_URL")

_SLACK_SDK_ROOTS = {"slack_sdk", "slack_bolt", "slack"}

_SKIP_TOP = {"tests"}   # 测试本来就要在桩上调发送方法


# ════════════════════════════════════════════════════════════════════════
# 扫描器
# ════════════════════════════════════════════════════════════════════════

def _sender_methods(lib_src: str, class_name: str) -> set[str]:
    """从发送器类的**源码**推出「会走到网络发送」的方法集合。

    种子 = 方法体里出现 `.post(` 调用的方法；再沿 `self.<m>(...)` 边求闭包
    （调了会发送的方法，自己也会发送）。推导而不手抄：将来给通知器加一个
    `send_weight_update`，它自动进集合，不必记得回来改名单。
    """
    tree = ast.parse(lib_src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    methods = {f.name: f for f in cls.body
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _calls(fn):
        return [c for c in ast.walk(fn) if isinstance(c, ast.Call)]

    senders = {name for name, fn in methods.items()
               if any(isinstance(c.func, ast.Attribute) and c.func.attr == "post"
                      for c in _calls(fn))}
    self_edges = {name: {c.func.attr for c in _calls(fn)
                         if isinstance(c.func, ast.Attribute)
                         and isinstance(c.func.value, ast.Name) and c.func.value.id == "self"}
                  for name, fn in methods.items()}
    changed = True
    while changed:
        changed = False
        for name, callees in self_edges.items():
            if name not in senders and callees & senders:
                senders.add(name)
                changed = True
    return senders


def _load_sender_specs(root: Path) -> dict[str, tuple[str, frozenset[str]]]:
    """{库模块: (类名, 发送方法集合)}，从 `root` 下的真源码推导。"""
    specs = {}
    for rel, cls in SENDER_LIBS.items():
        src = (root / rel).read_text(encoding="utf-8")
        specs[rel] = (cls, frozenset(_sender_methods(src, cls)))
    return specs


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """裸字符串语句（docstring / 当注释用的字符串）的 id —— 说到 Slack 不等于调用 Slack。"""
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(id(node.value))
    return out


def _strings_in(node: ast.AST, skip: set[int]) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _class_body_ranges(tree: ast.Module, class_name: str) -> list[tuple[int, int]]:
    return [(n.lineno, n.end_lineno) for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == class_name]


def _scan_source(src: str, rel: str, specs: dict, allowed=None) -> list[tuple[str, int, str]]:
    """扫一个文件的源码，返回 [(rel, 行号, 规则说明)]。`allowed` 缺省 = `ALLOWED_SENDERS`。"""
    allowed = ALLOWED_SENDERS if allowed is None else allowed
    if rel in allowed:
        return []
    tree = ast.parse(src)
    skip = _docstring_nodes(tree)
    hits: list[tuple[str, int, str]] = []

    # 本文件若是发送器库：类体内部豁免，类体外只查「自家发送方法被调」。
    own_lib = specs.get(rel)
    exempt_ranges = _class_body_ranges(tree, own_lib[0]) if own_lib else []

    def _exempt(node) -> bool:
        ln = getattr(node, "lineno", 0)
        return any(a <= ln <= b for a, b in exempt_ranges)

    distinctive = set()      # 按方法名就能认出来的发送方法
    by_class_name = set()    # 只能按类名认的发送器
    for cls, methods in specs.values():
        if methods & _GENERIC_METHOD_NAMES:
            by_class_name.add(cls)
        else:
            distinctive |= methods
    own_methods = own_lib[1] if own_lib else frozenset()

    for node in ast.walk(tree):
        if _exempt(node):
            continue

        # R5：Slack SDK
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _SLACK_SDK_ROOTS:
                    hits.append((rel, node.lineno, f"R5 import {a.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _SLACK_SDK_ROOTS:
                hits.append((rel, node.lineno, f"R5 from {node.module} import"))
            if not own_lib:
                for a in node.names:
                    if a.name in by_class_name:
                        hits.append((rel, node.lineno, f"R2 import {a.name}"))

        # R2：只能按类名认的发送器（非本库文件里出现类名即算）
        if not own_lib:
            if isinstance(node, ast.Name) and node.id in by_class_name:
                hits.append((rel, node.lineno, f"R2 引用 {node.id}"))
            elif isinstance(node, ast.Attribute) and node.attr in by_class_name:
                hits.append((rel, node.lineno, f"R2 引用 .{node.attr}"))

        # R6：凭证
        if (rel not in CREDENTIAL_REGISTRIES and isinstance(node, ast.Constant)
                and isinstance(node.value, str) and id(node) not in skip
                and any(m in node.value for m in _CREDENTIAL_MARKERS)):
            hits.append((rel, node.lineno, f"R6 Slack 凭证 {node.value[:40]!r}"))

        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)

        # R1：调用发送方法（x.send_risk_alert(...) / getattr(x, "send_risk_alert")）
        if isinstance(node.func, ast.Attribute) and (
                name in distinctive or name in own_methods):
            hits.append((rel, node.lineno, f"R1 .{name}()"))
        if name == "getattr" and len(node.args) >= 2:
            a1 = node.args[1]
            if (isinstance(a1, ast.Constant) and isinstance(a1.value, str)
                    and (a1.value in distinctive or a1.value in own_methods)):
                hits.append((rel, node.lineno, f"R1 getattr(…, {a1.value!r})"))

        # R3：把 Slack 端点传给网络调用
        if name in _NET_CALL_NAMES:
            args = list(node.args) + [k.value for k in node.keywords]
            if any(m in s for a in args for s in _strings_in(a, skip)
                   for m in _SLACK_ENDPOINT_MARKERS):
                hits.append((rel, node.lineno, f"R3 {name}(<Slack 端点>)"))

        # R4：本仓的 Slack 会话池
        if (name == "get_session" and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "slack"):
            hits.append((rel, node.lineno, 'R4 get_session("slack")'))
    return hits


def _scan_repo(root: Path = REPO_ROOT, allowed=None) -> list[tuple[str, int, str]]:
    specs = _load_sender_specs(root)
    hits = []
    for py in own_python_files(root)[0]:
        rel_p = py.relative_to(root)
        if rel_p.parts[0] in _SKIP_TOP:
            continue
        try:
            src = py.read_text(encoding="utf-8")
            hits += _scan_source(src, rel_p.as_posix(), specs, allowed)
        except (SyntaxError, UnicodeDecodeError) as e:   # 扫不了就红，别静默跳过
            hits.append((rel_p.as_posix(), 0, f"无法解析：{e!r}"))
    return hits


# ════════════════════════════════════════════════════════════════════════
# 1. 真仓库
# ════════════════════════════════════════════════════════════════════════

class TestOnlyWhitelistedModulesSend:

    def test_only_whitelisted_modules_send(self):
        """变红的变异：把 v0.45.339 之前 `_check_sentiment_spike` 里的
        `SlackReportNotifier().send_risk_alert(...)` 块放回去（见
        `TestScannerHasTeeth.test_real_file_mutation_goes_red` 的实跑版本）。"""
        hits = _scan_repo()
        assert not hits, (
            "白名单外的代码在发起 Slack 发送（CLAUDE.md「Slack 通知精简规则」只许 "
            f"{sorted(ALLOWED_SENDERS)}）：\n  "
            + "\n  ".join(f"{f}:{ln}  {why}" for f, ln, why in hits)
            + "\n告警类信息请改成写日志（_log.warning），不要加进白名单。")

    def test_every_whitelisted_module_is_still_seen_sending(self):
        """白名单的反方向：名单上的每个模块必须**真被扫到**在发送。

        两个用处：① 名单过期（某模块不再发送却还挂着许可）会红；② 这是扫描器在
        **真代码**上的正对照 —— 规则全瞎时上一条会恒绿，这一条先红。

        变红的变异：把 `_scan_source` 里的 `distinctive |= methods` 改成 `pass`
        （按方法名认发送整个失明）⇒ push_report_to_slack 唯一的命中
        `send_rich_daily_report` 消失 ⇒ 缺席。
        """
        seen = {f for f, _ln, _why in _scan_repo(allowed={})}
        missing = set(ALLOWED_SENDERS) - seen
        assert not missing, (
            f"白名单里的 {sorted(missing)} 没被扫到任何发送 —— 要么它已不再发 Slack"
            "（从 ALLOWED_SENDERS 删掉），要么扫描器瞎了（先修扫描器）。")
        assert set(ALLOWED_SENDERS) <= seen

    def test_sender_methods_are_derived_not_everything(self):
        """推导出的发送方法集合：该有的都有，不该有的没有。

        「该有」防推导漏掉（闭包没沿 self 边传播）；「不该有」防推导退化成
        「类里所有方法」（那样 `_read_user_token` 之类会让 run_daily_scan 误红，
        修的人就会去加白名单）。
        """
        specs = _load_sender_specs(REPO_ROOT)
        srn = specs["slack_report_notifier.py"][1]
        assert {"send_risk_alert", "send_opportunity_alert", "send_scan_progress",
                "send_x_thread", "send_daily_report", "send_rich_daily_report",
                "send_plain_text", "retry_failed", "test_connection"} <= srn
        assert not {"_read_user_token", "_check_webhook_alive", "_is_valid_webhook",
                    "_format_rich_daily_mrkdwn", "__init__"} & srn
        assert specs["slack_notifier.py"][1] == {"send"}


# ════════════════════════════════════════════════════════════════════════
# 2. 扫描器有牙
# ════════════════════════════════════════════════════════════════════════

#: v0.45.339 之前紧跟在 `_check_sentiment_spike` 那行日志后面的原样代码
#: （`git show fd1dea85:swarm_agents/sentiment.py` 第 189–199 行）。
_PRE_FIX_SENTIMENT = '''
    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if getattr(n, "enabled", False):
            n.send_risk_alert(
                alert_title=f"{ticker} 情绪突变告警",
                alert_message=msg,
                severity="HIGH" if abs(delta) >= 30 else "MEDIUM",
            )
    except Exception as _se:
        _log.debug("Slack 情绪突变告警发送失败: %s", _se)
'''

#: 同上，`resilience.py::CircuitBreaker.record_failure`（fd1dea85 第 132–143 行）。
_PRE_FIX_BREAKER = '''
                    # #18: 连续失败告警 → 尝试 Slack 通知（跳过测试用熔断器）
                    if "test" not in self.name.lower():
                        try:
                            from slack_report_notifier import SlackReportNotifier
                            _sn = SlackReportNotifier()
                            _sn.send_risk_alert(
                                f"数据源 {self.name} 连续失败",
                                f"CircuitBreaker 熔断：连续 {self._failure_count} 次失败",
                                severity="HIGH",
                            )
                        except (ImportError, OSError, ValueError):
                            pass
'''

_MUST_FLAG = {
    "别名导入": ("swarm_agents/x.py",
             "from slack_report_notifier import SlackReportNotifier as S\n"
             "S().send_opportunity_alert('NVDA', 8.0, '看多', 'd')\n"),
    "getattr 字符串": ("x.py", "getattr(n, 'send_risk_alert')('t', 'm')\n"),
    "retry 也是发送": ("x.py", "n.retry_failed()\n"),
    "SlackNotifier 经模块属性": ("alert_x.py",
                              "import slack_notifier\nslack_notifier.SlackNotifier().send(a)\n"),
    "SlackNotifier 直接导入": ("alert_x.py",
                            "from slack_notifier import SlackNotifier\n"),
    "裸 chat.postMessage": ("x.py",
                           "requests.post('https://slack.com/api/chat.postMessage', json={})\n"),
    "f-string webhook": ("x.py",
                         "requests.post(f'https://hooks.slack.com/services/{k}', json={})\n"),
    "Slack 会话池": ("x.py", "get_session('slack').post(url, json={})\n"),
    "Slack SDK": ("x.py", "from slack_sdk import WebClient\n"),
    "凭证文件": ("x.py", "tok = open(os.path.expanduser('~/.alpha_hive_slack_bot_token')).read()\n"),
    "凭证 secret 名": ("x.py", "tok = get_secret('SLACK_BOT_TOKEN')\n"),
    "库文件的 CLI 入口": ("slack_report_notifier.py",
                       "class SlackReportNotifier:\n    pass\n\n"
                       "if __name__ == '__main__':\n"
                       "    SlackReportNotifier().send_risk_alert('t', 'm')\n"),
    "SlackNotifier 库外函数": ("slack_notifier.py",
                           "class SlackNotifier:\n    pass\n\n"
                           "def main():\n    SlackNotifier().send(alert)\n"),
    "改前的情绪突变块": ("swarm_agents/sentiment.py",
                    "def _check_sentiment_spike(ticker, msg, delta):\n"
                    "    _log.warning('📡 情绪突变告警 %s', msg)" + _PRE_FIX_SENTIMENT),
    "改前的熔断器块": ("resilience.py",
                   "class CircuitBreaker:\n    def record_failure(self):\n"
                   "        with self._lock:\n            if True:\n                if True:"
                   + _PRE_FIX_BREAKER),
}

_MUST_NOT_FLAG = {
    "只写日志": ("swarm_agents/sentiment.py", "_log.warning('📡 情绪突变告警 %s', msg)\n"),
    "docstring 里提到": ("x.py",
                      'def f():\n    """旧版调 send_risk_alert → chat.postMessage。"""\n'),
    "print 示例 URL": ("x.py", "print('https://hooks.slack.com/services/...')\n"),
    "socket.send": ("x.py", "sock.send(b'x')\n"),
    "只读状态": ("run_daily_scan.py",
             "from slack_report_notifier import SlackReportNotifier\n"
             "n = SlackReportNotifier()\nprint(n.enabled, n.use_user_token)\n"),
    "白名单模块": ("pre_scan_notify.py",
              "requests.post('https://slack.com/api/chat.postMessage', json={})\n"),
    "库类体内部": ("slack_report_notifier.py",
               "class SlackReportNotifier:\n"
               "    def send_plain_text(self, t):\n        return self._send_via_api(t, 'C')\n"),
    "凭证登记表": ("config.py", "S = {'SLACK_BOT_TOKEN': '~/.alpha_hive_slack_bot_token'}\n"),
    "读 Slack 历史（非发送）": ("x.py",
                          "requests.get('https://slack.com/api/conversations.history')\n"),
}


class TestScannerHasTeeth:

    @pytest.fixture(scope="class")
    def specs(self):
        return _load_sender_specs(REPO_ROOT)

    @pytest.mark.parametrize("case", sorted(_MUST_FLAG))
    def test_flags(self, case, specs):
        rel, src = _MUST_FLAG[case]
        assert _scan_source(src, rel, specs), f"「{case}」应被判为发送却没被扫到：\n{src}"

    @pytest.mark.parametrize("case", sorted(_MUST_NOT_FLAG))
    def test_does_not_flag(self, case, specs):
        rel, src = _MUST_NOT_FLAG[case]
        assert not _scan_source(src, rel, specs), f"「{case}」不是发送，却被判为发送"

    def test_empty_whitelist_makes_allowed_module_red(self, specs):
        """白名单确实在起作用 —— 同一段代码，换掉路径豁免就红。"""
        rel, src = _MUST_NOT_FLAG["白名单模块"]
        assert _scan_source(src, rel, specs, allowed={})

    @pytest.mark.parametrize("rel, anchor, block", [
        ("swarm_agents/sentiment.py",
         '    _log.warning("📡 情绪突变告警 %s", msg)\n', _PRE_FIX_SENTIMENT),
        ("resilience.py",
         '                        self.name, self._failure_count,\n                    )\n',
         _PRE_FIX_BREAKER),
    ], ids=["sentiment", "resilience"])
    def test_real_file_mutation_goes_red(self, rel, anchor, block, specs):
        """真文件注入变异：把改前的发送块插回**当前**源码的原位置，扫描器必须报出来。

        锚点就是那行保留下来的日志 —— 所以这条同时断言了「日志还在」：
        有人把日志行删了，锚点找不到，这条先红（信息不许随 Slack 一起丢）。
        """
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert not _scan_source(src, rel, specs), f"{rel} 当前就已违规"
        assert src.count(anchor) == 1, f"{rel} 里找不到原日志行（锚点）：{anchor!r}"
        mutated = src.replace(anchor, anchor + block.lstrip("\n"))
        hits = _scan_source(mutated, rel, specs)
        assert hits, f"把改前的 Slack 块放回 {rel} 后扫描器没有红"
        inserted_at = src[:src.index(anchor)].count("\n") + 2
        assert all(ln >= inserted_at for _f, ln, _w in hits), hits


# ════════════════════════════════════════════════════════════════════════
# 3. 行为：Slack 武装起来，被禁路径仍然一条都不发，日志行仍在
# ════════════════════════════════════════════════════════════════════════

class _Recorder:
    """冒充 `get_session("slack")`：记下每一次调用并**假装成功**。

    记录发生在返回之前，所以「有没有尝试发送」与返回什么无关。假装成功是为了让
    被测代码走和生产一样的「发送成功」分支：不累加 `slack_breaker` 失败计数、
    不进 `_failed_queue` 重试队列，一次尝试只记一笔，断言读起来就是生产上的条数。
    """

    def __init__(self):
        self.attempts: list[str] = []

    def __call__(self, *_a, **_k):
        return self

    def __getattr__(self, verb):
        def _call(*args, **kwargs):
            self.attempts.append(f"{verb} {str(args[0] if args else kwargs.get('url'))[:80]}")
            return types.SimpleNamespace(
                status_code=200, text="ok", ok=True,
                json=lambda: {"ok": True, "ts": "1", "channel": "D1"},
                raise_for_status=lambda: None)
        return _call


@pytest.fixture
def armed_slack(monkeypatch):
    """把 Slack **武装**起来：假 token（enabled 为真）+ 假 webhook + 记录器会话。

    这是生产上的真实状态（本机有 Bot Token ⇒ enabled 恒真），也正是 conftest
    `_block_slack` 为了安全**故意去掉**的状态 —— 在它的默认状态下，被禁路径
    改没改都发不出去，这里的断言就会恒绿。
    """
    import slack_notifier
    import slack_report_notifier as srn

    rec = _Recorder()
    monkeypatch.setattr(srn, "get_session", rec)
    monkeypatch.setattr(slack_notifier, "get_session", rec)
    monkeypatch.setattr(srn.SlackReportNotifier, "_read_user_token",
                        lambda self: "xoxb-armed-by-test-not-a-real-token")
    monkeypatch.setattr(srn.SlackReportNotifier, "_read_webhook_from_file",
                        lambda self: "https://hooks.slack.com/services/T0/B0/armed-by-test")
    monkeypatch.setattr(srn.SlackReportNotifier, "_check_webhook_alive",
                        staticmethod(lambda url: False))
    monkeypatch.setattr(slack_notifier.SlackNotifier, "_read_webhook_from_file",
                        lambda self: "https://hooks.slack.com/services/T0/B0/armed-by-test")
    # 熔断器用新实例：别让本文件（或旧代码的死锁）污染全局 slack_breaker
    import resilience
    monkeypatch.setattr(resilience, "slack_breaker", resilience.CircuitBreaker("slack"))
    return rec


class TestForbiddenPathsStayLogOnly:

    def test_fixture_really_arms_slack(self, armed_slack):
        """正对照：武装后，直接调发送方法**确实**会发（被记录）。

        没有这一条，下面每条「零发送」都可能只是因为 Slack 根本没接上。
        """
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        assert n.enabled is True
        assert n.send_risk_alert("武装自检", "本条应被记录") is True
        assert len(armed_slack.attempts) == 1, armed_slack.attempts

    def test_sentiment_spike(self, armed_slack, monkeypatch, caplog):
        """变红的变异：恢复 fd1dea85 的 `_check_sentiment_spike`（发 send_risk_alert）。"""
        import swarm_agents.sentiment as s
        monkeypatch.setattr(s, "_get_sentiment_baseline", lambda t, days=30: 55.0)
        with caplog.at_level(logging.WARNING):
            msg = s._check_sentiment_spike("DELL", 30, "2026-09-23")
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert msg and "DELL" in msg
        assert "📡 情绪突变告警" in caplog.text and "DELL" in caplog.text

    def test_data_source_degradation(self, armed_slack, monkeypatch, caplog):
        """变红的变异：恢复 `_try_src_slack_alert` 及其调用。"""
        import real_data_sources as r
        monkeypatch.setattr(r, "_src_fail_counts", {})
        monkeypatch.setattr(r, "_src_degraded", {})
        with caplog.at_level(logging.WARNING):
            for _ in range(r._HEALTH_FAIL_THRESHOLD):
                r._record_src_failure("yfinance_short_interest")
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert r._src_degraded["yfinance_short_interest"] is True
        assert "yfinance_short_interest" in caplog.text and "触发降级告警" in caplog.text
        assert "数据质量受影响" in caplog.text      # 原 Slack 正文多出的那句并进了日志

    def test_edgar_rss_degradation(self, armed_slack, monkeypatch, caplog):
        """变红的变异：恢复 `_try_rss_slack_alert` 及其调用。"""
        import edgar_rss
        monkeypatch.setattr(edgar_rss, "_rss_fail_count", 0)
        monkeypatch.setattr(edgar_rss, "_rss_degraded", False)

        def _down(url, **_kw):
            raise ConnectionError("down")
        monkeypatch.setattr(edgar_rss, "get_session",
                            lambda source: types.SimpleNamespace(get=_down))
        client = edgar_rss.EdgarRSSClient()
        with caplog.at_level(logging.WARNING):
            for _ in range(edgar_rss._RSS_FAIL_THRESHOLD):
                client.get_recent_form4_alerts(force_refresh=True)
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert edgar_rss._rss_degraded is True
        assert "进入降级模式" in caplog.text and "实时内幕交易告警不可用" in caplog.text

    def test_circuit_breaker_open(self, armed_slack, caplog):
        """变红的变异：恢复 fd1dea85 `record_failure` 里的 send_risk_alert 块。

        名字刻意不含 "test" —— 旧代码对名字带 test 的熔断器跳过发送，
        用 test 命名会让这条对旧代码也恒绿。
        """
        from resilience import CircuitBreaker
        br = CircuitBreaker("yfinance_probe", failure_threshold=2)
        with caplog.at_level(logging.WARNING):
            br.record_failure()
            br.record_failure()
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert br.state == CircuitBreaker.OPEN
        assert "CircuitBreaker[yfinance_probe] -> OPEN" in caplog.text

    def test_alert_dispatcher_ignores_slack(self, armed_slack, caplog):
        """变红的变异：恢复 AlertDispatcher 里 `slack_enabled → SlackNotifier` 那两行。"""
        from alert_manager import Alert, AlertDispatcher, AlertLevel
        with caplog.at_level(logging.INFO):
            d = AlertDispatcher({"slack_enabled": True})
            d.dispatch([Alert(AlertLevel.CRITICAL, "扫描失败（武装自检）")])
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert not any("Slack" in type(n).__name__ for n in d.notifiers)
        assert "slack_enabled 被忽略" in caplog.text

    def test_earnings_update_notice(self, armed_slack, caplog):
        """变红的变异：恢复 check_earnings_updates 里的 send_opportunity_alert。

        `slack_notifier` 属性按旧接线挂一个**已武装**的通知器：谁把发送接回
        `self.slack_notifier`，这里就真的「发出去」被记录。
        """
        from alpha_hive_daily_report import AlphaHiveDailyReporter
        from slack_report_notifier import SlackReportNotifier

        class _Watcher:
            def check_and_update(self, tickers, report_path):
                return {"reporting_today": ["NVDA"], "updated": ["NVDA"], "errors": [],
                        "earnings_data": {"NVDA": {"revenue_actual": 3.5e10, "eps_actual": 0.81,
                                                   "yoy_revenue_growth": 0.56,
                                                   "data_completeness": "full"}}}

        r = object.__new__(AlphaHiveDailyReporter)
        r.earnings_watcher = _Watcher()
        r.slack_notifier = SlackReportNotifier()      # 旧接线；新代码不读它
        assert r.slack_notifier.enabled is True
        with caplog.at_level(logging.INFO):
            out = r.check_earnings_updates(report_path="unused.md", tickers=["NVDA"])
        assert armed_slack.attempts == []      # 先证本组要证的事：零发送
        assert out["updated"] == ["NVDA"]
        assert "📊 财报更新 NVDA 财报数据已自动更新" in caplog.text
        assert "营收 $35.0B" in caplog.text and "EPS $0.81" in caplog.text


class TestBreakerDoesNotDeadlock:

    def test_slack_breaker_opening_returns(self, armed_slack):
        """`slack_breaker` 自己熔断时，record_failure 必须返回。

        v0.45.339 之前：`record_failure` 持 `self._lock`（不可重入）调 send_risk_alert →
        `_send_slack_message` → `slack_breaker.allow_request()` → `state` →
        `with self._lock` ⇒ 在自己手里的锁上永久阻塞。生产日志 4 次
        `CircuitBreaker[slack] -> OPEN` 后扫描进程都静默到被编排器超时杀掉。

        变红的变异：恢复 fd1dea85 的 `record_failure`（该线程 10 秒不返回）。
        `armed_slack` 已把 `resilience.slack_breaker` 换成新实例 —— 变异跑时死锁的
        只是这个临时实例，不会拖住同进程里后续测试。
        """
        import resilience
        br = resilience.slack_breaker
        done = threading.Event()

        def _run():
            for _ in range(br._failure_threshold):
                br.record_failure()
            done.set()

        t = threading.Thread(target=_run, daemon=True, name="slack-breaker-deadlock-probe")
        t.start()
        t.join(timeout=10)
        assert done.is_set(), "slack_breaker.record_failure 没有返回 —— 持锁发 Slack 的自死锁回来了"
        assert br.state == resilience.CircuitBreaker.OPEN
        assert armed_slack.attempts == []
