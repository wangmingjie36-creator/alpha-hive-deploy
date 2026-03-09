"""
report_web_assets - PWA / RSS / HTML 生成模块

从 AlphaHiveDailyReporter 提取的 Web 资源生成方法。
每个函数接收 reporter 实例（原 self）作为第一个参数。
"""

import json
from typing import Dict
from datetime import datetime
from hive_logger import get_logger

_log = get_logger("report_web_assets")


def write_pwa_files(reporter):
    """生成 manifest.json + sw.js"""
    import json as _json2

    # ── manifest.json ──
    icon_svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cpolygon points='50,5 93,28 93,72 50,95 7,72 7,28' fill='%23F4A532'/%3E%3Cg transform='translate(26,24) scale(3)'%3E%3Crect x='6' y='1' width='4' height='1' fill='%23333'/%3E%3Crect x='4' y='2' width='2' height='1' fill='%23333'/%3E%3Crect x='10' y='2' width='2' height='1' fill='%23333'/%3E%3Crect x='5' y='3' width='1' height='1' fill='%23333'/%3E%3Crect x='10' y='3' width='1' height='1' fill='%23333'/%3E%3Crect x='3' y='4' width='1' height='1' fill='%23805215'/%3E%3Crect x='12' y='4' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='4' width='8' height='1' fill='%23fff'/%3E%3Crect x='3' y='5' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='5' width='8' height='1' fill='%23333'/%3E%3Crect x='12' y='5' width='1' height='1' fill='%23805215'/%3E%3Crect x='3' y='6' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='6' width='8' height='1' fill='%23fff'/%3E%3Crect x='12' y='6' width='1' height='1' fill='%23805215'/%3E%3Crect x='3' y='7' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='7' width='8' height='1' fill='%23333'/%3E%3Crect x='12' y='7' width='1' height='1' fill='%23805215'/%3E%3Crect x='3' y='8' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='8' width='8' height='1' fill='%23fff'/%3E%3Crect x='12' y='8' width='1' height='1' fill='%23805215'/%3E%3Crect x='4' y='9' width='8' height='1' fill='%23333'/%3E%3Crect x='5' y='10' width='6' height='1' fill='%23fff'/%3E%3Crect x='6' y='11' width='4' height='1' fill='%23333'/%3E%3Crect x='1' y='5' width='2' height='1' fill='%23fff' opacity='.65'/%3E%3Crect x='0' y='6' width='3' height='1' fill='%23fff' opacity='.45'/%3E%3Crect x='1' y='7' width='2' height='1' fill='%23fff' opacity='.3'/%3E%3Crect x='13' y='5' width='2' height='1' fill='%23fff' opacity='.65'/%3E%3Crect x='13' y='6' width='3' height='1' fill='%23fff' opacity='.45'/%3E%3Crect x='13' y='7' width='2' height='1' fill='%23fff' opacity='.3'/%3E%3Crect x='6' y='12' width='1' height='2' fill='%23805215' opacity='.5'/%3E%3Crect x='9' y='12' width='1' height='2' fill='%23805215' opacity='.5'/%3E%3C/g%3E%3C/svg%3E"
    manifest = {
        "name": "Alpha Hive 投资仪表板",
        "short_name": "Alpha Hive",
        "start_url": "./",
        "display": "standalone",
        "theme_color": "#F4A532",
        "background_color": "#0e1117",
        "icons": [
            {"src": icon_svg, "sizes": "any", "type": "image/svg+xml"}
        ]
    }
    manifest_path = reporter.report_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        _json2.dump(manifest, f, ensure_ascii=False, indent=2)

    # ── sw.js ──
    from datetime import datetime as _sw_dt
    _sw_ts = _sw_dt.now().strftime("%Y%m%d-%H%M")
    cache_name = f"alpha-hive-{_sw_ts}"
    sw_content = f"""// Alpha Hive Service Worker - {_sw_ts}
var CACHE_NAME='{cache_name}';
var PRECACHE_URLS=['./', 'index.html', 'manifest.json',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'];

self.addEventListener('install', function(e){{
  self.skipWaiting();
  e.waitUntil(
caches.open(CACHE_NAME).then(function(cache){{
  return cache.addAll(PRECACHE_URLS);
}})
  );
}});

self.addEventListener('activate', function(e){{
  e.waitUntil(
caches.keys().then(function(names){{
  return Promise.all(
    names.filter(function(n){{ return n!==CACHE_NAME; }})
         .map(function(n){{ return caches.delete(n); }})
  );
}}).then(function(){{ return self.clients.claim(); }})
  );
}});

self.addEventListener('fetch', function(e){{
  var url=new URL(e.request.url);
  // HTML 和 JSON 都用 network-first（确保内容最新）
  if(url.pathname.endsWith('.html') || url.pathname.endsWith('.json') || url.pathname.endsWith('/')){{
e.respondWith(
  fetch(e.request).then(function(r){{
    var rc=r.clone();
    caches.open(CACHE_NAME).then(function(c){{ c.put(e.request, rc); }});
    return r;
  }}).catch(function(){{ return caches.match(e.request); }})
);
return;
  }}
  // CDN/静态资源用 cache-first
  e.respondWith(
caches.match(e.request).then(function(r){{
  return r || fetch(e.request).then(function(resp){{
    var rc=resp.clone();
    caches.open(CACHE_NAME).then(function(c){{ c.put(e.request, rc); }});
    return resp;
  }});
}})
  );
}});
"""
    sw_path = reporter.report_dir / "sw.js"
    with open(sw_path, "w", encoding="utf-8") as f:
        f.write(sw_content)

    _log.info("PWA 文件已生成：manifest.json + sw.js")


