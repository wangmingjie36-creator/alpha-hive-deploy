"""账本 / 状态写入只经 `ledger_io`（静态守卫，v0.45.448 起；v0.45.452 改成按「写入落点」认）。

坏行的代码来源（`ledger_io` 模块文档）：写到一半崩溃（半行 / 半截文件）、读-改-写不加锁（丢更新）、不拒 NaN。
`ledger_io` 一次堵死；这里守的是「别处不许再造一份」。

v0.45.452 为什么改判据
--------------------
448 版按**名字长相**认人：`open(x, "a")` 与「名字以 `_jsonl` 结尾的函数」。于是 `_save_meta` 里
`json.dumps(meta)` → `os.replace` 这种整文件写法完全看不见——`meta.json`（纸面组合的现金就存在里面）和 24 个
手写 `os.replace` 不在守卫里，不是清单漏了，是**判据**漏了。现在认的是**写入落点**，与函数叫什么无关：

  · `raw_append`   —— `open(x, "a")` / `x.open("a")`（含 "ab" / "a+" 与 `mode=`）
  · `jsonl_helper` —— 名字以 `_jsonl` 结尾、却不经 `ledger_io` 的函数
  · `json_dump`    —— `json.dump(obj, fh)`（它只能写进文件）
  · `dumps_write`  —— `fh.write(json.dumps(..)…)` / `p.write_text(json.dumps(..))`
  · `file_replace` —— `os.replace` / `os.rename` / 单实参的 `Path.replace` / `Path.rename`（手写原子写的签名）
  · `json_to_file` —— 同一函数里既 `json.dumps` 又以写模式 `open` / `write_text`（中间隔一个变量也认得出）

每个命中按「文件::函数」登记在 `WRITERS`，带**类别**与理由。两头都守：新增命中红（怕它变大），登记了却不再命中
也红（怕它过期）。三个「待迁移」类别各有**上限**，等号比较：往里加一条必须同时改上限——新账本写入方的正确做法
是直接用 `ledger_io`，不是登记成债务。

清单走共享 `tests/_repo_files.own_python_files`（git ls-files；v0.45.442 教训：裸 rglob 扫进 iCloud 副本假红）。
`experiments/` 整体不扫：一次性研究脚本，产出进 scratch，不进任何账本（写进账本的实验必须搬出 experiments/）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _repo_files import own_python_files  # noqa: E402

# ─────────────────────────────── 类别
LEDGER = "账本·待迁移"      # 累积、不可再生、代码会读回参与计算的记录
STATE = "状态·待迁移"       # 每轮重写、但代码 / 编排器读回据以判断或报警的小文档
ARTIFACT = "产物·待迁移"    # 扫描产物，下游代码读回（日报 JSON / 蜂群结果 / 模型文件）
CACHE = "缓存"             # 可从源头重取；读坏 = 未命中、重取（新缓存一律用 hive_logger.atomic_json_write）
HUMAN = "只给人看"          # 报告 / 日志 / CLI --out / 只读展示，没有代码读回参与计算
CODE = "代码与部署"         # 不是 JSON 数据：脚本、git 钩子、app 包、日志轮转、库文件 / 目录搬移
CATEGORIES = {LEDGER, STATE, ARTIFACT, CACHE, HUMAN, CODE}
DEBT_CATEGORIES = (LEDGER, STATE, ARTIFACT)

#: 「待迁移」三类的条数上限（等号比较）。迁走一条 ⇒ 删条目并把这里减一；**不许为了登记新写入方而加一**。
CEILING = {LEDGER: 22, STATE: 12, ARTIFACT: 12, CACHE: 7}

_NA = "⚠️非原子"   # 理由里带这个标记 = 现在就是 open("w") 直写，写到一半崩溃 = 半截文件（迁移优先级最高）

#: 「文件::函数」→ (类别, 写的是什么 / 谁读回)。v0.45.452 全仓普查（逐条核过读者），迁一条删一条。
WRITERS: dict = {
    # ── 账本·待迁移（按风险排：非原子的整文件重写最先）
    "options_analyzer.py::OptionsAgent.analyze": (LEDGER, _NA + " 每日期权快照 options_snapshot_*（过了那一场就再也取不回；vrp / 财报波动 / IV·价格索引读回）"),
    "options_analyzer.py::OptionsAgent._refresh_price_derived": (LEDGER, _NA + " 期权快照重算价格派生字段后原地回写"),
    "options_analyzer.py::OptionsAgent._refill_empty_quote_set": (LEDGER, _NA + " 期权快照回填 quote_set 后原地回写"),
    "options_analyzer.py::OptionsAgent._drop_legacy_gex_signal": (LEDGER, _NA + " 期权快照去掉旧 GEX 加分后原地回写"),
    "backtest_engine.py::BacktestEngine.backfill_prices.save_callback": (LEDGER, _NA + " report_snapshots/*.json 原地回填实际价格（backtest / weekly_optimizer / self_analyst 读回）"),
    "cloud_snapshot_fetch.py::main": (LEDGER, "云端当日期权链快照（逐票原子；market.json / manifest.json " + _NA + "）"),
    "cboe_vix.py::_write_ledger": (LEDGER, "VIX 报价收盘核对账本（原子，不拒 NaN、无锁；读坏 ⇒ 空账本）"),
    "replay_ohlc_store.py::ReplayOhlcStore._write": (LEDGER, "回放日线库（含不可重取的时点修订 first_seen；原子，无锁）"),
    "sell_strike_ledger.py::_write_shard": (LEDGER, "卖权行权价月度分片账本（自带目录锁 + 原子；预注册样本，迁移需单独核对字节不变）"),
    "straddle_gex_prereg.py::run_once": (LEDGER, "跨式 GEX 预注册一次性冻结结果"),
    "iv_history.py::append_observation": (LEDGER, "IV 观测索引裸追加（进 iv_rank；读时跳坏行）"),
    "iv_history.py::merge_snapshots_into_index": (LEDGER, "IV 索引从快照合并重写（原子，无锁）"),
    "price_history.py::append_observation": (LEDGER, "本地价格索引裸追加（data_pipeline / vrp / 卖权账本读回）"),
    "price_history.py::merge_snapshots_into_index": (LEDGER, "价格索引从快照合并重写（原子，无锁）"),
    "probability_scorecard.py::record_published": (LEDGER, "概率记分卡前向账本裸追加"),
    "ibkr_sync.py::import_ibkr_statement": (LEDGER, "real_fills.jsonl 真实成交裸追加"),
    "weekly_optimizer.py::append_history": (LEDGER, "weight_history.jsonl 权重审计裸追加（health_check 坏一行判整份失败）"),
    "data_migrations/runner.py::append_record": (LEDGER, "数据迁移账本裸追加（读时坏行即抛、阻断迁移）"),
    "pheromone_board.py::PheromoneBoard._save_fallback_batch": (LEDGER, "信息素板写库失败时的兜底批次裸追加"),
    "alphabot/service.py::AlphaBotService.take_snapshot": (LEDGER, "Alpha Bot 盘中快照裸追加（页面读回，跳坏行）"),
    "data_backup/run_backup.py::_append_history": (LEDGER, "备份历史裸追加（backup_continuity 读回判连续性）"),
    "data_backup/migrate_data_root.py::retire": (LEDGER, _NA + " 旧数据根退役记录 RETIRE_RECORD.json（unretire / check-old 读回）"),
    # ── 状态·待迁移
    "scan_timing.py::write": (STATE, "scan_timing.json（编排器并进 status.json；原子）"),
    "step_contract.py::write_out": (STATE, "编排器各步骤 --out 契约外壳（jq 读回；原子）"),
    "production_sync.py::write_result": (STATE, "production_sync.json（scan_timing 读回；原子）"),
    "report_deployer.py::_append_gh_pages_deploy_log": (STATE, "gh-pages 部署结局日志裸追加（Step 5 读回，跳坏行）"),
    "data_backup/run_backup.py::_write_status": (STATE, _NA + " backup_status.json（编排器 jq 判 secret_scan）"),
    "economic_calendar_watch.py::_save_state": (STATE, "经济日历监视节流状态（原子；读坏 ⇒ {}）"),
    "alphabot/service.py::_atomic_write_json": (STATE, "Alpha Bot 用户设置 settings.json（原子）"),
    "alphabot/launcher.py::save_config": (STATE, "Alpha Bot 启动器配置（数据根；原子）"),
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter.run_swarm_scan._analyze_and_save": (STATE, _NA + " 扫描断点 checkpoint（读坏 ⇒ 丢弃重扫）"),
    "health_check.py::save_log": (STATE, "logs/health_*.json（health_check 只看 mtime）"),
    "deploy_orchestrator.py::main": (STATE, _NA + " 部署结局 orchestrator_deploy.json（一致性守卫 / status.json 读回）"),
    "data_migrations/__main__.py::main": (STATE, _NA + " 数据迁移步骤结局 --out（编排器读回）"),
    # ── 产物·待迁移
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter.save_report": (ARTIFACT, _NA + " 日报主 JSON（alert_manager / Slack / 网站 / collect_data 读回）"),
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter._post_scan_enrichment": (ARTIFACT, _NA + " 当日蜂群结果 .swarm_results_*（signal_archive / 覆盖闸 / 编排器读回）"),
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter._generate_synthetic_swarm_results": (ARTIFACT, _NA + " 回写增强版蜂群结果"),
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter._generate_ml_reports._gen_one": (ARTIFACT, _NA + " 每标的 ML 增强分析 JSON（记分卡 / 模型守卫 / IC 就绪度读回）"),
    "alpha_hive_daily_report.py::AlphaHiveDailyReporter._fallback_dashboard_data": (ARTIFACT, _NA + " 降级仪表盘数据（report_deployer 读 _generated_at）"),
    "generate_ml_report.py::MLEnhancedReportGenerator._write_file_async": (ARTIFACT, _NA + " 异步写 ML 分析 JSON"),
    "dashboard_renderer.py::render_dashboard_html": (ARTIFACT, _NA + " 网站 dashboard-data.json（report_deployer 读 _generated_at）"),
    "collect_data.py::main": (ARTIFACT, _NA + " 精简原始数据 {T}_raw.json（generate_ml_report / Slack 读回）"),
    "ml_predictor.py::HGBModel.save_model": (ARTIFACT, _NA + " 模型持久化（读坏 ⇒ 重训）"),
    "ml_predictor.py::SGDMLModel.save_model": (ARTIFACT, _NA + " 模型持久化（读坏 ⇒ 重训）"),
    "ml_predictor.py::SimpleMLModel.save_model": (ARTIFACT, _NA + " 模型持久化（读坏 ⇒ 重训）"),
    "data_backup/export.py::write_manifest_and_sums": (ARTIFACT, "备份清单 BACKUP_MANIFEST.json（restore 读回）"),
    # ── 缓存（可再生；该收进 hive_logger.atomic_json_write）
    "cboe_vix.py::_write_cache": (CACHE, "VIX 历史 CSV 缓存（原子；读坏退下载）"),
    "congress_trades_scraper.py::_save_cache": (CACHE, "国会交易缓存（读坏 ⇒ 重取）"),
    "pead_analyzer.py::_save_cache": (CACHE, "PEAD 缓存（读坏 ⇒ 重算）"),
    "quiver_fetcher.py::QuiverFetcher._save_cache": (CACHE, "Quiver 响应缓存（原子；读坏 ⇒ 重取）"),
    "risk_engine.py::_estimate_beta": (CACHE, "β 24h 缓存（读坏忽略）"),
    "vix_term_structure.py::get_vix_term_structure": (CACHE, "VIX 期限结构 30 分钟缓存（读坏 ⇒ 重取）"),
    "data_fetcher.py::<module>": (CACHE, "示例脚本采集汇总 realtime_metrics.json（无生产读者）"),
    # ── 只给人看
    "alert_manager.py::AlertAnalyzer.save_alerts": (HUMAN, "告警汇总文件"),
    "alpha_hive_daily_report.py::main": (HUMAN, "样本模式最小结果 JSON（仓内无读者）"),
    "entry_price_backfill.py::main": (HUMAN, "回填判决 --out 导出"),
    "ibkr_sync.py::export_daily_actions": (HUMAN, "给人手动下单的订单清单"),
    "ibkr_sync.py::reconcile": (HUMAN, "模拟 vs 真实成交对账报告"),
    "ml_model_guard.py::snapshot_model_file": (HUMAN, "模型留档 manifest.jsonl（无代码读者；v0.45.448 登记的「守卫读回」不对）"),
    "ml_predictor.py::HistoricalDataBuilder.save_to_file": (HUMAN, "训练数据导出（无调用者）"),
    "ml_predictor_extended.py::HistoricalDataBuilder.save_to_file": (HUMAN, "训练数据导出（无调用者）"),
    "ml_predictor_extended.py::SimpleMLModel.save_model": (HUMAN, "降级模型文件（无生产读者）"),
    "param_optimizer.py::run_grid": (HUMAN, "参数网格结果（只给 --html 渲染）"),
    "portfolio_backtest.py::main": (HUMAN, "组合回测 --save 导出"),
    "run_daily_scan.py::_write_status": (HUMAN, "last_run_status.json（无读者）"),
    "sell_strike_report.py::write_local_report": (HUMAN, "卖权本地 Markdown 报告"),
    "thesis_breaks.py::ThesisBreakMonitor.save_to_json": (HUMAN, "论点失效配置导出（只有 __main__ 示例调用）"),
    "crowding_detector.py::CrowdingDetector.save_to_json": (HUMAN, "拥挤度导出（无调用者）"),
    "data_backup/migrate_data_root.py::_write_report": (HUMAN, "数据根迁移报告"),
    "data_backup/export.py::export_db": (HUMAN, "库导出 _meta.json（restore 只做核对展示）"),
    "vrp_signal.py::main": (HUMAN, "CLI --out 导出"),
    "paper_portfolio.py::main": (HUMAN, "CLI：KPI 打印 + HTML 报告导出"),
    "code_executor.py::CodeExecutor._write_audit_log": (HUMAN, "代码执行沙箱审计日志（只原样读回给人看）"),
    "alphabot/straddle.py::read_jsonl": (HUMAN, "只读展示：坏行计数随结果返回并显示在页面上（不吞、也不因外部损坏让整页崩）"),
    # ── 代码与部署（不是 JSON 数据）
    "alphabot/launcher.py::_open_server_log": (CODE, "Alpha Bot 服务日志（追加 + 轮转），只给人看"),
    "alphabot/macos_app.py::build_app": (CODE, "替换 macOS app 包"),
    "deploy_orchestrator.py::deploy": (CODE, "部署编排器脚本"),
    "production_clone.py::_write_hook": (CODE, "安装生产克隆 git 钩子"),
    "data_backup/migrate_data_root.py::backup_db": (CODE, "SQLite 备份原子落位"),
    "data_backup/migrate_data_root.py::unretire": (CODE, "撤销退役：文件挪回 + 记录改名"),
    "data_migrations/runner.py::online_backup": (CODE, "迁移前在线备份库"),
    "replay_ohlc_store.py::ReplayOhlcStore._quarantine": (CODE, "坏库文件改名隔离"),
    "weekly_optimizer.py::_restore_config_from_backup": (CODE, "从备份还原 config.py"),
    "weekly_optimizer.py::write_weights_to_config": (CODE, "权重写回 config.py"),
}

#: 迁完的模块：除「只给人看 / 代码与部署」外一个命中都不许有（账本读写全经 ledger_io）。
MIGRATED = ("portfolio_greeks.py", "paper_portfolio.py",                      # v0.45.448
            "options_paper_leg.py", "vrp_signal.py", "earnings_vol_signal.py",  # v0.45.452
            "hive_logger.py", "cboe_fetcher.py", "options_backtester.py")      # v0.45.452（缓存写入实现收口）


# ─────────────────────────────── 扫描器

def _mode_of(call: ast.Call, method: bool):
    """open 调用的 mode 字面量。`open(path, mode)` 的 mode 是第 2 个实参；`path.open(mode)` 是第 1 个。"""
    idx = 0 if method else 1
    mode = None
    if len(call.args) > idx and isinstance(call.args[idx], ast.Constant):
        mode = call.args[idx].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return mode if isinstance(mode, str) else None


def _is_open(call: ast.Call):
    f = call.func
    if isinstance(f, ast.Name) and f.id == "open":
        return _mode_of(call, method=False)
    if isinstance(f, ast.Attribute) and f.attr == "open":
        return _mode_of(call, method=True)
    return None


def _is_json_attr(call: ast.AST, attr: str) -> bool:
    """`json.<attr>(…)` 或别名 `_json.<attr>(…)`（模块名以 json 结尾）。"""
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == attr
            and isinstance(call.func.value, ast.Name) and call.func.value.id.endswith("json"))


def _call_kinds(call: ast.Call) -> set:
    kinds = set()
    f = call.func
    mode = _is_open(call)
    if mode and "a" in mode:
        kinds.add("raw_append")
    if _is_json_attr(call, "dump"):
        kinds.add("json_dump")
    if isinstance(f, ast.Attribute):
        if f.attr in ("write", "write_text") and call.args:
            a0 = call.args[0]
            if _is_json_attr(a0, "dumps") or (isinstance(a0, ast.BinOp) and _is_json_attr(a0.left, "dumps")):
                kinds.add("dumps_write")
        base_os = isinstance(f.value, ast.Name) and f.value.id == "os"
        if f.attr in ("replace", "rename") and (base_os or (len(call.args) == 1 and not call.keywords)):
            kinds.add("file_replace")
    return kinds


def _own_calls(scope: ast.AST):
    """作用域里属于**它自己**的调用：不进嵌套 def / class（那些单独算），**进** lambda（lambda 没有自己的名字，
    记在外层作用域——v0.45.452 二次检查：初版跳过 lambda，`lambda: open(p, "a")` 整个看不见）。"""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.Call):
            yield n
        stack.extend(ast.iter_child_nodes(n))


def _scope_kinds(scope: ast.AST) -> set:
    kinds, dumps, writes = set(), False, False
    for c in _own_calls(scope):
        kinds |= _call_kinds(c)
        dumps = dumps or _is_json_attr(c, "dumps")
        mode = _is_open(c)
        if (mode and ("w" in mode or "x" in mode)) or (isinstance(c.func, ast.Attribute) and c.func.attr == "write_text"):
            writes = True
    if dumps and writes and not (kinds & {"dumps_write", "json_dump"}):
        kinds.add("json_to_file")
    return kinds


#: 经 ledger_io 的函数**只**豁免两个按形状猜的类别（名字像 JSONL 助手 / dumps 与写文件同在一个函数）；
#: 真实的写入落点（裸追加 / json.dump / dumps 直写 / 手写替换）照样算——v0.45.452 二次检查：初版整函数豁免，
#: 于是 `with ledger_io.locked(d): open(p, "a")` 这种「拿了锁再裸写」完全看不见（448 版是看得见的）。
_HEURISTIC_KINDS = {"jsonl_helper", "json_to_file"}


def scan_source(src: str) -> dict:
    """一段源码里的命中：{"限定名": {kind, …}}；模块级记在 "<module>"，类体里的记在类名下。"""
    tree = ast.parse(src)
    hits: dict = {}

    def record(qual, scope, is_fn_named=None):
        kinds = _scope_kinds(scope)
        if is_fn_named is not None and is_fn_named.endswith("_jsonl"):
            kinds.add("jsonl_helper")
        if _uses_ledger_io_directly(scope):
            kinds -= _HEURISTIC_KINDS
        if kinds:
            hits[qual] = kinds

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{qual}.{child.name}" if qual != "<module>" else child.name
                fn_name = child.name if not isinstance(child, ast.ClassDef) else None
                record(q, child, fn_name)
                visit(child, q)
    record("<module>", tree)
    visit(tree, "<module>")
    return hits


def _uses_ledger_io_directly(scope: ast.AST) -> bool:
    return any(isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
               and c.func.value.id == "ledger_io" for c in _own_calls(scope))


def _scan_repo() -> dict:
    found = {}
    files, _how = own_python_files(ROOT)
    for p in files:
        rel = p.relative_to(ROOT)
        if (rel.parts and rel.parts[0] in ("tests", "experiments")) or rel.as_posix() == "ledger_io.py":
            continue
        try:
            hits = scan_source(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for qual, kinds in hits.items():
            found[f"{rel.as_posix()}::{qual}"] = kinds
    return found


def unregistered(found: dict, registry: dict) -> dict:
    return {k: v for k, v in found.items() if k not in registry}


def stale(found: dict, registry: dict) -> list:
    return sorted(k for k in registry if k not in found)


def category_counts(registry: dict) -> dict:
    out = {c: 0 for c in CATEGORIES}
    for cat, _why in registry.values():
        out[cat] = out.get(cat, 0) + 1
    return out


_FOUND = None


def _found() -> dict:
    global _FOUND
    if _FOUND is None:
        _FOUND = _scan_repo()
    return _FOUND


class TestLedgerWritesGoThroughLedgerIo:

    def test_no_unregistered_file_writer(self):
        new = unregistered(_found(), WRITERS)
        assert not new, (f"这些地方手写了文件写入：{new}。账本 / 状态 → 用 ledger_io（write_json / write_jsonl / "
                         "append_jsonl / load_* / locked）；可再生缓存 → hive_logger.atomic_json_write；"
                         "确属只给人看的输出或代码部署，再登记进 WRITERS 并写明理由。")

    def test_registry_has_no_stale_entries(self):
        gone = stale(_found(), WRITERS)
        assert not gone, f"WRITERS 里这些条目已经不命中了（迁完了？），删掉并把 CEILING 对应减一——过期条目是后门：{gone}"

    def test_every_entry_has_a_known_category_and_a_reason(self):
        bad = {k: v for k, v in WRITERS.items()
               if not (isinstance(v, tuple) and len(v) == 2 and v[0] in CATEGORIES and str(v[1]).strip())}
        assert not bad, bad

    def test_debt_only_shrinks(self):
        counts = category_counts(WRITERS)
        diff = {c: (counts[c], CEILING[c]) for c in CEILING if counts[c] != CEILING[c]}
        assert not diff, (f"「待迁移 / 缓存」条数与上限不符（实际, 上限）：{diff}。迁走了 ⇒ 把上限减到实际值；"
                          "多出来了 ⇒ 别登记成债务，新写入直接用 ledger_io / atomic_json_write。")

    def test_migrated_modules_are_clean(self):
        left = {k: v for k, v in _found().items() if k.split("::")[0] in MIGRATED
                and WRITERS.get(k, (None,))[0] not in (HUMAN, CODE)}
        assert left == {}, left


class TestScannerHasTeeth:
    """反向自证：上面几条对真仓库为空，不能是因为扫描器是瞎的、比对是关着的。"""

    def test_flags_raw_appends_in_every_spelling(self):
        assert scan_source("def f(p):\n    with open(p, 'a') as fh:\n        fh.write('x')\n") == {"f": {"raw_append"}}
        assert scan_source("def f(p):\n    p.open('a', encoding='utf-8')\n") == {"f": {"raw_append"}}
        assert scan_source("def f(p):\n    open(p, mode='ab')\n") == {"f": {"raw_append"}}
        assert scan_source("class C:\n    def g(self, p):\n        open(p, 'a+')\n") == {"C.g": {"raw_append"}}
        assert scan_source("open('x.log', 'a')\n") == {"<module>": {"raw_append"}}

    def test_flags_whole_file_json_writes_whatever_the_function_is_called(self):
        """448 版看不见的那一族：名字里没有 _jsonl，照样认得出。"""
        assert scan_source("import json\ndef _save_meta(m, p):\n    with open(p, 'w') as f:\n"
                           "        json.dump(m, f)\n") == {"_save_meta": {"json_dump"}}
        assert scan_source("import json\ndef save(m, p):\n    p.write_text(json.dumps(m))\n") == {"save": {"dumps_write"}}
        assert scan_source("import json\ndef save(m, f):\n    f.write(json.dumps(m) + '\\n')\n") == {"save": {"dumps_write"}}
        assert scan_source("import json as _json\ndef save(m, f):\n    _json.dump(m, f)\n") == {"save": {"json_dump"}}

    def test_flags_hand_rolled_atomic_replace(self):
        assert scan_source("import os\ndef w(t, p):\n    os.replace(t, p)\n") == {"w": {"file_replace"}}
        assert scan_source("def w(t, p):\n    t.replace(p)\n") == {"w": {"file_replace"}}
        assert scan_source("import os\ndef w(a, b):\n    os.rename(a, b)\n") == {"w": {"file_replace"}}

    def test_flags_dumps_into_a_variable_then_written(self):
        src = "import json\ndef save(m, p):\n    text = json.dumps(m)\n    with open(p, 'w') as f:\n        f.write(text)\n"
        assert scan_source(src) == {"save": {"json_to_file"}}

    def test_ignores_reads_string_replace_and_delegating_helpers(self):
        assert scan_source("def f(p):\n    open(p)\n    open(p, 'r')\n    p.open('rb')\n") == {}
        assert scan_source("def f(s):\n    return s.replace('a', 'b')\n") == {}
        assert scan_source("import json\ndef f(m):\n    print(json.dumps(m))\n") == {}
        assert scan_source("import ledger_io\ndef _write_jsonl(p, r):\n    ledger_io.write_jsonl(p, r)\n") == {}
        assert scan_source("import json, ledger_io\ndef _save_meta(p, m):\n    ledger_io.write_json(p, m)\n") == {}

    def test_using_ledger_io_does_not_excuse_a_raw_write_next_to_it(self):
        """v0.45.452 二次检查：初版对「函数里出现过 ledger_io」整体豁免——拿了锁再裸追加完全看不见。"""
        src = "import ledger_io\ndef f(p):\n    with ledger_io.locked(p.parent):\n        open(p, 'a').write('x')\n"
        assert scan_source(src) == {"f": {"raw_append"}}
        src = "import json, ledger_io\ndef _x_jsonl(p, r):\n    ledger_io.write_jsonl(p, r)\n    print(json.dumps(r))\n"
        assert scan_source(src) == {}, "委托 ledger_io 的 *_jsonl 助手只豁免按名字 / 形状猜的那两类"

    def test_lambdas_and_class_bodies_are_scanned(self):
        """v0.45.452 二次检查：初版跳过 lambda、不看类体（448 版两处都看得见）。"""
        assert scan_source("def f(p):\n    g = lambda: open(p, 'a')\n    return g\n") == {"f": {"raw_append"}}
        assert scan_source("w = lambda p: open(p, 'a')\n") == {"<module>": {"raw_append"}}
        assert scan_source("class C:\n    fh = open('x.log', 'a')\n") == {"C": {"raw_append"}}

    def test_flags_hand_rolled_jsonl_helpers(self):
        assert scan_source("import json\ndef _load_jsonl(p):\n    return [json.loads(l) for l in open(p)]\n") \
            == {"_load_jsonl": {"jsonl_helper"}}

    def test_nested_functions_are_attributed_to_themselves(self):
        src = "import json\ndef outer(p):\n    def inner(m):\n        p.write_text(json.dumps(m))\n    return inner\n"
        assert scan_source(src) == {"outer.inner": {"dumps_write"}}

    def test_both_comparisons_can_go_red(self):
        found = {"a.py::f": {"raw_append"}, "b.py::g": {"json_dump"}}
        reg = {"a.py::f": (HUMAN, "日志")}
        assert set(unregistered(found, reg)) == {"b.py::g"}
        assert unregistered(found, {**reg, "b.py::g": (CACHE, "y")}) == {}
        assert stale(found, {**reg, "c.py::h": (LEDGER, "z")}) == ["c.py::h"]
        assert stale(found, reg) == []

    def test_ceiling_comparison_can_go_red(self):
        counts = category_counts({"a::f": (LEDGER, "x"), "b::g": (LEDGER, "y"), "c::h": (HUMAN, "z")})
        assert counts[LEDGER] == 2 and counts[HUMAN] == 1 and counts[STATE] == 0
