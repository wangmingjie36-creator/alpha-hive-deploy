#!/usr/bin/env python3
"""
🚀 Agent Toolbox - Python-native MCP replacement
统一的 Agent 工具集：文件系统 + GitHub + 通知
可直接用于蜂群系统，后续升级为真正的 MCP 服务器
"""

import logging as _logging
import os
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import sys

_log = _logging.getLogger("alpha_hive.toolbox")

# ==================== 文件系统工具 ====================

class FilesystemTool:
    """文件系统操作（替代 Filesystem MCP）"""

    ALLOWED_ROOTS = [
        os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__))),
        os.path.expanduser("~/.claude/projects"),
        "/tmp"
    ]

    @staticmethod
    def _is_safe_path(path: str) -> bool:
        """检查路径是否在允许的根目录中"""
        abs_path = str(Path(path).resolve())
        return any(abs_path.startswith(root) for root in FilesystemTool.ALLOWED_ROOTS)

    @classmethod
    def read_file(cls, file_path: str) -> str:
        """读取文件内容"""
        if not cls._is_safe_path(file_path):
            raise PermissionError(f"Path not allowed: {file_path}")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {file_path}")
        except (OSError, UnicodeDecodeError) as e:
            raise OSError(f"Error reading file: {e}") from e

    @classmethod
    def write_file(cls, file_path: str, content: str) -> str:
        """创建/更新文件"""
        if not cls._is_safe_path(file_path):
            raise PermissionError(f"Path not allowed: {file_path}")

        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"✅ File written: {file_path}"
        except OSError as e:
            raise OSError(f"Error writing file: {e}") from e

    @classmethod
    def list_directory(cls, dir_path: str) -> List[Dict[str, Any]]:
        """列出目录内容"""
        if not cls._is_safe_path(dir_path):
            raise PermissionError(f"Path not allowed: {dir_path}")

        try:
            entries = []
            for item in Path(dir_path).iterdir():
                entries.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                    "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat()
                })
            return sorted(entries, key=lambda x: x["name"])
        except FileNotFoundError:
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        except OSError as e:
            raise OSError(f"Error listing directory: {e}") from e

    @classmethod
    def search_files(cls, pattern: str, root: str = None) -> List[str]:
        """全文搜索文件"""
        root = root or cls.ALLOWED_ROOTS[0]
        if not cls._is_safe_path(root):
            raise PermissionError(f"Path not allowed: {root}")

        try:
            results = []
            for file_path in Path(root).rglob("*"):
                if file_path.is_file() and pattern.lower() in file_path.name.lower():
                    results.append(str(file_path))
            return results[:100]  # 限制结果数
        except OSError as e:
            raise OSError(f"Error searching files: {e}") from e


# ==================== GitHub 工具 ====================

