"""v0.45.544：本会话 504 ~ 542 二次检查里修掉的「None 往下游传」与展示类问题。

P/C / total_oi 自 v0.45.512 / 516 / 522 起会是 None（取不到），下游有几处没接住：
- 深度报告：`.get("total_oi", 0)` 在键存在、值为 None 时不生效 ⇒ `:,.0f` TypeError（整份报告失败；09-24 / 09-25
  那种全断日 30 只都会撞上）；论点失效评估把 None 兜成 1.0 冒充「评估过、没触发」；叙述写成「多空均衡」。
- 期权图表：None 兜成 0.00 ⇒ 染绿、标「Call偏多 →看涨」。
- RivalBee：None 兜成 1.0 当 ML 特征、不进 imputed_features。
- 网站 / ML 报告：流向 "unknown" 原样贴出；样本链早退仍写 "neutral"。
- 回测报告：窗口内 P/C 全 NULL ⇒ `AVG` 为 None ⇒ `:.2f` TypeError。
"""
from __future__ import annotations



def test_none_pc_is_listed_as_imputed():
    """P/C 取不到 ⇒ `TrainingData.put_call_ratio=None` ⇒ ml_predictor 列进 imputed_features。
    （v0.45.538：原本这里还守 RivalBee 的 `_usable_pc` 读法——Rival 的模型与 Oracle P/C 读取随合成样本模型一起退役，
    那半条随之删；`_feature_quality` 对 None 的处理仍服务 ML 报告路径，留着。）"""
    from ml_predictor import TrainingData, _feature_quality
    q = _feature_quality(TrainingData(ticker="SYN", date="2026-10-19", crowding_score=50.0, catalyst_quality="B",
                                      momentum_5d=0.0, volatility=0.3, market_sentiment=0.0, iv_rank=50.0,
                                      put_call_ratio=None, actual_return_3d=0.0, actual_return_7d=0.0,
                                      actual_return_30d=0.0, win_3d=False, win_7d=False, win_30d=False))
    assert "put_call_ratio" in q["imputed_features"]


def test_sample_chain_flow_is_unknown_and_site_hides_it():
    import inspect

    import dashboard_renderer as dr
    import options_analyzer as oa
    src = inspect.getsource(oa.OptionsAgent.analyze)
    assert '"flow_direction": "unknown",' in src and '"flow_direction": "neutral",' not in src
    sd = {"SYN": {"agent_details": {"ScoutBeeNova": {"details": {"price": 10.0}},
                                    "OracleBeeEcho": {"details": {"flow_direction": "unknown"}}}}}
    assert dr._detail("SYN", sd)["flow_dir"] == "-"
    sd["SYN"]["agent_details"]["OracleBeeEcho"]["details"]["flow_direction"] = "bullish"
    assert dr._detail("SYN", sd)["flow_dir"] == "bullish"


def test_backtester_source_guards_null_avgs():
    """`print_report` 的期权段直接查生产库、没有接缝 ⇒ 源码断言。变红的变异：`{…avg_pc_ratio']:.2f}` 写回去。"""
    import inspect

    import backtester as bt
    src = inspect.getsource(bt.Backtester.print_report)
    assert "{opts_row['avg_pc_ratio']:.2f}" not in src and "平均 P/C Ratio: —" in src
