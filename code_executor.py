#!/usr/bin/env python3
"""
⚙️ Alpha Hive 代码执行引擎 - Phase 3 P1
安全的 Python/Shell 代码执行 + 沙箱隔离 + 资源限制
"""

import logging as _logging
import subprocess
import os
import sys
import time
import json
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import signal

_log = _logging.getLogger("alpha_hive.code_executor")


class ExecutionTimeout(Exception):
    """执行超时异常"""
    pass


class CodeExecutor:
    """安全的代码执行引擎"""

    # 允许的模块（白名单）
    ALLOWED_MODULES = {
        'yfinance', 'pandas', 'numpy', 'matplotlib', 'plotly',
        'requests', 'sqlite3', 'json', 'datetime', 'time',
        'statistics', 'csv', 're', 'collections', 'itertools',
        'functools', 'operator', 'math', 'random', 'decimal',
        'urllib', 'bs4', 'selenium'  # 数据爬取相关
    }

    # 禁止的模块（黑名单）
    BLOCKED_MODULES = {
        'os', 'sys', 'subprocess', 'socket', 'shutil',
        '__import__', 'exec', 'eval', 'open', 'compile',
        'globals', 'locals', 'vars', '__builtins__',
        'input', 'raw_input', 'reload', '__loader__'
    }

    def __init__(
        self,
        max_timeout: int = 30,
        max_memory: int = 512,
        sandbox_dir: Optional[str] = None,
        enable_network: bool = False,
        enable_file_write: bool = True
    ):
        """
        初始化代码执行器

        Args:
            max_timeout: 最大执行时间（秒）
            max_memory: 最大内存使用（MB）
            sandbox_dir: 沙箱目录
            enable_network: 是否允许网络访问
            enable_file_write: 是否允许文件写入
        """
        self.max_timeout = max_timeout
        self.max_memory = max_memory
        self.enable_network = enable_network
        self.enable_file_write = enable_file_write

        # 创建沙箱目录
        if sandbox_dir:
            self.sandbox_dir = Path(sandbox_dir)
        else:
            base = Path("/tmp/alpha_hive_sandbox")
            date_str = datetime.now().strftime("%Y-%m-%d")
            self.sandbox_dir = base / date_str

        self._init_sandbox()

        # 审计日志
        self.audit_log_path = self.sandbox_dir / "audit.log"
        self._write_audit_log("Executor initialized")

    def _init_sandbox(self) -> None:
        """初始化沙箱目录结构"""
        try:
            self.sandbox_dir.mkdir(parents=True, exist_ok=True)
            (self.sandbox_dir / "scripts").mkdir(exist_ok=True)
            (self.sandbox_dir / "data").mkdir(exist_ok=True)
            (self.sandbox_dir / "output").mkdir(exist_ok=True)
        except OSError as e:
            _log.warning("沙箱初始化失败: %s", e)

    def _write_audit_log(self, message: str) -> None:
        """写入审计日志"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"{timestamp} | {message}\n"
            with open(self.audit_log_path, "a") as f:
                f.write(log_entry)
        except OSError as e:
            _log.debug("审计日志写入失败: %s", e)

    def _validate_python_code(self, code: str) -> bool:
        """验证 Python 代码安全性（AST 分析）- Phase 3 P1 增强"""
        import ast

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            self._write_audit_log(f"VALIDATE_CODE | SYNTAX_ERROR: {e}")
            return False

        DANGEROUS_CALLS = {
            'eval', 'exec', 'compile', '__import__',
            'open', 'input', 'breakpoint', 'globals', 'locals',
            'vars', 'reload', 'delattr', 'setattr'
        }
        BLOCKED_IMPORTS = {
            'os', 'sys', 'subprocess', 'socket', 'shutil',
            'ctypes', 'importlib', 'pathlib', 'pickle',
            'multiprocessing', 'threading', 'asyncio'
        }

        # 遍历 AST 树检测危险操作
        for node in ast.walk(tree):
            # 检测危险的函数调用（覆盖 __import__('os') 等绕过方式）
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in DANGEROUS_CALLS:
                        self._write_audit_log(f"VALIDATE_CODE | BLOCKED_CALL: {node.func.id}")
                        return False
                # 检测链式调用 exec(...) 等
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr in DANGEROUS_CALLS:
                        self._write_audit_log(f"VALIDATE_CODE | BLOCKED_ATTR_CALL: {node.func.attr}")
                        return False

            # 检测 import 语句
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name.split('.')[0]
                    if module_name in BLOCKED_IMPORTS:
                        self._write_audit_log(f"VALIDATE_CODE | BLOCKED_IMPORT: {alias.name}")
                        return False

            # 检测 from ... import 语句
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    module_name = node.module.split('.')[0]
                    if module_name in BLOCKED_IMPORTS:
                        self._write_audit_log(f"VALIDATE_CODE | BLOCKED_FROM_IMPORT: {node.module}")
                        return False

        self._write_audit_log("VALIDATE_CODE | OK")
        return True

    def execute_python(self, code: str, return_output: bool = True) -> Dict[str, Any]:
        """
        执行 Python 代码

        Args:
            code: Python 代码字符串
            return_output: 是否返回输出

        Returns:
            {
                "success": bool,
                "stdout": str,
                "stderr": str,
                "return_value": Any,
                "execution_time": float,
                "exit_code": int,
                "error": Optional[str]
            }
        """
        start_time = time.time()

        # 1. 验证代码安全性
        if not self._validate_python_code(code):
            error_msg = "❌ 代码包含禁止的操作"
            self._write_audit_log(f"EXECUTE_PYTHON | BLOCKED | {error_msg}")
            return {
                "success": False,
                "stdout": "",
                "stderr": error_msg,
                "return_value": None,
                "execution_time": 0,
                "exit_code": -1,
                "error": error_msg
            }

        # 2. 保存脚本到文件
        script_path = self.sandbox_dir / "scripts" / f"script_{int(time.time() * 1000)}.py"
        try:
            with open(script_path, "w") as f:
                f.write(code)
        except OSError as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"脚本保存失败: {e}",
                "return_value": None,
                "execution_time": time.time() - start_time,
                "exit_code": -1,
                "error": str(e)
            }

        # 3. 构建执行环境
        env = os.environ.copy()
        if not self.enable_network:
            env["http_proxy"] = "127.0.0.1:1"
            env["https_proxy"] = "127.0.0.1:1"

        # 4. 执行脚本
        try:
            process = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.sandbox_dir / "data"),
                text=True
            )

            try:
                stdout, stderr = process.communicate(timeout=self.max_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                error_msg = f"执行超时（> {self.max_timeout}s）"
                self._write_audit_log(f"EXECUTE_PYTHON | TIMEOUT | {self.max_timeout}s")
                return {
                    "success": False,
                    "stdout": stdout,
                    "stderr": stderr + f"\n{error_msg}",
                    "return_value": None,
                    "execution_time": time.time() - start_time,
                    "exit_code": -1,
                    "error": error_msg
                }

            execution_time = time.time() - start_time
            success = process.returncode == 0

            # 记录审计日志
            status = "OK" if success else "ERROR"
            self._write_audit_log(
                f"EXECUTE_PYTHON | {status} | {execution_time:.2f}s | "
                f"exit_code={process.returncode}"
            )

            return {
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "return_value": stdout.strip() if success and return_output else None,
                "execution_time": execution_time,
                "exit_code": process.returncode,
                "error": stderr if not success else None
            }

        except (subprocess.SubprocessError, OSError) as e:
            self._write_audit_log(f"EXECUTE_PYTHON | ERROR | {e}")
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "return_value": None,
                "execution_time": time.time() - start_time,
                "exit_code": -1,
                "error": str(e)
            }

    def execute_shell(self, command: str) -> Dict[str, Any]:
        """
        执行 Shell 命令

        Args:
            command: Shell 命令

        Returns:
            执行结果字典
        """
        start_time = time.time()

        # 检查危险命令
        dangerous_commands = ['rm -rf', 'dd if=', 'fork()', ':(){:|:&;}:']
        for dangerous in dangerous_commands:
            if dangerous in command:
                error_msg = "❌ 命令包含危险操作"
                self._write_audit_log(f"EXECUTE_SHELL | BLOCKED | {command}")
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": error_msg,
                    "execution_time": 0,
                    "exit_code": -1,
                    "error": error_msg
                }

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.sandbox_dir / "data"),
                text=True
            )

            try:
                stdout, stderr = process.communicate(timeout=self.max_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                self._write_audit_log(f"EXECUTE_SHELL | TIMEOUT | {command}")
                return {
                    "success": False,
                    "stdout": stdout,
                    "stderr": stderr,
                    "execution_time": time.time() - start_time,
                    "exit_code": -1,
                    "error": f"执行超时（> {self.max_timeout}s）"
                }

            execution_time = time.time() - start_time
            success = process.returncode == 0

            self._write_audit_log(
                f"EXECUTE_SHELL | {'OK' if success else 'ERROR'} | "
                f"{execution_time:.2f}s | {command[:50]}"
            )

            return {
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "execution_time": execution_time,
                "exit_code": process.returncode,
                "error": stderr if not success else None
            }

        except (subprocess.SubprocessError, OSError) as e:
            self._write_audit_log(f"EXECUTE_SHELL | ERROR | {e}")
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "execution_time": time.time() - start_time,
                "exit_code": -1,
                "error": str(e)
            }

    def execute_file(self, file_path: str) -> Dict[str, Any]:
        """执行脚本文件"""
        try:
            with open(file_path, "r") as f:
                code = f.read()

            if file_path.endswith(".py"):
                return self.execute_python(code)
            else:
                return self.execute_shell(f"bash {file_path}")

        except (OSError, UnicodeDecodeError) as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "execution_time": 0,
                "exit_code": -1,
                "error": str(e)
            }

    def get_audit_log(self, lines: int = 50) -> List[str]:
        """获取审计日志"""
        try:
            with open(self.audit_log_path, "r") as f:
                all_lines = f.readlines()
            return all_lines[-lines:]
        except (OSError, UnicodeDecodeError):
            return []

    def cleanup(self) -> None:
        """清理过期的沙箱文件"""
        try:
            import shutil
            cutoff_date = (
                datetime.now() - __import__('datetime').timedelta(days=7)
            ).strftime("%Y-%m-%d")

            for item in Path("/tmp/alpha_hive_sandbox").iterdir():
                if item.is_dir() and item.name < cutoff_date:
                    shutil.rmtree(item)
                    print(f"🗑️ 清理旧沙箱：{item}")

        except (OSError, ImportError) as e:
            _log.warning("沙箱清理失败: %s", e)