class GitHubTool:
    """GitHub 操作（替代 GitHub MCP）"""

    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path or os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__)))

    def run_git_cmd(self, cmd: str) -> Dict[str, Any]:
        """执行 Git 命令"""
        try:
            result = subprocess.run(
                f"cd {self.repo_path} && {cmd}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except (subprocess.SubprocessError, OSError) as e:
            return {"success": False, "error": str(e)}

    def status(self) -> Dict[str, Any]:
        """Git 状态"""
        result = self.run_git_cmd("git status --porcelain")
        if result["success"]:
            files = [line.split()[-1] for line in result["stdout"].split("\n") if line.strip()]
            return {"modified_files": files, "status": "✅ Clean" if not files else "⚠️ Dirty"}
        return {"error": result.get("stderr")}

    def commit(self, message: str) -> Dict[str, Any]:
        """创建提交"""
        stage = self.run_git_cmd("git add -A")
        if not stage["success"]:
            return {"error": f"Failed to stage: {stage['stderr']}"}

        commit = self.run_git_cmd(f"git commit -m '{message}'")
        return {
            "success": commit["success"],
            "message": commit.get("stdout") or commit.get("stderr"),
            "details": commit
        }

    def push(self, branch: str = "main") -> Dict[str, Any]:
        """推送到远程"""
        result = self.run_git_cmd(f"git push origin {branch}")
        return {
            "success": result["success"],
            "output": result.get("stdout") or result.get("stderr")
        }

    def create_issue(self, title: str, body: str) -> Dict[str, Any]:
        """创建 GitHub Issue（需要 gh CLI）"""
        try:
            result = subprocess.run(
                ["gh", "issue", "create", "--title", title, "--body", body],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return {"success": True, "issue_url": result.stdout.strip()}
            else:
                return {"success": False, "error": result.stderr}
        except FileNotFoundError:
            return {"error": "GitHub CLI (gh) not installed"}

    def list_branches(self) -> Dict[str, Any]:
        """列出分支"""
        result = self.run_git_cmd("git branch -a")
        branches = [line.strip() for line in result["stdout"].split("\n") if line.strip()]
        return {"branches": branches}

    def diff(self, branch1: str, branch2: str) -> Dict[str, Any]:
        """查看 diff"""
        result = self.run_git_cmd(f"git diff {branch1}...{branch2}")
        return {
            "diff": result["stdout"],
            "stats": self._parse_diff_stats(result["stdout"])
        }

    @staticmethod
    def _parse_diff_stats(diff: str) -> Dict[str, int]:
        """解析 diff 统计"""
        lines = diff.split("\n")
        additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        return {"additions": additions, "deletions": deletions, "total_changes": additions + deletions}


# ==================== 通知工具 ====================

class NotificationTool:
    """通知工具（替代 Slack + Email MCP）"""

    def __init__(self):
        self.slack_webhook = self._load_slack_webhook()
        self.gmail_creds = self._load_gmail_creds()

    @staticmethod
    def _load_slack_webhook() -> Optional[str]:
        """加载 Slack Webhook"""
        try:
            with open(os.path.expanduser("~/.alpha_hive_slack_webhook"), "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    @staticmethod
    def _load_gmail_creds() -> Optional[Dict]:
        """加载 Gmail 凭据"""
        try:
            creds_path = os.path.expanduser("~/.alpha_hive_gmail_credentials.json")
            if os.path.exists(creds_path):
                with open(creds_path, "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _log.debug("Gmail creds load failed: %s", e)
        return None

    def send_slack_message(self, channel: str, text: str, blocks: List[Dict] = None) -> Dict[str, Any]:
        """发送 Slack 消息"""
        if not self.slack_webhook:
            return {"error": "Slack webhook not configured"}

        try:
            import requests
            payload = {
                "channel": channel,
                "text": text
            }
            if blocks:
                payload["blocks"] = blocks

            response = requests.post(self.slack_webhook, json=payload, timeout=10)
            return {
                "success": response.status_code == 200,
                "status_code": response.status_code,
                "response": response.text
            }
        except ImportError:
            return {"error": "requests library not installed"}
        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            return {"error": str(e)}

    def send_email(self, to: str, subject: str, body: str) -> Dict[str, Any]:
        """发送邮件"""
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            # 从 config 读取
            from config import ALERT_CONFIG

            sender = ALERT_CONFIG["email_config"]["sender_email"]
            password = os.getenv("GMAIL_APP_PASSWORD")

            if not password:
                return {"error": "GMAIL_APP_PASSWORD environment variable not set"}

            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "html"))

            server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            server.login(sender, password)
            server.send_message(msg)
            server.quit()

            return {"success": True, "message": f"Email sent to {to}"}
        except (ImportError, ConnectionError, TimeoutError, OSError, ValueError) as e:
            return {"error": str(e)}

    def notify_all(self, message: str, channels: List[str] = None) -> Dict[str, Any]:
        """发送到多个渠道"""
        results = {}

        if "slack" in (channels or ["slack"]):
            results["slack"] = self.send_slack_message("#alpha-hive", message)

        if "email" in (channels or []):
            results["email"] = self.send_email(
                os.environ.get("ALPHA_HIVE_EMAIL_SENDER", ""),
                "Alpha Hive Alert",
                message
            )

        return results


# ==================== Agent 助手 ====================

class AgentHelper:
    """Agent 使用的统一工具集"""

    def __init__(self):
        self.fs = FilesystemTool()
        self.git = GitHubTool()
        self.notify = NotificationTool()

    def summary(self) -> str:
        """打印工具摘要"""
        return f"""
        🚀 Agent Toolbox 已就绪

        📁 文件系统
           - read_file(path) ✓
           - write_file(path, content) ✓
           - list_directory(path) ✓
           - search_files(pattern) ✓

        🐙 GitHub
           - status() ✓
           - commit(message) ✓
           - push(branch) ✓
           - diff(branch1, branch2) ✓

        🔔 通知
           - send_slack_message(channel, text) ✓
           - send_email(to, subject, body) ✓
           - notify_all(message, channels) ✓
        """


def main():
    """演示用法"""
    helper = AgentHelper()
    print(helper.summary())

    # 测试文件系统
    print("\n测试文件系统...")
    try:
        files = helper.fs.list_directory(os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__))))
        print(f"✅ 列出 {len(files)} 个文件")
    except (OSError, FileNotFoundError) as e:
        print(f"❌ {e}")

    # 测试 GitHub
    print("\nGit 状态...")
    status = helper.git.status()
    print(f"✅ {status}")

    # 测试通知
    print("\n测试通知...")
    result = helper.notify.send_slack_message("#alpha-hive", "🧪 Agent Toolbox 测试")
    if result.get("success"):
        print("✅ Slack 消息已发送")
    else:
        print(f"⚠️ {result.get('error', 'Unknown error')}")


if __name__ == "__main__":
    main()
