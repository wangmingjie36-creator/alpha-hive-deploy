"""
Alpha Hive 测试 fixtures - 共享 mock 数据 + 隔离数据库
"""

import sys
import os
import pytest
import tempfile

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== 环境隔离 ====================

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """所有测试自动使用临时目录，防止污染生产数据库"""
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHA_HIVE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALPHA_HIVE_CHROMA_PATH", str(tmp_path / "test_chroma"))
    monkeypatch.setenv("ALPHA_HIVE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(tmp_path / "cache"))
    # v0.41.3 第二层防线：显式禁用期权快照写盘。曾发生 pytest 的 mock 期权链
    # 经 OptionsAgent.analyze() 写进生产 cache/options_snapshot_NVDA_*.json
    # （data_quality 标 real），被当日正式扫描按"快照命中"复用进日报
    monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")


# ==================== 禁止测试调用真实 Anthropic API ====================

@pytest.fixture(autouse=True)
def _block_llm_api(monkeypatch):
    """禁止所有测试调用真实 Anthropic API，避免消耗余额。

    llm_service 内置 _disabled 标志：设为 True 后 is_available() 返回 False，
    所有 Agent 自动降级到规则引擎模式。monkeypatch 在每个测试结束后自动恢复。
    """
    import llm_service
    monkeypatch.setattr(llm_service, "_disabled", True)
    # 同时清除已缓存的 client，防止之前初始化的 client 被复用
    monkeypatch.setattr(llm_service, "_client", None)


# ==================== 禁止测试调用真实宏观数据源 ====================

@pytest.fixture(autouse=True)
def _block_same_day_macro(monkeypatch):
    """默认关掉 v0.45.60 的当日宏观取数层（财政部 + Finnhub）。

    为什么必须有这道闸：conftest 一直只 mock 了 yfinance（见下方
    `mock_yfinance`），而 v0.45.60 新增的取数层**绕过 yfinance 直接打外网**。
    接上当天就有两个后果：

      · `test_yield_curve_inverted` 红了 —— 测试构造的倒挂曲线被真实的
        2026-08-27 数据（10Y 4.67 / 2Y 4.20，normal）盖掉
      · 整个套件开始打真网络：变慢、变脆、且在离线环境下不可用

    加数据源时必须同时确认「测试里它被关掉了吗」—— 与 `http_gate` docstring
    记过的那条教训同形（「加源之前先确认闸门覆盖它」）。

    需要验证这一层的测试自己 monkeypatch `_same_day_macro_data`，
    那会覆盖本 fixture。
    """
    try:
        import fred_macro
    except Exception:  # pragma: no cover - 模块不可得时无需拦
        pass
    else:
        monkeypatch.setattr(fred_macro, "_same_day_macro_data",
                            lambda as_of=None: ({}, {}))

    # v0.45.61：Twelve Data 同理。配上 key 后 `calculate_iv_rv_spread` 会先走它，
    # 于是 `test_iv_rv_reports_unavailable_not_zero`（构造"取不到数"场景）被
    # 真实数据盖掉 —— **同一个错误的第三次**：
    #   ① v0.45.56 加 yf_gate 时                → 已由 mock_yfinance 覆盖
    #   ② v0.45.60 加财政部/Finnhub 宏观层时    → 漏了，本 fixture 上半段补的
    #   ③ v0.45.61 加 Twelve Data 时            → 又漏了，这段补的
    # 教训固定下来：**新增任何外部数据源，同一个 commit 里必须在这里加一行。**
    # 需要验证它的测试自己 monkeypatch，会覆盖本 fixture。
    try:
        import twelve_data
    except Exception:  # pragma: no cover
        return
    monkeypatch.setattr(twelve_data, "api_key", lambda: "")


# ==================== weekly_optimizer 生产库隔离 ====================

@pytest.fixture(autouse=True)
def _isolate_weekly_optimizer_db(tmp_path, monkeypatch):
    """把 weekly_optimizer.PHEROMONE_DB_PATH 指向不存在的临时路径（v0.45.86）。

    该常量不走 `_isolate_env` 的 ALPHA_HIVE_DB_PATH 隔离（weekly_optimizer.py
    自己算 ALPHAHIVE_DIR，不读那个 env var）。v0.45.86 起 Track A 会用它
    覆盖快照的 T+7 价格（见 weekly_optimizer._load_close_t7_map）——测试
    构造的 (ticker,date) 在生产库里查无匹配，会被误判"没有干净价格"整批
    丢弃，而不是真的在测原本要测的语义。指向不存在的路径，让查表函数走
    "库不存在→保留旧值"分支，把 actual_price_t7 的控制权还给测试自己构造
    的快照。**加数据源时必须同时确认测试里它被关掉了吗**——与本文件里
    `_block_same_day_macro` 记的同一条教训。需要单独验证覆盖行为的测试
    自己 monkeypatch 一个真实存在、有数据的临时库，会覆盖本 fixture。
    """
    try:
        import weekly_optimizer
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return
    monkeypatch.setattr(weekly_optimizer, "PHEROMONE_DB_PATH",
                        tmp_path / "_no_such_pheromone.db", raising=False)


# ==================== paper_portfolio 生产状态隔离 ====================

_PP_STATE_FILES = ("positions.jsonl", "closed_trades.jsonl",
                   "equity_curve.jsonl", "meta.json")


def _pp_state_digest(path):
    """文件内容指纹；不存在给 'MISSING'（删除也算改动，不能悄悄放过）。"""
    import hashlib
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return "MISSING"


@pytest.fixture(autouse=True)
def _isolate_paper_portfolio_state(tmp_path, monkeypatch):
    """把 paper_portfolio 的四个状态文件全局重绑到 tmp，并核对真身没被动过。

    v0.45.104。事故：一个只想验「run_for_date 入口会拒绝错模式」的测试，
    monkeypatch 了 `_load_meta` / `_append_jsonl` / `_save_meta` 却漏了路径本身，
    于是跑到收尾那两行——
        `_write_jsonl(EQUITY_FILE, ...)` / `_write_jsonl(POSITIONS_FILE, ...)`
    ——**整体重写**了生产状态：equity_curve 93 行 → 1 行、positions 13 行 → 0。
    `_append_jsonl` 打桩挡不住它，因为收尾走的根本不是 append。

    两道防线，缺一不可：
      ① 四个路径全局默认指向 tmp —— 漏绑一个就出事，所以不靠各测试自觉；
      ② teardown 比对真身指纹 —— 兜住任何绕过全局的写法（直接用 STATE_DIR、
         subprocess 调 CLI 等）。没有②的话，将来有人加一条新写入路径，
         ①会静默失效而没人知道。
    需要读真实状态的测试自己 monkeypatch 回去即可覆盖①（②仍会看着它）。
    """
    try:
        import paper_portfolio as _pp
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return

    real = {n: _pp.STATE_DIR / n for n in _PP_STATE_FILES}
    before = {n: _pp_state_digest(p) for n, p in real.items()}

    sandbox = tmp_path / "paper_portfolio_state"
    sandbox.mkdir(exist_ok=True)
    monkeypatch.setattr(_pp, "STATE_DIR", sandbox, raising=False)
    monkeypatch.setattr(_pp, "POSITIONS_FILE", sandbox / "positions.jsonl")
    monkeypatch.setattr(_pp, "CLOSED_FILE", sandbox / "closed_trades.jsonl")
    monkeypatch.setattr(_pp, "EQUITY_FILE", sandbox / "equity_curve.jsonl")
    monkeypatch.setattr(_pp, "META_FILE", sandbox / "meta.json")

    yield

    touched = [n for n, p in real.items() if _pp_state_digest(p) != before[n]]
    assert not touched, (
        f"测试写到了**生产** paper_portfolio_state/：{touched}。"
        "四个路径全局已默认指向 tmp，还能改到真身说明有绕过它们的写入路径——"
        "去把那条路径也接到模块级常量上，不要在这里放行。")


@pytest.fixture(autouse=True)
def _isolate_feedback_loop_close_t7_db(tmp_path, monkeypatch):
    """把 feedback_loop.PHEROMONE_DB_PATH 指向不存在的临时路径（v0.45.87）。

    v0.45.87 把 close_t7 干净口径的实现从 weekly_optimizer.py 挪到了
    feedback_loop.py，供 `BacktestAnalyzer(clean_t7=True)` 的全部消费者
    共用——包括这个模块自己缺省的 PHEROMONE_DB_PATH
    (`Path(__file__).parent / "pheromone.db"`，即真实生产库路径)。
    测试若构造 `BacktestAnalyzer(clean_t7=True)` 而不显式覆盖
    close_t7_db_path，会在本机真实生产库里查询——与
    `_isolate_weekly_optimizer_db` 记的是同一条教训（加数据源时必须同时
    确认测试里它被关掉了吗）。指向不存在的路径，让查表函数走
    "库不存在→保留旧值"分支。需要单独验证覆盖行为的测试自己 monkeypatch
    一个真实存在、有数据的临时库（或显式传 close_t7_db_path），会覆盖/
    绕开本 fixture。
    """
    try:
        import feedback_loop
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return
    monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH",
                        tmp_path / "_no_such_pheromone.db", raising=False)


