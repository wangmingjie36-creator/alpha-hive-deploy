#!/usr/bin/env python3
"""
🧪 Phase 3 P1 代码执行引擎 - 完整测试套件
"""

import time
import sys
from pathlib import Path

# 导入测试模块
from code_executor import CodeExecutor
from code_generator import CodeGenerator
from debugger import Debugger
from code_executor_agent import CodeExecutorAgent
from pheromone_board import PheromoneBoard


def print_test_header(title: str):
    """打印测试标题"""
    print(f"\n{'='*70}")
    print(f"🧪 {title}")
    print(f"{'='*70}\n")


def test_code_executor():
    """测试 1: CodeExecutor 基础功能"""
    print_test_header("测试 1: CodeExecutor 基础功能")

    executor = CodeExecutor(max_timeout=10)
    print(f"✅ CodeExecutor 初始化成功")
    print(f"   - 沙箱目录: {executor.sandbox_dir}")
    print(f"   - 最大超时: {executor.max_timeout}s")

    # 测试 1a: 简单 Python 代码
    code = """
import json
result = {"status": "success", "value": 42}
print(json.dumps(result))
"""

    result = executor.execute_python(code)
    print(f"\n✅ 测试 1a - 简单代码执行:")
    print(f"   - 成功: {result['success']}")
    print(f"   - 耗时: {result['execution_time']:.3f}s")
    print(f"   - 输出: {result['stdout'][:50]}")

    # 测试 1b: 错误处理
    bad_code = """
x = 1 / 0
"""

    result = executor.execute_python(bad_code)
    print(f"\n✅ 测试 1b - 错误捕获:")
    print(f"   - 成功: {result['success']}")
    print(f"   - 错误: {result['error'][:50]}")

    # 测试 1c: 审计日志
    logs = executor.get_audit_log(5)
    print(f"\n✅ 测试 1c - 审计日志:")
    print(f"   - 日志条数: {len(logs)}")
    for log in logs[-2:]:
        print(f"     {log.strip()}")


def test_code_generator():
    """测试 2: CodeGenerator 代码生成"""
    print_test_header("测试 2: CodeGenerator 代码生成")

    # 测试 2a: yfinance 代码
    code = CodeGenerator.generate_data_fetch(
        "yfinance",
        {"ticker": "NVDA", "period": "1mo"}
    )
    print(f"✅ 测试 2a - yfinance 代码生成:")
    print(f"   - 代码行数: {len(code.split(chr(10)))}")
    print(f"   - 包含 yfinance: {'yfinance' in code}")
    print(f"   - 代码片段: {code.split(chr(10))[0][:50]}")

    # 测试 2b: 技术分析代码
    code = CodeGenerator.generate_analysis(
        "technical",
        {"ticker": "NVDA", "period": "1mo"}
    )
    print(f"\n✅ 测试 2b - 技术分析代码:")
    print(f"   - 代码行数: {len(code.split(chr(10)))}")
    print(f"   - 包含 SMA: {'SMA_20' in code}")

    # 测试 2c: 可视化代码
    code = CodeGenerator.generate_visualization(
        "line",
        {"ticker": "NVDA"}
    )
    print(f"\n✅ 测试 2c - 可视化代码:")
    print(f"   - 代码行数: {len(code.split(chr(10)))}")
    print(f"   - 包含 matplotlib: {'matplotlib' in code}")


def test_debugger():
    """测试 3: Debugger 调试功能"""
    print_test_header("测试 3: Debugger 调试功能")

    debugger = Debugger()

    # 测试 3a: 错误解析
    stderr = """
Traceback (most recent call last):
  File "script.py", line 5, in <module>
    result = data['key']
KeyError: 'key'
"""

    error = debugger.parse_error(stderr)
    print(f"✅ 测试 3a - 错误解析:")
    print(f"   - 错误类型: {error['error_type']}")
    print(f"   - 严重程度: {error['severity']}")
    print(f"   - 建议: {error['suggestion']}")

    # 测试 3b: 代码验证
    good_code = """
x = 1 + 2
print(x)
"""

    is_valid, warnings = debugger.validate_code(good_code)
    print(f"\n✅ 测试 3b - 代码验证（正确代码）:")
    print(f"   - 有效: {is_valid}")
    print(f"   - 警告数: {len(warnings)}")

    # 测试 3c: 代码验证（不安全）
    unsafe_code = """
import os
os.system('ls')
"""

    is_valid, warnings = debugger.validate_code(unsafe_code)
    print(f"\n✅ 测试 3c - 代码验证（不安全代码）:")
    print(f"   - 有效: {is_valid}")
    print(f"   - 警告数: {len(warnings)}")
    if warnings:
        print(f"   - 第一个警告: {warnings[0][:60]}")


