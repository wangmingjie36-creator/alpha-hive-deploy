"""Alpha Bot HTTP 层（Starlette）：静态前端 + JSON 接口。业务全在 `alphabot.service`。

安全边界（本机工具，但浏览器里别的网页也能向 127.0.0.1 发请求）：
  · 只绑回环地址（`alphabot.__main__` 拒绝其它 host）；
  · **Host 头白名单**：挡 DNS rebinding（恶意域名解析到 127.0.0.1 后读私有账本）；
  · 改状态的请求（PUT / POST）必须带自定义头 `X-AlphaBot: 1`——跨站表单 / fetch 带不上它而不触发预检，
    本服务不应答任何 CORS 预检 ⇒ 跨站写不进来；
  · 响应带 CSP（只许本源脚本、Google Fonts 样式与字体）。
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from alphabot import __version__
from alphabot.chat import ChatEngine, ChatError
from alphabot.service import (AlphaBotService, BadRequest, IntradayPoller, MAX_FOCUS, clean_json,
                              market_session)
from hive_logger import get_logger

_log = get_logger("alphabot.server")

# 静态前端随代码发布 ⇒ `__file__` 锚定才对（CLAUDE.md「指向代码还是数据」那张表）
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _json(obj, status: int = 200) -> Response:
    body = json.dumps(clean_json(obj), ensure_ascii=False, allow_nan=False, default=str)
    return Response(body, status_code=status, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


def create_app(service: Optional[AlphaBotService] = None, *, poller: Optional[IntradayPoller] = None,
               port: int = 8765, start_poller: bool = False,
               on_shutdown: Optional[Callable[[], None]] = None, from_app: bool = False,
               chat: Optional[ChatEngine] = None) -> Starlette:
    """`on_shutdown`：页面「停止服务」调它（`alphabot.__main__` 传入 uvicorn 的退出开关）；
    不传 ⇒ `/api/shutdown` 答 400，页面也不显示该按钮。
    `from_app`：由 Alpha Bot.app 起的（`--from-app`）——`/api/ping` 报出来，启动器据此决定关窗时停不停它；
    终端里起的服务关窗不停。
    `chat`：对话引擎（v0.45.614），只给测试换（配假的 API 客户端）；缺省按本服务建一个。"""
    svc = service or AlphaBotService()
    chat = chat or ChatEngine(svc)
    allowed_hosts = {f"{h}:{port}" for h in LOOPBACK_HOSTS} | set(LOOPBACK_HOSTS)

    def _q(request: Request, name: str) -> Optional[str]:
        v = request.query_params.get(name)
        return v if v not in (None, "") else None

    def api(fn):
        def endpoint(request: Request):
            try:
                return _json(fn(request))
            except BadRequest as exc:
                body = {"error": str(exc)}
                if getattr(exc, "param", None):
                    body["param"] = exc.param
                return _json(body, 400)
        endpoint.__name__ = fn.__name__
        return endpoint

    @api
    def meta(request):
        return {"app": "Alpha Bot", "version": __version__, "demo": svc.demo,
                "session": market_session(), "settings": svc.settings(), "max_focus": MAX_FOCUS,
                "watchlist": svc.watchlist(), "ledger": svc.ledger_dates(),
                "alphabot_state": str(svc.state_dir()),
                "poller": dict(poller.status) if poller else {"running": False, "disabled": True},
                "stats": dict(svc.stats), "can_shutdown": on_shutdown is not None}

    @api
    def ping(request):
        # 桌面启动器（`alphabot.launcher`）靠它认出「端口上跑的就是 Alpha Bot」——轻量，不碰账本
        return {"app": "alphabot", "version": __version__, "demo": svc.demo, "pid": os.getpid(),
                "can_shutdown": on_shutdown is not None, "from_app": from_app}

    def shutdown(request: Request):
        if on_shutdown is None:
            return _json({"error": "本进程不支持从页面停止（在启动它的终端里 Ctrl-C）"}, 400)
        on_shutdown()
        return _json({"stopping": True})

    @api
    def live(request):
        return svc.live(request.path_params["ticker"], force=_q(request, "force") == "1")

    @api
    def overview(request):
        return svc.overview(_q(request, "date"))

    @api
    def ledger_dates(request):
        return svc.ledger_dates()

    @api
    def ledger_rows(request):
        d = _q(request, "date")
        if d is None:
            raise BadRequest("缺 date")
        return svc.ledger_rows(d, full=_q(request, "full") == "1")

    @api
    def ledger_ticker(request):
        d = _q(request, "date")
        if d is None:
            raise BadRequest("缺 date")
        return svc.ticker_ledger(request.path_params["ticker"], d)

    @api
    def assess(request):
        return svc.assess()

    @api
    def history(request):
        return svc.env_history(request.path_params["ticker"])

    @api
    def bars(request):
        return svc.bars(request.path_params["ticker"])

    @api
    def premium(request):
        return svc.premium(request.path_params["ticker"], dict(request.query_params))

    @api
    def method(request):
        return svc.meta_texts()

    @api
    def intraday(request):
        return svc.intraday(request.path_params["ticker"], _q(request, "date"))

    @api
    def intraday_dates(request):
        return svc.intraday_dates(request.path_params["ticker"])

    @api
    def straddle(request):
        return svc.straddle()

    @api
    def hedge(request):
        return svc.hedge()

    @api
    def hedge_tail(request):
        return svc.tail_hedge(force=_q(request, "force") == "1")

    @api
    def hedge_beta(request):
        return svc.beta_sensitivity(force=_q(request, "force") == "1")

    @api
    def straddle_live(request):
        return svc.straddle_live(request.path_params["ticker"], force=_q(request, "force") == "1")

    @api
    def straddle_compare(request):
        return svc.straddle_compare(request.path_params["ticker"], _q(request, "as_of"))

    async def settings(request: Request):
        if request.method == "GET":
            return _json(svc.settings())
        try:
            patch = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _json({"error": "请求体须为 JSON"}, 400)
        try:
            from starlette.concurrency import run_in_threadpool
            return _json(await run_in_threadpool(svc.update_settings, patch))
        except BadRequest as exc:
            return _json({"error": str(exc)}, 400)

    async def chat_api(request: Request):
        """GET = 状态（开没开、今天花了多少、上限）；POST = 问一句（同步调 API，可能要一两分钟——放线程池）。"""
        from starlette.concurrency import run_in_threadpool
        if request.method == "GET":
            return _json(await run_in_threadpool(chat.status))
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _json({"error": "请求体须为 JSON"}, 400)
        if not isinstance(body, dict):
            return _json({"error": "请求体须为 JSON 对象"}, 400)
        try:
            return _json(await run_in_threadpool(chat.send, body.get("message"), body.get("conversation_id"),
                                                 body.get("page")))
        except ChatError as exc:
            return _json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001 - API 报错 / 网络 / 记账失败：原样说出类型与原因，不吞
            _log.warning("对话失败：%s: %s", type(exc).__name__, exc)
            return _json({"error": f"对话失败：{type(exc).__name__}: {exc}"}, 502)

    def snap_now(request: Request):
        if svc.demo:
            return _json({"error": "演示模式不写快照"}, 400)
        try:
            return _json(svc.take_snapshot(request.path_params["ticker"]))
        except BadRequest as exc:
            return _json({"error": str(exc)}, 400)

    def index(request: Request):
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    routes = [
        Route("/", index),
        Route("/api/meta", meta),
        Route("/api/ping", ping),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/live/{ticker}", live),
        Route("/api/overview", overview),
        Route("/api/ledger/dates", ledger_dates),
        Route("/api/ledger/rows", ledger_rows),
        Route("/api/ledger/ticker/{ticker}", ledger_ticker),
        Route("/api/assess", assess),
        Route("/api/history/{ticker}", history),
        Route("/api/bars/{ticker}", bars),
        Route("/api/premium/{ticker}", premium),
        Route("/api/method", method),
        Route("/api/intraday/{ticker}", intraday),
        Route("/api/intraday-dates/{ticker}", intraday_dates),
        Route("/api/intraday/{ticker}/snap", snap_now, methods=["POST"]),
        Route("/api/straddle", straddle),
        Route("/api/hedge", hedge),
        Route("/api/hedge/tail", hedge_tail),
        Route("/api/hedge/beta", hedge_beta),
        Route("/api/straddle/live/{ticker}", straddle_live),
        Route("/api/straddle/compare/{ticker}", straddle_compare),
        Route("/api/settings", settings, methods=["GET", "PUT"]),
        Route("/api/chat", chat_api, methods=["GET", "POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    @asynccontextmanager
    async def lifespan(app):
        if poller is not None and start_poller:
            poller.start()
        try:
            yield
        finally:
            if poller is not None:
                poller.stop()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.service = svc
    app.state.poller = poller
    return _Guard(app, allowed_hosts)


class _Guard:
    """纯 ASGI 中间件（不用装饰器 / BaseHTTPMiddleware：Starlette 0.x 与 1.x 两边都得能跑）。"""

    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.state = app.state            # 测试经 app.state 拿 service / poller

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        host = headers.get("host", "").lower()
        method = scope.get("method", "GET")
        deny = None
        if host not in self.allowed_hosts:
            deny = (403, {"error": f"Host {host!r} 不在本机白名单（防 DNS rebinding）"})
        elif method == "OPTIONS":
            deny = (405, {"error": "不应答 CORS 预检"})
        elif method not in ("GET", "HEAD") and headers.get("x-alphabot") != "1":
            deny = (403, {"error": "写请求须带 X-AlphaBot: 1"})
        if deny is not None:
            return await JSONResponse(deny[1], status_code=deny[0])(scope, receive, send)

        # /static/ 的 JS / CSS 必须每次向服务端核对（no-cache + ETag ⇒ 没变时 304）。此前不带 Cache-Control，浏览器 /
        # 程序窗口按「启发式新鲜度」直接用缓存：升级后 index.html（no-store）是新的、app.js 却是旧的 ⇒ 新导航链接点开
        # 「没有这个页面」（v0.45.459 二次检查在浏览器里实际复现）。本机服务，核对一次只是一个 304。
        is_static = str(scope.get("path") or "").startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers") or [])
                hs += [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
                       (b"referrer-policy", b"no-referrer")]
                if is_static and not any(k.lower() == b"cache-control" for k, _ in hs):
                    hs.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": hs}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
