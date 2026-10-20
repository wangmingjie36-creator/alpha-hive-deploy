#!/usr/bin/env python3
"""
🔔 Alpha Hive 智能告警系统
实时异常检测 + 多渠道通知 + 智能优先级排序
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from enum import Enum

from hive_logger import PATHS, get_logger
from production_sync import OK_OUTCOMES

#: 扫描前同步非 OK 结局 → 这一轮实际跑的是什么（v0.45.223）
_SYNC_MEANING = {
    "ff_refused": "快进被拒，本轮跑的是 origin/main 之前的旧代码",
    "local_ahead": "生产在跑 main 上没有的提交（本地未推送），不是旧代码",
    "diverged": "既缺 main 的新提交、又含 main 没有的提交",
    "not_on_main": "生产 checkout 不在 main 分支上，跑的是那个分支的代码",
}

_log = get_logger("alerts")

#: v0.45.444：新闻通道「没走配置的主源」的标的占比，超过即 P1（v0.45.445 起主源 Massive）。
#: AV 时代常态是 0.20–0.27（30>25 的结构性本地配额 + 开跑第一秒的每分钟限速），所以 444 定的是 0.40；
#: Massive 无日上限、5 次/分钟远高于扫描节奏（≈1.2 只/分钟）⇒ 常态应接近 0，降到 0.20（30 只里 ≥7 只降级才报）。
#: 降级去的 Yahoo 是另一套分类器，同票系统性低约 27 分（2026-10-08 校准）——比例一高，横截面就是两种量纲混算。
NEWS_NON_PRIMARY_P1_SHARE = 0.20
#: 已知来源的标的少于这个数就不判（Buzz 大面积报错时比例没有意义），改报 P2「无法判定」。
NEWS_MIN_KNOWN = 10


def _ran_from_production_clone(scan_timing: Dict) -> bool:
    """本轮扫描的代码目录（扫描启动时 `code_version.repo_dir`）是不是生产独立克隆（v0.45.432）。

    取不到 / 解析不了 ⇒ False（不据此报警：判不了不等于该报）。比较走 `os.path.realpath`，
    不认符号链接 / `..` 的写法差异。
    """
    repo_dir = (scan_timing.get("code_version") or {}).get("repo_dir")
    if not repo_dir:
        return False
    try:
        import production_clone
        return os.path.realpath(repo_dir) == os.path.realpath(production_clone.default_dest())
    except Exception:  # noqa: BLE001 —— 判不了就不报，见 docstring
        return False


class AlertLevel(Enum):
    """告警级别"""
    CRITICAL = "CRITICAL"  # P0: 系统完全失败
    HIGH = "HIGH"          # P1: 关键步骤失败
    MEDIUM = "MEDIUM"      # P2: 性能下降/低分报告
    INFO = "INFO"          # 信息提示


# 中文告警消息
ALERT_MESSAGES_CN = {
    'pipeline_failed': '完整流程失败',
    'step_failed': '步骤失败',
    'performance_degradation': '性能异常',
    'no_report': '未生成报告',
    'low_scores': '机会评分偏低',
    'very_low_top': '最高分过低',
    'deployment_failed': 'GitHub 部署失败'
}


class Alert:
    """告警对象"""

    def __init__(self, level: AlertLevel, message: str, details: Dict = None, tags: List[str] = None):
        self.level = level
        self.message = message
        self.details = details or {}
        self.tags = tags or []
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return {
            "level": self.level.value,
            "message": self.message,
            "details": self.details,
            "tags": self.tags,
            "timestamp": self.timestamp
        }

    def to_slack_format(self) -> Dict:
        """转换为 Slack 消息格式"""
        color_map = {
            AlertLevel.CRITICAL: "#FF0000",
            AlertLevel.HIGH: "#FF9900",
            AlertLevel.MEDIUM: "#FFCC00",
            AlertLevel.INFO: "#0099FF"
        }

        emoji_map = {
            AlertLevel.CRITICAL: "🚨",
            AlertLevel.HIGH: "⚠️ ",
            AlertLevel.MEDIUM: "⏱️ ",
            AlertLevel.INFO: "ℹ️ "
        }

        return {
            "color": color_map[self.level],
            "pretext": f"{emoji_map[self.level]} {self.level.value}",
            "title": self.message,
            "fields": [
                {"title": key, "value": str(value), "short": True}
                for key, value in self.details.items()
            ],
            "ts": int(datetime.fromisoformat(self.timestamp).timestamp())
        }


class AlertAnalyzer:
    """告警分析引擎"""

    def __init__(self, report_dir: Path = None, perf_baseline_seconds: float = 5.0):
        self.report_dir = report_dir or PATHS.home
        self.perf_baseline = perf_baseline_seconds
        self.alerts: List[Alert] = []
        # v0.45.47：记录**哪些检查没能执行**。
        # 「零告警」有两种完全不同的成因——「查过了，没问题」与「根本没查成」，
        # 而旧实现把两者都渲染成 "No alerts detected - system healthy"。
        # 告警系统自己静默失效，是最不该发生的一种静默失效。
        self.checks_skipped: List[str] = []

    def analyze(self, status_json_path: Path) -> List[Alert]:
        """分析执行结果并生成告警"""
        self.alerts = []
        self.checks_skipped = []

        try:
            with open(status_json_path, 'r', encoding='utf-8') as f:
                status = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # 无法读取 status.json
            _log.error("Failed to read status.json: %s", e, exc_info=True)
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "Cannot read status.json",
                {"error": str(e)},
                ["system", "file_io"]
            ))
            return self.alerts

        # 1. 检测 P0: 总体失败
        if status.get('status') == 'failed':
            self.alerts.append(Alert(
                AlertLevel.CRITICAL,
                "🚨 【P0 严重】完整流程失败",
                {
                    "系统状态": "失败",
                    "影响范围": "全部步骤",
                    "建议": "立即查看日志，排查根本原因"
                },
                ["critical", "pipeline"]
            ))
            return self.alerts  # P0 优先返回

        # 2. 检测 P1: 步骤失败
        # v0.45.47：`.get('steps_result', {})` 拿到空 dict 时循环直接不执行，
        # 于是「一个失败步骤都没有」与「编排器没写这个字段」产出完全相同的结果。
        steps_result = status.get('steps_result')
        if not isinstance(steps_result, dict) or not steps_result:
            self.checks_skipped.append("步骤失败检查（status.json 缺 steps_result）")
            _log.warning("status.json 无 steps_result —— **步骤失败检查未执行**，"
                         "本次「无告警」不等于「无失败」")
            steps_result = {}
        for step_name, step_result in steps_result.items():
            if not isinstance(step_result, dict):
                # v0.45.386：此前 .get 直接 AttributeError、整个 Step 6 崩；编排器 B 的兜底恒写对象，
                # 这里只是兜底并留痕（「没查成」不渲染成「查过了没事」）
                self.checks_skipped.append(f"步骤失败检查：{step_name} 不是对象（{type(step_result).__name__}）")
                _log.warning("steps_result.%s 不是对象（%s）—— 该步的失败检查未执行",
                             step_name, type(step_result).__name__)
                continue
            if step_result.get('status') == 'failed':
                self.alerts.append(Alert(
                    AlertLevel.HIGH,
                    f"⚠️ 【P1 高】步骤失败：{step_name}",
                    {
                        "步骤": step_name,
                        "耗时": f"{step_result.get('duration_seconds', 'N/A')}秒",
                        "状态": "失败"
                    },
                    ["step_failure", step_name]
                ))
            elif step_name == 'step2_hive_analysis' and step_result.get('status') == 'success_with_warning':
                # v0.45.386（编排器 B2）：rc=1 但主流程确实跑完 ⇒ 解释器记 success_with_warning（此前是 failed，
                # 且 step4_dashboard 也记 failed，共两条 P1）。这是 rc=1 跑完的日子（09-24 / 09-25；09-25 是 ML 近常数，
                # 09-24 的 ML 守卫判 ok、退出码 1 的原因待验证）唯一的告警来源，
                # 不许因为标签更准了就消失；warning 取值 ml_model_constant / rc1_after_completion_unexplained 都报。
                self.alerts.append(Alert(
                    AlertLevel.HIGH,
                    f"⚠️ 【P1 高】扫描已跑完但退出码 1：{step_name}（{step_result.get('warning', '原因未记')}）",
                    {
                        "步骤": step_name,
                        "耗时": f"{step_result.get('duration_seconds', 'N/A')}秒",
                        "状态": "已跑完但退出码 1",
                        "原因": step_result.get('warning', '原因未记'),
                        "建议": "ml_model_constant ⇒ /usr/local/bin/python3 ml_model_guard.py --date <当日>；"
                                "其余 ⇒ 查编排器日志 Step 2 行",
                    },
                    ["step_warning", step_name]
                ))
            if step_result.get('interp_fallback'):
                # v0.45.386（编排器 B）：步骤解释器不可用、按退出码兜底记——摘要缺失，本身就是要人看的事（谁会红：这条）。
                # ⚠️ 生产上只有 Step 2/4 走得到这里：Step 6（本分析）跑在 Step 10–15 之前，读的 status.json 里还没有它们的片段；
                # 10–15 的兜底只留编排器日志 ERROR + 最终 status.json 的 interp_fallback，没有告警。
                self.alerts.append(Alert(
                    AlertLevel.MEDIUM,
                    f"【P2】步骤解释器不可用，{step_name} 按退出码兜底记",
                    {
                        "步骤": step_name,
                        "状态": step_result.get('status'),
                        "原因": str(step_result.get('interp_fallback'))[:300],
                    },
                    ["step_interp_fallback", step_name]
                ))

        # 3. 检测 P1: 性能异常 (>150% baseline)
        total_duration = status.get('total_duration_seconds', 0)
        if total_duration > self.perf_baseline * 1.5:
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】性能异常",
                {
                    "实际耗时": f"{total_duration}秒",
                    "基线耗时": f"{self.perf_baseline}秒",
                    "性能下降": f"{(total_duration / self.perf_baseline - 1) * 100:.1f}%",
                    "建议": "检查系统负载，优化缓慢的步骤"
                },
                ["performance"]
            ))

        # 4. 检测 P1: 数据异常 (无报告生成)
        report_file = self.report_dir / f"alpha-hive-daily-{datetime.now().strftime('%Y-%m-%d')}.md"
        if not report_file.exists():
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】未生成日报",
                {
                    "预期文件": report_file.name,
                    "当前状态": "文件不存在"
                },
                ["data_quality"]
            ))

        # 5. 检测 P2: 低分报告
        try:
            json_report_file = self.report_dir / f"alpha-hive-daily-{datetime.now().strftime('%Y-%m-%d')}.json"
            if json_report_file.exists():
                with open(json_report_file, 'r', encoding='utf-8') as f:
                    report = json.load(f)

                opportunities = report.get('opportunities', [])
                if opportunities:
                    top_score = opportunities[0].get('opp_score', 0)
                    avg_score = sum(o.get('opp_score', 0) for o in opportunities) / len(opportunities)

                    if avg_score < 6.0:
                        self.alerts.append(Alert(
                            AlertLevel.MEDIUM,
                            "📊 【P2 中】机会评分偏低",
                            {
                                "最高分": f"{top_score:.1f}/10",
                                "平均分": f"{avg_score:.1f}/10",
                                "解释": "当前市场交易机会有限"
                            },
                            ["data_quality", "market"]
                        ))

                    if top_score < 5.0:
                        self.alerts.append(Alert(
                            AlertLevel.MEDIUM,
                            "📉 【P2 中】最高分过低",
                            {
                                "top_score": f"{top_score:.1f}/10",
                                "recommendation": "Consider expanding analysis scope or monitoring period"
                            },
                            ["data_quality"]
                        ))
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError) as e:
            # v0.45.47：debug → warning，并记入 checks_skipped。
            # 这个 except 覆盖了 KeyError/ValueError/TypeError —— 日报 JSON 结构
            # 稍有变化（opportunities 从 list 变 dict 等）就会整块跳过 P2 低分检查，
            # 而调用方只看到「零告警」。
            self.checks_skipped.append(f"低分/数据质量检查（报告解析失败：{type(e).__name__}）")
            _log.warning("日报 JSON 解析失败，**低分与数据质量检查未执行**：%s: %s",
                         type(e).__name__, e)

        # 6. 检测 P1: gh-pages 部署失败 / 生产代码没同步到 origin/main（v0.45.214；v0.45.402 起不再含 main 推送）
        self._check_deploy_and_code_sync(status)

        # 7. 检测 P2: 组合 Greeks 用到了不属于当天那一场的标的价 / 因此拒绝成交（v0.45.423）
        self._check_portfolio_greeks_prices(status)
        self._check_paper_portfolio_run(status)
        self._check_ledger_io_failures(status)
        self._check_history_fill(status)

        # 8. 检测 P1: 新闻通道降级（v0.45.444）
        self._check_news_sources(status)

        # 9. 检测 P2: 云端快照没刷新上 / 落后（v0.45.466）
        self._check_cloud_snapshots(status)

        # 10. 检测 P2: T+N 回填队列没在排空（v0.45.493）
        self._check_outcomes_backlog(status)

        # 11. 检测 P2: 本轮有票判不了财报日 / 观测字段接线断了（v0.45.500）
        self._check_earnings_dates(status)

        return self.alerts

    def _check_outcomes_backlog(self, status: Dict) -> None:
        """T+N 实际价格回填队列（v0.45.493，`scan_timing.extra.outcomes_backlog`，写入者 `alpha_hive_daily_report.main`）。

        0.45.493 之前回填每轮按文件名只取 50 份，字母序靠后的 7 只标的 09 月起一份都没回填过，日志每轮却是
        `updated=50 errors=0`——「每轮都成功」不等于「队列在排空」。判据是最老一格的逾期天数：排空时停在 0~2，
        饿死 / 出网长期失败时只增不减。超过 `max_overdue_days` ⇒ P2（不影响当天评分；影响 self_analyst 月度自诊断、
        risk_engine 历史 VaR 等读 actual_prices 的诊断）。
        """
        if not self._swarm_scan_actually_ran(status):
            return
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        ob = (st.get("extra") or {}).get("outcomes_backlog")
        if not isinstance(ob, dict) or not ob.get("available"):
            self.checks_skipped.append("回填队列检查（scan_timing 无可用 outcomes_backlog）")
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                "📊 【P2 中】T+N 回填队列未观测——实际价格有没有在补上未知",
                {"原因": (ob or {}).get("reason") if isinstance(ob, dict) else "scan_timing.extra 无 outcomes_backlog",
                 "影响": "不是「检查了没积压」，是「没检查」"},
                ["outcomes_backlog", "observability"]
            ))
            return
        late, limit = ob.get("oldest_overdue_days"), ob.get("max_overdue_days")
        if not isinstance(late, int) or not isinstance(limit, int):
            self.checks_skipped.append("回填队列逾期检查（outcomes_backlog 缺 oldest_overdue_days / max_overdue_days）")
            return
        if late > limit:
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】T+N 回填积压：最老一格已逾期 {late} 天（允许 {limit}）——队列没在排空",
                {"最老一格": ob.get("oldest_item"),
                 "待回填": f"{ob.get('pending_files')} 份 / {ob.get('pending_offsets')} 格",
                 "影响": "report_snapshots 的 actual_prices 缺口持续：self_analyst 月度自诊断（要求 t7 非空）、"
                         "risk_engine 历史 VaR 等会系统性漏掉这些样本",
                 "建议": "看 alpha_hive.log 里「OutcomesFetcher 完成」的 network / deferred / unresolved：deferred 一直有 ⇒ "
                         "出网配额不够或连续限流；unresolved 一直有 ⇒ 本地快照与 yfinance 都取不到价（标的停牌 / 代码写法）"},
                ["outcomes_backlog", "data_quality"]
            ))

    def _check_earnings_dates(self, status: Dict) -> None:
        """本轮财报日能不能判（v0.45.500，`scan_timing.extra.earnings_dates`，写入者 `alpha_hive_daily_report.main`，
        统计 `earnings_calendar.summarize`）。

        判不了的票：卖权行权价账本记 `unknown`（整单位排出检验）、跨式不出信号。此前唯一的痕迹是一条 warning，
        而那条 yfinance 兜底在需要它的日子（09-24 / 09-25 / 10-12）一次都没被调到——没有任何东西会红。
        `legacy_source` > 0 ⇒ ChronosBee 没写观测字段（接线断了），读者退回旧格式催化剂、当天财报又会被丢。
        回放 09-15 ~ 10-12：正常日 unresolved = 0，只在 09-24 / 09-25（30 只）与 09-30（MU 财报当天，旧格式）非零。
        """
        if not self._swarm_scan_actually_ran(status):
            return
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        ed = (st.get("extra") or {}).get("earnings_dates")
        if not isinstance(ed, dict) or not ed.get("available"):
            self.checks_skipped.append("财报日观测检查（scan_timing 无可用 earnings_dates）")
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                "📊 【P2 中】财报日观测未统计——本轮各票财报日能不能判未知",
                {"原因": (ed or {}).get("reason") if isinstance(ed, dict) else "scan_timing.extra 无 earnings_dates",
                 "影响": "不是「检查了都能判」，是「没检查」"},
                ["earnings_dates", "observability"]
            ))
            return
        n_bad, limit = ed.get("unresolved"), ed.get("max_unresolved")
        legacy = ed.get("legacy_source")
        if not isinstance(n_bad, int) or not isinstance(limit, int):
            self.checks_skipped.append("财报日观测检查（earnings_dates 缺 unresolved / max_unresolved）")
            return
        if n_bad > limit:
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】{n_bad}/{ed.get('n')} 只票本轮判不了财报日（允许 {limit}）",
                {"标的": ", ".join(ed.get("unresolved_tickers") or [])[:300],
                 "影响": "卖权行权价账本这些票记 unknown（整单位不进检验）；跨式纸面腿不出信号",
                 "建议": "看 alpha_hive.log 里 ChronosBeeHorizon 的 calendar unavailable：多为 yfinance 限流 / 断网，"
                         "全体判不了时期权取数通常也挂了；个别票则查它的 yfinance calendar 是否没有 Earnings Date"},
                ["earnings_dates", "data_quality"]
            ))
        if isinstance(legacy, int) and legacy > 0:
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】{legacy} 只票的财报日读的是旧格式催化剂——ChronosBee 没写 earnings_calendar 观测",
                {"影响": "旧格式按 datetime.now() 过滤过，财报当天会被丢成「判不了」",
                 "建议": "查 swarm_agents/chronos_bee.py 的 details[earnings_calendar] 是否还在写"},
                ["earnings_dates", "observability"]
            ))

    @staticmethod
    def _swarm_scan_actually_ran(status: Dict) -> bool:
        """区分「早退不部署，`scan_timing` 本来就不该有」与「扫描真跑完了，它却丢了」（v0.45.255）。

        只看 `step2_hive_analysis` 是否成功——它是 `scan_timing.write()` 所在的那条扫描路径
        唯一的先决条件：空扫描护栏 / `--no-swarm` 早退等路径到不了这一步，`scan_timing` 缺失
        对它们是设计内的正常状态，不该被下面新加的 P1 误伤。

        v0.45.386 起 `success_with_warning`（Step 2 退出 1 但主流程跑完）也算：该判定要求本轮
        `scan_timing.json` 带 gh_pages（`orchestrator_steps._step2_rc1_evidence`）——正是 write_status
        并入的那份，故放宽只会增加真阳性。
        """
        step2 = (status.get("steps_result") or {}).get("step2_hive_analysis") or {}
        if not isinstance(step2, dict):
            return False
        return step2.get("status") in ("success", "success_with_warning")

    def _check_deploy_and_code_sync(self, status: Dict) -> None:
        """读 `status.scan_timing` 里两个**真有写入者**的字段。

        v0.45.214 前这里读 `status['deploy_status']` —— 全仓零写入者，规则结构上不可能触发；
        2026-09-01~11 生产 `git push origin main` 六次 non-fast-forward 被拒，零告警。
          - `scan_timing.extra.gh_pages`（v0.45.351）：`alpha_hive_daily_report.main` 写，网站部署结局
            （v0.45.402 起日报不再提交 / 推送 main，原 `git_push` / `git_commit` 两个字段与对应告警已随链退役）
          - `scan_timing.production_sync`：编排器 Step 1 前跑 `production_sync.py` 写
        ⚠️ `scan_timing` 进 status.json 靠编排器 `write_status()` 的 jq 合并，而那一步 09-14~09-25
        每个扫描日都没生效（见 v0.45.351 CHANGELOG：疑为 TCC 拦 /usr/bin/jq 读 ~/Desktop，
        阶段 5 已把文件搬出 Desktop，待验证）。所以 gh-pages 另有一条不经 scan_timing 的路：
        编排器 Step 5 读部署日志（`report_deployer.gh_pages_step_status`）→ `steps_result`。

        v0.45.255：`scan_timing` 整段缺失以前**一律**当「早退未部署，本来就没写」处理——静默
        `checks_skipped` + 一行 WARNING。2026-09-14 14:48 实测反例：蜂群扫描真跑完了（`step2` 成功、
        日报已提交推送、耗时 2889s），`logs/scan_timing.json` 本身数据完整，唯独编排器 `write_status()`
        把它并进 `status.json` 那一步没生效——根因排查了 TCC 权限 / 脚本改动 / 写入时序 / 线程卡死强退
        四个假设，**逐一用 09-10/09-11 两天的日志证伪**（同样的条件那两天都有，合并却都成功），
        真正触发条件仍不明。**不追那个可能永远抓不住的瞬时原因，把这一类失败本身变成可观测的**：
        真扫描跑完了、`scan_timing` 却整段没进 `status.json`，本身就是「不知道自己不知道」——
        比任何一条已知的推送/提交失败都更危险，因为它连「有没有出事」都看不出来。
        判别用 `steps_result.step2_hive_analysis.status` ∈ {"success", "success_with_warning"}（后者 v0.45.386 起，
        见 `_swarm_scan_actually_ran`）——它是 `scan_timing.write()`
        所在的那条扫描路径唯一的先决条件，早退路径（空扫描护栏等）到不了这一步，不会被误伤。
        """
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            self.checks_skipped.append("gh-pages 部署 / 生产代码同步检查（status.json 缺 scan_timing）")
            _log.warning("status.json 无 scan_timing —— **推送与代码同步检查未执行**")
            if self._swarm_scan_actually_ran(status):
                self.alerts.append(Alert(
                    AlertLevel.HIGH,
                    "⚠️ 【P1 高】扫描已完成，但 status.json 缺整段 scan_timing——网站部署 / 生产同步全部失去可观测性",
                    {
                        "现象": "本轮 step2 蜂群分析已成功，scan_timing 却整段没进 status.json",
                        "影响": "本轮 production_sync / gh_pages 部署是否成功完全未知，"
                                "不是「检查了没问题」，是「没检查」",
                        "建议": "查 logs/scan_timing.json 是否存在且日期匹配当天；"
                                "核对编排器日志 write_status() 附近有无异常（2026-09-14 一例未查明根因）",
                    },
                    ["deployment", "observability"]
                ))
            return

        # v0.45.351：gh-pages（= 网站）部署结局。此前 status.json 里根本没有这一项——
        # 2026-09-25 推送失败被重试捷径判成「成功」，网站停在 09-24 两天，零告警。
        ghp = (st.get("extra") or {}).get("gh_pages")
        if ghp is None:
            self.checks_skipped.append("gh-pages 部署检查（scan_timing 无 gh_pages 记录）")
        elif ghp.get("success") is not True:
            probe = ghp.get("transport_probe") or {}
            details = {
                "原因": ghp.get("error") or ghp.get("reason") or ghp.get("skipped") or "（无输出）",
                "尝试次数": ghp.get("attempts"),
                "父提交已校验": ghp.get("parent_verified"),
                "建议": "网络恢复后下一轮扫描会重新 fetch 远端头再提交；急需时人工补推。"
                        "判网站是否最新只认远端（线上 dashboard-data.json 的 _date），不认部署日志",
            }
            if probe:
                details["传输探测"] = f"{probe.get('verdict')}：{probe.get('meaning')}"
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】gh-pages 部署失败（网站停在上一次成功部署）",
                details,
                ["deployment", "gh_pages"]
            ))

        sync = st.get("production_sync")
        if sync is None:
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                "📊 【P2 中】扫描前生产代码同步未执行",
                {
                    "含义": "不知道本轮跑的代码是不是 origin/main（编排器没调 production_sync.py，"
                            "或结果日期对不上）",
                    "代码版本": (st.get("code_version") or {}).get("sha"),
                },
                ["code_sync"]
            ))
        elif sync.get("outcome") not in OK_OUTCOMES:
            outcome = sync.get("outcome")
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                f"⚠️ 【P1 高】生产代码 ≠ origin/main（{outcome}）",
                {
                    # v0.45.223：按结局说清「跑的是什么」——local_ahead 不是「旧代码」，是 main 上没有的提交
                    "含义": _SYNC_MEANING.get(outcome, "本轮沿用同步前的代码（可能是旧代码）"),
                    "详情": sync.get("detail"),
                    "落后/领先": f"{sync.get('behind')} / {sync.get('ahead')}",
                    "影响": "世代边界按日期划分，默认代码落地当天就在跑；本轮样本可能被记进错误世代",
                },
                ["code_sync"]
            ))

        # 阶段 8（v0.45.431）：编排器声明本轮跑在生产独立克隆里（ALPHA_HIVE_PRODUCTION_CLONE=1）时，
        # production_sync 写 `clone_guard`。没有这个键 = 编排器没声明（切换前 / 回退后），不是「检查过没问题」，
        # 但也不是故障，不报。有键而 ok 不是 True ⇒ 守卫缺 / 被改道 / 克隆里有人改了代码 / origin 不对。
        guard = (sync or {}).get("clone_guard")
        # v0.45.432：「没声明」与「声明了但没核」要分开。本轮代码就是从生产克隆跑的（code_version.repo_dir），
        # 却没有 clone_guard ⇒ 编排器丢了声明，或克隆里的 production_sync 太旧不认它——守卫从未被核对。
        # sync 整个缺失时上面已报 P2，这里不重复。
        if sync is not None and guard is None and _ran_from_production_clone(st):
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】扫描从生产克隆跑，但本轮没有核对克隆守卫（clone_guard 缺失）",
                {
                    "代码目录": (st.get("code_version") or {}).get("repo_dir"),
                    "含义": "编排器没给 production_sync 设 ALPHA_HIVE_PRODUCTION_CLONE=1，或克隆里的 production_sync "
                            "早于 v0.45.431、不认这个声明——钩子没有被补齐，克隆是否被改过本轮没人看",
                    "建议": "核对部署副本里 production_sync 那一行；/usr/local/bin/python3 production_clone.py check",
                },
                ["code_sync"]
            ))
        if guard is not None and guard.get("ok") is not True:
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                "⚠️ 【P1 高】生产克隆不合格（守卫缺失 / 被绕过，或克隆里有未提交改动）",
                {
                    "问题": "；".join(guard.get("problems") or []) or "（无问题清单——核对本身失败）",
                    "钩子": guard.get("hooks"),
                    "本轮补装": guard.get("actions"),
                    "影响": "生产代码可能被直接提交 / 手改而不经 origin/main（本轮扫描照常跑）",
                    "建议": "/usr/local/bin/python3 production_clone.py check --repo <生产克隆> 看详情；"
                            "工作区改动先搬进开发 worktree 再在克隆里 git restore；"
                            "foreign 钩子确认来源后删掉，下一轮自动补装",
                },
                ["code_sync"]
            ))


    def _check_portfolio_greeks_prices(self, status: Dict) -> None:
        """组合 Greeks 的标的价场次核对（v0.45.423；`portfolio_greeks.price_check_stats` 经 scan_timing 进来）。

        v0.45.103~422 同日跑恒拿前一交易日的收盘算 $Delta、SPY 按昨收「收盘成交」，零告警——那个失败被
        「5 个日历日内最后一根」的容差改写成了正常值。现在陈旧价不进计算（当日不对冲），这里让它有人看见：
        有行判陈旧 / SPY 价不是当天那一场 / 有合约报价属于别的场次 / 因 SPY 不是官方收盘而拒绝成交 ⇒ P2。
        v0.45.435：都不是、但对冲决定做不出来（`hedge_undecided`：两源取不到价 / 缺报价 / 缺 β / 缺 NAV）⇒ 另一条 P2
        （「数据不全」）；陈旧那条也把这些缺口挂进「另缺」。旧版扫描的计数没有这个键 ⇒ 不报（不猜）。
        `scan_timing` 整段缺失已由 `_check_deploy_and_code_sync` 报过，这里不重复；
        `counters.portfolio_greeks` 不是对象 ＝ 本轮没跑到组合 Greeks（钩子异常 / 早退 / 旧版扫描）⇒ checks_skipped。
        """
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        pg = (st.get("counters") or {}).get("portfolio_greeks")
        if not isinstance(pg, dict):
            self.checks_skipped.append("组合 Greeks 标的价场次检查（scan_timing 无 portfolio_greeks 计数）")
            return
        if pg.get("error"):
            # v0.45.442：`run_for_date` 抛了（日报当非致命吞掉）——此前这里是 None ⇒ 只进 checks_skipped，覆盖层整轮没跑不红
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】组合 Greeks：{pg.get('as_of')} 这一轮异常中断，对冲决定 / 成交 / 净值快照可能都没落盘",
                {
                    "异常": str(pg["error"]),
                    "影响": "覆盖层这一天没有（完整的）决定；连续几天都这样 = 对冲整段停摆",
                    "建议": "看 alpha_hive.log 里「组合 Greeks 更新失败」那一行与 hedge_state/ 当日文件是否齐全",
                },
                ["portfolio_greeks", "data_quality"]
            ))
            return
        dhc = pg.get("delta_hedge")
        if isinstance(dhc, dict) and dhc.get("mode") == "paper" and (dhc.get("n_undecided") or dhc.get("n_blocked")):
            # v0.45.526：逐票 Delta 对冲（纸面成交）：只影响缺数据的那几只，别的照常对冲——但停摆不能无人知道。
            # shadow 下不报（只算不成交，未决只进日志与审计）。独立于下面 SPY 带的告警，两边互不吞。
            und = "；".join(f"{u.get('ticker')}（{u.get('reason')}）" for u in dhc.get("undecided") or []) or "—"
            blk = "；".join(str(v) for v in (dhc.get("blocked") or {}).values()) or "—"
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】逐票 Delta 对冲：{dhc.get('n_undecided', 0)} 只决定做不出来、"
                f"{dhc.get('n_blocked', 0)} 只拒绝成交，这几只今天不对冲",
                {
                    "未决": und,
                    "拒绝成交": blk,
                    "影响": "只影响这几只标的的跨式腿 Delta 没被对冲；别的标的照常。连续几天都这样 = 这几只的 Delta 一直裸着",
                    "建议": "看 hedge_state/greeks_<日期>.json 的 delta_hedge 一节（reason 码：nav_unavailable / "
                            "option_delta_unknown / price_missing / price_stale / held_shares_unknown）",
                },
                ["portfolio_greeks", "data_quality"]
            ))
        spy = pg.get("spy") or {}
        n_stale = int(pg.get("n_stale") or 0)
        n_quote_stale = int(pg.get("n_quote_stale") or 0)
        blocked = pg.get("execution_blocked")
        undecided = pg.get("hedge_undecided")         # v0.45.435：不是陈旧、是缺 ⇒ 对冲决定做不出来
        gaps = "；".join(str(g) for g in pg.get("gaps") or []) or "—"
        if not (n_stale or n_quote_stale or spy.get("stale") or blocked):
            if undecided:
                # 2026-09-15~25 连续 7 天 unknown（BRK-B β 取不到、SPY 价与期权报价取不到），只陈旧会红 ⇒ 零告警
                self.alerts.append(Alert(
                    AlertLevel.MEDIUM,
                    f"📊 【P2 中】组合 Greeks：数据不全，{pg.get('expected_session') or pg.get('as_of')} "
                    f"的对冲决定做不出来（{gaps}），当日不对冲",
                    {
                        "缺口": gaps,
                        "决定": undecided,
                        "SPY": f"{spy.get('price')} @ {spy.get('session')}（{spy.get('source')}）",
                        "影响": "覆盖层今天不动；连续几天都这样 = 对冲整段停摆",
                        "建议": "看 hedge_state/greeks_<日期>.json 的 price_check 与 rows；取不到价 = CBOE 与 "
                                "Twelve Data 都没给，缺 β = 日线不够 60 个共同收益（常见于代码映射 / 新票）",
                    },
                    ["portfolio_greeks", "data_quality"]
                ))
            return
        names = "，".join(dict.fromkeys(f"{x.get('ticker')} {x.get('seen_price')}@{x.get('session')}"
                                        for x in pg.get("stale") or []))
        spy_px = spy.get("seen_price") if spy.get("stale") else spy.get("price")
        self.alerts.append(Alert(
            AlertLevel.MEDIUM,
            f"📊 【P2 中】组合 Greeks：标的价不属于 {pg.get('expected_session') or pg.get('as_of')} 这一场"
            f"（陈旧 {n_stale} 行" + ("，SPY 也是" if spy.get("stale") else "")
            + ("，且拒绝成交" if blocked else "") + "），当日不对冲",
            {
                "陈旧行": names or "—",
                "SPY": f"{spy_px} @ {spy.get('session')}（{spy.get('source')}）",
                "报价错场": n_quote_stale,
                "拒绝成交": blocked or "—",
                "另缺": gaps,
                "影响": "这些行不进 $Delta / 净值 / 成交，β·Δ 判 unknown，当日不对冲",
                "建议": "看 hedge_state/greeks_<日期>.json 的 price_check；CBOE 盘中陈旧文件 / 整源停更 / "
                        "过了次日开盘才补跑时会出现",
            },
            ["portfolio_greeks", "data_quality"]
        ))

    def _check_news_sources(self, status: Dict) -> None:
        """新闻通道的实际来源（v0.45.444，`scan_timing.extra.news_sources`，写入者 `alpha_hive_daily_report.main`）。

        降级到 Yahoo 不报错、Buzz 的 data_quality.news 两种源都写 `keyword`，所以原来没人会红；
        但 Yahoo 关键词打标与主源（v0.45.445 起 Massive，此前 AV）的逐文章模型标签是两个分类器——2026-10-06 全 30 只
        降级到 Yahoo，同票 news_signal 比各自 AV 日均值平均低 21 分（21/25 更低），Buzz 偏空、Scout consensus_strength 28/30 为 0；
        10-08 同一时刻校准：Yahoo 比 Massive 同票低 28.8 分（25/28）。

        只在扫描真跑完时判（同 `_check_deploy_and_code_sync`）；`scan_timing` 整段缺失那条 P1 已在上一步报过。
        """
        if not self._swarm_scan_actually_ran(status):
            return
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        ns = (st.get("extra") or {}).get("news_sources")
        if not isinstance(ns, dict) or not ns.get("available"):
            reason = (ns or {}).get("reason") if isinstance(ns, dict) else None
            self.checks_skipped.append("新闻通道来源检查（scan_timing 无可用 news_sources）")
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                "📊 【P2 中】新闻通道来源统计缺失——本轮新闻是否降级到 Yahoo 未知",
                {
                    "原因": reason or ("scan_timing.extra 无 news_sources" if ns is None
                                     else "news_sources 无已知来源的标的"),
                    "影响": "不是「检查了没降级」，是「没检查」",
                },
                ["data_quality", "news", "observability"]
            ))
            return
        n_known = ns.get("n_known") or 0
        share = ns.get("non_primary_share")
        if share is None or n_known < NEWS_MIN_KNOWN:
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】新闻通道来源无法判定（已知来源仅 {n_known} 只）",
                {"来源分布": ns.get("by_source"), "未知": ns.get("n_unknown")},
                ["data_quality", "news"]
            ))
            return
        if share > NEWS_NON_PRIMARY_P1_SHARE:
            by_source = ns.get("by_source") or {}
            primary = ns.get("primary_source") or "?"
            n_non = n_known - by_source.get(primary, 0)
            msgs = ns.get("refusal_messages") or []
            self.alerts.append(Alert(
                AlertLevel.HIGH,
                f"⚠️ 【P1 高】新闻通道降级：{n_non}/{n_known} 只没走主源 {primary}（阈值 {NEWS_NON_PRIMARY_P1_SHARE:.0%}）",
                {
                    "来源分布": by_source,
                    "主源结局": ns.get("primary_status"),
                    "主源拒绝原文": [f"{m.get('count')}× {m.get('text')}" for m in msgs[:2]] or "（无）",
                    "影响": "降级的标的 news_signal 改用 Yahoo 关键词打标，同票系统性偏低约 20–29 分（10-06 / 10-08 实测）——"
                            "不是缺数据，是换了分类器；今天的 Buzz / 情绪类分数不要与主源日直接比较",
                    "建议": "看主源结局与拒绝原文：limiter_timeout ⇒ 本进程排队太久（同进程别的调用方在抢名额）；"
                            "server_refused ⇒ 看原文（Massive 免费档 5 次/分钟，AV 是每日 25 次 + 每分钟 5 次）；"
                            "network_error ⇒ 出站网络 / 代理",
                },
                ["data_quality", "news"]
            ))

    def _check_cloud_snapshots(self, status: Dict) -> None:
        """云端快照新鲜度（v0.45.466，`scan_timing.extra.cloud_snapshots`，写入者 `alpha_hive_daily_report.main`）。

        补跑过去某天时期权 / IV / 官方收盘取自 `origin/cloud-snapshots`。阶段 8 后生产克隆里没人刷新它，
        routine 自己也三次静默停过——两种情况此前都要等到补跑那天才显形，且那时补跑已降级成用当天数据。
        每天看一眼：刷新失败、或落后超过 `max_lag` 个交易日 ⇒ P2（不影响当天扫描，只影响补跑可信度）。
        """
        if not self._swarm_scan_actually_ran(status):
            return
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        cs = (st.get("extra") or {}).get("cloud_snapshots")
        if not isinstance(cs, dict) or not cs.get("available"):
            self.checks_skipped.append("云端快照新鲜度检查（scan_timing 无可用 cloud_snapshots）")
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                "📊 【P2 中】云端快照新鲜度未观测——补跑时能不能用上快照未知",
                {"原因": (cs or {}).get("reason") if isinstance(cs, dict) else "scan_timing.extra 无 cloud_snapshots",
                 "影响": "不是「检查了没落后」，是「没检查」"},
                ["cloud_snapshots", "observability"]
            ))
            return
        newest, lag, max_lag = cs.get("newest_date"), cs.get("lag"), cs.get("max_lag")
        if not cs.get("refresh_ok"):
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】cloud-snapshots 刷新失败——本地最新只到 {newest}",
                {"错误": cs.get("refresh_error"),
                 "影响": "此后补跑读到的是本地旧 ref：缺的日子会被当成「没快照」、期权 / IV 改用当天数据",
                 "建议": "在生产克隆里 git fetch origin cloud-snapshots 看报错（网络 / 凭据 / iCloud 重名 ref）"},
                ["cloud_snapshots", "data_quality"]
            ))
            return                          # 本地 ref 没刷新上，落后量量的是本地，不另报
        if not isinstance(lag, int) or not isinstance(max_lag, int):
            self.checks_skipped.append("云端快照落后量检查（cloud_snapshots 缺 lag / max_lag）")
            return
        if lag > max_lag:
            missing = cs.get("missing_sessions") or []
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】云端快照落后 {lag} 个交易日（最新 {newest}，允许 {max_lag}）——routine 可能停了",
                {"缺的交易日": ", ".join(missing[:10]) + (" …" if len(missing) > 10 else ""),
                 "影响": "这些日子若要补跑，期权 / IV / 官方收盘拿不到快照，只能用当天数据",
                 "建议": "看 claude.ai routine `alpha-hive-cloud-snapshot` 的最近运行与回复里的代码版本"},
                ["cloud_snapshots", "data_quality"]
            ))

    def _check_paper_portfolio_run(self, status: Dict) -> None:
        """纸面组合这一轮跑没跑完（v0.45.448；`paper_portfolio.run_stats` 经 scan_timing 进来）。

        日报把 `paper_portfolio.run_for_date` 的任何异常当「非致命」吞成一行 WARNING——账本读到坏行
        （`ledger_io.LedgerCorrupt`）、写锁超时、写入被拒（NaN / 非对象）都在这条路上，此前**整轮没跑也不红**。
        error ⇒ P2；计数缺失（钩子没跑到 / 旧版扫描）⇒ checks_skipped，不当「查过了没事」。"""
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        pp = (st.get("counters") or {}).get("paper_portfolio")
        if not isinstance(pp, dict):
            self.checks_skipped.append("纸面组合运行结局检查（scan_timing 无 paper_portfolio 计数）")
            return
        if pp.get("skipped") and not pp.get("error"):
            # v0.45.535：持仓日线全断 ⇒ 整天跳过（不开仓、不写净值）。设计内的跳过，但这一天组合没有记录——要看得见
            self.alerts.append(Alert(
                AlertLevel.MEDIUM,
                f"📊 【P2 中】纸面组合：{pp.get('as_of')} 持仓日线全部取不到，整天跳过（未开仓、未写净值）",
                {
                    "跳过原因": str(pp["skipped"]),
                    "取不到的持仓": "、".join(pp.get("ohlc_unavailable") or []) or "（未记）",
                    "影响": "这一天没有净值行与新仓；下次运行会从入场日起补查出场（出场日照实际触发日记）",
                    "建议": "看 alpha_hive.log 里「_fetch_ohlc … 失败」与代理 / yfinance 限流；连续多天跳过要查出站",
                },
                ["paper_portfolio", "data_quality"]
            ))
            return
        if not pp.get("error"):
            return
        self.alerts.append(Alert(
            AlertLevel.MEDIUM,
            f"📊 【P2 中】纸面组合：{pp.get('as_of')} 这一轮异常中断，开平仓 / 净值快照可能都没落盘",
            {
                "异常": str(pp["error"]),
                "影响": "纸面组合这一天没有（完整的）记录；账本读到坏行时不会再静默跳过，修好之前每轮都会停在这里",
                "建议": "LedgerCorrupt ⇒ 按报错的文件与行号核对，从每日异地备份恢复该文件；LedgerLockTimeout ⇒ 查是否有"
                        "别的写者卡住；其他 ⇒ 看 alpha_hive.log 里「纸面组合更新失败」那一行",
            },
            ["paper_portfolio", "data_quality"]
        ))

    def _check_ledger_io_failures(self, status: Dict) -> None:
        """账本 / 状态文件读写失败（v0.45.452；`ledger_io.failures()` 经 scan_timing 进来）。

        失败在 `ledger_io` **源头**登记：坏行 / 坏文档（LedgerCorrupt）、写入被拒（NaN / 非对象，LedgerWriteError）、
        写锁超时、磁盘错误。调用方大多把这些包在「非致命」的 except 里吞成一行 WARNING——所以不能指望每个调用方
        各自上报，这里一处兜住全部。n>0 ⇒ P2；计数缺失 ⇒ checks_skipped。"""
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        lf = (st.get("counters") or {}).get("ledger_io")
        if not isinstance(lf, dict):
            self.checks_skipped.append("账本读写失败检查（scan_timing 无 ledger_io 计数）")
            return
        if not lf.get("n"):
            return
        items = lf.get("items") or []
        self.alerts.append(Alert(
            AlertLevel.MEDIUM,
            f"📊 【P2 中】账本 / 状态文件读写失败 {lf['n']} 次：本轮有记录没有落盘，或读到了损坏的文件",
            {
                "明细": "；".join(f"{i.get('op')} {i.get('path')}：{i.get('kind')} {i.get('error')}" for i in items[:5])
                        + (f"（另 {lf['n'] - 5} 次略）" if lf["n"] > 5 else ""),
                "影响": "涉及的账本这一轮没更新（写入被拒时文件一个字节都没动）；读到坏文件的模块停在这里，修好之前每轮都会红",
                "建议": "LedgerCorrupt ⇒ 按报错的文件与行号核对，从每日异地备份恢复；LedgerWriteError ⇒ 找出算出 NaN / 非对象"
                        "的上游（不要在写入端把它改成 null 了事）；LedgerLockTimeout ⇒ 查是否有别的写者卡住",
            },
            ["ledger_io", "data_quality"]
        ))

    def _check_history_fill(self, status: Dict) -> None:
        """momentum_5d / volume_ratio / volatility_20d 过了取数出口的回落仍缺（v0.45.473；volatility v0.45.557；
        `data_pipeline.history_fill_stats` 经 scan_timing 进来）。

        前两项是 BuzzBee 动量 / 量能通道、ScoutBee 背离、BearBee、RivalBee ML 特征的输入；volatility_20d 是 Buzz
        波动率通道与纸面组合波动率目标仓位的输入。三项也是 `signal_archive` 的 `price.*`（维度 IC 协议 H1 输入）。
        缺了下游各自按中性 / 不可用处理、不报错——所以在这里红。`still_missing` 非空 ⇒ P2；计数缺失 ⇒ checks_skipped
        （`--date` 补跑走历史取数、不经 fetcher，本就没有这项）。"""
        st = status.get("scan_timing")
        if not isinstance(st, dict):
            return
        hf = (st.get("counters") or {}).get("history_fill")
        if not isinstance(hf, dict):
            self.checks_skipped.append("历史字段回落检查（scan_timing 无 history_fill 计数）")
            return
        missing = hf.get("still_missing") or {}
        mom, vol = list(missing.get("momentum") or []), list(missing.get("volume") or [])
        vlt = list(missing.get("volatility") or [])
        if not (mom or vol or vlt):
            return

        def _names(ts):
            return "、".join(ts[:10]) + (f" 等 {len(ts)} 只" if len(ts) > 10 else "")
        paths = "；".join(
            f"{p}：{v.get('n', 0)} 次"
            + "".join(f"，{f} 缺 {v[f]['needed']} 补上 {v[f]['filled']}"
                      + (f" 出错 {v[f]['errors']}" if v[f].get("errors") else "")
                      for f in ("momentum", "volume", "volatility")
                      if isinstance(v.get(f), dict) and v[f].get("needed"))
            for p, v in sorted((hf.get("by_path") or {}).items()))
        self.alerts.append(Alert(
            AlertLevel.MEDIUM,
            f"📊 【P2 中】历史字段回落后仍缺：momentum_5d {len(mom)} 只、volume_ratio {len(vol)} 只、"
            f"volatility_20d {len(vlt)} 只",
            {
                "momentum_5d": _names(mom) if mom else "无",
                "volume_ratio": _names(vol) if vol else "无",
                "volatility_20d": _names(vlt) if vlt else "无",
                "各出口": paths or "无",
                "影响": "这些标的的动量 / 量能 / 波动率通道按中性处理，signal_archive 当天缺对应的 price.* 行；"
                        "缺波动率的纸面组合退回分档仓位",
                "建议": "出错 > 0 ⇒ 看 alpha_hive.log「出口回落出错」；momentum 缺 ⇒ 查自攒价格索引覆盖"
                        "（price_history.coverage_report）；volume 缺 ⇒ 查 Twelve Data key / 配额（收盘后扫描里拒收是"
                        "常态，见 v0.45.505）；volatility 缺 ⇒ 看「波动率回落拒收」WARNING（多半是 CBOE 当场收盘没核实）"
                        "与 Twelve Data 失败原因（status.json twelve_data.failed）",
            },
            ["history_fill", "data_quality"]
        ))

    def get_critical_alerts(self) -> List[Alert]:
        """获取 P0 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.CRITICAL]

    def get_high_alerts(self) -> List[Alert]:
        """获取 P1 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.HIGH]

    def get_medium_alerts(self) -> List[Alert]:
        """获取 P2 级别告警"""
        return [a for a in self.alerts if a.level == AlertLevel.MEDIUM]

    def save_alerts(self, output_path: Path) -> None:
        """保存告警到文件"""
        alerts_data = {
            "timestamp": datetime.now().isoformat(),
            "total_alerts": len(self.alerts),
            "critical_count": len(self.get_critical_alerts()),
            "high_count": len(self.get_high_alerts()),
            "medium_count": len(self.get_medium_alerts()),
            "alerts": [a.to_dict() for a in self.alerts],
            # v0.45.543：无告警也写本文件 ⇒「没告警」与「检查没跑全」要能从文件里分开
            "checks_skipped": list(getattr(self, "checks_skipped", []) or []),
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(alerts_data, f, ensure_ascii=False, indent=2)


class AlertDispatcher:
    """告警分发器（多渠道发送）"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.notifiers = []

        # 动态加载可用的 notifier
        # v0.45.339：**不再挂 Slack**（此前 `slack_enabled` 为真就加 SlackNotifier）。
        # 本分发器发的是扫描健康 / SLO 类告警，属 CLAUDE.md「Slack 通知精简规则」
        # 明令只进日志的那一类；告警本身已由 `main()` 写入 alerts-*.json 与日志。
        # 编排器 Step 6 不带 `--dispatch`，故生产此前未走到这里；堵的是手动那条路。
        if self.config.get('slack_enabled', False):
            _log.info("ALERT_CONFIG.slack_enabled 被忽略：Slack 只许发 LLM 模式确认与富文本日报"
                      "（CLAUDE.md「Slack 通知精简规则」），告警只写 alerts-*.json 与日志")

        if self.config.get('email_enabled', False):
            from email_notifier import EmailNotifier
            self.notifiers.append(EmailNotifier(self.config.get('email_config')))

    def dispatch(self, alerts: List[Alert], test_mode: bool = False) -> Dict:
        """分发告警到所有渠道"""
        results = {
            "dispatched": 0,
            "failed": 0,
            "details": []
        }

        if test_mode:
            _log.info("[TEST MODE] Would dispatch %d alerts", len(alerts))
            for alert in alerts:
                _log.info("  - %s: %s", alert.level.value, alert.message)
            return results

        for alert in alerts:
            for notifier in self.notifiers:
                try:
                    notifier.send(alert)
                    results["dispatched"] += 1
                    results["details"].append({
                        "notifier": notifier.__class__.__name__,
                        "alert": alert.message,
                        "status": "success"
                    })
                except (ConnectionError, TimeoutError, OSError, ValueError, RuntimeError) as e:
                    _log.error("Alert dispatch failed via %s: %s", notifier.__class__.__name__, e, exc_info=True)
                    results["failed"] += 1
                    results["details"].append({
                        "notifier": notifier.__class__.__name__,
                        "alert": alert.message,
                        "status": "failed",
                        "error": str(e)
                    })

        return results


