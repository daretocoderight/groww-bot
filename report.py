"""Self-contained HTML reports (no CDN, no JS libraries) - open offline, attach to email.

reports/YYYY-MM-DD.html  - daily session report
reports/index.html       - month dashboard (cumulative P&L vs target, daily bars, stats)
reports/journal.jsonl    - one machine-readable JSON line per session (source of truth)
reports/journal.csv      - one row per trade, for Excel / tax records
"""
from __future__ import annotations

import csv
import html
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import Config

# ---------------------------------------------------------------- formatting
def inr(x: float, sign: bool = False, dec: int = 0) -> str:
    neg = x < 0
    n = abs(x)
    whole = int(round(n * 10**dec)) // 10**dec if dec else int(round(n))
    frac = f"{n:.{dec}f}".split(".")[1] if dec else ""
    s = str(whole)
    if len(s) > 3:  # Indian grouping: 1,23,45,678
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    out = "₹" + s + (("." + frac) if frac else "")
    if neg:
        return "−" + out
    return ("+" + out) if sign and x > 0 else out


def esc(x) -> str:
    return html.escape(str(x))


def _secs(hms: str) -> int:
    h, m, s = (int(p) for p in hms.split(":"))
    return h * 3600 + m * 60 + s


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=10 * mag)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 6))
        v += step
    return ticks


def _fmt_tick(v: float) -> str:
    if abs(v) >= 100_000:
        return f"{v/100_000:.1f}L".replace(".0L", "L")
    if abs(v) >= 10_000:
        return f"{v/1000:.0f}K"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


# ---------------------------------------------------------------- shared CSS/JS
CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;
--neg:#e34948;--wash:#f0efec;--track:#cde2fb;--good:#0ca30c;--good-text:#006300;--critical:#d03b3b;--warning:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;
--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--neg:#e66767;--wash:#383835;--track:#184f95;--good-text:#0ca30c}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;
--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--neg:#e66767;--wash:#383835;--track:#184f95;--good-text:#0ca30c}
*{box-sizing:border-box}body{margin:0}
.viz-root{background:var(--page);color:var(--text-primary);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
min-height:100vh;padding:32px 20px 64px}
.wrap{max-width:1080px;margin:0 auto;display:grid;gap:20px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px}
h1{font-size:22px;margin:0;font-weight:600}h2{font-size:15px;margin:0 0 4px;font-weight:600}
.sub{color:var(--text-secondary);font-size:13px}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:999px;padding:3px 10px;
font-size:12px;color:var(--text-secondary);background:var(--surface-1)}
.chip .dot{width:8px;height:8px;border-radius:50%}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:20px}
.hero{display:grid;grid-template-columns:minmax(240px,1.2fr) 2fr;gap:20px;align-items:center}
@media (max-width:760px){.hero{grid-template-columns:1fr}}
.hero-label{color:var(--text-secondary);font-size:13px}.hero-value{font-size:52px;font-weight:600;line-height:1.1;margin:4px 0}
.delta{font-size:13px;font-weight:500}.delta.up{color:var(--good-text)}.delta.down{color:var(--critical)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .l{color:var(--text-secondary);font-size:12px}.tile .v{font-size:20px;font-weight:600;margin-top:2px}
.tile .s{color:var(--muted);font-size:12px}
.meter{height:8px;border-radius:999px;background:var(--track);overflow:hidden;margin-top:8px}
.meter>span{display:block;height:100%;border-radius:999px;background:var(--series-1)}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--text-secondary);font-size:12px;margin:6px 0 10px}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:6px;border-radius:2px}
.legend i.band{height:10px;background:var(--wash)}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{font:11px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--muted);font-variant-numeric:tabular-nums}
svg text.lbl{fill:var(--text-secondary)}
.tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9;min-width:140px}
.tip b{font-weight:600}.tip .row{display:flex;justify-content:space-between;gap:12px;color:var(--text-secondary)}
.tip .row span:last-child{color:var(--text-primary);font-variant-numeric:tabular-nums}
.tip .k{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--text-secondary);font-weight:500;border-bottom:1px solid var(--axis);padding:8px 10px;white-space:nowrap}
td{border-bottom:1px solid var(--grid);padding:8px 10px;vertical-align:top}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.status{display:inline-flex;align-items:center;gap:6px;font-weight:500}
.status .ic{width:18px;height:18px;border-radius:50%;display:inline-grid;place-items:center;color:#fff;font-size:11px}
.ic.good{background:var(--good)}.ic.critical{background:var(--critical)}.ic.warning{background:var(--warning);color:#0b0b0b}
.log{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;max-height:340px;overflow:auto}
.log div{padding:3px 0;border-bottom:1px solid var(--grid);display:grid;grid-template-columns:70px 64px 1fr;gap:8px}
.log .t{color:var(--muted)}.log .lv{color:var(--text-secondary);text-transform:uppercase;font-size:11px}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13px;margin-top:10px}
a{color:var(--series-1)}
.banner{border:1px solid var(--border);border-left:4px solid var(--warning);border-radius:8px;padding:10px 14px;
background:var(--surface-1);font-size:13px;color:var(--text-secondary)}
.toggle{border:1px solid var(--border);background:var(--surface-1);color:var(--text-secondary);border-radius:8px;
padding:4px 10px;cursor:pointer;font-size:12px}
"""

JS = """
(function(){
const tip=document.querySelector('.tip');
function show(e,h){tip.innerHTML=h;tip.style.display='block';const r=tip.getBoundingClientRect();
 let x=e.clientX+14,y=e.clientY+14;if(x+r.width>innerWidth-8)x=e.clientX-r.width-14;if(y+r.height>innerHeight-8)y=e.clientY-r.height-14;
 tip.style.left=x+'px';tip.style.top=y+'px';}
function hide(){tip.style.display='none';}
document.querySelectorAll('[data-tip]').forEach(el=>{el.addEventListener('mousemove',e=>show(e,el.dataset.tip));
 el.addEventListener('mouseleave',hide);});
document.querySelectorAll('svg[data-series]').forEach(svg=>{
 const d=JSON.parse(svg.dataset.series),hair=svg.querySelector('.hair'),dots=svg.querySelectorAll('.hdot');
 const pt=svg.createSVGPoint();
 svg.addEventListener('mousemove',e=>{pt.x=e.clientX;pt.y=e.clientY;const p=pt.matrixTransform(svg.getScreenCTM().inverse());
  if(p.x<d.x0||p.x>d.x1){hide();hair.style.display='none';dots.forEach(c=>c.style.display='none');return;}
  let lo=0,hi=d.xs.length-1;while(hi-lo>1){const m=(lo+hi)>>1;if(d.xs[m]<p.x)lo=m;else hi=m;}
  const i=(p.x-d.xs[lo]<d.xs[hi]-p.x)?lo:hi;const x=d.xs[i];
  hair.setAttribute('x1',x);hair.setAttribute('x2',x);hair.style.display='';
  let h='<b>'+d.labels[i]+'</b>';
  d.series.forEach((s,k)=>{const v=s.v[i],c=dots[k];
   if(v===null){c.style.display='none';return;}
   c.setAttribute('cx',x);c.setAttribute('cy',s.y[i]);c.style.display='';
   h+='<div class="row"><span><i class="k" style="background:'+s.color+'"></i>'+s.name+'</span><span>'+s.fmt.replace('{}',v.toLocaleString('en-IN',{minimumFractionDigits:s.dec,maximumFractionDigits:s.dec}))+'</span></div>';});
  show(e,h);});
 svg.addEventListener('mouseleave',()=>{hide();hair.style.display='none';dots.forEach(c=>c.style.display='none');});
});
const btn=document.querySelector('.toggle');if(btn)btn.onclick=()=>{const r=document.documentElement;
 const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
 r.dataset.theme=dark?'light':'dark';};
})();
"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">{body}</div>'
            f'<div class="tip" role="tooltip"></div></div><script>{JS}</script></body></html>')


