"""Alpha Bot 的原生窗口（pywebview / WKWebView）：`alphabot.launcher` 在服务就绪后调 `run_window`。

只在 macOS 上用，pywebview 是可选依赖：`unavailable_reason()` 非 None 时启动器退回浏览器。

身份：.app 的 bash 壳 exec 的是 python.org 的 `python3`，它为了拿 GUI 权限会转进框架里的 `Python.app`，
窗口一起来进程就被 LaunchServices 登记成「Python」（火箭图标，实测）。所以在 NSApplication 初始化**之前**
改内存里的 bundle 信息（名称 + bundle id），起来之后再换 Dock 图标——实测登记名变成「Alpha Bot」。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from alphabot.macos_app import APP_NAME, BUNDLE_ID

WATCH_INTERVAL_SEC = 1.5
WATCH_MISSES_TO_CLOSE = 2          # 连续两次探不到服务才关窗：一次超时不算


def unavailable_reason() -> Optional[str]:
    """None = 能开原生窗口；否则是退回浏览器的原因（写进 launcher.log）。"""
    try:
        import webview  # noqa: F401
        from AppKit import NSApplication  # noqa: F401
    except Exception as exc:  # noqa: BLE001 —— ImportError 之外 pyobjc 还可能抛别的；原因照实记
        return f"{type(exc).__name__}: {exc}"
    return None


def _icon_path() -> Path:
    # 代码锚点：图标随代码发布
    return Path(__file__).resolve().parent / "macos" / "AlphaBot.icns"


def _adopt_identity() -> None:
    """必须在 NSApplication 初始化之前：登记进 LaunchServices 的名字 / bundle id 就是这时读的。"""
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary()
    info["CFBundleName"] = APP_NAME
    info["CFBundleDisplayName"] = APP_NAME
    info["CFBundleIdentifier"] = BUNDLE_ID


def _set_dock_icon() -> None:
    from AppKit import NSApplication, NSImage
    img = NSImage.alloc().initWithContentsOfFile_(str(_icon_path()))
    if img is not None:
        NSApplication.sharedApplication().setApplicationIconImage_(img)


def watch_server(is_alive: Callable[[], bool], on_gone: Callable[[], None], stop: threading.Event,
                 interval: float = WATCH_INTERVAL_SEC, misses_to_close: int = WATCH_MISSES_TO_CLOSE) -> None:
    """服务没了（页面「停止服务」、进程被杀）⇒ 调 `on_gone` 关窗；`stop` 置位即退出。纯逻辑，便于测试。"""
    misses = 0
    while not stop.wait(interval):
        misses = 0 if is_alive() else misses + 1
        if misses >= misses_to_close:
            on_gone()
            return


_quit_observer = None                    # 留住引用：NSNotificationCenter 不 retain 观察者


def _on_terminate(callback: Callable[[], None]) -> None:
    """⌘Q 走 `NSApplication terminate:`：pywebview 答应退出后 Cocoa **直接 exit()**，`webview.start()` 永不返回
    （实测）——关窗后的收尾（停服务）只能挂在 willTerminate 通知上。"""
    global _quit_observer
    from AppKit import NSApplicationWillTerminateNotification
    from Foundation import NSNotificationCenter, NSObject

    class _QuitObserver(NSObject):
        def appWillTerminate_(self, _note):
            callback()

    _quit_observer = _QuitObserver.alloc().init()
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        _quit_observer, "appWillTerminate:", NSApplicationWillTerminateNotification, None)


def run_window(url: str, is_alive: Callable[[], bool], on_quit: Optional[Callable[[], None]] = None, *,
               title: str = APP_NAME) -> None:
    """阻塞到窗口关掉（关窗 / 服务没了自动关）后返回。⌘Q 不返回（进程直接退出），只调 `on_quit`。必须在主线程调。"""
    _adopt_identity()
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True   # 缺省 False：页面里 <a download>（导出 CSV）会静默无效；放行后进「下载」
    _set_dock_icon()                     # 主线程上设（AppKit 不保证线程安全）；NSApp 是单例，pywebview 沿用它
    if on_quit is not None:
        _on_terminate(on_quit)
    window = webview.create_window(title, url, width=1440, height=920, min_size=(960, 640))
    stop = threading.Event()

    def started():                       # pywebview 在后台线程里跑它
        watch_server(is_alive, window.destroy, stop)

    try:
        webview.start(started)
    finally:
        stop.set()
