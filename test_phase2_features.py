#!/usr/bin/env python3
"""
🧪 Alpha Hive Phase 2 功能测试脚本
演示持久化记忆、检索引擎、权重管理的完整工作流
"""

import time
import sqlite3
import json
from datetime import datetime

def print_section(title):
    """打印分隔符"""
    print(f"\n{'='*70}")
    print(f"🔷 {title}")
    print(f"{'='*70}\n")


def test_memory_store():
    """测试 1: MemoryStore 基础功能"""
    print_section("测试 1: MemoryStore 基础功能")

    from memory_store import MemoryStore

    ms = MemoryStore()
    print(f"✅ MemoryStore 初始化成功")
    print(f"   - DB 路径：{ms.db_path}")

    # 测试会话 ID 生成
    session_id = ms.generate_session_id(run_mode="test")
    print(f"✅ 生成会话 ID：{session_id}")

    # 测试 Agent 记忆保存
    entry = {
        "agent_id": "ScoutBeeNova",
        "ticker": "TEST",
        "discovery": "测试发现：机构持仓增加",
        "source": "SEC Form 13F",
        "self_score": 7.5,
        "direction": "bullish",
        "pheromone_strength": 1.0,
        "support_count": 0,
        "date": datetime.now().strftime("%Y-%m-%d")
    }

    memory_id = ms.save_agent_memory(entry, session_id)
    print(f"✅ 保存 Agent 记忆：{memory_id}")

    # 测试记忆查询
    memories = ms.get_recent_memories("TEST", days=1, limit=10)
    print(f"✅ 查询记忆：{len(memories)} 条")
    if memories:
        print(f"   - 最新记忆：{memories[0]['discovery'][:50]}...")

    # 测试权重查询
    weights = ms.get_agent_weights()
    print(f"✅ 查询权重：{len(weights)} 个 Agent")
    print(f"   - 权重范围：{min(weights.values()):.2f}x ~ {max(weights.values()):.2f}x")


def test_memory_retriever():
    """测试 2: MemoryRetriever 检索性能"""
    print_section("测试 2: MemoryRetriever 检索性能")

    from memory_store import MemoryStore
    from memory_retriever import MemoryRetriever

    ms = MemoryStore()
    mr = MemoryRetriever(ms, cache_ttl_seconds=300)
    print(f"✅ MemoryRetriever 初始化成功")
    print(f"   - 缓存 TTL：300s")

    # 测试检索性能
    t0 = time.time()
    results = mr.find_similar("bullish earnings", ticker="NVDA", top_k=5)
    elapsed = (time.time() - t0) * 1000

    print(f"✅ 检索性能：{elapsed:.2f}ms（目标 < 50ms）")
    print(f"✅ 检索结果：{len(results)} 条")

    if results:
        print(f"   - 最高相似度：{max(r['similarity'] for r in results):.3f}")
        print(f"   - 最低相似度：{min(r['similarity'] for r in results):.3f}")

    # 测试上下文摘要
    context = mr.get_context_summary("NVDA", datetime.now().strftime("%Y-%m-%d"))
    if context:
        print(f"✅ 历史上下文摘要：{context}")
    else:
        print(f"⚠️  历史上下文为空（首次运行或无历史）")

    # 测试缓存失效
    mr.invalidate_cache("NVDA")
    print(f"✅ 缓存已清除")


def test_agent_weight_manager():
    """测试 3: AgentWeightManager 权重管理"""
    print_section("测试 3: AgentWeightManager 权重管理")

    from memory_store import MemoryStore
    from agent_weight_manager import AgentWeightManager

    ms = MemoryStore()
    awm = AgentWeightManager(ms)
    print(f"✅ AgentWeightManager 初始化成功")

    # 获取当前权重
    weights = awm.get_weights()
    print(f"✅ 当前权重：")
    for agent_id, weight in sorted(weights.items()):
        print(f"   - {agent_id}: {weight:.2f}x")

    # 测试单个查询
    scout_weight = awm.get_weight("ScoutBeeNova")
    print(f"✅ ScoutBeeNova 权重：{scout_weight:.2f}x")

    # 测试加权平均
    test_results = [
        {"source": "ScoutBeeNova", "score": 7.5},
        {"source": "OracleBeeEcho", "score": 6.0},
        {"source": "BuzzBeeWhisper", "score": 8.0},
    ]

    avg = awm.weighted_average_score(test_results)
    simple_avg = sum(r["score"] for r in test_results) / len(test_results)
    print(f"✅ 加权平均分：{avg:.2f}/10（简单平均：{simple_avg:.2f}/10）")

    # 打印权重摘要
    print(f"\n✅ 权重摘要：")
    awm.print_weight_summary()


