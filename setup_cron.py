#!/usr/bin/env python3
"""
🐝 Alpha Hive Cron 设置助手
交互式配置定时任务
"""

import subprocess
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def print_header(title):
    """打印标题"""
    print(f"\n{'='*70}")
    print(f"🐝 {title}")
    print(f"{'='*70}\n")


def get_crontab():
    """获取当前 crontab"""
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Failed to read crontab: %s", e)
        return ""


def set_crontab(crontab_content):
    """设置 crontab（含内容安全验证）"""
    # 验证 crontab 内容：仅允许指向已知 Alpha Hive 脚本
    ALLOWED_SCRIPTS = {
        "/Users/igg/.claude/reports/run_alpha_hive_daily.sh",
        # alpha-hive-daily.sh 已于 v0.45.293 退役（移入 ~/.claude/scripts/retired/），不再放行：
        # 手写进 crontab 的话，让它在安装时被拦下，而不是安装成功后每次都找不到脚本。
    }
    for line in crontab_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 提取命令部分（跳过 cron 时间字段）
        parts = stripped.split()
        if len(parts) >= 6:
            cmd_path = parts[5]
            if cmd_path not in ALLOWED_SCRIPTS:
                logger.warning("Blocked crontab entry with unknown script: %s", cmd_path)
                return False
    try:
        process = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE, text=True)
        process.communicate(crontab_content, timeout=10)
        return process.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Failed to set crontab: %s", e)
        return False


def show_cron_options():
    """显示 cron 选项"""
    options = {
        "1": {
            "name": "🌅 早上 8 点（推荐）",
            "cron": "0 8 * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "每个工作日早上 8 点运行（周一到周五）",
            "cron_full": "0 8 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "2": {
            "name": "🌆 中午 12 点",
            "cron": "0 12 * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "每天中午 12 点运行",
            "cron_full": "0 12 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "3": {
            "name": "🌙 晚上 5 点（美股收盘）",
            "cron": "0 17 * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "每天下午 5 点运行（美股交易结束）",
            "cron_full": "0 17 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "4": {
            "name": "🌃 晚上 8 点",
            "cron": "0 20 * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "每天晚上 8 点运行",
            "cron_full": "0 20 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "5": {
            "name": "⏰ 每小时运行",
            "cron": "0 * * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "每小时运行一次（需要更多资源）",
            "cron_full": "0 * * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "6": {
            "name": "🔄 工作日 8、12、17 点",
            "cron": "0 8,12,17 * * * /Users/igg/.claude/reports/run_alpha_hive_daily.sh",
            "description": "工作日的三个关键时段运行",
            "cron_full": "0 8,12,17 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh"
        },
        "7": {
            "name": "✏️ 自定义",
            "cron": None,
            "description": "输入自定义 cron 表达式",
            "cron_full": None
        }
    }

    print("请选择运行时间:\n")
    for key, option in options.items():
        print(f"  {key}. {option['name']}")
        print(f"     ℹ️  {option['description']}\n")

    return options


def validate_cron_expression(cron_expr):
    """验证 cron 表达式格式"""
    parts = cron_expr.strip().split()
    # 基础验证：应该有 5 个部分（分 小时 日 月 周）
    if len(parts) < 5:
        return False
    return True


def main():
    print_header("Alpha Hive Cron 定时任务设置")

    print("📋 当前 Cron 任务:\n")

    # 获取当前 crontab
    current_crontab = get_crontab()
    if current_crontab:
        alpha_hive_tasks = [line for line in current_crontab.split('\n') if 'alpha_hive' in line]
        if alpha_hive_tasks:
            print("已有 Alpha Hive 任务:")
            for task in alpha_hive_tasks:
                print(f"  {task}\n")
        else:
            print("  (无 Alpha Hive 相关任务)\n")
    else:
        print("  (无 Cron 任务)\n")

    # 显示选项
    options = show_cron_options()

    # 获取用户选择
    choice = input("请选择 (1-7): ").strip()

    if choice not in options:
        print("\n❌ 无效选择")
        return

    selected_option = options[choice]

    # 处理自定义选项
    if choice == "7":
        print("\n请输入 Cron 表达式（格式: 分 小时 日 月 周）")
        print("例如: 0 8 * * 1-5 /Users/igg/.claude/reports/run_alpha_hive_daily.sh")
        custom_cron = input("Cron 表达式: ").strip()

        if not validate_cron_expression(custom_cron):
            print("\n❌ 无效的 Cron 表达式")
            return

        selected_option["cron_full"] = custom_cron
    else:
        # 询问是否仅在工作日运行
        if choice != "5":  # 每小时的选项
            work_days_only = input(f"\n仅在工作日（周一-周五）运行？ (y/n, 默认y): ").strip().lower()
            if work_days_only != "n":
                selected_option["cron_full"] = selected_option.get("cron_full", selected_option["cron"])
            else:
                selected_option["cron_full"] = selected_option["cron"]

    # 显示最终的 cron 表达式
    print_header("确认配置")
    print(f"运行时间: {selected_option['name']}")
    print(f"描述: {selected_option['description']}")
    print(f"\nCron 表达式:")
    print(f"  {selected_option['cron_full']}\n")

    # 确认
    confirm = input("确认添加此任务？(y/n): ").strip().lower()
    if confirm != "y":
        print("\n❌ 已取消")
        return

    # 构建新的 crontab 内容
    if current_crontab and not current_crontab.endswith('\n'):
        current_crontab += '\n'

    new_crontab = current_crontab + "\n# Alpha Hive 定时扫描\n"
    new_crontab += f"# {selected_option['name']} - {datetime.now().strftime('%Y-%m-%d')}\n"
    new_crontab += selected_option['cron_full'] + "\n"

    # 设置新的 crontab
    if set_crontab(new_crontab):
        print_header("✅ 配置成功！")
        print("Cron 任务已添加。\n")

        print("📝 验证配置:")
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
        alpha_hive_tasks = [line for line in result.stdout.split('\n') if 'alpha_hive' in line]
        for task in alpha_hive_tasks:
            print(f"  ✅ {task}")

        print("\n" + "="*70)
        print("🚀 后续步骤:\n")
        print("1. 等待下一次定时时间自动运行")
        print("2. 查看 Slack 频道接收通知")
        print("3. 监控日志: tail -50 /Users/igg/.claude/logs/alpha_hive_*.log")
        print("4. 如需修改，再次运行此脚本")
        print("="*70 + "\n")

    else:
        print("\n❌ 配置失败，请检查权限")


if __name__ == "__main__":
    main()
