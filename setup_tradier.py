#!/usr/bin/env python3
"""
🐝 Tradier API 配置脚本
用于设置和验证 Tradier API Token
"""

import os
import sys
import json
from pathlib import Path

def setup_tradier_token():
    """交互式设置 Tradier API Token"""

    print("=" * 70)
    print("🐝 Alpha Hive - Tradier API 配置向导")
    print("=" * 70)
    print()

    print("📌 获取 Tradier API Token 的步骤：")
    print("  1. 访问 https://tradier.com/developer")
    print("  2. 注册开发者账户（免费）")
    print("  3. 创建应用并获取 API Token")
    print("  4. 选择沙箱（测试）或生产环境")
    print()

    # 选择环境
    print("选择环境：")
    print("  1. Sandbox（沙箱，推荐用于测试）")
    print("  2. Production（生产环境）")
    env_choice = input("请选择（1-2，默认 1）: ").strip() or "1"

    if env_choice == "1":
        environment = "sandbox"
        base_url = "https://sandbox.tradier.com"
        print("✓ 已选择沙箱环境")
    else:
        environment = "production"
        base_url = "https://api.tradier.com"
        print("✓ 已选择生产环境（需要真实账户和资金）")

    print()

    # 输入 Token
    token = input("请输入您的 Tradier API Token: ").strip()

    if not token or len(token) < 10:
        print("❌ Token 无效，长度应至少 10 个字符")
        return False

    print()

    # 保存到环境变量配置
    config_method = input("保存方式（1=环境变量, 2=.env 文件, 默认 1）: ").strip() or "1"

    if config_method == "2":
        # 保存到 .env 文件
        env_file = Path.home() / ".claude" / ".env.tradier"
        env_file.parent.mkdir(parents=True, exist_ok=True)

        env_content = f"""# Tradier API 配置
TRADIER_ENV={environment}
TRADIER_BASE_URL={base_url}
TRADIER_API_TOKEN={token}
"""
        env_file.write_text(env_content)
        env_file.chmod(0o600)  # 设置文件权限为 600（仅所有者可读写）

        print(f"✓ 配置已保存到 {env_file}")
        print(f"  文件权限：600（仅您可读写）")

        # 也保存到 shell 配置
        shell_rc = Path.home() / ".zshrc"
        if shell_rc.exists():
            existing = shell_rc.read_text()
            if "TRADIER_API_TOKEN" not in existing:
                shell_rc.append_text(f"\n# Tradier API\nexport TRADIER_API_TOKEN='{token}'\nexport TRADIER_ENV={environment}\n")
                print(f"✓ 已添加到 {shell_rc}")
    else:
        # 直接设置环境变量
        os.environ["TRADIER_API_TOKEN"] = token
        os.environ["TRADIER_ENV"] = environment
        os.environ["TRADIER_BASE_URL"] = base_url
        print("✓ 环境变量已在当前会话中设置")
        print("  注意：重启 Shell 后需要重新设置（或在 .zshrc/.bashrc 中持久化）")

    return True

def test_tradier_connection():
    """测试 Tradier API 连接"""

    print()
    print("=" * 70)
    print("🧪 测试 Tradier API 连接")
    print("=" * 70)
    print()

    # 检查 Token
    token = os.getenv("TRADIER_API_TOKEN")
    if not token:
        print("❌ TRADIER_API_TOKEN 环境变量未设置")
        return False

    print(f"✓ Token 已设置：{token[:10]}...{token[-4:]}")

    # 检查 requests 库
    try:
        import requests
    except ImportError:
        print("⚠️  requests 库未安装，正在安装...")
        os.system("pip install requests")
        import requests

    # 测试 API 连接
    env = os.getenv("TRADIER_ENV", "sandbox")
    base_url = os.getenv("TRADIER_BASE_URL", "https://sandbox.tradier.com")

    print(f"📡 环境：{env}")
    print(f"📡 URL：{base_url}")
    print()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        # 获取账户信息作为连接测试
        print("测试请求：GET /v1/user/profile")
        response = requests.get(
            f"{base_url}/v1/user/profile",
            headers=headers,
            timeout=10,
        )

        print(f"响应状态码：{response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print("✅ API 连接成功！")
            print(f"   账户信息：{json.dumps(data, indent=2, ensure_ascii=False)}")
            return True

        elif response.status_code == 401:
            print("❌ 认证失败（401）：API Token 无效或已过期")
            print("   请检查 Token 是否正确")
            return False

        elif response.status_code == 429:
            print("⚠️  API 速率限制（429）：请稍后重试")
            return False

        else:
            print(f"❌ API 返回错误 ({response.status_code})：")
            print(f"   {response.text[:200]}")
            return False

    except requests.exceptions.Timeout:
        print("❌ 请求超时：无法连接到 Tradier API")
        print("   请检查网络连接")
        return False

    except requests.exceptions.ConnectionError:
        print("❌ 连接错误：无法连接到 Tradier API")
        print("   请检查 URL 和网络连接")
        return False

    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"❌ 错误：{e}")
        return False

def test_options_analysis():
    """测试期权分析功能"""

    print()
    print("=" * 70)
    print("🧪 测试期权分析功能")
    print("=" * 70)
    print()

    try:
        from options_analyzer import OptionsAgent

        agent = OptionsAgent()
        print(f"测试标的：NVDA")
        print("正在获取期权数据...")

        result = agent.analyze('NVDA', stock_price=145.0)

        print(f"✅ 期权分析成功！")
        print(f"   • 数据源：{result.get('source', 'Unknown')}")
        print(f"   • Options Score：{result['options_score']}/10")
        print(f"   • IV Rank：{result['iv_rank']}")
        print(f"   • P/C Ratio：{result['put_call_ratio']:.2f}")
        print(f"   • Flow Direction：{result['flow_direction']}")
        print(f"   • 异动信号数：{len(result['unusual_activity'])}")

        return True

    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
        print(f"❌ 期权分析失败：{e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主程序"""

    print()

    # 检查是否已配置
    token = os.getenv("TRADIER_API_TOKEN")

    if token and len(token) > 10:
        print("✓ 检测到已配置的 Tradier API Token")
        print()
        choice = input("是否（1）重新配置，（2）测试连接，或（3）测试分析？（默认 2）: ").strip() or "2"

        if choice == "1":
            setup_tradier_token()
            test_tradier_connection()
        elif choice == "2":
            test_tradier_connection()
        elif choice == "3":
            if test_tradier_connection():
                test_options_analysis()
    else:
        # 首次配置
        if setup_tradier_token():
            test_tradier_connection()
            test_options_analysis()

    print()
    print("=" * 70)
    print("✅ 配置完成！")
    print("=" * 70)
    print()
    print("后续步骤：")
    print("  1. 重启您的 Shell（如果使用环境变量方式）")
    print("  2. 运行 python3 -c \"from options_analyzer import OptionsAgent\"")
    print("  3. 在代码中使用：agent = OptionsAgent(); agent.analyze('NVDA')")
    print()

if __name__ == "__main__":
    main()
