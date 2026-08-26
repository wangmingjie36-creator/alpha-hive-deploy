"""
v0.45.21 回归：卡片必须并列展示 final_score 与 sentiment。

守两条不变式：
  1. **有 sentiment 时并列展示**——只显示综合分等于把唯一有 IC 的维度
     藏进一个净剩为 0 的合成分里（见 experiments/final_score_dilution_report.md）。
  2. **没有 sentiment 时不得兜底成 5.0**——那会把「没有数据」画成「中性」，
     是本项目最常见的故障模式（见 [[alpha-hive-silent-degradation]]）。

按项目惯例，每条不变式都配「喂退化数据看它红」的用例。
"""
import pathlib

import pytest

from dashboard_renderer import _build_top_cards_html


def _render(swarm_detail, tickers, tmp_path):
    return _build_top_cards_html(
        tickers,
        {},                      # opp_by_ticker：留空，走 final_score 回退路径
        swarm_detail,
        pathlib.Path(tmp_path),
        "2026-08-25",
        {},                      # score_deltas
        {},                      # hist_full
    )


@pytest.fixture()
def detail_with_sentiment():
    return {
        "AAA": {"final_score": 7.2, "direction": "bullish",
                "dimension_scores": {"signal": 6.0, "catalyst": 5.0,
                                     "sentiment": 8.3, "odds": 5.0, "risk_adj": 6.0}},
    }


def test_both_scores_rendered(detail_with_sentiment, tmp_path):
    """并列的两个数字都要出现，且情绪分带专属类名。"""
    html = _render(detail_with_sentiment, ["AAA"], tmp_path)
    assert "7.2" in html, "综合分缺失"
    assert "8.3" in html, "情绪分缺失"
    assert "score-senti" in html, "情绪分未用专属类名（前端样式会失效）"
    assert 'data-senti="8.3"' in html, "data-senti 属性缺失（排序/筛选会拿不到值）"
    assert "情绪分" in html, "情绪分标签缺失"


def test_missing_sentiment_is_omitted_not_faked(tmp_path):
    """
    没有 sentiment 时整段省略，**绝不能**出现 5.0 兜底。

    这是本测试存在的主要理由：`.get("sentiment", 5.0)` 这种写法看起来无害，
    但它让「没有数据」和「情绪中性」在页面上长得一模一样。
    """
    detail = {
        "BBB": {"final_score": 6.4, "direction": "neutral",
                "dimension_scores": {"signal": 6.0, "catalyst": 5.0,
                                     "odds": 5.0, "risk_adj": 6.0}},  # 无 sentiment
    }
    html = _render(detail, ["BBB"], tmp_path)
    assert "6.4" in html, "综合分仍应展示"
    assert "score-senti" not in html, "无 sentiment 数据时不得渲染情绪分块"
    assert 'data-senti=""' in html, "data-senti 应为空串而非兜底值"
    assert "情绪分" not in html, "无数据时不得出现情绪分标签"


def test_no_dimension_scores_at_all(tmp_path):
    """整个 dimension_scores 缺失时同样不得伪造。"""
    detail = {"CCC": {"final_score": 5.5, "direction": "neutral"}}
    html = _render(detail, ["CCC"], tmp_path)
    assert "score-senti" not in html
    assert 'data-senti=""' in html