def main():
    """主函数 - 用于命令行调用"""
    import argparse

    parser = argparse.ArgumentParser(description="Alpha Hive Alert Manager")
    parser.add_argument('--status-json', default=str(PATHS.home / 'status.json'))
    parser.add_argument('--output-dir', default=str(PATHS.logs_dir))
    parser.add_argument('--test-mode', action='store_true')
    parser.add_argument('--dispatch', action='store_true', help='Send alerts via configured channels')

    args = parser.parse_args()

    # 1. 分析告警
    analyzer = AlertAnalyzer()
    alerts = analyzer.analyze(Path(args.status_json))

    # 2. 保存告警——v0.45.543 起**无告警也写**（alerts 为空列表）：编排器 Step 6 以「本步写出了
    # alerts-<日期>.json」为后置条件，此前无告警不写文件 ⇒ 退出码 0 是唯一证据，与「什么都没干」分不开
    # （v0.45.534 删掉的 Step 8 就是这样天天记 success）。文件只给人看，没有代码读者。
    output_path = Path(args.output_dir) / f"alerts-{datetime.now().strftime('%Y-%m-%d')}.json"
    analyzer.save_alerts(output_path)

    if not alerts:
        # v0.45.47：只有**全部检查都执行过**才敢说 healthy
        if analyzer.checks_skipped:
            _log.warning("⚠️ 无告警，但有 %d 项检查未能执行 —— 不能判定为健康：\n  · %s",
                         len(analyzer.checks_skipped),
                         "\n  · ".join(analyzer.checks_skipped))
        else:
            _log.info("No alerts detected - system healthy")
        return

    _log.info("Alerts saved: %s", output_path)
    _log.info("   Critical: %d", len(analyzer.get_critical_alerts()))
    _log.info("   High: %d", len(analyzer.get_high_alerts()))
    _log.info("   Medium: %d", len(analyzer.get_medium_alerts()))

    # 3. 分发告警（可选）
    if args.dispatch:
        from config import ALERT_CONFIG
        dispatcher = AlertDispatcher(ALERT_CONFIG)
        result = dispatcher.dispatch(alerts, test_mode=args.test_mode)
        _log.info("Dispatch result:")
        _log.info("   Sent: %d", result['dispatched'])
        _log.info("   Failed: %d", result['failed'])


if __name__ == "__main__":
    main()