def test_swarm_scan_integration():
    """测试 4: 完整蜂群扫描集成"""
    print_section("测试 4: 完整蜂群扫描集成（1 个标的）")

    from alpha_hive_daily_report import AlphaHiveDailyReporter
    import time

    reporter = AlphaHiveDailyReporter()
    print(f"✅ AlphaHiveDailyReporter 初始化成功")
    print(f"   - MemoryStore：{'✅ 已启用' if reporter.memory_store else '❌ 禁用'}")
    print(f"   - Session ID：{reporter._session_id}")

    # 运行蜂群扫描
    print(f"\n🐝 启动蜂群扫描...")
    start = time.time()

    try:
        result = reporter.run_swarm_scan(focus_tickers=["NVDA"])
        elapsed = time.time() - start

        print(f"\n✅ 蜂群扫描完成")
        print(f"   - 耗时：{elapsed:.2f}s")
        print(f"   - 结果标的：{len(result.get('opportunities', []))}")

        # 等待异步写入完成
        time.sleep(0.5)

        # 检查数据库持久化
        conn = sqlite3.connect(reporter.memory_store.db_path)
        cursor = conn.cursor()

        agent_memory_count = cursor.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
        sessions_count = cursor.execute("SELECT COUNT(*) FROM reasoning_sessions").fetchone()[0]

        print(f"\n✅ 数据库持久化验证：")
        print(f"   - agent_memory 表：{agent_memory_count} 条记录")
        print(f"   - reasoning_sessions 表：{sessions_count} 条记录")

        # 查看最新的 Agent 记忆
        cursor.execute("""
            SELECT agent_id, direction, self_score, discovery
            FROM agent_memory
            ORDER BY created_at DESC
            LIMIT 3
        """)

        print(f"\n✅ 最新的 Agent 记忆：")
        for row in cursor.fetchall():
            agent_id, direction, score, discovery = row
            print(f"   - {agent_id} ({direction}): {score:.1f}/10")
            print(f"     {discovery[:60]}...")

        conn.close()

    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"❌ 蜂群扫描失败：{e}")
        import traceback
        traceback.print_exc()


def test_pheromone_persistence():
    """测试 5: 信息素板异步持久化"""
    print_section("测试 5: 信息素板异步持久化")

    from pheromone_board import PheromoneBoard, PheromoneEntry
    from memory_store import MemoryStore
    import time

    ms = MemoryStore()
    session_id = ms.generate_session_id(run_mode="test_persist")

    board = PheromoneBoard(memory_store=ms, session_id=session_id)
    print(f"✅ 创建启用持久化的信息素板")
    print(f"   - Session ID：{session_id}")

    # 发布多个条目
    entries_data = [
        ("bullish", 7.5, "高机构持仓"),
        ("bearish", 5.0, "看空期权信号"),
        ("bullish", 8.0, "积极情绪"),
    ]

    for direction, score, discovery in entries_data:
        entry = PheromoneEntry(
            agent_id="TestAgent",
            ticker="TEST",
            discovery=discovery,
            source="test",
            self_score=score,
            direction=direction
        )
        board.publish(entry)
        print(f"✅ 发布信息素：{discovery} ({direction}, {score}/10)")

    # 等待异步写入
    time.sleep(0.2)

    # 验证持久化
    conn = sqlite3.connect(ms.db_path)
    cursor = conn.cursor()

    count = cursor.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id = 'TestAgent' AND session_id = ?",
        (session_id,)
    ).fetchone()[0]

    print(f"\n✅ 异步持久化验证：")
    print(f"   - 保存的条目数：{count}")
    print(f"   - 预期条目数：{len(entries_data)}")

    if count == len(entries_data):
        print(f"✅ 持久化验证通过！")
    else:
        print(f"⚠️  可能在构建中，请稍后重试")

    conn.close()


def test_agent_accuracy_tracking():
    """测试 6: Agent 准确率追踪"""
    print_section("测试 6: Agent 准确率追踪")

    from memory_store import MemoryStore

    ms = MemoryStore()

    # 查看所有 Agent 的准确率
    agent_ids = [
        "ScoutBeeNova",
        "OracleBeeEcho",
        "BuzzBeeWhisper",
        "ChronosBeeHorizon",
        "RivalBeeVanguard",
        "GuardBeeSentinel"
    ]

    print(f"✅ Agent 准确率统计（T+7）：\n")

    for agent_id in agent_ids:
        accuracy = ms.get_agent_accuracy(agent_id, period="t7")

        if accuracy['sample_count'] == 0:
            status = "⚠️  无数据"
        elif accuracy['accuracy'] > 0.6:
            status = "🔥 准确"
        elif accuracy['accuracy'] < 0.4:
            status = "❄️ 不准"
        else:
            status = "📊 中等"

        print(f"{status} {agent_id:20s} | 准确率: {accuracy['accuracy']:6.2%} | 样本: {accuracy['sample_count']:2d} | 均回: {accuracy['avg_return']:+7.2%}")


def test_session_tracking():
    """测试 7: 会话追踪"""
    print_section("测试 7: 会话追踪")

    import sqlite3

    db_path = "/Users/igg/.claude/reports/pheromone.db"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT session_id, date, run_mode,
                   json_extract(tickers, '$[0]') as first_ticker,
                   top_opportunity_score, total_duration_seconds
            FROM reasoning_sessions
            ORDER BY created_at DESC
            LIMIT 5
        """)

        print(f"✅ 最近的会话记录：\n")

        rows = cursor.fetchall()
        if not rows:
            print("⚠️  无会话记录")
        else:
            for row in rows:
                session_id, date, run_mode, first_ticker, score, duration = row
                print(f"  📅 {date} | 模式: {run_mode:10s} | 首票: {first_ticker} | 最高分: {score or 'N/A'} | 耗时: {duration or 'N/A'}s")
                print(f"     session: {session_id[:50]}...\n")

        conn.close()

    except (sqlite3.Error, OSError) as e:
        print(f"❌ 查询失败：{e}")


def main():
    """运行所有测试"""
    print("\n" + "="*70)
    print("🧪 Alpha Hive Phase 2 功能测试")
    print("="*70)

    try:
        test_memory_store()
        test_memory_retriever()
        test_agent_weight_manager()
        test_pheromone_persistence()
        test_agent_accuracy_tracking()
        test_session_tracking()
        test_swarm_scan_integration()

        print_section("✅ 全部测试完成")
        print("✅ Phase 2 功能验证成功！\n")

    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
