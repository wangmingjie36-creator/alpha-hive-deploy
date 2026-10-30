"""假的 `anthropic` 模块：CI 刻意不装 anthropic（requirements.txt 第 28 行，v0.45.601），测试一律用假模块。

v0.45.683 的 8 条测试直接 `import anthropic` 取真异常类，本机装着所以绿，CI 上全红（v0.45.688 改）。

类层级抄自真 SDK 0.84.0（2026-10-29 在装了它的开发机上逐个核对过 MRO）：
    AnthropicError ← APIError ← APIConnectionError ← APITimeoutError
                            ← APIStatusError ← BadRequestError / RateLimitError
被测代码关心的只有这几件事：`APITimeoutError` 是 `APIConnectionError` 的子类、两者都不是内置
`ConnectionError / TimeoutError / OSError / ValueError` 的子类、`BadRequestError` 属于状态错误而不是连接错误。
SDK 升级后若层级变了，这里要跟着改——没有第二份核对（CI 里没有真 SDK 可比），换版本时记得再核一遍 MRO。
"""
import sys
import types


class AnthropicError(Exception):
    pass


class APIError(AnthropicError):
    def __init__(self, message="", request=None, *, body=None):
        super().__init__(message)
        self.request = request
        self.body = body


class APIConnectionError(APIError):
    def __init__(self, *, message="Connection error.", request=None):
        super().__init__(message, request)


class APITimeoutError(APIConnectionError):
    def __init__(self, request=None):
        super().__init__(message="Request timed out.", request=request)


class APIStatusError(APIError):
    def __init__(self, message="", *, response=None, body=None):
        super().__init__(message, getattr(response, "request", None), body=body)
        self.response = response
        self.status_code = getattr(response, "status_code", None)


class BadRequestError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


def make_module() -> types.ModuleType:
    mod = types.ModuleType("anthropic")
    for cls in (AnthropicError, APIError, APIConnectionError, APITimeoutError, APIStatusError,
                BadRequestError, RateLimitError):
        setattr(mod, cls.__name__, cls)
    return mod


def install(monkeypatch) -> types.ModuleType:
    """把假模块装进 `sys.modules["anthropic"]`（测试结束自动还原）。返回它，测试里用 `mod.APITimeoutError(...)`。"""
    mod = make_module()
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod
