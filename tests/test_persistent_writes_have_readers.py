"""每一处落盘写入都要登记「谁读它」（读者登记表守卫，v0.45.543）。

墓碑：为什么有这个文件
--------------------
2026-10-19 全仓只读审计找出约 15 条**死链**——每轮扫描都在写、却没有任何读者——最早的从 2 月就在：
metrics.db 三张表、`reasoning_sessions`、`sentiment_baseline.db`、编排器 Step 8……v0.45.531 / 534 / 541 一条条删。
逐条删治不了根：**没有任何东西让「写了没人读」可见**。最常见的死法是「上游改道、下游孤儿」——
`sentiment_baseline` 在 v0.45.340 改读 `signal_archive` 之后、`realtime_metrics` 在 Step 2 接管之后，
写入方原样留着，全套测试照样全绿（auto-memory `alpha-hive-dead-field.md`「全仓死链二次检查」）。

本文件让「写了」与「谁读」成对出现，双向核对（照 `test_ledger_writes_go_through_ledger_io.py::WRITERS`
的形状：新增写入不登记即红、登记了却不再命中也红、待清理类有上限只许减）：

  **表**（`TABLES`）：生产源码里写某张 SQLite 表的 SQL（CREATE / INSERT / REPLACE / UPDATE，含 f-string 与
  `.format(t=TABLE)`）⇒ 表名必须登记。登记的读者必须**仍存在**且在**它自己的函数体里** `SELECT … FROM/JOIN 该表`。
  **文件**（`SITE_ARTIFACTS` + `ARTIFACTS`）：写入落点（`文件::限定名`，判据见 `tests/_persistent_io_scan.py`）
  ⇒ 必须归到一个或多个产物；产物登记读者 `(文件::限定名, 令牌)`——读者必须仍存在、且函数体里出现令牌
  （文件名片段 / `PATHS.<属性>`）；非 Python 读者（编排器 .sh、Alpha Bot 静态页）按文件找令牌。

  没有程序读者的产物必须写明**类别**：HUMAN（只给人看）/ AUDIT（审计轨迹）/ FORENSIC（事后取证）/
  BACKUP（备份恢复链）/ CODE（不是数据：代码、钩子、app 包、搬文件）。
  CACHE 也要登记读者（读回缓存的那个函数）——没人读回的缓存同样是死链。

  **PENDING_DEAD**（已知死链、待清理）：每条带一个**见证**（写入点或「哪个函数里还有哪段字符串」）。
  条数与 `CEILING_PENDING_DEAD` 等号比较 ⇒ 只许减：清理掉一条，见证消失 ⇒ 红，提醒删条目并把上限减一；
  新发现的死链**不许**登记进来当债务——要么删掉写入方，要么接上读者。
  2026-10-19 审计里还开着的：编排器 Step 1 `data_fetcher`（`realtime_metrics.json` + `cache/` 下的
  `metrics_*` 等缓存）、`barrier_outcomes`、`agent_memory`（读者只在 LLM 模式）、`ml_model.json`（无读者）、
  扫描后 ML 增量重训的产出。

v1 的边界（写明，免得把「没查」读成「没问题」）
------------------------------------------
  · **只查表与文件**。`swarm_results` 等 dict 里的字段算了没人读（`thesis_break_l1/l2` 那一族）**不在本守卫范围**。
  · 读者「存在且引用了令牌」是静态判据：读者函数自己若从不被调用、或只在 `--use-llm` 下调用，它看不见——
    这正是 PENDING_DEAD 要人工登记的那一类（`agent_memory`）。
  · 写入助手吃路径参数时（`ledger_io.write_json(path, …)`、`_write_jsonl(path, rows)`），落点↔产物的对应来自登记表本身，
    扫描器不核对路径值。
  · 读者令牌是子串匹配：令牌要挑能认出产物的片段（文件名），别挑 `.json`。
  · `tests/` 与 `experiments/` 不扫，也不算读者（测试读它不等于生产有人用它）。

有牙：`TestScannerHasTeeth` 对每个检测器给合成源码的正 / 反例；`TestRegistryComparisonsGoRed` 对每种对账
给「该红」的登记表变体；`TestRealRepoPositiveControls` 在真仓库上核对扫描器认得出已知的写入与读者。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _persistent_io_scan as S  # noqa: E402

# ─────────────────────────────── 类别
READ = "有程序读者"
CACHE = "缓存（读者 = 读回它的函数）"
HUMAN = "只给人看"
AUDIT = "审计轨迹"
FORENSIC = "事后取证"
BACKUP = "备份恢复链"
CODE = "不是数据"
PENDING_DEAD = "已知死链·待清理"
CATEGORIES = {READ, CACHE, HUMAN, AUDIT, FORENSIC, BACKUP, CODE, PENDING_DEAD}
NEEDS_READER = {READ, CACHE}

#: 已知死链的条数（表 + 文件产物合计）。等号比较：清理一条就减一，**不许加**。
CEILING_PENDING_DEAD = 9

#: 「自读」标记：缓存的读写在同一个函数里（读不到才取、取完写回）。只对 CACHE 有效，且该函数里必须真有读调用。
SELF = "self"

# ═════════════════════════════════════════════════════════════════════ 表
#: 表 → (类别, [读者 文件::限定名], 说明)。读者核对 = 该函数体里 SELECT … FROM/JOIN 这张表。
TABLES: dict = {
    # ── pheromone.db
    "predictions": (READ, ["ic_rerun_readiness.py::assess", "scan_coverage_gate.py::check_prices",
                           "dashboard_renderer.py::_load_accuracy_data", "signal_archive.py::load_panel"],
                    "预测账本（PredictionStore）：IC 就绪度 / Step 12 覆盖闸 / 网站准确率 / 维度 IC 面板"),
    "signal_archive": (READ, ["signal_archive.py::load_panel", "swarm_agents/sentiment.py::_get_sentiment_momentum",
                              "paper_portfolio.py::_lookup_market_fear_greed"],
                       "逐标的逐维度信号存档：IC 面板 / Buzz 情绪动量 / 纸面组合 F&G 敞口门"),
    "adapted_weights": (READ, ["backtester.py::Backtester.load_adapted_weights"],
                        "v0.45.176 起只读诊断（不进评分）：扫描里 load_adapted_weights() 读回打日志，表留审计轨迹"),
    "agent_memory": (PENDING_DEAD, [],
                     "全库最大表；唯一内容读者 MemoryStore.get_recent_memories 只在 LLM 模式的 QueenDistiller 里调用，"
                     "生产恒 --no-llm（outcomes_fetcher._find_memory_ids 只为回填 outcome 找行号）。政策问题待拍板"),
    "barrier_outcomes": (PENDING_DEAD, [],
                         "纸面组合三重障碍出场记录：注释写明「只写不读、待攒样本」，与 closed_trades.jsonl 逐行重复。用户 10-19：先不动"),
    # ── alpha_hive_bot/subscribers.db（Telegram 订阅机器人，进程外独立服务）
    "subscribers": (READ, ["alpha_hive_bot/subscriber_db.py::SubscriberDB.get_tier",
                           "alpha_hive_bot/subscriber_db.py::SubscriberDB.list_active_chat_ids"],
                    "订阅者与等级：推送任务按它选收件人"),
    "watchlist": (READ, ["alpha_hive_bot/subscriber_db.py::SubscriberDB.get_watch"], "订阅者自选股"),
    "alert_rules": (READ, ["alpha_hive_bot/subscriber_db.py::SubscriberDB.get_alerts",
                           "alpha_hive_bot/subscriber_db.py::SubscriberDB.list_active_alerts"], "订阅者价格提醒规则"),
}

#: 通用表搬运（备份导出 / 恢复按表名循环）：写的是「任何表」，不是某张表的生产者。表名 → 说明。
GENERIC_TABLE_WRITES: dict = {
    "{table}": "data_backup/export.py::dump_table_sql 把每张表导成 SQL 文本（INSERT INTO {table} 写进 .sql 文件，restore 读回）",
    "sqlite_sequence": "SQLite 内部表；备份导出 / 恢复时按原样重放（同上）",
}

#: PENDING_DEAD 表的见证就是它的写入 SQL（扫描器自动认出）——清理掉写入，表就从扫描结果里消失 ⇒ 登记过期红。

# ═════════════════════════════════════════════════════════════════════ 文件
#: 写入落点 → 它写的产物（一个或多个）。v0.45.543 全仓普查：147 处落点，每处读过代码、读者逐条经
#: `scope_reads_token` 核对（研究底稿：三路只读代理 + 主会话复核；模块常量里的文件名归到真正引用它的函数）。
SITE_ARTIFACTS: dict = {
    'alert_manager.py::AlertAnalyzer.save_alerts': ('logs/alerts-<date>.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter._fallback_dashboard_data': ('dashboard-data.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter._generate_ml_reports._gen_one': ('analysis-{T}-ml-{date}.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter._generate_synthetic_swarm_results': ('.swarm_results_*.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter._post_scan_enrichment': ('.swarm_results_*.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter._save_output_files': ('alpha-hive-daily-*.md (+thread txt/index.html/rss.xml)',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter.run_swarm_scan._analyze_and_save': ('.checkpoint_<session>_<date>.json',),
    'alpha_hive_daily_report.py::AlphaHiveDailyReporter.save_report': ('alpha-hive-daily-*.json',),
    'alpha_hive_daily_report.py::main': ('.samples-only-<date>.json',),
    'alphabot/launcher.py::_open_server_log': ('Alpha Bot server.log',),
    'alphabot/launcher.py::_present': ('Alpha Bot window.pid',),
    'alphabot/launcher.py::announce_app_shell': ('Alpha Bot shell_drift_seen.json',),
    'alphabot/launcher.py::save_config': ('Alpha Bot launcher.json',),
    'alphabot/macos_app.py::build_app': ('~/Applications/Alpha Bot.app',),
    'alphabot/service.py::AlphaBotService.take_snapshot': ('alphabot_state/intraday/<date>/*.jsonl',),
    'alphabot/service.py::_atomic_write_json': ('alphabot_state/settings.json',),
    'backtest_engine.py::BacktestEngine.backfill_prices.save_callback': ('report_snapshots/*.json',),
    'cboe_fetcher.py::<module>': ('cboe_sample.html',),
    'cboe_fetcher.py::CBOEDailyFetcher._write_cache': ('cache/cboe_daily/*.json',),
    'cboe_vix.py::_write_cache': ('cache/vix_history_cboe.csv',),
    'cboe_vix.py::_write_ledger': ('cache/vix_quote_ledger.json',),
    'changelog_guard.py::_run_contract_gate': ('tmp contract-gate plugin .py',),
    'changelog_guard.py::install_hooks': ('.git/hooks/*',),
    'chart_engine.py::_fig_to_b64': ('in-memory PNG (BytesIO)',),
    'cloud_snapshot_fetch.py::main': ('cloud_snapshots/<date>/{T,market,manifest}.json',),
    'code_executor.py::CodeExecutor._write_audit_log': ('code_executor sandbox audit.log',),
    'code_executor.py::CodeExecutor.execute_python': ('sandbox scripts/script_*.py',),
    'collect_data.py::main': ('{T}_raw.json',),
    'congress_trades_scraper.py::_save_cache': ('cache/congress_trades.json',),
    'crowding_detector.py::CrowdingDetector.save_to_json': ('crowding_<T>.json',),
    'dashboard_renderer.py::render_dashboard_html': ('dashboard-data.json',),
    'data_backup/export.py::copy_root_files': ('data backup repo (root files)',),
    'data_backup/export.py::copy_state_dir': ('data backup repo (state dirs)',),
    'data_backup/export.py::export_db': ('data backup db_exports/*.sql + _meta.json',),
    'data_backup/export.py::write_manifest_and_sums': ('data backup BACKUP_MANIFEST.json + SHA256SUMS',),
    'data_backup/migrate_data_root.py::_write_report': ('migrate_data_root report *.json',),
    'data_backup/migrate_data_root.py::backup_db': ('migrated SQLite db copy',),
    'data_backup/migrate_data_root.py::copy': ('migrated data files',),
    'data_backup/migrate_data_root.py::retire': ('RETIRE_RECORD.json',),
    'data_backup/migrate_data_root.py::unretire': ('RETIRE_RECORD.undone-*.json (+moved files)',),
    'data_backup/restore.py::restore_state': ('restored state dirs',),
    'data_backup/run_backup.py::_append_history': ('logs/backup_status_history.jsonl',),
    'data_backup/run_backup.py::_write_status': ('logs/backup_status.json',),
    'data_fetcher.py::<module>': ('realtime_metrics.json',),
    'data_fetcher.py::CacheManager.save': ('cache/<source>_<T>.json (data_fetcher)',),
    'data_migrations/__main__.py::main': ('logs/data_migrations.json',),
    'data_migrations/runner.py::append_record': ('migrations_state/applied.jsonl',),
    'data_migrations/runner.py::online_backup': ('_manual_backups/pheromone.db.bak-*',),
    'deep_analysis.py::main': ('deep-*.html',),
    'deploy_orchestrator.py::deploy': ('~/.claude/scripts/alpha-hive-orchestrator.sh',),
    'deploy_orchestrator.py::main': ('orchestrator_deploy.json',),
    'earnings_history.py::earnings_move_stats_detail': ('earnings_cache/{T}_moves.json',),
    'earnings_history.py::get_past_earnings_dates': ('earnings_cache/{T}_history.json',),
    'earnings_vol_signal.py::_write_jsonl': ('options_paper_state/earnings_signals.jsonl',),
    'earnings_watcher.py::EarningsWatcher.fetch_earnings_results': ('earnings_cache/{T}_results.json',),
    'earnings_watcher.py::EarningsWatcher.get_earnings_date': ('earnings_cache/{T}_date.json',),
    'earnings_watcher.py::EarningsWatcher.update_report_with_earnings': ('daily report markdown (in-place edit)',),
    'economic_calendar_watch.py::_save_state': ('cache/economic_calendar_watch.json',),
    'edgar_rss.py::EdgarRSSClient.get_recent_form4_alerts': ('sec_cache/edgar_rss.json',),
    'entry_price_backfill.py::main': ('entry_price_backfill --out decisions JSON',),
    'factor_attribution.py::_download_ff5': ('cache/ff5_daily.parquet',),
    'factor_attribution.py::_download_mom': ('cache/mom_daily.parquet',),
    'fear_greed.py::get_fear_greed': ('cache/fear_greed.json',),
    'feedback_loop.py::BacktestAnalyzer.save_accuracy_dashboard': ('accuracy_dashboard.html',),
    'feedback_loop.py::ReportSnapshot.save_to_json': ('report_snapshots/*.json',),
    'generate_deep_v2.py::main': ('deep-{T}-{date}.html',),
    'generate_ml_report.py::MLEnhancedReportGenerator._write_file_async': ('analysis-{T}-ml-{date}.json',),
    'health_check.py::push_slack': ('logs/health_slack_payload_{date}.txt',),
    'health_check.py::save_log': ('logs/health_{date}.json',),
    'ibkr_sync.py::export_daily_actions': ('paper_account/actions/actions_{date}.json',),
    'ibkr_sync.py::import_ibkr_statement': ('paper_account/real_fills.jsonl',),
    'ibkr_sync.py::reconcile': ('paper_account/reconcile/reconcile_{date}.json',),
    'iv_history.py::append_observation': ('cache/iv_history_{T}.jsonl',),
    'iv_history.py::load_iv_history': ('cache/.iv_index_migrated_{T}',),
    'iv_history.py::merge_snapshots_into_index': ('cache/iv_history_{T}.jsonl',),
    'ledger_io.py::_atomic_write_text': ("ledger_io primitive (callers' ledgers)",),
    'migrate_ambiguous_backfill.py::main': ('db_backups/pheromone_pre_P0_tolerance_fix_*.db',),
    'ml_model_guard.py::snapshot_model_file': ('ml_model_history/{stem}-{date}.json + manifest.jsonl',),
    'ml_predictor.py::HGBModel.save_model': ('ml_model_cache.json', 'ml_model.json'),
    'ml_predictor.py::HistoricalDataBuilder.save_to_file': ('training_data.json',),
    'ml_predictor.py::SGDMLModel.save_model': ('ml_model_cache.json', 'ml_model.json'),
    'ml_predictor.py::SimpleMLModel.save_model': ('ml_model_cache.json', 'ml_model.json'),
    'ml_predictor_extended.py::HistoricalDataBuilder.save_to_file': ('training_data_extended.json',),
    'ml_predictor_extended.py::SimpleMLModel.save_model': ('ml_model_extended.json',),
    'newsapi_client.py::_safe_cache': ('cache/news/{T}_news.json',),
    'options_analyzer.py::OptionsDataFetcher._save_last_valid_iv': ('cache/options_{T}_last_valid_iv.json',),
    'options_analyzer.py::OptionsDataFetcher._write_cache': ('cache/options_{T}_{type}.json',),
    'options_analyzer.py::_write_snapshot': ('cache/options_snapshot_{T}_{date}.json',),
    'options_backtester.py::<module>': ('strategy_backtest_report.txt',),
    'options_paper_leg.py::_append_jsonl': ('options_paper_state/*.jsonl',),
    'options_paper_leg.py::_save_meta': ('options_paper_state/meta.json',),
    'options_paper_leg.py::_write_jsonl': ('options_paper_state/*.jsonl',),
    'paper_portfolio.py::_append_jsonl': ('paper_portfolio_state/*.jsonl',),
    'paper_portfolio.py::_save_meta': ('paper_portfolio_state/meta.json',),
    'paper_portfolio.py::_write_jsonl': ('paper_portfolio_state/*.jsonl',),
    'paper_portfolio.py::main': ('paper_portfolio_card.html',),
    'param_optimizer.py::_backup_state': ('paper_portfolio_state_backup/',),
    'param_optimizer.py::_restore_state': ('paper_portfolio_state/ (restored)',),
    'param_optimizer.py::main': ('param_optimization_report.html',),
    'param_optimizer.py::run_grid': ('param_optimization_results.json',),
    'pead_analyzer.py::_save_cache': ('cache/pead_*.json',),
    'pheromone_board.py::PheromoneBoard._save_fallback_batch': ('pheromone_fallback.jsonl',),
    'portfolio_backtest.py::main': ('portfolio_backtest --save JSON',),
    'portfolio_greeks.py::_append_jsonl': ('hedge_state/*.jsonl',),
    'portfolio_greeks.py::_default_beta': ('hedge_state/beta_cache.json',),
    'portfolio_greeks.py::_delta_save_meta': ('hedge_state/delta_meta.json',),
    'portfolio_greeks.py::_run_for_date': ('hedge_state/greeks_*.json',),
    'portfolio_greeks.py::_save_meta': ('hedge_state/meta.json',),
    'portfolio_greeks.py::_write_jsonl': ('hedge_state/*.jsonl',),
    'price_history.py::append_observation': ('cache/price_history_*.jsonl',),
    'price_history.py::load_price_history': ('cache/.price_index_migrated_*',),
    'price_history.py::merge_snapshots_into_index': ('cache/price_history_*.jsonl',),
    'probability_scorecard.py::record_published': ('probability_scorecard_state/published.jsonl',),
    'production_clone.py::_write_hook': ('production clone .git/hooks',),
    'production_sync.py::write_result': ('logs/production_sync.json',),
    'quiver_fetcher.py::QuiverFetcher._save_cache': ('cache/quiver/*.json',),
    'real_data_sources.py::_write_cache': ('real_data_sources cache/{name}.json',),
    'reddit_sentiment.py::RedditSentimentClient._fetch_ranking': ('reddit_cache/ranking_*.json',),
    'reddit_sentiment.py::RedditSentimentClient.get_ticker_sentiment': ('reddit_cache/*_sentiment.json',),
    'relabel_nyse_calendar.py::apply': ('relabel snapshot backups (report_snapshots copies)',),
    'relabel_nyse_calendar.py::main': ('nyse_relabel_v0.45.521_audit.json',),
    'replay_ohlc_store.py::ReplayOhlcStore._quarantine': ('replay_ohlc_state/*.json.invalid-*',),
    'replay_ohlc_store.py::ReplayOhlcStore._write': ('replay_ohlc_state/<TICKER>.json',),
    'report_deployer.py::_append_gh_pages_deploy_log': ('logs/.gh_pages_deploy_log.jsonl',),
    'report_web_assets.py::write_pwa_files': ('manifest.json + sw.js (PWA)',),
    'risk_engine.py::_estimate_beta': ('.risk_cache/beta_*.json',),
    'run_daily_scan.py::_write_status': ('logs/last_run_status.json',),
    'scan_timing.py::write': ('logs/scan_timing.json',),
    'sec_edgar.py::SECEdgarClient._load_cik_map': ('sec_cache/company_tickers.json',),
    'sec_edgar.py::SECEdgarClient.get_insider_trades': ('sec_cache/*_insider_summary.json',),
    'sec_edgar.py::SECEdgarClient.get_recent_form4_filings': ('sec_cache/*_form4_list.json',),
    'self_analyst.py::main': ('self_analysis_briefs/self_analysis_*.md',),
    'sell_strike_ledger.py::_write_shard': ('sell_strike_state/<tenor>/*.jsonl',),
    'sell_strike_report.py::write_local_report': ('sell_strike_state/reports/sell-strike-*.md',),
    'step_contract.py::write_out': ('orchestrator step --out JSON envelopes',),
    'straddle_gex_prereg.py::run_once': ('options_paper_state/straddle_gex_prereg_result.json',),
    'thesis_breaks.py::ThesisBreakMonitor.save_to_json': ('thesis_breaks_<T>.json (cwd)',),
    'tools/clockshift/audit.py::main': ('$GITHUB_STEP_SUMMARY',),
    'tools/clockshift/audit.py::run_pytest': ('clockshift --log-dir/{label}.log',),
    'vix_term_structure.py::get_vix_term_structure': ('cache/vix_term_structure.json',),
    'vrp_signal.py::_write_jsonl': ('vrp_state/vrp_signals.jsonl',),
    'vrp_signal.py::main': ('vrp_signal --out JSON',),
    'weekly_optimizer.py::_backup_config': ('config.py.weights.bak + weight_backups/config_*.py',),
    'weekly_optimizer.py::_restore_config_from_backup': ('config.py',),
    'weekly_optimizer.py::append_history': ('weight_history.jsonl',),
    'weekly_optimizer.py::write_weights_to_config': ('config.py',),
    'yahoo_trending.py::get_trending_tickers': ('cache/yahoo_trending.json',),
}

#: 产物 → (类别, [(读者, 令牌) 或 (读者, 令牌, SELF)], 说明)
ARTIFACTS: dict = {
    # ── READ
    '.swarm_results_*.json': (READ, [('scan_coverage_gate.py::check', '.swarm_results_'), ('earnings_calendar.py::_DiskObservations.results', '.swarm_results_'), ('orchestrator_steps.py::_step2_rc1_evidence', '.swarm_results_')],
        '文件不存在时从 opportunities 合成的回退版蜂群结果；与主写入者同一产物'),
    'Alpha Bot launcher.json': (READ, [('alphabot/launcher.py::config_path', 'launcher.json')],
        '启动器配置（数据根/端口）；消费者 load_config（launcher.main、macos_app）'),
    'Alpha Bot window.pid': (READ, [('alphabot/launcher.py::_window_pid_path', 'window.pid')],
        '开着窗口的启动器 pid；路径助手 _window_pid_path，实际消费者 window_owner（main 中判重复窗口）'),
    'RETIRE_RECORD.json': (READ, [('data_backup/migrate_data_root.py::check_old', 'RETIRE_RECORD.json'), ('data_backup/migrate_data_root.py::unretire', 'RETIRE_RECORD.json')],
        '旧数据根退役记录（同时挪文件）；check-old/unretire CLI 子命令读回，一次性迁移工具'),
    'alpha-hive-daily-*.json': (READ, [('push_report_to_slack.py::main', 'alpha-hive-daily-'), ('dashboard_renderer.py::_iter_prev_reports', 'alpha-hive-daily-'), ('alert_manager.py::AlertAnalyzer.analyze', 'alpha-hive-daily-')],
        '日报主 JSON；Slack 推送 Step7、仪表板历史、告警、orchestrator_steps/collect_data 读'),
    'alpha-hive-daily-*.md (+thread txt/index.html/rss.xml)': (READ, [('dashboard_renderer.py::render_dashboard_html', 'alpha-hive-daily-'), ('report_deployer.py::deploy_static_to_ghpages', 'alpha-hive-daily-'), ('alpha_hive_bot/push_job.py::fetch_daily_md', 'alpha-hive-daily-')],
        '日报 MD 被仪表板嵌入、部署到 gh-pages 后 Telegram 推送拉取；X 线程 txt 只给人看；index/rss 经部署上网站'),
    'alphabot_state/intraday/<date>/*.jsonl': (READ, [('alphabot/service.py::AlphaBotService.intraday', '.jsonl'), ('alphabot/service.py::AlphaBotService.intraday_dates', 'intraday')],
        '盘中快照追加；server.py /intraday 接口经 intraday()/intraday_dates() 读回给页面'),
    'alphabot_state/settings.json': (READ, [('alphabot/service.py::AlphaBotService._settings_path', 'settings.json')],
        '唯一调用点 update_settings；消费者 settings()（server.py /settings、轮询 focus 列表）'),
    'analysis-{T}-ml-{date}.json': (READ, [('ml_model_guard.py::read_day_records', 'analysis-*-ml-'), ('dashboard_renderer.py::render_dashboard_html', 'analysis-'), ('alpha_hive_mcp.py::_load_json', 'analysis-'), ('generate_deep_v2.py::find_latest_json', 'analysis-'), ('probability_scorecard.py::load_ml_probabilities', 'analysis-')],
        '每标的 ML 增强 JSON（另写 ml-enhanced HTML 给人看）；模型守卫/仪表板注入/MCP/ic_rerun_readiness 读'),
    'cache/.iv_index_migrated_{T}': (READ, [('iv_history.py::_migration_marker', '.iv_index_migrated_')],
        '一次性迁移哨兵；load_iv_history 每次 os.path.exists 检查它决定是否重扫快照'),
    'cache/economic_calendar_watch.json': (READ, [('economic_calendar_watch.py::_state_path', 'economic_calendar_watch.json')],
        'Step 13 监视器节流状态；check() 经 _state_path→_load_state 读回决定是否探上游'),
    'cache/iv_history_{T}.jsonl': (READ, [('iv_history.py::_index_path', 'iv_history_'), ('vrp_signal.py::assess', 'iv_history_')],
        'IV 观测索引；_read_index→load_iv_history 进 iv_rank，vrp_signal/Alpha Bot 溢价页也读'),
    'cache/options_snapshot_{T}_{date}.json': (READ, [('vrp_signal.py::_iter_snapshots', 'options_snapshot_'), ('earnings_vol_signal.py::_iter_snapshots', 'options_snapshot_'), ('options_analyzer.py::OptionsAgent.analyze', 'options_snapshot_')],
        '期权快照（不可再取）；VRP/财报波动信号/同日复用读回'),
    'cache/price_history_*.jsonl': (READ, [('price_history.py::_index_path', 'price_history_'), ('price_history.py::coverage_report', 'price_history_')],
        '价格索引；_read_index←load_price_history（vrp/卖权/portfolio_greeks/earnings_history）。注：本写者没有生产调用方'),
    'cache/vix_quote_ledger.json': (READ, [('cboe_vix.py::_ledger_path', 'vix_quote_ledger.json')],
        'VIX 报价收盘核对账本；消费者 _read_ledger → verify_quote_ledger / get_vix_session_close（mismatch 闸）'),
    'cloud_snapshots/<date>/{T,market,manifest}.json': (READ, [('cloud_snapshot_loader.py::load_market', 'market.json'), ('cloud_snapshot_loader.py::load_manifest', 'manifest.json')],
        '云端当日快照（提交到 cloud-snapshots 分支）；loader 经 git show 读，snapshot_mode/entry_price_backfill 用'),
    'dashboard-data.json': (READ, [('report_deployer.py::verify_cdn_deployment', 'dashboard-data.json'), ('templates/dashboard.js', 'dashboard-data.json'), ('alpha_hive_bot/query_commands.py::fetch_dashboard', 'dashboard-data.json')],
        'index.html 渲染崩溃时的降级版；CDN 校验读 _generated_at，网站前端与 Telegram bot 经 gh-pages 拉取'),
    'hedge_state/*.jsonl': (READ, [('portfolio_greeks.py::ledger_view', 'positions.jsonl'), ('portfolio_greeks.py::load_history', 'trades.jsonl'), ('portfolio_greeks.py::_delta_paths', 'delta_trades.jsonl')],
        '对冲账本成交追加；load_history / ledger_view → Alpha Bot 对冲页 (service.py:360)'),
    'hedge_state/delta_meta.json': (READ, [('portfolio_greeks.py::ledger_view', 'delta_meta.json'), ('portfolio_greeks.py::_delta_paths', 'delta_meta.json')],
        '逐票对冲账 meta；_delta_load_meta 与 ledger_view(Alpha Bot) 读；缺省 shadow 模式下不写'),
    'hedge_state/greeks_*.json': (READ, [('portfolio_greeks.py::load_history', 'greeks_'), ('portfolio_greeks.py::_load_audit', 'greeks_'), ('alphabot/straddle.py::load_ledger', 'greeks_')],
        '每日 Greeks 审计；日报 render_markdown(_load_audit)、Alpha Bot 跨式页 / 对冲页历史读'),
    'hedge_state/meta.json': (READ, [('portfolio_greeks.py::ledger_view', 'meta.json')],
        'SPY 覆盖账 meta（现金）；_load_meta(META_FILE) 在 _run_for_date 里读，ledger_view → Alpha Bot'),
    'logs/.gh_pages_deploy_log.jsonl': (READ, [('report_deployer.py::gh_pages_step_status', '.gh_pages_deploy_log.jsonl'), ('scripts/alpha-hive-orchestrator.sh', '.gh_pages_deploy_log.jsonl')],
        'gh-pages 部署结局；编排器 Step 5 经 gh_pages_step_status 判定'),
    'logs/backup_status.json': (READ, [('scripts/alpha-hive-orchestrator.sh', 'backup_status.json')],
        '最近一次备份结局；编排器 Step14 jq 判 secret_scan/stage'),
    'logs/backup_status_history.jsonl': (READ, [('backup_continuity.py::_history_file', 'backup_status_history.jsonl')],
        '备份结果逐日追加；backup_continuity.assess（编排器 Step15）判连续性'),
    'logs/data_migrations.json': (READ, [('scripts/alpha-hive-orchestrator.sh', 'data_migrations.json')],
        '--out 迁移结局（经 run_data_migrations.py）；编排器 _data_migrations_step jq 读进 steps_result'),
    'logs/health_{date}.json': (READ, [('health_check.py::check_health_check_self', 'health_')],
        '体检结果；下次 health_check 的自检只看最新文件 mtime（手动运行工具）'),
    'logs/production_sync.json': (READ, [('production_sync.py::load_for_date', 'PATHS.production_sync')],
        '生产同步结局；scan_timing.production_sync_result → scan_timing.json → status.json → alert_manager'),
    'logs/scan_timing.json': (READ, [('orchestrator_steps.py::_step2_rc1_evidence', 'scan_timing.json'), ('scripts/alpha-hive-orchestrator.sh', 'scan_timing.json'), ('alert_manager.py::AlertAnalyzer._check_deploy_and_code_sync', 'scan_timing.json')],
        '扫描计时 / 计数；编排器用 jq 并进 status.json，Step2 解释器拿它作证据'),
    'migrations_state/applied.jsonl': (READ, [('data_migrations/runner.py::read_ledger', 'applied.jsonl'), ('data_migrations/runner.py::record_tool_run', 'applied.jsonl')],
        '迁移账本追加；文件名在模块常量 LEDGER_NAME，消费者 read_ledger → run_pending 判已应用'),
    'ml_model_cache.json': (READ, [('alpha_hive_daily_report.py::AlphaHiveDailyReporter._init_scan_context', 'PATHS.ml_model_cache'), ('generate_ml_report.py::MLEnhancedReportGenerator._model_file', 'PATHS.ml_model_cache')],
        '生产模型（HGB；无 sklearn 时 SGD / Simple 降级同一文件）：扫描与 ML 报告 load_model 读回，QueenDistiller._ml_oos_trust_factor 读 oos'),
    'options_paper_state/*.jsonl': (READ, [('alphabot/straddle.py::load_ledger', 'closed_trades.jsonl'), ('alphabot/straddle.py::load_ledger', 'positions.jsonl'), ('alphabot/straddle.py::load_ledger', 'equity_curve.jsonl')],
        '跨式账本 closed_trades；options_paper_leg 自身 compute_kpis/run 与 Alpha Bot 跨式页读'),
    'options_paper_state/earnings_signals.jsonl': (READ, [('alphabot/straddle.py::load_ledger', 'earnings_signals.jsonl'), ('straddle_gex_prereg.py::_load_rows', 'earnings_signals.jsonl')],
        '财报跨式信号；options_paper_leg 经 evs.signals_for_date/load_signals 开仓，Alpha Bot 跨式页、GEX 预注册检验读'),
    'options_paper_state/meta.json': (READ, [('alphabot/straddle.py::load_ledger', 'meta.json')],
        '跨式账本现金/元数据；_load_meta 每轮读回，Alpha Bot 跨式页读'),
    'options_paper_state/straddle_gex_prereg_result.json': (READ, [('straddle_gex_prereg.py::_result_path', 'PATHS.straddle_prereg_result')],
        '跨式 GEX 预注册冻结结果；read_result ← progress（Alpha Bot 跨式页）/ run_once'),
    'orchestrator step --out JSON envelopes': (READ, [('orchestrator_steps.py::_load_doc', 'schema_version')],
        'Step 10–15 工具的 --out 外壳；编排器步骤解释器 _load_doc 读'),
    'orchestrator_deploy.json': (READ, [('scripts/alpha-hive-orchestrator.sh', 'orchestrator_deploy.json')],
        '--out 部署结局；编排器 _orchestrator_autodeploy 用 jq 读 outcome 并并进 status.json steps_result（另有测试守卫读）'),
    'paper_account/real_fills.jsonl': (READ, [('ibkr_sync.py::_fills_file', 'real_fills.jsonl')],
        'IBKR 真实成交；reconcile() 经 _fills_file 读回对账（手动流程）'),
    'paper_portfolio_state/*.jsonl': (READ, [('paper_portfolio.py::compute_kpis', 'closed_trades.jsonl'), ('paper_portfolio.py::_run_for_date', 'closed_trades.jsonl'), ('paper_portfolio.py::_run_for_date', 'positions.jsonl'), ('paper_portfolio.py::render_portfolio_card', 'positions.jsonl'), ('paper_portfolio.py::compute_kpis', 'equity_curve.jsonl'), ('paper_portfolio.py::_run_for_date', 'equity_curve.jsonl')],
        'closed_trades；文件名只在模块常量，compute_kpis/ibkr_sync.reconcile/param_optimizer 经 CLOSED_FILE 读'),
    'paper_portfolio_state/meta.json': (READ, [('report_formatters.py::_build_data_quality_section', 'paper_portfolio_state'), ('paper_portfolio.py::_load_meta', 'meta.json')],
        '现金/last_run_date；_load_meta 每轮读回，日报数据质量节读新鲜度'),
    'param_optimization_results.json': (READ, [('param_optimizer.py::_results_file', 'param_optimization_results.json')],
        '网格结果；main --html 经 _results_file 读回渲染 HTML（手动离线工具）'),
    'probability_scorecard_state/published.jsonl': (READ, [('probability_scorecard.py::_ledger_path', 'published.jsonl')],
        '概率前向账本；load_ledger→score_published 只经人工跑的 CLI --published，没有定时读者'),
    'realtime_metrics.json': (READ, [('generate_ml_report.py::main', 'realtime_metrics.json')],
        '编排器 Step1 采集汇总；Step3 generate_ml_report.main 加载，编排器后置条件核存在'),
    'replay_ohlc_state/<TICKER>.json': (READ, [('replay_ohlc_store.py::ReplayOhlcStore._path', '{ticker}.json')],
        '回放日线库；_entry 经 _path 读。唯一的构造者在 experiments/fg_exposure_gate_forward_test.py，由 ic_rerun_readiness Step11 加载'),
    'report_snapshots/*.json': (READ, [('self_analyst.py::load_snapshots', 'actual_prices'), ('risk_engine.py::_load_snapshot_returns', 'actual_prices'), ('feedback_loop.py::ReportSnapshot.load_from_json', 'actual_prices'), ('feedback_loop.py::BacktestAnalyzer.__init__', 'report_snapshots'), ('weekly_optimizer.py::_snapshots_dir', 'report_snapshots'), ('alert_manager.py::AlertAnalyzer._check_outcomes_backlog', 'report_snapshots')],
        '原地回填 actual_prices；写者只经 backtest_engine CLI 手动触发，读者是月度自诊断/风控/反馈环'),
    'sell_strike_state/<tenor>/*.jsonl': (READ, [('sell_strike_ledger.py::_state_dir', 'PATHS.sell_strike_state'), ('sell_strike_ledger.py::_shard', '{date_str[:7]}.jsonl')],
        '卖权月度分片账本；_load_shard/load_rows → sell_strike_report、MCP、Alpha Bot'),
    'vrp_state/vrp_signals.jsonl': (READ, [('vrp_signal.py::load_rows', 'vrp_signals.jsonl'), ('vrp_signal.py::_record_day_with_info', 'vrp_signals.jsonl')],
        'VRP 账本；文件名在模块常量 SIGNALS_FILE 里，load_rows→render_markdown（日报），Alpha Bot _premium_vrp'),
    'weight_history.jsonl': (READ, [('health_check.py::check_weekly_optimizer', 'weight_history.jsonl'), ('weekly_optimizer.py::_history_file', 'weight_history.jsonl')],
        '权重审计日志；health_check（定时任务）读，_last_applied_record 用于回滚'),
    '{T}_raw.json': (READ, [('generate_ml_report.py::main', '_raw.json'), ('slack_report_notifier.py::SlackReportNotifier._format_rich_daily_mrkdwn', '_raw.json')],
        '手动方案B 精简原始数据；ML 报告取价兜底、Slack 富文本日报优先读（push_report_to_slack 调）'),
    # ── CACHE
    '.checkpoint_<session>_<date>.json': (CACHE, [('alpha_hive_daily_report.py::AlphaHiveDailyReporter._init_scan_context', '.checkpoint_'), ('alpha_hive_daily_report.py::AlphaHiveDailyReporter._load_checkpoint', 'saved_at')],
        '扫描崩溃恢复断点；_init_scan_context 定路径，_load_checkpoint（run_swarm_scan 调用）读回，扫完删除'),
    '.risk_cache/beta_*.json': (CACHE, [('risk_engine.py::_estimate_beta', 'beta_', SELF)],
        'β 24h 缓存，同一函数读写（self）；risk_engine 只有 CLI 调用'),
    'Alpha Bot shell_drift_seen.json': (CACHE, [('alphabot/launcher.py::_shell_drift_seen_path', 'shell_drift_seen.json')],
        '启动壳过时「已提醒」指纹；消费者 _app_shell_drift（check_app_shell），丢了只是多提醒一次'),
    'cache/.price_index_migrated_*': (CACHE, [('price_history.py::_migration_marker', '.price_index_migrated_')],
        '一次性迁移哨兵；load_price_history 自己用 exists() 检查（self）'),
    'cache/<source>_<T>.json (data_fetcher)': (CACHE, [('data_fetcher.py::CacheManager.load', '.json'), ('data_fetcher.py::CacheManager.__init__', 'PATHS.cache_dir')],
        'data_fetcher 各源 TTL 缓存；同类 get_* 先 cache.load'),
    'cache/cboe_daily/*.json': (CACHE, [('cboe_fetcher.py::CBOEDailyFetcher._read_cache', 'cached_at'), ('cboe_fetcher.py::CBOEDailyFetcher.__init__', 'PATHS.cboe_daily_cache')],
        'CBOE PCCE/VIX期限/SKEW/VVIX TTL 缓存；fetch_* 先 _read_cache'),
    'cache/congress_trades.json': (CACHE, [('congress_trades_scraper.py::_cache_path', 'congress_trades.json')],
        '国会交易 4h 缓存；消费者 _load_cached（ScoutBee 经 get_congress_trades_for_ticker）'),
    'cache/fear_greed.json': (CACHE, [('fear_greed.py::_cache_path', 'fear_greed.json')],
        '1h F&G 缓存；get_fear_greed 自己经 _cache_path 读回（日报/纸面组合/云快照调用）'),
    'cache/ff5_daily.parquet': (CACHE, [('factor_attribution.py::_download_ff5', 'ff5_daily.parquet', SELF)],
        'self：French FF5 因子缓存，经 _load_cached 读回；generate_deep_v2/portfolio_factor_attribution 用'),
    'cache/mom_daily.parquet': (CACHE, [('factor_attribution.py::_download_mom', 'mom_daily.parquet', SELF)],
        'self：French MOM 因子缓存，经 _load_cached 读回'),
    'cache/news/{T}_news.json': (CACHE, [('newsapi_client.py::get_ticker_news', '_news.json')],
        '30min 新闻缓存；get_ticker_news 按 mtime 读回（BuzzBee）'),
    'cache/options_{T}_last_valid_iv.json': (CACHE, [('options_analyzer.py::OptionsDataFetcher._read_last_valid_iv', 'last_valid_iv')],
        '收市后 IV 降级值；OptionsAgent.analyze 经 _read_last_valid_iv 读回'),
    'cache/options_{T}_{type}.json': (CACHE, [('options_analyzer.py::OptionsDataFetcher._get_cache_path', 'options_')],
        '期权链/HV 缓存；_read_cache 经 _get_cache_path 读回'),
    'cache/pead_*.json': (CACHE, [('pead_analyzer.py::_cache_path', 'pead_')],
        'PEAD 7 天缓存；_load_cache 经 _cache_path 读回 ← get_pead_analysis ← ChronosBee'),
    'cache/quiver/*.json': (CACHE, [('quiver_fetcher.py::QuiverFetcher.__init__', 'cache/quiver')],
        'Quiver 响应缓存，由 _load_cache 读回；QuiverFetcher 只在 __main__ demo 里用，生产里写者和读者都跑不到'),
    'cache/vix_history_cboe.csv': (CACHE, [('cboe_vix.py::_cache_path', 'vix_history_cboe.csv')],
        'VIX 历史 CSV 缓存；消费者 _read_cache / _cache_fresh（get_vix_history）'),
    'cache/vix_term_structure.json': (CACHE, [('vix_term_structure.py::_cache_path', 'vix_term_structure.json')],
        'VIX 期限结构 30 分钟缓存；get_vix_term_structure 自己读回（GuardBee / fear_greed / cboe_fetcher）'),
    'cache/yahoo_trending.json': (CACHE, [('yahoo_trending.py::_cache_path', 'yahoo_trending.json')],
        'Yahoo 热搜 15 分钟缓存；get_trending_tickers 自己读回 ← get_ticker_attention ← BuzzBee'),
    'earnings_cache/{T}_date.json': (CACHE, [('earnings_watcher.py::EarningsWatcher.get_earnings_date', '_date.json', SELF)],
        'self：yfinance 财报日缓存；v0.45.500 起只服务 --check-earnings，禁止别处读'),
    'earnings_cache/{T}_history.json': (CACHE, [('earnings_history.py::get_past_earnings_dates', '_history.json', SELF)],
        'self：历史财报日缓存 30 天 TTL；由 earnings_move_stats_detail 调用'),
    'earnings_cache/{T}_moves.json': (CACHE, [('earnings_history.py::earnings_move_stats_detail', '_moves.json', SELF)],
        'self：同函数 read_json_cache 读回；生产经 earnings_vol_signal._scan_with_info→earnings_move_stats'),
    'earnings_cache/{T}_results.json': (CACHE, [('earnings_watcher.py::EarningsWatcher.fetch_earnings_results', '_results.json', SELF)],
        'self：财报实际结果缓存，只服务 --check-earnings'),
    'hedge_state/beta_cache.json': (CACHE, [('portfolio_greeks.py::_read_beta_cache', 'beta_cache.json')],
        'β 兜底缓存；文件名只在模块常量 BETA_CACHE_FILE 里，实际读者 _read_beta_cache ← _fresh_cached_beta'),
    'real_data_sources cache/{name}.json': (CACHE, [('real_data_sources.py::_read_cache', '{name}.json')],
        'social/short 磁盘缓存；get_social_buzz/get_short_interest 读回（crowding_detector / RivalBee）'),
    'reddit_cache/*_sentiment.json': (CACHE, [('reddit_sentiment.py::RedditSentimentClient.get_ticker_sentiment', '_sentiment.json', SELF)],
        '逐票 Reddit 情绪缓存，同一函数读写（self）'),
    'reddit_cache/ranking_*.json': (CACHE, [('reddit_sentiment.py::RedditSentimentClient._fetch_ranking', 'ranking_', SELF)],
        'ApeWisdom 排名缓存，同一函数先读后写（self）；BuzzBee 用'),
    'sec_cache/*_form4_list.json': (CACHE, [('sec_edgar.py::SECEdgarClient.get_recent_form4_filings', '_form4_list.json', SELF)],
        'Form4 列表 5 分钟缓存，同一函数读写（self）'),
    'sec_cache/*_insider_summary.json': (CACHE, [('sec_edgar.py::SECEdgarClient.get_insider_trades', '_insider_summary.json', SELF)],
        '内部人汇总缓存，同一函数读写（self）；data_fetcher / BearBee 用'),
    'sec_cache/company_tickers.json': (CACHE, [('sec_edgar.py::SECEdgarClient._load_cik_map', 'company_tickers.json', SELF)],
        'CIK 映射缓存，同一函数读写（self）'),
    'sec_cache/edgar_rss.json': (CACHE, [('edgar_rss.py::EdgarRSSClient.get_recent_form4_alerts', 'edgar_rss.json', SELF)],
        'self：文件名只在模块常量 _CACHE_PATH，同函数磁盘缓存读回；ScoutBee 经 get_today_form4_alerts 调用'),
    # ── PENDING_DEAD
    '.samples-only-<date>.json': (PENDING_DEAD, [],
        '只在手动 --samples-only 时写；注释说「供 Hive app 读取」但仓内无读者，编排器也不跑该模式'),
    'crowding_<T>.json': (PENDING_DEAD, [],
        '方法全仓零调用者（.save_to_json 调用点都是 ReportSnapshot 的），写者与读者都不存在'),
    'logs/last_run_status.json': (PENDING_DEAD, [],
        '全仓没有读者；run_daily_scan.py 只能手动跑，编排器不调用它'),
    'ml_model.json': (PENDING_DEAD, [],
        'save_model() 无参时的缺省落点（MLPredictionService.train_model，RivalBee 经 ml_predictor_extended 触发）：全仓无参 load_model 调用点 = 0，生产只读 ml_model_cache.json'),
    'ml_model_extended.json': (PENDING_DEAD, [],
        '只在 import ml_predictor 失败时才写；其 load_model 全仓零调用'),
    'training_data.json': (PENDING_DEAD, [],
        '生产零调用者（仅 tests 调用），也无任何读者'),
    'training_data_extended.json': (PENDING_DEAD, [],
        '全仓零调用者（含测试外），也无读者'),
    # ── AUDIT
    'code_executor sandbox audit.log': (AUDIT, [],
        '代码执行沙箱审计追加；get_audit_log 存在但全仓零调用'),
    'nyse_relabel_v0.45.521_audit.json': (AUDIT, [],
        '一次性重标 T+N 的逐行新旧值审计 JSON，没有程序读回'),
    # ── FORENSIC
    'ml_model_history/{stem}-{date}.json + manifest.jsonl': (FORENSIC, [],
        '模型逐日留档+manifest 供退化事后归因；仅 _snapshot_exists 查存在性，无代码读内容'),
    'pheromone_fallback.jsonl': (FORENSIC, [],
        '只在 pheromone.db 写失败时追加的兜底副本；没有回放代码，只有 data_backup/export 备份它，供人工恢复'),
    'replay_ohlc_state/*.json.invalid-*': (FORENSIC, [],
        '读不懂的库文件改名隔离、留作证据；quarantined() 只列出文件名'),
    # ── BACKUP
    '_manual_backups/pheromone.db.bak-*': (BACKUP, [],
        '迁移/回填前 SQLite 在线备份'),
    'data backup BACKUP_MANIFEST.json + SHA256SUMS': (BACKUP, [],
        '备份清单与校验和；restore.find_manifest 读回'),
    'data backup db_exports/*.sql + _meta.json': (BACKUP, [],
        '库导出 SQL 与 _meta.json（restore.main 读 _meta 取原文件名）'),
    'data backup repo (root files)': (BACKUP, [],
        '数据根文件复制进备份仓库'),
    'data backup repo (state dirs)': (BACKUP, [],
        '状态目录复制进备份仓库'),
    'db_backups/pheromone_pre_P0_tolerance_fix_*.db': (BACKUP, [],
        '一次性迁移前 shutil.copy2 备份 pheromone.db'),
    'paper_portfolio_state/ (restored)': (BACKUP, [],
        '从 paper_portfolio_state_backup 还原生产状态（备份/恢复机制）'),
    'paper_portfolio_state_backup/': (BACKUP, [],
        '网格搜索前把纸面组合状态 copytree 备份，跑完由 _restore_state 还原'),
    'relabel snapshot backups (report_snapshots copies)': (BACKUP, [],
        '--apply 前逐份复制受影响的 report_snapshots，用于回滚'),
    'restored state dirs': (BACKUP, [],
        '从备份恢复状态目录'),
    # ── HUMAN
    '$GITHUB_STEP_SUMMARY': (HUMAN, [],
        '拨钟审计摘要追加进 Actions 页面，给人看'),
    'Alpha Bot server.log': (HUMAN, [],
        'Alpha Bot 服务 stdout/stderr 日志（追加+轮转到 server.log.1），只给人看'),
    'accuracy_dashboard.html': (HUMAN, [],
        '准确度看板 HTML，仅模块 __main__ 演示调用'),
    'cboe_sample.html': (HUMAN, [],
        '__main__ 测试脚本写到 cwd 的样例 HTML，只给人看'),
    'clockshift --log-dir/{label}.log': (HUMAN, [],
        '每轮 pytest 的完整输出日志（CI / 人工排查）'),
    'daily report markdown (in-place edit)': (HUMAN, [],
        '把实际财报数字插进已生成的 Markdown 简报，只给人看'),
    'deep-*.html': (HUMAN, [],
        '手动深度分析 HTML 报告，只给人看'),
    'deep-{T}-{date}.html': (HUMAN, [],
        '深度报告 HTML，只给人看（随数据备份）'),
    'entry_price_backfill --out decisions JSON': (HUMAN, [],
        '回填工具 CLI --out 逐行判决导出，只给人看'),
    'in-memory PNG (BytesIO)': (HUMAN, [],
        '非落盘：savefig 到 BytesIO 转 base64 嵌进 HTML 报告（扫描器误报）'),
    'logs/alerts-<date>.json': (HUMAN, [],
        '告警汇总；编排器 Step6 后置条件只核存在/新鲜度，不读内容，无程序读者'),
    'logs/health_slack_payload_{date}.txt': (HUMAN, [],
        '预构造 Slack 文案供人/Claude 复制；无代码读，health-check 定时任务已禁用'),
    'manifest.json + sw.js (PWA)': (HUMAN, [],
        '网站 PWA 资源；部署器按 CORE_STATIC_FILES 拷到 gh-pages，由浏览器消费'),
    'migrate_data_root report *.json': (HUMAN, [],
        '数据根迁移 copy/verify/retire 报告，只给人看'),
    'paper_account/actions/actions_{date}.json': (HUMAN, [],
        '给人手动在 TWS 下单的订单清单'),
    'paper_account/reconcile/reconcile_{date}.json': (HUMAN, [],
        '模拟 vs 真实成交对账报告，无代码读回'),
    'paper_portfolio_card.html': (HUMAN, [],
        'CLI --html 渲染 KPI 卡片，只给人看'),
    'param_optimization_report.html': (HUMAN, [],
        '参数网格对比 HTML，只给人看'),
    'portfolio_backtest --save JSON': (HUMAN, [],
        'CLI --save 导出完整回测结果，没有程序读回'),
    'self_analysis_briefs/self_analysis_*.md': (HUMAN, [],
        '月度自诊断 Markdown，给人 / 月度任务看；health_check 只查文件在不在'),
    'sell_strike_state/reports/sell-strike-*.md': (HUMAN, [],
        '本地卖权 Markdown 报告；MCP 现场渲染，不读这个文件'),
    'strategy_backtest_report.txt': (HUMAN, [],
        '__main__ 演示报告，写死 Cowork VM 路径 /sessions/...，用户 Mac 上不可达'),
    'thesis_breaks_<T>.json (cwd)': (HUMAN, [],
        '只有 __main__ 示例调用的配置导出，没有读者'),
    'vrp_signal --out JSON': (HUMAN, [],
        'CLI --out 导出；编排器并没有调用 vrp_signal'),
    # ── CODE
    '.git/hooks/*': (CODE, [],
        '安装 pre-commit/pre-push 钩子'),
    'RETIRE_RECORD.undone-*.json (+moved files)': (CODE, [],
        '撤销退役：文件挪回、记录改名存档'),
    'config.py': (CODE, [],
        '从备份还原 config.py'),
    'config.py.weights.bak + weight_backups/config_*.py': (CODE, [],
        '改写 config.py 之前备份代码；_restore_config_from_backup 和 --rollback 读 .bak'),
    "ledger_io primitive (callers' ledgers)": (CODE, [],
        '通用原子写原语本身不是产物；落盘文件在各调用点登记（旧守卫整文件豁免 ledger_io.py）'),
    'migrated SQLite db copy': (CODE, [],
        '迁移工具把库在线备份到新数据根（一次性迁移）'),
    'migrated data files': (CODE, [],
        '迁移工具复制旧根数据到新根'),
    'production clone .git/hooks': (CODE, [],
        '给生产克隆装 git 钩子'),
    'sandbox scripts/script_*.py': (CODE, [],
        '把待执行代码写成临时脚本再 subprocess 跑'),
    'tmp contract-gate plugin .py': (CODE, [],
        '在临时目录写 pytest 报告插件代码；report.json 由 pytest 写、本函数读，随临时目录删'),
    '~/.claude/scripts/alpha-hive-orchestrator.sh': (CODE, [],
        '部署编排器脚本副本'),
    '~/Applications/Alpha Bot.app': (CODE, [],
        '生成/替换 macOS app 包（壳脚本 + Info.plist）'),
}


#: PENDING_DEAD 文件产物的见证：(文件::限定名, 令牌)——该函数体里还出现这段字符串 ⇒ 死链还在。
#: 写入落点本身就是专门写它的（`data_fetcher.py::<module>`）时，见证可以就写那个落点与文件名。
PENDING_WITNESS: dict = {
    ".samples-only-<date>.json": ("alpha_hive_daily_report.py::main", ".samples-only-"),
    "crowding_<T>.json": ("crowding_detector.py::CrowdingDetector.save_to_json", "crowding_"),
    "training_data.json": ("ml_predictor.py::HistoricalDataBuilder.save_to_file", "training_data.json"),
    "training_data_extended.json": ("ml_predictor_extended.py::HistoricalDataBuilder.save_to_file", "training_data_extended.json"),
    "ml_model_extended.json": ("ml_predictor_extended.py::default_extended_model_path", "PATHS.ml_model_extended"),
    "logs/last_run_status.json": ("run_daily_scan.py::_write_status", "last_run_status.json"),
    "ml_model.json": ("ml_predictor.py::default_model_path", "PATHS.ml_model"),
}


# ═════════════════════════════════════════════════════════════════════ 对账（纯函数，便于有牙测试）

def table_problems(facts: dict, tables: dict, generic: dict) -> list:
    problems = []
    written = S.table_writers(facts)
    for t in sorted(set(written) - set(tables) - set(generic)):
        problems.append(f"表 {t} 有写入（{sorted(written[t])}）却未登记——登记读者（文件::函数，函数体里 SELECT 它），"
                        "或写明类别；没人读就别写")
    for t in sorted((set(tables) | set(generic)) - set(written)):
        problems.append(f"登记的表 {t} 已经没有写入了（清理完了？）——删条目" +
                        ("，并把 CEILING_PENDING_DEAD 减一" if tables.get(t, ("",))[0] == PENDING_DEAD else ""))
    for t, entry in tables.items():
        problems += _entry_shape(f"表 {t}", entry, table=True)
        cat, readers = entry[0], entry[1]
        for r in readers:
            if r.split("::", 1)[0].split("/")[0] in S.EXCLUDED_TOP:
                problems.append(f"表 {t} 的读者 {r} 在 tests/ 或 experiments/——测试读它不算生产读者")
            elif not S.scope_exists(facts, r):
                problems.append(f"表 {t} 的读者 {r} 不存在了（改名 / 删了？）——读者没了，这张表可能刚成为死链")
            elif t not in _scope(facts, r)["tables_read"]:
                problems.append(f"表 {t} 的读者 {r} 函数体里已经没有 SELECT … FROM {t}——它不再读这张表")
            elif r in written.get(t, set()) and cat != CACHE:
                problems.append(f"表 {t} 的读者 {r} 同时是写入方——自己读自己写的不算消费者")
    return problems


def file_problems(facts: dict, site_artifacts: dict, artifacts: dict, witnesses: dict) -> list:
    problems = []
    found = S.file_write_sites(facts)
    for site in sorted(set(found) - set(site_artifacts)):
        problems.append(f"未登记的落盘写入 {site}（{sorted(found[site])}）——登记它写什么、谁读（SITE_ARTIFACTS + ARTIFACTS），"
                        "或写明类别；没人读就别写")
    for site in sorted(set(site_artifacts) - set(found)):
        problems.append(f"SITE_ARTIFACTS 里的 {site} 已经不写文件了——删条目（过期条目是后门）")
    used = set()
    for site, arts in site_artifacts.items():
        if not arts:
            problems.append(f"{site} 没归到任何产物")
        for a in arts:
            used.add(a)
            if a not in artifacts:
                problems.append(f"{site} 归到的产物「{a}」没在 ARTIFACTS 里登记")
    for a in sorted(set(artifacts) - used - set(witnesses)):
        problems.append(f"产物「{a}」没有任何登记的写入落点了——删条目")
    writers_of: dict = {}
    for site, arts in site_artifacts.items():
        for a in arts:
            writers_of.setdefault(a, set()).add(site)
    for a, entry in artifacts.items():
        problems += _entry_shape(f"产物「{a}」", entry, table=False)
        cat, readers = entry[0], entry[1]
        for r in readers:
            site, token = r[0], r[1]
            self_read = len(r) > 2 and r[2] == SELF
            if site.split("::", 1)[0].split("/")[0] in S.EXCLUDED_TOP:
                problems.append(f"「{a}」的读者 {site} 在 tests/ 或 experiments/——测试读它不算生产读者")
                continue
            if "::" not in site:
                ok = S.nonpy_reads_token(site, token)
                if ok is None:
                    problems.append(f"「{a}」的读者文件 {site} 不存在了")
                elif not ok:
                    problems.append(f"「{a}」的读者文件 {site} 里已经没有「{token}」——它不再读这个产物")
                continue
            if not S.scope_exists(facts, site):
                problems.append(f"「{a}」的读者 {site} 不存在了（改名 / 删了？）——读者没了，这个产物可能刚成为死链")
            elif not S.scope_reads_token(facts, site, token):
                problems.append(f"「{a}」的读者 {site} 函数体里已经没有「{token}」——它不再读这个产物")
            elif site in writers_of.get(a, set()):
                if not (self_read and cat == CACHE):
                    problems.append(f"「{a}」的读者 {site} 同时是写入方——自读只许 CACHE 且标 SELF")
                elif not S.scope_has_read_call(facts, site):
                    problems.append(f"「{a}」标了 SELF，但 {site} 里没有任何读调用（open 读 / json.load / read_text …）")
    for a, (site, token) in witnesses.items():
        if artifacts.get(a, ("",))[0] != PENDING_DEAD:
            problems.append(f"见证 {a} 不是 PENDING_DEAD 产物")
        elif not S.scope_exists(facts, site) or not S.scope_reads_token(facts, site, token):
            problems.append(f"PENDING_DEAD「{a}」的见证没了（{site} 里不再有「{token}」）——清理完了？删条目并把 "
                            "CEILING_PENDING_DEAD 减一")
    return problems


def _entry_shape(label: str, entry, *, table: bool) -> list:
    if not (isinstance(entry, tuple) and len(entry) == 3 and entry[0] in CATEGORIES
            and isinstance(entry[1], list) and str(entry[2]).strip()):
        return [f"{label} 条目形状不对（应为 (类别, [读者…], 说明)）：{entry!r}"]
    cat, readers = entry[0], entry[1]
    if cat in NEEDS_READER and not readers:
        return [f"{label} 类别是「{cat}」却没登记读者"]
    if cat not in NEEDS_READER and readers:
        return [f"{label} 类别是「{cat}」，不该带读者（有读者就改成 READ / CACHE）"]
    if not table:
        for r in readers:
            if not (isinstance(r, tuple) and len(r) in (2, 3) and all(isinstance(x, str) and x for x in r)):
                return [f"{label} 读者形状不对（应为 (文件::限定名, 令牌[, SELF])）：{r!r}"]
    return []


def _scope(facts: dict, site: str) -> dict:
    rel, qual = site.split("::", 1)
    return facts[rel].scopes[qual]


def pending_dead_count(tables: dict, artifacts: dict) -> int:
    return (sum(1 for e in tables.values() if e[0] == PENDING_DEAD)
            + sum(1 for e in artifacts.values() if e[0] == PENDING_DEAD))


# ═════════════════════════════════════════════════════════════════════ 测试

class TestEveryPersistentWriteHasAReader:

    def test_tables(self):
        problems = table_problems(S.repo_facts(), TABLES, GENERIC_TABLE_WRITES)
        assert not problems, "\n".join(problems)

    def test_files(self):
        problems = file_problems(S.repo_facts(), SITE_ARTIFACTS, ARTIFACTS, PENDING_WITNESS)
        assert not problems, "\n".join(problems)

    def test_pending_dead_only_shrinks(self):
        n = pending_dead_count(TABLES, ARTIFACTS)
        assert n == CEILING_PENDING_DEAD, (
            f"已知死链 {n} 条，上限 {CEILING_PENDING_DEAD}。清理掉了 ⇒ 把上限减到 {n}；"
            "多出来了 ⇒ 别登记成债务：删掉写入方，或接上读者")

    def test_every_pending_dead_file_has_a_witness(self):
        missing = sorted(a for a, e in ARTIFACTS.items() if e[0] == PENDING_DEAD and a not in PENDING_WITNESS)
        assert not missing, f"这些 PENDING_DEAD 产物没有见证，清理之后不会有人提醒删条目：{missing}"


# ═════════════════════════════════════════════════════════════════════ 有牙

def _facts(**sources) -> dict:
    """合成仓库：{"文件名": 源码}（文件名里的 "__" 换成 "/"，"_py" 结尾换成 ".py"）。"""
    out = {}
    for name, src in sources.items():
        rel = name.replace("__", "/").removesuffix("_py") + ".py"
        out[rel] = S.scan_source(src, rel)
    return out


class TestScannerHasTeeth:
    """每个检测器的正例与反例。上面的对账全绿不能是因为扫描器是瞎的。"""

    def _sql(self, src, qual="f"):
        s = S.scan_source(src).scopes[qual]
        return s["tables_written"], s["tables_read"]

    def test_sql_writes_in_every_spelling(self):
        assert self._sql("def f(c):\n    c.execute('INSERT INTO t1 (a) VALUES (?)', (1,))\n")[0] == {"t1"}
        assert self._sql("def f(c):\n    c.execute('INSERT OR REPLACE INTO t2 VALUES (1)')\n")[0] == {"t2"}
        assert self._sql("def f(c):\n    c.execute('REPLACE INTO t3 VALUES (1)')\n")[0] == {"t3"}
        assert self._sql("def f(c):\n    c.execute('UPDATE t4 SET a = 1')\n")[0] == {"t4"}
        assert self._sql("def f(c):\n    c.execute('''\n        CREATE TABLE IF NOT EXISTS t5 (a INT)''')\n")[0] == {"t5"}

    def test_placeholders_resolve_from_the_same_file(self):
        src = ("class Store:\n    TABLE = 'preds'\n    def f(self, c):\n"
               "        c.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE} (a)')\n"
               "        c.execute(f'SELECT a FROM {self.TABLE}')\n")
        assert self._sql(src, "Store.f") == ({"preds"}, {"preds"})
        src = "TABLE = 'x'\ndef f(c, p):\n    c.execute('UPDATE {t} SET a_{p} = 1'.format(t=TABLE, p=p))\n"
        assert self._sql(src)[0] == {"x"}
        src = "def f(c, name):\n    c.execute(f'INSERT INTO {name} VALUES (1)')\n"
        assert self._sql(src)[0] == {"{name}"}, "解析不了的占位必须原样留着，让登记表对不上而红"

    def test_not_sql_writes(self):
        src = '"""文档里写着 CREATE TABLE IF NOT EXISTS t (a)。"""\ndef f(c):\n    """INSERT INTO doc VALUES 1"""\n    log("please update the cache")\n'
        assert self._sql(src)[0] == set()
        assert S.scan_source(src).scopes["<module>"]["tables_written"] == set()

    def test_sql_reads(self):
        assert self._sql("def f(c):\n    c.execute('SELECT a FROM t1 JOIN t2 ON 1')\n")[1] == {"t1", "t2"}
        assert self._sql("def f(c):\n    c.execute('DELETE FROM t3 WHERE a < 1')\n")[1] == set()
        assert self._sql("def f(c):\n    log('copied FROM somewhere')\n")[1] == set()

    def test_module_level_sql_constant_is_attributed_to_the_function_using_it(self):
        src = "_Q = 'SELECT a FROM t9'\ndef g(c):\n    return c.execute(_Q)\ndef h():\n    return 1\n"
        ff = S.scan_source(src)
        assert ff.scopes["g"]["tables_read"] == {"t9"} and ff.scopes["h"]["tables_read"] == set()

    def test_file_write_kinds(self):
        cases = {
            "def f(p):\n    open(p, 'w').write('x')\n": "open_write",
            "def f(p):\n    p.write_text('x')\n": "write_text",
            "import pickle\ndef f(o, fh):\n    pickle.dump(o, fh)\n": "binary_dump",
            "def f(df, p):\n    df.to_csv(p)\n": "frame_write",
            "import ledger_io\ndef f(p, d):\n    ledger_io.write_json(p, d)\n": "ledger_io",
            "from hive_logger import atomic_json_write\ndef f(p, d):\n    atomic_json_write(p, d)\n": "atomic_json_write",
            "import shutil\ndef f(a, b):\n    shutil.copy2(a, b)\n": "shutil",
        }
        for src, kind in cases.items():
            assert kind in S.scan_source(src).scopes["f"]["write_kinds"], (kind, src)

    def test_reads_are_not_writes(self):
        src = "import json\ndef f(p):\n    with open(p) as fh:\n        return json.load(fh)\n"
        assert S.scan_source(src).scopes["f"]["write_kinds"] == set()

    def test_reader_token_is_member_granular(self):
        src = ("from hive_logger import PATHS\n"
               "def r1(home):\n    return home / 'market.json'\n"
               "def r2():\n    return PATHS.ml_model_cache\n"
               "def r3(d, t):\n    return d / f'{t}_raw.json'\n"
               "def outer():\n    def inner():\n        return 'nested.json'\n    return inner\n")
        facts = {"x.py": S.scan_source(src, "x.py")}
        assert S.scope_reads_token(facts, "x.py::r1", "market.json")
        assert S.scope_reads_token(facts, "x.py::r2", "PATHS.ml_model_cache")
        assert not S.scope_reads_token(facts, "x.py::r2", "PATHS.ml_model")
        assert S.scope_reads_token(facts, "x.py::r3", "_raw.json")
        assert not S.scope_reads_token(facts, "x.py::outer", "nested.json"), "嵌套函数里的字符串不算外层的"
        assert S.scope_reads_token(facts, "x.py::outer.inner", "nested.json")
        assert not S.scope_exists(facts, "x.py::gone")


_W = "def save(p):\n    open(p / 'out.json', 'w').write('x')\n"
_R = "def load(p):\n    return open(p / 'out.json').read()\n"
_T = "def put(c):\n    c.execute('INSERT INTO t VALUES (1)')\ndef get(c):\n    return c.execute('SELECT * FROM t').fetchall()\n"


class TestRegistryComparisonsGoRed:
    """每种对账都给一个「该红」的登记表变体，并给一个「该绿」的对照（不然红可能只是因为什么都红）。"""

    def test_tables_green_control_and_every_red(self):
        facts = _facts(w_py=_T)
        good = {"t": (READ, ["w.py::get"], "x")}
        assert table_problems(facts, good, {}) == []
        assert any("未登记" in p for p in table_problems(facts, {}, {}))
        assert any("没有写入了" in p for p in table_problems(facts, {**good, "gone": (HUMAN, [], "x")}, {}))
        assert any("不存在了" in p for p in table_problems(facts, {"t": (READ, ["w.py::nope"], "x")}, {}))
        assert any("不再读" in p for p in table_problems(facts, {"t": (READ, ["w.py::put"], "x")}, {}))
        assert any("没登记读者" in p for p in table_problems(facts, {"t": (READ, [], "x")}, {}))
        assert any("不该带读者" in p for p in table_problems(facts, {"t": (HUMAN, ["w.py::get"], "x")}, {}))
        facts_t = {**facts, "tests/t.py": S.scan_source(_T, "tests/t.py")}
        assert any("tests/" in p for p in table_problems(facts_t, {"t": (READ, ["tests/t.py::get"], "x")}, {}))

    def test_files_green_control_and_every_red(self):
        facts = _facts(w_py=_W, r_py=_R)
        sites = {"w.py::save": ("out",)}
        good = {"out": (READ, [("r.py::load", "out.json")], "x")}
        assert file_problems(facts, sites, good, {}) == []
        assert any("未登记的落盘写入" in p for p in file_problems(facts, {}, good, {}))
        assert any("已经不写文件了" in p for p in file_problems(facts, {**sites, "w.py::ghost": ("out",)}, good, {}))
        assert any("没在 ARTIFACTS" in p for p in file_problems(facts, {"w.py::save": ("other",)}, good, {}))
        assert any("没有任何登记的写入落点" in p for p in file_problems(facts, sites, {**good, "orphan": (HUMAN, [], "x")}, {}))
        assert any("不存在了" in p for p in file_problems(facts, sites, {"out": (READ, [("r.py::nope", "out.json")], "x")}, {}))
        assert any("已经没有「other.json」" in p
                   for p in file_problems(facts, sites, {"out": (READ, [("r.py::load", "other.json")], "x")}, {}))
        assert any("同时是写入方" in p
                   for p in file_problems(facts, sites, {"out": (READ, [("w.py::save", "out.json")], "x")}, {}))
        assert any("没有任何读调用" in p
                   for p in file_problems(facts, sites, {"out": (CACHE, [("w.py::save", "out.json", SELF)], "x")}, {}))
        assert any("没登记读者" in p for p in file_problems(facts, sites, {"out": (CACHE, [], "x")}, {}))
        assert any("读者文件" in p and "不存在" in p
                   for p in file_problems(facts, sites, {"out": (READ, [("scripts/nope.sh", "x")], "x")}, {}))
        assert any("读者文件" in p and "已经没有" in p
                   for p in file_problems(facts, sites,
                                          {"out": (READ, [("scripts/alpha-hive-orchestrator.sh", "zz_never_zz")], "x")}, {}))

    def test_self_read_cache_needs_a_real_read_call(self):
        src = "import json\ndef get(p):\n    if p.exists():\n        return json.load(open(p / 'c.json'))\n    open(p / 'c.json', 'w').write('{}')\n"
        facts = _facts(c_py=src)
        ok = {"c": (CACHE, [("c.py::get", "c.json", SELF)], "x")}
        assert file_problems(facts, {"c.py::get": ("c",)}, ok, {}) == []
        helper = "def get(p):\n    hit = read_json_cache(p / 'c.json')\n    open(p / 'c.json', 'w').write('{}')\n"
        assert file_problems(_facts(c_py=helper), {"c.py::get": ("c",)}, ok, {}) == [], "经读助手读回也算"
        no_read = "def get(p):\n    d = _cache_dir()\n    open(d / 'c.json', 'w').write('{}')\n"
        assert any("没有任何读调用" in p for p in file_problems(_facts(c_py=no_read), {"c.py::get": ("c",)}, ok, {}))

    def test_pending_dead_witness_and_ceiling(self):
        facts = _facts(w_py=_W)
        sites = {"w.py::save": ("out",)}
        dead = {"out": (PENDING_DEAD, [], "x")}
        assert file_problems(facts, sites, dead, {"out": ("w.py::save", "out.json")}) == []
        assert any("见证没了" in p for p in file_problems(facts, sites, dead, {"out": ("w.py::save", "gone.json")}))
        assert any("见证没了" in p for p in file_problems(facts, sites, dead, {"out": ("w.py::deleted", "out.json")}))
        assert any("不是 PENDING_DEAD" in p
                   for p in file_problems(facts, sites, {"out": (HUMAN, [], "x")}, {"out": ("w.py::save", "out.json")}))
        assert pending_dead_count({"a": (PENDING_DEAD, [], "x"), "b": (READ, ["r"], "x")}, dead) == 2


class TestRealRepoPositiveControls:
    """扫描器在真仓库上认得出已知的写入与读者（不然对账全绿可能只是因为什么都没扫到）。"""

    def test_known_table_writers_and_readers(self):
        f = S.repo_facts()
        w, r = S.table_writers(f), S.table_readers(f)
        assert "signal_archive.py::archive" in w["signal_archive"]
        assert "backtester.py::PredictionStore.save_prediction" in w["predictions"]
        assert "signal_archive.py::load_panel" in r["signal_archive"]
        assert not any("{" in t for t in w if t not in GENERIC_TABLE_WRITES), \
            f"有表名占位解析不了：{[t for t in w if '{' in t]}"

    def test_known_file_writes_and_readers(self):
        f = S.repo_facts()
        sites = S.file_write_sites(f)
        assert "data_fetcher.py::<module>" in sites and "paper_portfolio.py::_save_meta" in sites
        assert S.scope_reads_token(f, "cloud_snapshot_loader.py::load_market", "market.json")
        assert len(sites) > 100, f"只扫到 {len(sites)} 处写入——扫描器坏了？"