def _status_badge(kind: str, text: str) -> str:
    icon = {"good": "✓", "critical": "✕", "warning": "!"}[kind]
    return f'<span class="status"><span class="ic {kind}" aria-hidden="true">{icon}</span>{esc(text)}</span>'


# ---------------------------------------------------------------- line chart
def _line_chart(xs_labels: list[str], xs_secs: list[int], series: list[dict], *, height=320,
                band: tuple[float, float] | None = None, band_x: tuple[int, int] | None = None,
                refs: list[tuple[float, str]] = (), markers: list[dict] = (), y_fmt=_fmt_tick,
                x_tick_every: int = 300, aria: str = "") -> str:
    W, H, L, R, T, B = 960, height, 56, 96, 14, 28
    pw, ph = W - L - R, H - T - B
    allv = [v for s in series for v in s["values"] if v is not None] + [r[0] for r in refs]
    if band:
        allv += list(band)
    if not allv or not xs_secs:
        return '<p class="sub">No data recorded for this session.</p>'
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.08 or 1
    ticks = _nice_ticks(lo - pad, hi + pad)
    y0, y1 = ticks[0], ticks[-1]
    x0s, x1s = xs_secs[0], max(xs_secs[-1], xs_secs[0] + 1)
    X = lambda s: L + (s - x0s) / (x1s - x0s) * pw  # noqa: E731
    Y = lambda v: T + (1 - (v - y0) / (y1 - y0)) * ph  # noqa: E731

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(aria)}" data-series=\'__DATA__\'>']
    for t in ticks:  # recessive hairline grid
        out.append(f'<line x1="{L}" x2="{L+pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="var(--grid)" stroke-width="1"/>'
                   f'<text x="{L-8}" y="{Y(t)+4:.1f}" text-anchor="end">{y_fmt(t)}</text>')
    if band and band_x:
        bx0, bx1 = X(max(band_x[0], x0s)), X(min(band_x[1], x1s))
        out.append(f'<rect x="{bx0:.1f}" y="{Y(band[1]):.1f}" width="{max(0, bx1-bx0):.1f}" '
                   f'height="{max(1, Y(band[0])-Y(band[1])):.1f}" fill="var(--wash)" opacity=".9"/>')
    first = math.ceil(x0s / x_tick_every) * x_tick_every if x_tick_every else x1s + 1
    for s in range(first, x1s + 1, x_tick_every or 1):
        h, m = divmod(s // 60, 60)
        out.append(f'<line x1="{X(s):.1f}" x2="{X(s):.1f}" y1="{T+ph}" y2="{T+ph+4}" stroke="var(--axis)"/>'
                   f'<text x="{X(s):.1f}" y="{H-8}" text-anchor="middle">{h:02d}:{m:02d}</text>')
    out.append(f'<line x1="{L}" x2="{L+pw}" y1="{T+ph}" y2="{T+ph}" stroke="var(--axis)"/>')
    for v, label in refs:
        out.append(f'<line x1="{L}" x2="{L+pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--axis)" stroke-width="1"/>'
                   f'<text class="lbl" x="{L+pw+6}" y="{Y(v)+4:.1f}">{esc(label)}</text>')

    payload = {"x0": L, "x1": L + pw, "xs": [round(X(s), 1) for s in xs_secs], "labels": xs_labels, "series": []}
    for s in series:
        pts, seg = [], []
        for sx, v in zip(xs_secs, s["values"]):
            if v is None:
                if seg:
                    pts.append(seg)
                seg = []
            else:
                seg.append(f"{X(sx):.1f},{Y(v):.1f}")
        if seg:
            pts.append(seg)
        for sg in pts:
            out.append(f'<polyline points="{" ".join(sg)}" fill="none" stroke="{s["color"]}" stroke-width="2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
        last = next(((sx, v) for sx, v in zip(reversed(xs_secs), reversed(s["values"])) if v is not None), None)
        if last and s.get("end_label"):
            out.append(f'<text class="lbl" x="{X(last[0])+8:.1f}" y="{Y(last[1])+4:.1f}">{esc(s["end_label"])}</text>')
        payload["series"].append({"name": s["name"], "color": s["color"], "fmt": s.get("fmt", "{}"),
                                  "dec": s.get("dec", 2), "v": s["values"],
                                  "y": [None if v is None else round(Y(v), 1) for v in s["values"]]})
    for mk in markers:
        cx, cy = X(mk["x"]), Y(mk["y"])
        out.append(f'<g data-tip="{esc(mk["tip"])}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" fill="transparent"/>'
                   f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{mk["color"]}" stroke="var(--surface-1)" stroke-width="2"/>'
                   f'<text class="lbl" x="{cx:.1f}" y="{cy-12:.1f}" text-anchor="middle">{esc(mk["label"])}</text></g>')
    out.append(f'<line class="hair" x1="0" x2="0" y1="{T}" y2="{T+ph}" stroke="var(--axis)" style="display:none"/>')
    for s in series:
        out.append(f'<circle class="hdot" r="4" fill="{s["color"]}" stroke="var(--surface-1)" stroke-width="2" style="display:none"/>')
    out.append("</svg>")
    return "".join(out).replace("__DATA__", esc(json.dumps(payload, separators=(",", ":"))))


def _downsample(rows: list, n: int = 700) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)] + [rows[-1]]