# ==================== Mock 股票数据 ====================

MOCK_STOCK_DATA = {
    "NVDA": {
        "price": 142.50,
        "momentum_5d": 3.2,
        "avg_volume": 45_000_000,
        "volume_ratio": 1.4,
        "volatility_20d": 38.5,
    },
    "TSLA": {
        "price": 340.00,
        "momentum_5d": -1.8,
        "avg_volume": 80_000_000,
        "volume_ratio": 0.9,
        "volatility_20d": 52.0,
    },
    "VKTX": {
        "price": 48.20,
        "momentum_5d": 7.5,
        "avg_volume": 3_000_000,
        "volume_ratio": 2.1,
        "volatility_20d": 65.0,
    },
}


@pytest.fixture
def mock_stock_data(monkeypatch):
    """Mock yfinance 数据，避免测试中调用外部 API"""
    def _mock_fetch(ticker):
        return MOCK_STOCK_DATA.get(ticker, MOCK_STOCK_DATA["NVDA"])

    from swarm_agents import cache as _swarm_cache
    monkeypatch.setattr(_swarm_cache, "_fetch_stock_data", _mock_fetch)
    return MOCK_STOCK_DATA


# ==================== PheromoneBoard fixture ====================

@pytest.fixture
def board():
    """创建干净的信息素板"""
    from pheromone_board import PheromoneBoard
    b = PheromoneBoard()
    yield b
    b.clear()


