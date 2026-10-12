"""`/usr/local/bin/python3 -m alphabot [--port 8765] [--no-poll] [--demo]`

只绑回环地址：给了别的 `--host` ⇒ 退出码 2（账本是私有数据，局域网可见需要另做决定，不是一个参数的事）。
读生产账本需要同 MCP 一样设 `ALPHA_HIVE_HOME`；没设时启动就说清楚读的是哪个目录。
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import tempfile
import webbrowser


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alphabot", description="Alpha Bot —— 卖权选择器本机前端")
    ap.add_argument("--host", default="127.0.0.1", help="只接受回环地址（缺省 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-poll", action="store_true", help="不跑盘中定时快照")
    ap.add_argument("--demo", action="store_true", help="演示模式：合成期权链、不读账本、不写真状态目录")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--from-app", action="store_true", help="由 Alpha Bot.app 启动（关掉它的窗口时服务随之停止）")
    args = ap.parse_args(argv)
    if not _is_loopback(args.host):
        print(f"alphabot: 拒绝绑定非回环地址 {args.host!r}——Alpha Bot 只在本机运行（账本是私有数据）",
              file=sys.stderr)
        return 2

    import uvicorn
    from alphabot.server import create_app
    from alphabot.service import AlphaBotService, IntradayPoller

    if args.demo:
        from alphabot import synthetic as syn
        tmp = tempfile.mkdtemp(prefix="alphabot-demo-")
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tmp,
                              bars_fn=lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t)})
        poller = None
        print(f"alphabot: 演示模式（合成数据；状态写临时目录 {tmp}）", file=sys.stderr)
    else:
        if not os.environ.get("ALPHA_HIVE_HOME"):
            from hive_logger import PATHS
            # v0.45.422 起未设时取缺省数据根 ~/alpha-hive-data（与编排器同址）；此前这里警告「读的是代码目录」
            print(f"alphabot: 未设 ALPHA_HIVE_HOME ⇒ 用缺省数据根 {PATHS.home}"
                  "（与编排器同址；数据根在别处请 export ALPHA_HIVE_HOME=<数据根>）", file=sys.stderr)
        svc = AlphaBotService()
        poller = None if args.no_poll else IntradayPoller(svc)
    holder = {}

    def _stop():
        # 与 Ctrl-C 同一条退出路径：uvicorn 收尾 → lifespan 停盘中轮询
        holder["server"].should_exit = True

    app = create_app(svc, poller=poller, port=args.port, start_poller=poller is not None, on_shutdown=_stop,
                     from_app=args.from_app)
    url = f"http://{args.host}:{args.port}/"
    print(f"alphabot: {url}", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    holder["server"] = server
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