# ---------------------------------------------------------------- daily report
def _daily_chart(res: dict, cfg: Config) -> tuple[str, str]:
    tracks = res.get("tracks") or []
    if not tracks:
        return "", ""
    traded_sym = res["trades"][0]["symbol"] if res["trades"] else None
    tr = next((t for t in tracks if t["symbol"] == traded_sym), tracks[0])
    rows = _downsample(tr["ticks"])
    if not rows:
        return f'<p class="sub">No ticks recorded for {esc(tr["symbol"])}.</p>', ""
    labels = [r[0] for r in rows]
    secs = [_secs(r[0]) for r in rows]
    price = [r[1] for r in rows]
    stop = [r[2] for r in rows]
    series = [{"name": "Premium (LTP)", "color": "var(--series-1)", "values": price, "fmt": "₹{}",
               "end_label": f"LTP {price[-1]:,.2f}"}]
    if any(v is not None for v in stop):
        series.append({"name": "Trailing stop", "color": "var(--series-2)", "values": stop, "fmt": "₹{}"})
    refs = []
    band = None
    or_end = cfg.opening_range_end.hour * 3600 + cfg.opening_range_end.minute * 60
    if tr["or_high"] and tr["or_low"] and str(res.get("strategy", "orb")).startswith("orb"):
        band = (tr["or_low"], tr["or_high"])
        trig = tr["or_high"] * (1 + cfg.breakout_buffer_pct / 100)
        refs.append((trig, f"Trigger {trig:,.2f}"))
    markers = []
    for t in res["trades"]:
        if t["symbol"] != tr["symbol"]:
            continue
        markers.append({"x": _secs(t["entry_time"]), "y": t["entry_price"], "color": "var(--series-1)", "label": "Buy",
                        "tip": f"BUY {t['qty']} @ ₹{t['entry_price']:,.2f} · {t['entry_time']}"})
        if t["exit_time"]:
            markers.append({"x": _secs(t["exit_time"]), "y": t["exit_price"], "color": "var(--series-2)", "label": "Exit",
                            "tip": f"SELL @ ₹{t['exit_price']:,.2f} · {t['exit_time']} · {t['exit_reason']} · net {inr(t['net_pnl'], True)}"})
    svg = _line_chart(labels, secs, series, band=band, band_x=(secs[0], or_end), refs=refs, markers=markers,
                      aria=f"{tr['symbol']} premium and trailing stop, {labels[0]} to {labels[-1]}")
    legend = ('<div class="legend"><span><i style="background:var(--series-1)"></i>Premium (LTP)</span>'
              + ('<span><i style="background:var(--series-2)"></i>Trailing stop</span>' if len(series) > 1 else "")
              + ('<span><i class="band"></i>Opening range</span>' if band else '') + '</div>')
    table_rows = "".join(f"<tr><td>{r[0]}</td><td class='num'>{r[1]:,.2f}</td>"
                         f"<td class='num'>{'' if r[2] is None else f'{r[2]:,.2f}'}</td></tr>"
                         for r in _downsample(tr["ticks"], 120))
    chart = (f'<div class="card"><h2>{esc(tr["symbol"])} - session price action</h2>'
             f'<div class="sub">Opening range {tr["or_low"]:,.2f} - {tr["or_high"]:,.2f} · {tr["or_ticks"]} ticks in range window</div>'
             f'{legend}{svg}<details><summary>Show data table</summary><table><thead><tr><th>Time</th>'
             f'<th class="num">LTP</th><th class="num">Stop</th></tr></thead><tbody>{table_rows}</tbody></table></details></div>')
    return chart, tr["symbol"]


def _risk_checks(res: dict, cfg: Config) -> list[tuple[str, str, str]]:
    checks = []
    s = res["sentiment"]["score"]
    checks.append(("good" if s >= cfg.sentiment_block_below else "critical", "Sentiment gate",
                   f"score {s:+.2f} (block below {cfg.sentiment_block_below})"))
    if res["vix_open"]:
        checks.append(("good" if res["vix_open"] <= cfg.max_vix_to_trade else "critical", "VIX regime",
                       f"open {res['vix_open']:.2f}, high {res['vix_high']:.2f}"))
    for t in res["trades"]:
        cap = cfg.total_capital * cfg.max_risk_pct / 100
        checks.append(("good" if t["risk_actual"] <= cap + 1 else "critical", f"Risk cap · {t['symbol']}",
                       f"planned risk {inr(t['risk_actual'])} vs ceiling {inr(cap)}"))
        slip = t["exit_price"] < t["final_stop"] * 0.97 and t["exit_reason"].endswith("stop")
        checks.append(("warning" if slip else "good", "Stop execution",
                       f"exit {t['exit_price']:,.2f} vs stop {t['final_stop']:,.2f}" + (" - heavy slippage" if slip else "")))
    loss = -res["net_pnl"]
    checks.append(("good" if loss < cfg.daily_loss_limit else "critical", "Daily loss limit",
                   f"day {inr(res['net_pnl'], True)} vs limit −{inr(cfg.daily_loss_limit)}"))
    flat = all(t["exit_time"] for t in res["trades"])
    checks.append(("good" if flat else "critical", "Flat at close of window", "no open positions" if flat else "OPEN POSITION"))
    checks.append(("good" if res["errors"] == 0 else "warning", "Data feed", f"{res['errors']} error(s)"))
    return checks