def generate_rss_xml(reporter, report: Dict) -> str:
    """生成 RSS 2.0 XML 订阅源"""
    import glob as _glob
    from xml.sax.saxutils import escape as _esc

    from datetime import timezone as _tz
    now_rfc = datetime.now(_tz.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    base_url = "https://wangmingjie36-creator.github.io/alpha-hive-deploy/"

    items_xml = ""
    # 当前简报作为第一条 item
    opps = report.get("opportunities", [])
    top3 = sorted(opps, key=lambda x: float(x.get("opp_score", 0)), reverse=True)[:3]
    top3_str = ", ".join(
        f"{o.get('ticker','')} ({float(o.get('opp_score',0)):.1f})" for o in top3
    ) if top3 else "无新机会"
    desc_text = f"今日 Top 3：{top3_str}"
    items_xml += (
        f"    <item>\n"
        f"      <title>{_esc('Alpha Hive 日报 ' + reporter.date_str)}</title>\n"
        f"      <link>{base_url}</link>\n"
        f"      <description>{_esc(desc_text)}</description>\n"
        f"      <pubDate>{now_rfc}</pubDate>\n"
        f"      <guid>{base_url}#{reporter.date_str}</guid>\n"
        f"    </item>\n"
    )

    # 历史 JSON 作为 items（最多 10 条）
    hist_files = sorted(
        _glob.glob(str(reporter.report_dir / "alpha-hive-daily-*.json")),
        reverse=True
    )
    count = 0
    for hf in hist_files:
        from pathlib import Path as _P
        hdate = _P(hf).stem.replace("alpha-hive-daily-", "")
        if hdate == reporter.date_str:
            continue
        try:
            with open(hf, encoding="utf-8") as fp:
                hrpt = json.load(fp)
            hopps = hrpt.get("opportunities", [])
            htop3 = sorted(hopps, key=lambda x: float(x.get("opp_score", 0)), reverse=True)[:3]
            htop3_str = ", ".join(
                f"{o.get('ticker','')} ({float(o.get('opp_score',0)):.1f})" for o in htop3
            ) if htop3 else "无机会"
            items_xml += (
                f"    <item>\n"
                f"      <title>{_esc('Alpha Hive 日报 ' + hdate)}</title>\n"
                f"      <link>{base_url}</link>\n"
                f"      <description>{_esc('Top 3：' + htop3_str)}</description>\n"
                f"      <pubDate>{hdate}</pubDate>\n"
                f"      <guid>{base_url}#{hdate}</guid>\n"
                f"    </item>\n"
            )
            count += 1
            if count >= 10:
                break
        except (json.JSONDecodeError, KeyError, OSError, ValueError) as _rss_err:
            _log.debug("RSS 历史条目解析失败: %s", _rss_err)
            continue

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        '    <title>Alpha Hive 投资日报</title>\n'
        f'    <link>{base_url}</link>\n'
        '    <description>去中心化蜂群智能投资研究平台 — 每日投资机会扫描</description>\n'
        '    <language>zh-CN</language>\n'
        f'    <lastBuildDate>{now_rfc}</lastBuildDate>\n'
        f'{items_xml}'
        '  </channel>\n'
        '</rss>\n'
    )


def generate_index_html(reporter, report: Dict) -> str:
    """委托给 dashboard_renderer 模块生成仪表板 HTML"""
    from dashboard_renderer import render_dashboard_html
    return render_dashboard_html(
        report=report,
        date_str=reporter.date_str,
        report_dir=reporter.report_dir,
        opportunities=reporter.opportunities,
    )