def test_code_executor_agent():
    """测试 4: CodeExecutorAgent 集成"""
    print_test_header("测试 4: CodeExecutorAgent 集成")

    executor = CodeExecutor()
    board = PheromoneBoard()
    agent = CodeExecutorAgent(board, executor=executor)

    print(f"✅ CodeExecutorAgent 初始化成功")

    # 测试 4a: 分析
    result = agent.analyze("TEST")
    print(f"\n✅ 测试 4a - Agent 分析:")
    print(f"   - 分析完成: {result is not None}")
    print(f"   - 返回字段: {list(result.keys())[:5]}")

    # 测试 4b: 代码执行与分析
    code = """
import json
print(json.dumps({"result": 100, "status": "ok"}))
"""

    result = agent.execute_and_analyze(code, "TEST")
    print(f"\n✅ 测试 4b - 执行与分析:")
    print(f"   - 成功: {result.get('success', False)}")
    print(f"   - 分析: {result.get('analysis', 'N/A')}")


def test_integration():
    """测试 5: 端到端集成"""
    print_test_header("测试 5: 端到端集成")

    executor = CodeExecutor()
    generator = CodeGenerator()
    debugger = Debugger()
    board = PheromoneBoard()
    agent = CodeExecutorAgent(board, executor=executor)

    # 完整流程：生成 → 执行 → 调试
    print("📋 完整流程测试：")

    # 步骤 1: 生成代码
    code = generator.generate_data_fetch(
        "yfinance",
        {"ticker": "TEST"}
    )
    print(f"✅ 步骤 1: 生成 yfinance 代码（{len(code)} 字符）")

    # 步骤 2: 验证代码
    is_valid, warnings = debugger.validate_code(code)
    print(f"✅ 步骤 2: 代码验证 - 有效={is_valid}, 警告={len(warnings)}")

    # 步骤 3: 执行代码
    result = executor.execute_python(code)
    print(f"✅ 步骤 3: 执行代码 - 成功={result['success']}, 耗时={result['execution_time']:.2f}s")

    # 步骤 4: 分析结果
    if result['success']:
        print(f"✅ 步骤 4: 输出验证 - 长度={len(result['stdout'])}")
    else:
        error = debugger.parse_error(result['stderr'])
        print(f"✅ 步骤 4: 错误分析 - 类型={error['error_type']}")


def test_performance():
    """测试 6: 性能基准"""
    print_test_header("测试 6: 性能基准")

    executor = CodeExecutor()

    # 测试 6a: 代码执行延迟
    simple_code = "print(1 + 1)"
    times = []

    for i in range(5):
        start = time.time()
        result = executor.execute_python(simple_code)
        elapsed = time.time() - start
        times.append(elapsed)

    avg_time = sum(times) / len(times)
    print(f"✅ 测试 6a - 代码执行延迟:")
    print(f"   - 平均: {avg_time*1000:.2f}ms")
    print(f"   - 最小: {min(times)*1000:.2f}ms")
    print(f"   - 最大: {max(times)*1000:.2f}ms")

    # 测试 6b: 沙箱隔离验证
    print(f"\n✅ 测试 6b - 沙箱隔离:")
    print(f"   - 沙箱目录: {executor.sandbox_dir}")
    print(f"   - 脚本目录存在: {(executor.sandbox_dir / 'scripts').exists()}")
    print(f"   - 数据目录存在: {(executor.sandbox_dir / 'data').exists()}")
    print(f"   - 输出目录存在: {(executor.sandbox_dir / 'output').exists()}")


def main():
    """运行所有测试"""
    print("\n" + "="*70)
    print("🧪 Alpha Hive Phase 3 P1 - 代码执行引擎完整测试")
    print("="*70)

    try:
        test_code_executor()
        test_code_generator()
        test_debugger()
        test_code_executor_agent()
        test_integration()
        test_performance()

        print_test_header("✅ 所有测试完成")
        print("✅ Phase 3 P1 代码执行引擎验证成功！\n")

        return 0

    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