def render_daily(res: dict, cfg: Config, month_net: float, month_days: int) -> str:
    d = date.fromisoformat(res["date"])
    net = res["net_pnl"]
    daily_target = cfg.monthly_target / 22
    trades = res["trades"]
    gross = sum(t["gross_pnl"] for t in trades)
    charges = sum(t["charges"] for t in trades)
    r_tot = sum(t["r_multiple"] for t in trades)
    mode = res["mode"].upper()
    status_kind = "good" if net > 0 else ("critical" if net < 0 else "warning")
    pct = max(0.0, min(100.0, month_net / cfg.monthly_target * 100)) if cfg.monthly_target else 0

    delta_cls = "up" if net >= daily_target else "down"
    delta_txt = (f"{'▲' if net >= daily_target else '▼'} {inr(net - daily_target, True)} vs daily pace {inr(daily_target)}")

    banner = ""
    if res["mode"] == "sim":
        banner = '<div class="banner"><b>Simulated session.</b> Synthetic prices - for testing the pipeline only, not evidence of edge.</div>'
    elif res["mode"] == "backtest":
        banner = '<div class="banner"><b>Backtest replay.</b> Historical 1-min candles, modelled fills, neutral sentiment. Past results do not guarantee future returns.</div>'
    elif res["mode"] == "paper":
        banner = '<div class="banner"><b>Paper trading.</b> Live market data, simulated fills with modelled slippage.</div>'

    chart, _ = _daily_chart(res, cfg)

    trade_rows = "".join(
        f"<tr><td>{esc(t['symbol'])}</td><td class='num'>{t['lots']} × {res['lot_size']}</td>"
        f"<td class='num'>{t['entry_time']}<br>₹{t['entry_price']:,.2f}</td>"
        f"<td class='num'>{t['exit_time']}<br>₹{t['exit_price']:,.2f}</td>"
        f"<td class='num'>₹{t['initial_stop']:,.2f}</td><td class='num'>₹{t['peak']:,.2f}</td>"
        f"<td>{esc(t['exit_reason'])}</td><td class='num'>{inr(t['gross_pnl'], True)}</td>"
        f"<td class='num'>{inr(t['charges'])}</td><td class='num'><b>{inr(t['net_pnl'], True)}</b></td>"
        f"<td class='num'>{t['r_multiple']:+.2f}R</td></tr>" for t in trades)
    trades_html = (f"<table><thead><tr><th>Contract</th><th class='num'>Lots</th><th class='num'>Entry</th>"
                   f"<th class='num'>Exit</th><th class='num'>Init. SL</th><th class='num'>Peak</th><th>Reason</th>"
                   f"<th class='num'>Gross</th><th class='num'>Charges</th><th class='num'>Net</th><th class='num'>R</th>"
                   f"</tr></thead><tbody>{trade_rows}</tbody></table>") if trades else \
        f'<p class="sub">No trades - {esc(res["status"])}.</p>'

    ch_tot = defaultdict(float)
    for t in trades:
        for k, v in t["charges_detail"].items():
            ch_tot[k] += v
    ch_rows = "".join(f"<tr><td>{k.upper() if k in ('stt', 'gst', 'sebi') else k.title()}</td>"
                      f"<td class='num'>{inr(v, dec=2)}</td></tr>" for k, v in ch_tot.items())
    ch_html = (f"<table><tbody>{ch_rows}<tr><td><b>Total</b></td><td class='num'><b>{inr(charges, dec=2)}</b></td></tr>"
               f"</tbody></table><p class='sub'>Estimated from configured rates - reconcile with the Groww contract note.</p>"
               ) if trades else '<p class="sub">No charges today.</p>'

    checks_html = "<table><tbody>" + "".join(
        f"<tr><td>{_status_badge(k, n)}</td><td class='sub'>{esc(dsc)}</td></tr>" for k, n, dsc in _risk_checks(res, cfg)
    ) + "</tbody></table>"

    heads = res["sentiment"].get("headlines") or []
    heads_html = ("<table><thead><tr><th>Headline</th><th class='num'>Score</th></tr></thead><tbody>" + "".join(
        f"<tr><td>{esc(h)}</td><td class='num'>{s:+.2f}</td></tr>" for h, s in heads) + "</tbody></table>"
                  ) if heads else '<p class="sub">No headlines supplied.</p>'

    log_html = '<div class="log">' + "".join(
        f'<div><span class="t">{esc(t)}</span><span class="lv">{esc(lv)}</span><span>{esc(m)}</span></div>'
        for t, lv, m in res["events"]) + "</div>"

    body = f"""
<header><div><div class="sub">Morning ORB · Groww · {esc(cfg.exchange)}</div>
<h1>Daily trading report - {d.strftime('%A, %d %B %Y')}</h1></div>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<span class="chip">{_status_badge(status_kind, res['status'])}</span><span class="chip">{esc(mode)} mode</span><span class="chip">{esc(res.get("strategy") or "-")}</span>
<a class="chip" href="month-{res['date'][:7]}.html">Month dashboard →</a><button class="toggle" type="button">Theme</button></div></header>
{banner}
<section class="card hero"><div><div class="hero-label">Net P&amp;L today (after charges)</div>
<div class="hero-value">{inr(net, True)}</div><div class="delta {delta_cls}">{delta_txt}</div></div>
<div class="tiles">
<div class="tile"><div class="l">Gross P&amp;L</div><div class="v">{inr(gross, True)}</div><div class="s">{len(trades)} trade(s)</div></div>
<div class="tile"><div class="l">Charges</div><div class="v">{inr(charges)}</div><div class="s">brokerage, STT, GST…</div></div>
<div class="tile"><div class="l">R-multiple</div><div class="v">{r_tot:+.2f}R</div><div class="s">return ÷ planned risk</div></div>
<div class="tile"><div class="l">Sentiment</div><div class="v">{res['sentiment']['score']:+.2f}</div><div class="s">{esc(res['sentiment']['source'])}</div></div>
<div class="tile"><div class="l">India VIX</div><div class="v">{f"{res['vix_open']:.2f}" if res['vix_open'] else "n/a"}</div><div class="s">{f"range {res['vix_low']:.2f}-{res['vix_high']:.2f}" if res['vix_open'] else "not used for this market"}</div></div>
<div class="tile"><div class="l">Month to date</div><div class="v">{inr(month_net, True)}</div>
<div class="s">{pct:.0f}% of {inr(cfg.monthly_target)} · {month_days} day(s)</div>
<div class="meter" role="meter" aria-valuenow="{pct:.0f}" aria-valuemin="0" aria-valuemax="100" aria-label="Monthly target progress"><span style="width:{pct:.1f}%"></span></div></div>
</div></section>
{chart}
<section class="card"><h2>Trades</h2>{trades_html}</section>
<div class="grid2"><section class="card"><h2>Risk checks</h2>{checks_html}</section>
<section class="card"><h2>Charges breakdown</h2>{ch_html}</section></div>
<div class="grid2"><section class="card"><h2>Pre-market headlines</h2>{heads_html}</section>
<section class="card"><h2>Event log</h2>{log_html}</section></div>
<p class="sub">Generated by groww-orb-bot · capital {inr(cfg.total_capital)} · base risk {cfg.base_risk_pct}% · daily loss limit {inr(cfg.daily_loss_limit)}</p>
"""
    return _page(f"ORB report {res['date']}", body)


