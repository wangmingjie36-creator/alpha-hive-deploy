#!/usr/bin/env python3
"""
🐛 Alpha Hive 调试器 - Phase 3 P1
代码错误解析 + 自动修复建议 + 自动重试
"""

import logging as _logging
import re
from typing import Dict, List, Any, Optional, Tuple
from code_executor import CodeExecutor

_log = _logging.getLogger("alpha_hive.debugger")


class Debugger:
    """代码调试与错误处理"""

    # 常见错误类型映射
    ERROR_PATTERNS = {
        "ModuleNotFoundError": {
            "pattern": r"ModuleNotFoundError: No module named '(\w+)'",
            "suggestion": "安装缺失模块：pip install {module}",
            "severity": "high"
        },
        "ImportError": {
            "pattern": r"ImportError: (.*)",
            "suggestion": "检查导入语句和模块安装",
            "severity": "high"
        },
        "AttributeError": {
            "pattern": r"AttributeError: (.*)",
            "suggestion": "检查对象属性是否存在",
            "severity": "medium"
        },
        "KeyError": {
            "pattern": r"KeyError: '(\w+)'",
            "suggestion": "字典中缺少键 '{key}'，检查数据结构",
            "severity": "medium"
        },
        "ValueError": {
            "pattern": r"ValueError: (.*)",
            "suggestion": "检查输入值是否有效",
            "severity": "medium"
        },
        "TypeError": {
            "pattern": r"TypeError: (.*)",
            "suggestion": "检查数据类型是否匹配",
            "severity": "medium"
        },
        "IndexError": {
            "pattern": r"IndexError: (.*)",
            "suggestion": "索引超出范围，检查列表长度",
            "severity": "low"
        },
        "ZeroDivisionError": {
            "pattern": r"ZeroDivisionError: (.*)",
            "suggestion": "检查除数是否为 0",
            "severity": "high"
        },
        "ConnectionError": {
            "pattern": r"ConnectionError: (.*)",
            "suggestion": "网络连接失败，检查网络或 URL",
            "severity": "high"
        },
        "TimeoutError": {
            "pattern": r"TimeoutError: (.*)",
            "suggestion": "请求超时，增加超时时间或检查网络",
            "severity": "medium"
        }
    }

    @staticmethod
    def parse_error(stderr: str) -> Dict[str, Any]:
        """
        解析错误信息

        Args:
            stderr: 错误输出字符串

        Returns:
            {
                "error_type": str,
                "line_number": Optional[int],
                "message": str,
                "suggestion": str,
                "severity": str,  # "low", "medium", "high"
                "traceback": List[str]
            }
        """
        lines = stderr.strip().split("\n")
        traceback_lines = [l for l in lines if l.strip()]

        # 解析错误类型
        error_type = "UnknownError"
        line_number = None
        message = ""

        for line in lines:
            # 查找错误类型
            for err_name in Debugger.ERROR_PATTERNS.keys():
                if err_name in line:
                    error_type = err_name
                    # 提取消息
                    if ":" in line:
                        message = line.split(":", 1)[1].strip()
                    break

            # 查找行号
            match = re.search(r'line (\d+)', line)
            if match:
                line_number = int(match.group(1))

        # 生成建议
        suggestion = Debugger._generate_suggestion(error_type, message)
        severity = Debugger.ERROR_PATTERNS.get(
            error_type, {}
        ).get("severity", "medium")

        return {
            "error_type": error_type,
            "line_number": line_number,
            "message": message,
            "suggestion": suggestion,
            "severity": severity,
            "traceback": traceback_lines
        }

    @staticmethod
    def _generate_suggestion(error_type: str, message: str) -> str:
        """生成修复建议"""
        if error_type not in Debugger.ERROR_PATTERNS:
            return "无法识别的错误，请手动检查"

        pattern_info = Debugger.ERROR_PATTERNS[error_type]
        suggestion = pattern_info["suggestion"]

        # 替换占位符
        if "{module}" in suggestion:
            match = re.search(r"No module named '(\w+)'", message)
            if match:
                suggestion = suggestion.format(module=match.group(1))

        if "{key}" in suggestion:
            match = re.search(r"'(\w+)'", message)
            if match:
                suggestion = suggestion.format(key=match.group(1))

        return suggestion

    @staticmethod
    def suggest_fix(error: Dict[str, Any], code: str) -> str:
        """
        建议代码修复

        Args:
            error: 错误字典（来自 parse_error）
            code: 原始代码

        Returns:
            修复后的代码建议
        """
        error_type = error["error_type"]
        message = error["message"]

        if error_type == "ModuleNotFoundError":
            match = re.search(r"No module named '(\w+)'", message)
            if match:
                module = match.group(1)
                # 在代码开始添加导入注释
                suggestion = f"# 需要安装: pip install {module}\n\n{code}"
                return suggestion

        elif error_type == "KeyError":
            match = re.search(r"'(\w+)'", message)
            if match:
                key = match.group(1)
                suggestion = code.replace(
                    f"['{key}']",
                    f".get('{key}', 'N/A')  # 使用 get() 避免 KeyError"
                )
                return suggestion

        elif error_type == "IndexError":
            # 建议添加长度检查
            suggestion = "# 建议添加长度检查:\n"
            suggestion += "if len(data) > 0:\n"
            for line in code.split("\n"):
                suggestion += f"    {line}\n"
            return suggestion

        elif error_type == "ZeroDivisionError":
            # 建议添加除以 0 检查
            return code.replace(
                "/ ",
                "/ (value if value != 0 else 1)  # 避免除以 0\n"
            )

        return code

    @staticmethod
    def auto_retry(
        code: str,
        executor: Optional[CodeExecutor] = None,
        max_attempts: int = 3
    ) -> Dict[str, Any]:
        """
        自动重试机制

        Args:
            code: Python 代码
            executor: CodeExecutor 实例
            max_attempts: 最大重试次数

        Returns:
            {
                "success": bool,
                "result": Dict,  # 最后执行结果
                "attempts": int,
                "modifications": List[str]
            }
        """
        if executor is None:
            executor = CodeExecutor()

        modifications = []
        current_code = code
        attempt = 0

        for attempt in range(max_attempts):
            # 执行代码
            result = executor.execute_python(current_code)

            if result["success"]:
                return {
                    "success": True,
                    "result": result,
                    "attempts": attempt + 1,
                    "modifications": modifications
                }

            # 解析错误
            error = Debugger.parse_error(result["stderr"])
            pass  # 尝试 {attempt + 1} 失败

            # 生成修复建议
            suggested_fix = Debugger.suggest_fix(error, current_code)

            if suggested_fix != current_code:
                modifications.append(f"Attempt {attempt + 1}: 修复 {error['error_type']}")
                current_code = suggested_fix
                pass  # 应用修复
            else:
                # 无法自动修复，返回失败
                return {
                    "success": False,
                    "result": result,
                    "attempts": attempt + 1,
                    "modifications": modifications
                }

        return {
            "success": False,
            "result": result,
            "attempts": max_attempts,
            "modifications": modifications
        }

    @staticmethod
    def validate_code(code: str) -> Tuple[bool, List[str]]:
        """
        代码静态验证

        Args:
            code: Python 代码

        Returns:
            (是否有效, 警告列表)
        """
        warnings = []

        # 检查语法
        try:
            compile(code, "<string>", "exec")
        except SyntaxError as e:
            return False, [f"语法错误: {e}"]

        # 检查常见问题
        if "import os" in code or "os.system" in code:
            warnings.append("⚠️ 警告：代码包含 os 模块，可能存在安全风险")

        if "eval(" in code or "exec(" in code:
            warnings.append("⚠️ 警告：代码包含 eval/exec，存在安全风险")

        if "open(" in code and "r" not in code:
            warnings.append("⚠️ 警告：代码打开文件进行写入，请检查是否必要")

        if "while True:" in code:
            warnings.append("⚠️ 警告：代码包含无限循环，确保有退出条件")

        # 检查未定义变量
        try:
            tree = __import__("ast").parse(code)
            defined_vars = set()
            used_vars = set()

            for node in __import__("ast").walk(tree):
                if isinstance(node, __import__("ast").Assign):
                    for target in node.targets:
                        if isinstance(target, __import__("ast").Name):
                            defined_vars.add(target.id)
                elif isinstance(node, __import__("ast").Name):
                    used_vars.add(node.id)

            undefined = used_vars - defined_vars
            if undefined:
                warnings.append(f"⚠️ 警告：可能的未定义变量: {undefined}")

        except (SyntaxError, ValueError, TypeError) as exc:
            _log.debug("AST 解析失败: %s", exc)

        return len(warnings) == 0, warnings

    @staticmethod
    def get_error_summary(stderr: str) -> str:
        """获取错误摘要"""
        lines = stderr.strip().split("\n")
        error_line = next(
            (l for l in lines if any(e in l for e in Debugger.ERROR_PATTERNS.keys())),
            "未知错误"
        )
        return error_line
