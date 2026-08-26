"""
标的名单单一真相源的回归闸（v0.45.6）

**这个文件存在的理由**：2026-08-25 发现 `config.WATCHLIST`（24 只）与编排器
`alpha-hive-orchestrator.sh` 的 `DEFAULT_TICKERS`（30 只）各存一份、内容早已
漂移——重合仅 13 只，且 config 里有 11 只（AMD/AMGN/BIIB/REGN/PLUG/RUN/ICLN/
SQ/COIN/MSTR/UPST）**从未被扫过**。改 config 以为生效，扫描其实纹丝不动。

修复后编排器改为从 `config.WATCHLIST` 读取，硬编码数组降级为"config 读不出来
时的兜底"。兜底一旦与 config 漂移，就会在 config 损坏那天**静默换成另一个
标的池**——比读不出来更糟。故此处强制两者一致。
"""

import re
from pathlib import Path

import pytest

ORCH = Path.home() / ".claude/scripts/alpha-hive-orchestrator.sh"


def _orch_source() -> str:
    if not ORCH.exists():
        pytest.skip(f"编排器脚本不存在: {ORCH}")
    return ORCH.read_text()


def _fallback_tickers() -> list:
    m = re.search(r"DEFAULT_TICKERS_FALLBACK=\((.*?)\)", _orch_source(), re.S)
    assert m, "编排器里找不到 DEFAULT_TICKERS_FALLBACK"
    return re.findall(r'"([A-Z0-9.\-]+)"', m.group(1))


class TestOrchestratorReadsFromConfig:
    def test_orchestrator_no_longer_hardcodes_the_live_list(self):
        """实际使用的名单必须来自 config，硬编码的那份只能叫 *_FALLBACK。"""
        src = _orch_source()
        assert "from config import WATCHLIST" in src, "编排器未从 config 读取名单"
        assert not re.search(r"^DEFAULT_TICKERS=\(", src, re.M), \
            "又出现了硬编码的 DEFAULT_TICKERS=(...)，双名单漂移会复发"

    def test_fallback_matches_config_exactly(self):
        """兜底名单与 config 必须逐个一致（含顺序）。

        只比集合不够：多处代码用 `list(WATCHLIST.keys())[:10]` 取前 N，
        顺序不同 ⇒ 走兜底那天扫的是另外一批标的。
        """
        from config import WATCHLIST

        assert _fallback_tickers() == list(WATCHLIST.keys()), \
            "编排器兜底名单与 config.WATCHLIST 漂移了"


class TestWatchlistIntegrity:
    def test_every_scanned_ticker_has_sector(self):
        """sector 缺失不会报错，只会让板块集中度/宏观映射静默退化。"""
        from config import WATCHLIST

        missing = [t for t, m in WATCHLIST.items() if not (m.get("sector") or "").strip()]
        assert missing == [], f"这些标的缺 sector，下游会静默按'无板块'处理: {missing}"

    def test_every_scanned_ticker_has_name(self):
        from config import WATCHLIST

        missing = [t for t, m in WATCHLIST.items() if not (m.get("name") or "").strip()]
        assert missing == []

    def test_tickers_match_validator_regex(self):
        """BRK-B 这类类份额后缀必须通过——它是每日扫描标的。"""
        from swarm_agents._config import _RE_TICKER

        from config import WATCHLIST, WATCHLIST_EXTENDED

        bad = [t for t in {**WATCHLIST, **WATCHLIST_EXTENDED} if not _RE_TICKER.match(t)]
        assert bad == [], f"这些 ticker 会被蜂群 _validate_ticker 静默中性化: {bad}"

    def test_pools_do_not_overlap(self):
        """两池重叠会让 `--extended-pool` 的计数与实际不符。"""
        from config import WATCHLIST, WATCHLIST_EXTENDED

        dup = sorted(set(WATCHLIST) & set(WATCHLIST_EXTENDED))
        assert dup == [], f"标的同时出现在两个池子里: {dup}"

    def test_extended_pool_not_silently_shrunk(self):
        """候补池是统计功效的来源（实测扩池 10→30 把出结论时间缩短 5.18×）。

        把标的移出每日扫描池时应降级到候补池，而不是删除——这条断言防止
        "顺手清理"悄悄砍掉样本量。
        """
        from config import get_extended_watchlist

        assert len(get_extended_watchlist()) >= 100

    def test_config_validation_clean(self):
        from config import validate_watchlist

        assert validate_watchlist() == []