# ==================== Agent fixtures ====================

@pytest.fixture
def all_agents(board, mock_stock_data):
    """实例化 6 个 Agent（使用 mock 数据）"""
    from swarm_agents import (
        ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
        ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel,
    )
    return {
        "scout": ScoutBeeNova(board),
        "oracle": OracleBeeEcho(board),
        "buzz": BuzzBeeWhisper(board),
        "chronos": ChronosBeeHorizon(board),
        "rival": RivalBeeVanguard(board),
        "guard": GuardBeeSentinel(board),
    }


@pytest.fixture
def bear_bee(board, mock_stock_data):
    """创建 BearBeeContrarian（Phase-2 看空对冲蜂）"""
    from swarm_agents import BearBeeContrarian
    return BearBeeContrarian(board)


@pytest.fixture
def queen(board):
    """创建 QueenDistiller"""
    from swarm_agents import QueenDistiller
    return QueenDistiller(board)


# ==================== MemoryStore fixture ====================

@pytest.fixture
def memory_store(tmp_path):
    """创建隔离的 MemoryStore（使用临时 DB）"""
    from memory_store import MemoryStore
    ms = MemoryStore(db_path=str(tmp_path / "test_memory.db"))
    yield ms
    ms.close()


@pytest.fixture(autouse=True)
def _fast_yfinance_limiter(monkeypatch):
    """测试期间把 yfinance 限流速率调到不产生等待（v0.45.56）。

    生产速率是 0.5 req/s（2s 一个令牌）—— 那是给真实 Yahoo 配额用的。
    单测里 yfinance 全被 mock，令牌等待没有任何语义，只是纯粹的墙钟浪费：
    `test_dashboard_renderer` 逐票渲染 30 只标的，实测从秒级涨到 **56s**，
    在全量跑里直接撞 60s 超时。

    ⚠️ 这里只改**速率**，不改任何行为。真正验证限流生效的
    `tests/test_yf_gate.py::TestGateBehaviour::test_throttles` 自带慢桶，
    不依赖这个全局值 —— 否则那条测试会被本 fixture 架空成永真。
    """
    try:
        import resilience
        from resilience import RateLimiter
    except ImportError:  # pragma: no cover
        return
    fast = RateLimiter(rate=10_000.0, burst=1000)
    monkeypatch.setattr(resilience, "yfinance_limiter", fast, raising=False)

    # `from resilience import yfinance_limiter` 的模块**各自持有一份绑定**，
    # 只改 resilience 上的名字够不到它们 —— 必须逐个替换。
    # （这正是 yf_gate 选择 patch yfinance 模块本身、而非逐处接线的原因：
    #   全仓已核实无 `from yfinance import X`，所以那边不存在这个问题。）
    import importlib
    for _mod in ("swarm_agents.cache", "earnings_watcher", "yf_gate"):
        try:
            _m = importlib.import_module(_mod)
        except ImportError:  # pragma: no cover
            continue
        if hasattr(_m, "yfinance_limiter"):
            monkeypatch.setattr(_m, "yfinance_limiter", fast)
        if _mod == "yf_gate":
            monkeypatch.setattr(_m, "_bucket", fast, raising=False)