# ---------------------------------------------------------------- journal
def append_journal(res: dict, cfg: Config) -> None:
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    if cfg.journal_file.exists():  # replace an existing entry for the same date+mode (re-runs)
        lines = [ln for ln in cfg.journal_file.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not (json.loads(ln)["date"] == res["date"] and json.loads(ln)["mode"] == res["mode"])]
    lines.append(json.dumps(res, ensure_ascii=False, separators=(",", ":")))
    lines.sort(key=lambda ln: json.loads(ln)["date"])
    cfg.journal_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_journal(cfg: Config, mode: str | None = None) -> list[dict]:
    if not cfg.journal_file.exists():
        return []
    rows = [json.loads(ln) for ln in cfg.journal_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r for r in rows if mode is None or r["mode"] == mode]


def write_trades_csv(cfg: Config, sessions: list[dict]) -> None:
    path = cfg.reports_dir / "journal.csv"
    cols = ["date", "mode", "symbol", "lots", "qty", "entry_time", "entry_price", "initial_stop", "exit_time",
            "exit_price", "exit_reason", "peak", "gross_pnl", "charges", "net_pnl", "r_multiple", "risk_actual"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sessions:
            for t in s["trades"]:
                w.writerow([s["date"], s["mode"]] + [t[c] for c in cols[2:]])


# ---------------------------------------------------------------- month dashboard
def _stats(sessions: list[dict]) -> dict:
    trades = [t for s in sessions for t in s["trades"]]
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for s in sessions:
        eq += s["net_pnl"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {
        "net": sum(s["net_pnl"] for s in sessions), "trades": len(trades),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf") if wins else 0.0,
        "avg_r": (sum(t["r_multiple"] for t in trades) / len(trades)) if trades else 0.0,
        "charges": sum(t["charges"] for t in trades), "mdd": mdd,
        "green": sum(1 for s in sessions if s["net_pnl"] > 0), "red": sum(1 for s in sessions if s["net_pnl"] < 0),
    }


def _bar_chart(sessions: list[dict]) -> str:
    W, H, L, R, T, B = 960, 260, 56, 16, 14, 30
    pw, ph = W - L - R, H - T - B
    vals = [s["net_pnl"] for s in sessions]
    lo, hi = min(0.0, *vals), max(0.0, *vals)
    ticks = _nice_ticks(lo, hi)
    y0, y1 = ticks[0], ticks[-1]
    Y = lambda v: T + (1 - (v - y0) / (y1 - y0)) * ph  # noqa: E731
    n = len(vals)
    band = pw / max(n, 1)
    bw = min(24.0, band * 0.6)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Daily net P&amp;L columns">']
    for t in ticks:
        out.append(f'<line x1="{L}" x2="{L+pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{L-8}" y="{Y(t)+4:.1f}" text-anchor="end">{_fmt_tick(t)}</text>')
    zero = Y(0)
    every = max(1, math.ceil(n / 12))
    for i, s in enumerate(sessions):
        v = s["net_pnl"]
        cx = L + band * (i + 0.5)
        x = cx - bw / 2
        top, bot = (Y(v), zero) if v >= 0 else (zero, Y(v))
        h = max(0.0, bot - top)
        r = min(4.0, h / 2, bw / 2)
        col = "var(--series-1)" if v >= 0 else "var(--neg)"
        if h > 0:  # rounded data-end, square at the baseline
            if v >= 0:
                p = (f"M{x:.1f},{bot:.1f}V{top+r:.1f}Q{x:.1f},{top:.1f} {x+r:.1f},{top:.1f}H{x+bw-r:.1f}"
                     f"Q{x+bw:.1f},{top:.1f} {x+bw:.1f},{top+r:.1f}V{bot:.1f}Z")
            else:
                p = (f"M{x:.1f},{top:.1f}V{bot-r:.1f}Q{x:.1f},{bot:.1f} {x+r:.1f},{bot:.1f}H{x+bw-r:.1f}"
                     f"Q{x+bw:.1f},{bot:.1f} {x+bw:.1f},{bot-r:.1f}V{top:.1f}Z")
            out.append(f'<path d="{p}" fill="{col}"/>')
        nt = len(s["trades"])
        tip = f"<b>{s['date']}</b><div class='row'><span>Net</span><span>{inr(v, True)}</span></div>" \
              f"<div class='row'><span>Trades</span><span>{nt}</span></div><div class='row'><span>Status</span><span>{esc(s['status'])}</span></div>"
        out.append(f'<rect x="{cx-band/2:.1f}" y="{T}" width="{band:.1f}" height="{ph}" fill="transparent" data-tip="{esc(tip)}"/>')
        if i % every == 0:
            out.append(f'<text x="{cx:.1f}" y="{H-10}" text-anchor="middle">{date.fromisoformat(s["date"]).strftime("%d %b")}</text>')
    out.append(f'<line x1="{L}" x2="{L+pw}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="var(--axis)"/></svg>')
    return "".join(out)


def render_month(cfg: Config, sessions: list[dict], month: str) -> str:
    st = _stats(sessions)
    pct = max(0.0, min(100.0, st["net"] / cfg.monthly_target * 100)) if cfg.monthly_target else 0
    labels, secs, cum, target, eq = [], [], [], [], 0.0
    for i, s in enumerate(sessions):
        eq += s["net_pnl"]
        labels.append(date.fromisoformat(s["date"]).strftime("%d %b"))
        secs.append(i)
        cum.append(round(eq, 2))
        target.append(round(cfg.monthly_target / 22 * (i + 1), 2))
    eq_svg = _line_chart(labels, secs, [
        {"name": "Cumulative net", "color": "var(--series-1)", "values": cum, "fmt": "₹{}", "dec": 0,
         "end_label": inr(cum[-1], True) if cum else ""},
        {"name": "Target pace", "color": "var(--series-2)", "values": target, "fmt": "₹{}", "dec": 0,
         "end_label": "Target pace"},
    ], height=300, x_tick_every=0, aria="Cumulative net P&L vs target pace") if sessions else ""
    # day labels on the equity chart's x-axis
    if sessions:
        W, L, R, H = 960, 56, 96, 300
        pw = W - L - R
        every = max(1, math.ceil(len(sessions) / 12))
        xt = "".join(f'<text x="{L + (i / max(1, len(sessions)-1)) * pw:.1f}" y="{H-8}" text-anchor="middle">{labels[i]}</text>'
                     for i in range(0, len(sessions), every))
        eq_svg = eq_svg.replace("</svg>", xt + "</svg>", 1) if eq_svg.count("</svg>") == 1 else eq_svg

    pf = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    rows = "".join(
        f"<tr><td><a href='{s['date']}.html'>{s['date']}</a></td><td>{esc(s['status'])}</td>"
        f"<td class='num'>{len(s['trades'])}</td><td class='num'>{round(s["sentiment"]["score"], 2) + 0.0:+.2f}</td>"
        f"<td class='num'>{f"{s['vix_open']:.2f}" if s['vix_open'] else "—"}</td><td class='num'>{sum(t['r_multiple'] for t in s['trades']):+.2f}R</td>"
        f"<td class='num'><b>{inr(s['net_pnl'], True)}</b></td></tr>" for s in reversed(sessions))
    mode = sessions[-1]["mode"].upper() if sessions else ""
    body = f"""
<header><div><div class="sub">Morning ORB · Groww · {esc(mode)} journal</div><h1>Month dashboard - {esc(month)}</h1></div>
<button class="toggle" type="button">Theme</button></header>
{'<div class="banner"><b>Simulated data.</b> These numbers come from synthetic prices and say nothing about real profitability.</div>' if mode == 'SIM' else ''}
<section class="card hero"><div><div class="hero-label">Month-to-date net P&amp;L</div>
<div class="hero-value">{inr(st['net'], True)}</div>
<div class="sub">{pct:.0f}% of the {inr(cfg.monthly_target)} target · {len(sessions)} session(s)</div>
<div class="meter" role="meter" aria-valuenow="{pct:.0f}" aria-valuemin="0" aria-valuemax="100" aria-label="Monthly target progress"><span style="width:{pct:.1f}%"></span></div></div>
<div class="tiles">
<div class="tile"><div class="l">Win rate</div><div class="v">{st['win_rate']:.0f}%</div><div class="s">{st['trades']} trades</div></div>
<div class="tile"><div class="l">Profit factor</div><div class="v">{pf}</div><div class="s">gross wins ÷ losses</div></div>
<div class="tile"><div class="l">Avg R / trade</div><div class="v">{st['avg_r']:+.2f}R</div><div class="s">expectancy</div></div>
<div class="tile"><div class="l">Avg win / loss</div><div class="v">{inr(st['avg_win'])}</div><div class="s">vs {inr(st['avg_loss'])}</div></div>
<div class="tile"><div class="l">Max drawdown</div><div class="v">{inr(st['mdd'])}</div><div class="s">peak-to-trough, net</div></div>
<div class="tile"><div class="l">Charges paid</div><div class="v">{inr(st['charges'])}</div><div class="s">{st['green']} green · {st['red']} red days</div></div>
</div></section>
<section class="card"><h2>Cumulative net P&amp;L vs target pace</h2>
<div class="legend"><span><i style="background:var(--series-1)"></i>Cumulative net</span><span><i style="background:var(--series-2)"></i>Target pace ({inr(cfg.monthly_target/22)}/day)</span></div>
{eq_svg}</section>
<section class="card"><h2>Daily net P&amp;L</h2><div class="sub">Hover a day for details</div>
<div class="legend"><span><i style="background:var(--series-1);height:10px;width:10px"></i>Profit day</span><span><i style="background:var(--neg);height:10px;width:10px"></i>Loss day</span></div>
{_bar_chart(sessions) if sessions else ''}</section>
<section class="card"><h2>Sessions</h2><table><thead><tr><th>Date</th><th>Status</th><th class="num">Trades</th>
<th class="num">Sentiment</th><th class="num">VIX</th><th class="num">R</th><th class="num">Net</th></tr></thead><tbody>{rows}</tbody></table>
<p class="sub">Trade-level CSV: <a href="journal.csv">journal.csv</a></p></section>
"""
    return _page(f"ORB month {month}", body)


# ---------------------------------------------------------------- entrypoint
def publish(res: dict, cfg: Config) -> Path:
    append_journal(res, cfg)
    month = res["date"][:7]
    sessions = [s for s in load_journal(cfg, res["mode"]) if s["date"].startswith(month)]
    month_net = sum(s["net_pnl"] for s in sessions)
    daily_path = cfg.reports_dir / f"{res['date']}.html"
    daily_path.write_text(render_daily(res, cfg, month_net, len(sessions)), encoding="utf-8")
    month_html = render_month(cfg, sessions, date.fromisoformat(res["date"]).strftime("%B %Y"))
    (cfg.reports_dir / f"month-{month}.html").write_text(month_html, encoding="utf-8")
    (cfg.reports_dir / "index.html").write_text(month_html, encoding="utf-8")  # latest month
    write_trades_csv(cfg, load_journal(cfg, res["mode"]))
    return daily_path



# ---------------------------------------------------------------- statistics
def bootstrap_ci(xs: list[float], n: int = 2000, seed: int = 7, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean. Deterministic (seeded) so reports are reproducible."""
    import random
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rng, k = random.Random(seed), len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return means[int(n * alpha / 2)], means[int(n * (1 - alpha / 2)) - 1]


def diff_ci(a: list[float], b: list[float], n: int = 2000, seed: int = 11) -> tuple[float, float]:
    """Bootstrap CI of mean(a) - mean(b): does strategy a beat the random control b?"""
    import random
    if len(a) < 2 or len(b) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    d = sorted(sum(rng.choice(a) for _ in a) / len(a) - sum(rng.choice(b) for _ in b) / len(b) for _ in range(n))
    return d[int(n * 0.025)], d[int(n * 0.975) - 1]


def _trades(sessions: list[dict]):
    for s in sessions:
        for t in s["trades"]:
            yield s, t


def _r_unit(t: dict) -> float:
    return (t["entry_price"] - t["initial_stop"]) or 1e-9


def advanced(sessions: list[dict]) -> dict:
    rs = [t["r_multiple"] for _, t in _trades(sessions)]
    n = len(rs)
    mean = sum(rs) / n if n else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    lo, hi = bootstrap_ci(rs)
    daily = [s["net_pnl"] for s in sessions]
    dm = sum(daily) / len(daily) if daily else 0.0
    dsd = math.sqrt(sum((x - dm) ** 2 for x in daily) / (len(daily) - 1)) if len(daily) > 1 else 0.0
    streak = cur = 0
    for _, t in _trades(sessions):
        cur = cur + 1 if t["net_pnl"] <= 0 else 0
        streak = max(streak, cur)
    mfe = [(t["peak"] - t["entry_price"]) / _r_unit(t) for _, t in _trades(sessions)]
    mae = [(t["entry_price"] - t["trough"]) / _r_unit(t) for _, t in _trades(sessions)]
    reached = [(m, t["r_multiple"]) for m, (_, t) in zip(mfe, _trades(sessions)) if m >= 1.0]
    return {
        "rs": rs, "n": n, "mean": mean, "sd": sd, "lo": lo, "hi": hi,
        "need": math.ceil((1.96 * sd / 0.15) ** 2) if sd else 0,  # trades to resolve +0.15R at 95%
        "sharpe": (dm / dsd * math.sqrt(252)) if dsd else 0.0, "streak": streak,
        "mfe": sum(mfe) / n if n else 0.0, "mae": sum(mae) / n if n else 0.0,
        "reach1r": (len(reached) / n * 100) if n else 0.0,
        "giveback": (sum(1 for _, r in reached if r < 0.5) / len(reached) * 100) if reached else 0.0,
    }


def regimes(sessions: list[dict]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for s, t in _trades(sessions):
        v = s.get("vix_open") or 0
        out["VIX n/a" if not v else "VIX < 14" if v < 14 else "VIX 14-18" if v <= 18 else "VIX > 18"].append(t["r_multiple"])
        dte = s.get("dte", -1)
        out["Expiry day" if dte == 0 else "1-2 days to expiry" if dte in (1, 2) else
            "3+ days to expiry" if dte >= 3 else "Expiry unknown"].append(t["r_multiple"])
        out["CE (bullish)" if t["symbol"].upper().endswith("CE") else "PE (bearish)"].append(t["r_multiple"])
        out[date.fromisoformat(s["date"]).strftime("%A")].append(t["r_multiple"])
    return out


REGIME_ORDER = ["VIX n/a", "VIX < 14", "VIX 14-18", "VIX > 18", "Expiry day", "1-2 days to expiry", "3+ days to expiry",
                "Expiry unknown", "CE (bullish)", "PE (bearish)", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# ---------------------------------------------------------------- A/B comparison
def render_compare(cfg: Config, results: dict[str, list[dict]], title: str, synthetic: bool,
                   descriptions: dict[str, str] | None = None) -> str:
    names = list(results)
    stats = {n: _stats(results[n]) for n in names}
    colors = [f"var(--series-{k})" for k in range(1, 6)]  # fixed categorical order, never cycled past 5
    names = names[:5]

    def reasons(sessions):
        out = defaultdict(int)
        for s in sessions:
            for t in s["trades"]:
                out[t["exit_reason"].split(" (")[0]] += 1
            if not s["trades"]:
                out["no trade: " + s["status"].split(":")[0]] += 1
        return out

    metric_rows = [
        ("Net P&L", lambda s: inr(s["net"], True)),
        ("Trades", lambda s: str(s["trades"])),
        ("Win rate", lambda s: f"{s['win_rate']:.0f}%"),
        ("Profit factor", lambda s: "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"),
        ("Avg R / trade (expectancy)", lambda s: f"{s['avg_r']:+.2f}R"),
        ("Avg win / avg loss", lambda s: f"{inr(s['avg_win'])} / {inr(s['avg_loss'])}"),
        ("Max drawdown", lambda s: inr(s["mdd"])),
        ("Charges paid", lambda s: inr(s["charges"])),
        ("Green / red days", lambda s: f"{s['green']} / {s['red']}"),
    ]
    head = "".join(f"<th class='num'><i class='k' style='display:inline-block;width:8px;height:8px;border-radius:50%;"
                   f"background:{colors[i]};margin-right:6px'></i>{esc(n)}</th>" for i, n in enumerate(names))
    body_rows = "".join(f"<tr><td>{esc(label)}</td>" + "".join(f"<td class='num'>{fn(stats[n])}</td>" for n in names)
                        + "</tr>" for label, fn in metric_rows)

    days = sorted({s["date"] for n in names for s in results[n]})
    labels = [date.fromisoformat(d).strftime("%d %b") for d in days]
    by_day = {n: {s["date"]: s["net_pnl"] for s in results[n]} for n in names}
    series = []
    for i, n in enumerate(names):
        eq, cum = 0.0, []
        for d in days:  # union of dates: a strategy missing a day contributes 0 that day
            eq += by_day[n].get(d, 0.0)
            cum.append(round(eq, 2))
        series.append({"name": n, "color": colors[i], "values": cum, "fmt": "₹{}", "dec": 0,
                       "end_label": (f"{n} {inr(cum[-1], True)}" if cum else n) if len(names) <= 2 else ""})
    svg = _line_chart(labels, list(range(len(days))), series, height=320, x_tick_every=0,
                      aria="Cumulative net P&L by strategy variant") if days else ""
    if days:
        W, L, R, H = 960, 56, 96, 320
        every = max(1, math.ceil(len(days) / 12))
        xt = "".join(f'<text x="{L + (i / max(1, len(days) - 1)) * (W - L - R):.1f}" y="{H - 8}" text-anchor="middle">'
                     f'{labels[i]}</text>' for i in range(0, len(days), every))
        svg = svg.replace("</svg>", xt + "</svg>")

    all_reasons = sorted({k for n in names for k in reasons(results[n])})
    rs = {n: reasons(results[n]) for n in names}
    reason_rows = "".join(f"<tr><td>{esc(k)}</td>" + "".join(f"<td class='num'>{rs[n].get(k, 0)}</td>" for n in names)
                          + "</tr>" for k in all_reasons)

    adv = {n: advanced(results[n]) for n in names}
    ctrl = "random" if "random" in names else None

    def fmt_ci(a):
        return "-" if a["n"] < 2 else f"{a['lo']:+.2f} to {a['hi']:+.2f}R"

    def sig_cell(n):
        a = adv[n]
        if a["n"] < 10:
            return _status_badge("warning", f"too few trades ({a['n']})")
        if a["lo"] > 0:
            return _status_badge("good", "edge > 0 (95%)")
        if a["hi"] < 0:
            return _status_badge("critical", "negative edge")
        return _status_badge("warning", "not distinguishable from 0")

    def vs_ctrl(n):
        if not ctrl or n == ctrl:
            return "<span class='sub'>control</span>" if n == ctrl else "<span class='sub'>add 'random'</span>"
        lo, hi = diff_ci(adv[n]["rs"], adv[ctrl]["rs"])
        if lo != lo:  # nan
            return "<span class='sub'>too few trades</span>"
        kind, txt = ("good", "beats random") if lo > 0 else ("critical", "worse than random") if hi < 0 else ("warning", "same as random")
        return _status_badge(kind, txt) + f"<div class='sub'>{lo:+.2f} to {hi:+.2f}R</div>"

    edge_rows = "".join(
        f"<tr><td>{esc(n)}</td><td class='num'>{adv[n]['n']}</td><td class='num'>{adv[n]['mean']:+.2f}R</td>"
        f"<td class='num'>{fmt_ci(adv[n])}</td><td>{sig_cell(n)}</td><td>{vs_ctrl(n)}</td>"
        f"<td class='num'>{adv[n]['need'] or '-'}</td><td class='num'>{adv[n]['sharpe']:.2f}</td>"
        f"<td class='num'>{adv[n]['streak']}</td></tr>" for n in names)
    exit_rows = "".join(
        f"<tr><td>{esc(n)}</td><td class='num'>{adv[n]['mfe']:+.2f}R</td><td class='num'>−{adv[n]['mae']:.2f}R</td>"
        f"<td class='num'>{adv[n]['reach1r']:.0f}%</td><td class='num'>{adv[n]['giveback']:.0f}%</td></tr>" for n in names)
    reg = {n: regimes(results[n]) for n in names}
    keys = [k for k in REGIME_ORDER if any(reg[n].get(k) for n in names)]

    def reg_cell(xs):
        if not xs:
            return "<td class='num sub'>-</td>"
        m = sum(xs) / len(xs)
        return f"<td class='num'>{m:+.2f}R <span class='sub'>({len(xs)})</span></td>"
    regime_rows = "".join(f"<tr><td>{esc(k)}</td>" + "".join(reg_cell(reg[n].get(k, [])) for n in names) + "</tr>"
                          for k in keys)

    real = [n for n in names if n != ctrl and adv[n]["n"] >= 10 and adv[n]["lo"] > 0
            and (not ctrl or diff_ci(adv[n]["rs"], adv[ctrl]["rs"])[0] > 0)]
    best = max(names, key=lambda n: (adv[n]["mean"], stats[n]["net"])) if names else ""
    if real:
        verdict = (f"<b>{esc(', '.join(real))}</b> show an edge that is statistically above zero"
                   + (" <i>and</i> above the random control" if ctrl else "") + " on this sample. "
                   "Confirm on a second, unseen period before trading it live.")
    else:
        verdict = (f"<b>No strategy has a statistically proven edge yet.</b> Highest average so far: {esc(best)} "
                   f"({adv[best]['mean']:+.2f}R over {adv[best]['n']} trades) - but its 95% interval includes zero"
                   + (" or it doesn't beat random entries" if ctrl else "") + ". Keep testing; don't go live on this."
                   ) if best else "No sessions yet."
    banner = ('<div class="banner"><b>Synthetic data.</b> The price generator is a toy - these numbers say nothing '
              'about which variant wins in the real market. Run a real backtest with --from/--to.</div>') if synthetic else \
        ('<div class="banner"><b>Historical replay.</b> 1-min candles, modelled fills, neutral sentiment. '
         'Validate on a second, unseen period before trusting a winner (avoid curve-fitting).</div>')
    links = " · ".join(f'<a href="{esc(n)}/index.html">{esc(n)} dashboard</a>' for n in names)
    descriptions = descriptions or {}
    desc_rows = "".join(f"<tr><td><span class='status'><i class='k' style='display:inline-block;width:8px;height:8px;"
                        f"border-radius:50%;background:{colors[i]}'></i>{esc(n)}</span></td>"
                        f"<td class='sub'>{esc(descriptions.get(n, ''))}</td></tr>" for i, n in enumerate(names))

    def day_cell(n, d):
        if d not in by_day[n]:
            return "<td class='num sub'>-</td>"
        v = by_day[n][d]
        best = v != 0 and v == max(by_day[m].get(d, float("-inf")) for m in names)
        link = f"<a href='{esc(n)}/{d}.html'>{inr(v, True)}</a>"
        return f"<td class='num'>{'<b>' + link + '</b>' if best else link}</td>"
    day_rows = "".join(f"<tr><td>{d}</td>" + "".join(day_cell(n, d) for n in names) + "</tr>" for d in reversed(days))

    page = f"""
<header><div><div class="sub">Groww intraday · {esc(title)}</div><h1>Strategy leaderboard</h1></div>
<button class="toggle" type="button">Theme</button></header>
{banner}
<section class="card"><h2>Verdict</h2><p>{verdict}</p><p class="sub">{len(days)} sessions · {links}</p></section>
<section class="card"><h2>Is the edge real?</h2>
<div class="sub">Average R per trade with a 95% bootstrap interval. An edge is real only if the whole interval is above 0
and above the random-entry control. "Trades needed" = sample size to resolve ±0.15R.</div>
<table><thead><tr><th>Strategy</th><th class="num">Trades</th><th class="num">Avg R</th><th class="num">95% interval</th>
<th>Edge vs zero</th><th>vs random entries</th><th class="num">Trades needed</th><th class="num">Daily Sharpe</th>
<th class="num">Worst losing streak</th></tr></thead><tbody>{edge_rows}</tbody></table></section>
<section class="card"><h2>Cumulative net P&amp;L</h2>
<div class="legend">{''.join(f'<span><i style="background:{colors[i]}"></i>{esc(n)} {inr(stats[n]["net"], True)}</span>' for i, n in enumerate(names))}</div>
{svg}</section>
<div class="grid2">
<section class="card"><h2>Metrics</h2><table><thead><tr><th>Metric</th>{head}</tr></thead><tbody>{body_rows}</tbody></table></section>
<section class="card"><h2>Outcomes by reason</h2><table><thead><tr><th>Reason</th>{head}</tr></thead><tbody>{reason_rows}</tbody></table>
<p class="sub">How sessions ended per strategy. Many initial stops = false signals; many skips = stop too wide for the risk budget.</p></section></div>
<div class="grid2">
<section class="card"><h2>Exit quality</h2><div class="sub">MFE = best open profit, MAE = worst open loss (in R).
High "gave back" = exits leak profit; tune trailing/breakeven.</div>
<table><thead><tr><th>Strategy</th><th class="num">Avg MFE</th><th class="num">Avg MAE</th>
<th class="num">Reached +1R</th><th class="num">…then closed &lt; +0.5R</th></tr></thead><tbody>{exit_rows}</tbody></table></section>
<section class="card"><h2>Where it works</h2><div class="sub">Avg R (trades) by condition. Most intraday edges live in one regime.</div>
<table><thead><tr><th>Condition</th>{head}</tr></thead><tbody>{regime_rows}</tbody></table></section></div>
<section class="card"><h2>Daily leaderboard</h2><div class="sub">Net P&amp;L per session; best strategy of the day in bold</div>
<div style="max-height:420px;overflow:auto"><table><thead><tr><th>Date</th>{head}</tr></thead><tbody>{day_rows}</tbody></table></div></section>
<section class="card"><h2>Strategies</h2><table><tbody>{desc_rows}</tbody></table></section>
"""
    return _page("ORB strategy comparison", page)
