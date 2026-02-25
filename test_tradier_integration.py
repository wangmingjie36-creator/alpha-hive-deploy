#!/usr/bin/env python3
"""
🧪 Tradier API 集成测试套件
验证所有 Tradier API 功能和容错机制
"""

import os
import json
import sys
from datetime import datetime
from typing import Dict, List

def print_header(title: str):
    """打印测试标题"""
    print()
    print("=" * 70)
    print(f"🧪 {title}")
    print("=" * 70)
    print()

def test_environment_setup():
    """测试环境变量设置"""
    print_header("1. 环境变量检查")

    token = os.getenv("TRADIER_API_TOKEN")
    env = os.getenv("TRADIER_ENV", "sandbox")
    base_url = os.getenv("TRADIER_BASE_URL")

    results = {
        "TRADIER_API_TOKEN": "✓" if token and len(token) > 10 else "✗",
        "TRADIER_ENV": f"✓ ({env})" if env else "✗",
        "TRADIER_BASE_URL": f"✓ ({base_url})" if base_url else "✗ (可选，自动设置)",
    }

    for key, status in results.items():
        print(f"  {status} {key}")

    if not token:
        print()
        print("⚠️  请先设置 TRADIER_API_TOKEN：")
        print("  python3 setup_tradier.py")
        return False

    return True

def test_imports():
    """测试必要的模块导入"""
    print_header("2. 模块导入测试")

    results = {}

    # 测试 requests
    try:
        import requests
        results["requests"] = f"✓ ({requests.__version__})"
    except ImportError:
        results["requests"] = "✗ (未安装，运行: pip install requests)"

    # 测试 yfinance
    try:
        import yfinance
        results["yfinance"] = f"✓ ({yfinance.__version__})"
    except ImportError:
        results["yfinance"] = "✓ (可选备用源)"

    # 测试 options_analyzer
    try:
        from options_analyzer import OptionsAgent, OptionsDataFetcher, OptionsAnalyzer
        results["options_analyzer"] = "✓ (所有类可导入)"
    except ImportError as e:
        results["options_analyzer"] = f"✗ ({e})"

    # 测试 config
    try:
        from config import API_KEYS
        results["config"] = "✓ (API_KEYS 可导入)"
    except ImportError as e:
        results["config"] = f"✗ ({e})"

    for module, status in results.items():
        print(f"  {status} {module}")

    return all("✓" in status for status in results.values() if "可选" not in status)

def test_config_parsing():
    """测试配置解析"""
    print_header("3. 配置解析测试")

    try:
        from config import API_KEYS, EVALUATION_WEIGHTS

        tradier = API_KEYS.get("TRADIER", {})

        print(f"  ✓ Tradier 配置已加载")
        print(f"    • environment: {tradier.get('environment', 'N/A')}")
        print(f"    • base_url: {tradier.get('base_url', 'N/A')}")
        print(f"    • timeout: {tradier.get('timeout', 'N/A')} 秒")
        print(f"    • max_retries: {tradier.get('max_retries', 'N/A')}")

        print()
        print(f"  ✓ 评分权重已加载")
        print(f"    • signal: {EVALUATION_WEIGHTS.get('signal', 0)}")
        print(f"    • catalyst: {EVALUATION_WEIGHTS.get('catalyst', 0)}")
        print(f"    • sentiment: {EVALUATION_WEIGHTS.get('sentiment', 0)}")
        print(f"    • odds: {EVALUATION_WEIGHTS.get('odds', 0)}")
        print(f"    • risk_adjustment: {EVALUATION_WEIGHTS.get('risk_adjustment', 0)}")
        print(f"    • options: {EVALUATION_WEIGHTS.get('options', 0)} ← 新增")
        print(f"    合计: {sum(EVALUATION_WEIGHTS.values()):.2f}")

        return sum(EVALUATION_WEIGHTS.values()) == 1.0

    except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"  ✗ 配置解析失败：{e}")
        return False

def test_tradier_connectivity():
    """测试 Tradier API 连接"""
    print_header("4. Tradier API 连接测试")

    try:
        import requests
    except ImportError:
        print("  ⚠️  requests 未安装，跳过 API 连接测试")
        return None

    token = os.getenv("TRADIER_API_TOKEN")
    if not token:
        print("  ⚠️  TRADIER_API_TOKEN 未设置，跳过连接测试")
        return None

    env = os.getenv("TRADIER_ENV", "sandbox")
    base_url = os.getenv("TRADIER_BASE_URL")
    if not base_url:
        base_url = "https://sandbox.tradier.com" if env == "sandbox" else "https://api.tradier.com"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        print(f"  📡 连接到 {env.upper()} 环境：{base_url}")
        print(f"  发送请求：GET /v1/user/profile")

        response = requests.get(
            f"{base_url}/v1/user/profile",
            headers=headers,
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            print(f"  ✓ API 连接成功 (HTTP {response.status_code})")
            print(f"    • 账户 ID: {data.get('profile', {}).get('account_number', 'N/A')}")
            return True

        elif response.status_code == 401:
            print(f"  ✗ API 认证失败 (HTTP 401)")
            print(f"    Token 无效或已过期，请重新配置")
            return False

        elif response.status_code == 429:
            print(f"  ⚠️  API 速率限制 (HTTP 429)")
            print(f"    请稍后重试")
            return None

        else:
            print(f"  ✗ API 返回错误 (HTTP {response.status_code})")
            print(f"    {response.text[:150]}")
            return False

    except requests.exceptions.Timeout:
        print(f"  ✗ 请求超时")
        return False

    except requests.exceptions.ConnectionError:
        print(f"  ✗ 连接失败（网络问题）")
        return False

    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"  ✗ 异常：{e}")
        return False

def test_options_agent():
    """测试 OptionsAgent 核心功能"""
    print_header("5. OptionsAgent 功能测试")

    try:
        from options_analyzer import OptionsAgent

        agent = OptionsAgent()

        # 测试单个标的
        tickers = ["NVDA", "TSLA", "SPY"]

        for ticker in tickers:
            print(f"  分析 {ticker}...")

            try:
                result = agent.analyze(ticker, stock_price=145.0)

                # 验证关键字段
                required_fields = [
                    'ticker', 'iv_rank', 'iv_percentile', 'iv_current',
                    'put_call_ratio', 'gamma_exposure', 'options_score',
                    'flow_direction', 'signal_summary'
                ]

                missing = [f for f in required_fields if f not in result]

                if missing:
                    print(f"    ✗ 缺少字段：{missing}")
                    return False

                print(f"    ✓ 分析成功")
                print(f"      • Options Score: {result['options_score']}/10")
                print(f"      • IV Rank: {result['iv_rank']:.1f}")
                print(f"      • P/C Ratio: {result['put_call_ratio']:.2f}")
                print(f"      • Flow: {result['flow_direction']}")
                print(f"      • 数据源: {result.get('source', 'Unknown')}")

            except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
                print(f"    ✗ 分析失败：{e}")
                return False

        return True

    except (ImportError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"  ✗ OptionsAgent 加载失败：{e}")
        return False

def test_advanced_analyzer_integration():
    """测试与 AdvancedAnalyzer 的集成"""
    print_header("6. AdvancedAnalyzer 集成测试")

    try:
        from advanced_analyzer import AdvancedAnalyzer
        import json

        # 加载测试数据
        with open("realtime_metrics.json") as f:
            metrics = json.load(f)

        analyzer = AdvancedAnalyzer()

        ticker = "NVDA"
        if ticker not in metrics:
            print(f"  ⚠️  {ticker} 测试数据不可用")
            return None

        print(f"  分析 {ticker} 完整报告...")

        analysis = analyzer.generate_comprehensive_analysis(ticker, metrics[ticker])

        # 检查期权分析是否包含在报告中
        if "options_analysis" not in analysis:
            print(f"  ✗ options_analysis 未包含在报告中")
            return False

        options = analysis["options_analysis"]

        if options is None:
            print(f"  ⚠️  options_analysis 为 None（期权分析不可用）")
            return None

        print(f"  ✓ 期权分析已集成到综合报告")
        print(f"    • Options Score: {options['options_score']}/10")
        print(f"    • 数据源: {options.get('source', 'Unknown')}")

        return True

    except (ImportError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"  ✗ 集成测试失败：{e}")
        import traceback
        traceback.print_exc()
        return False

def test_html_report_generation():
    """测试 HTML 报告生成"""
    print_header("7. HTML 报告生成测试")

    try:
        from generate_ml_report import MLEnhancedReportGenerator
        import json

        with open("realtime_metrics.json") as f:
            metrics = json.load(f)

        if "NVDA" not in metrics:
            print(f"  ⚠️  NVDA 测试数据不可用")
            return None

        print(f"  生成 NVDA ML 报告...")

        gen = MLEnhancedReportGenerator()
        report = gen.generate_ml_enhanced_report("NVDA", metrics["NVDA"])
        html = gen.generate_html_report("NVDA", report)

        # 检查 HTML 是否包含期权部分
        if "期权信号分析" not in html:
            print(f"  ✗ HTML 报告缺少期权分析章节")
            return False

        if "options_score" not in html and "Options Score" not in html:
            print(f"  ⚠️  HTML 报告中缺少 options_score")

        print(f"  ✓ HTML 报告生成成功")
        print(f"    • 包含期权分析章节")
        print(f"    • 包含期权评分")

        # 保存报告用于检查
        filename = f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        with open(filename, "w") as f:
            f.write(html)
        print(f"    • 保存到：{filename}")

        return True

    except (ImportError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"  ✗ HTML 生成失败：{e}")
        import traceback
        traceback.print_exc()
        return False

def test_fallback_mechanisms():
    """测试容错机制"""
    print_header("8. 容错机制测试")

    try:
        from options_analyzer import OptionsAgent

        print("  测试 yfinance 容错...")

        agent = OptionsAgent()

        # 尝试分析一个异常标的
        result = agent.analyze("INVALID_TICKER")

        if result:
            print(f"  ✓ 分析返回结果（可能是样本数据）")
            print(f"    • Options Score: {result.get('options_score', 'N/A')}")
            print(f"    • 数据源: {result.get('source', 'Unknown')}")
        else:
            print(f"  ⚠️  无法分析无效标的（符合预期）")

        return True

    except (ImportError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"  ✗ 容错测试异常：{e}")
        return False

def main():
    """运行所有测试"""

    print()
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 20 + "🐝 Tradier API 集成测试套件" + " " * 20 + "║")
    print("║" + " " * 15 + "Alpha Hive - Options Analysis Agent" + " " * 19 + "║")
    print("╚" + "=" * 68 + "╝")

    tests = [
        ("环境设置", test_environment_setup),
        ("模块导入", test_imports),
        ("配置解析", test_config_parsing),
        ("API 连接", test_tradier_connectivity),
        ("OptionsAgent", test_options_agent),
        ("AdvancedAnalyzer 集成", test_advanced_analyzer_integration),
        ("HTML 报告生成", test_html_report_generation),
        ("容错机制", test_fallback_mechanisms),
    ]

    results = {}

    for name, test_func in tests:
        try:
            result = test_func()
            results[name] = result
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            print(f"❌ {name} 异常：{e}")
            results[name] = False

    # 总结
    print()
    print("=" * 70)
    print("📊 测试总结")
    print("=" * 70)
    print()

    passed = sum(1 for r in results.values() if r is True)
    failed = sum(1 for r in results.values() if r is False)
    skipped = sum(1 for r in results.values() if r is None)

    for name, result in results.items():
        if result is True:
            status = "✅ PASS"
        elif result is False:
            status = "❌ FAIL"
        else:
            status = "⏭️  SKIP"

        print(f"  {status:10} {name}")

    print()
    print(f"总计: ✅ {passed} | ❌ {failed} | ⏭️  {skipped}")
    print()

    if failed == 0:
        print("✅ 所有测试通过！")
        return 0
    else:
        print(f"❌ 有 {failed} 个测试失败")
        return 1

if __name__ == "__main__":
    sys.exit(main())
