#!/usr/bin/env python3
"""
Live trading dashboard — Flask web app.

Reads positions.json, trades.jsonl, equity_log.jsonl from data/live/
and serves a real-time portfolio view at http://localhost:5050

Usage:
    python scripts/live/dashboard.py
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
from flask import Flask, jsonify, render_template_string

_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

DATA_DIR    = _root / "data" / "live"
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"

app = Flask(__name__)
_price_cache: Dict[str, tuple] = {}  # market_id -> (price, fetched_at)
_CACHE_TTL = 120  # seconds


def _read_positions() -> dict:
    p = DATA_DIR / "positions.json"
    if not p.exists():
        return {"total_capital": 0, "positions": []}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {"total_capital": 0, "positions": []}


def _read_trades() -> List[dict]:
    p = DATA_DIR / "trades.jsonl"
    if not p.exists():
        return []
    trades = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return trades


def _read_equity_log() -> List[dict]:
    p = DATA_DIR / "equity_log.jsonl"
    if not p.exists():
        return []
    rows = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def _fetch_price(market_id: str) -> float | None:
    now = time.time()
    if market_id in _price_cache:
        price, fetched_at = _price_cache[market_id]
        if now - fetched_at < _CACHE_TTL:
            return price

    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": market_id},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                mkt = data[0] if isinstance(data, list) else data
                price_str = mkt.get("lastTradePrice") or mkt.get("price") or mkt.get("outcomePrices", "0.5")
                if isinstance(price_str, list):
                    price = float(price_str[0])
                else:
                    price = float(price_str)
                _price_cache[market_id] = (price, now)
                return price
    except Exception:
        pass
    return None


def _calc_pnl(pos: dict, current_price: float | None) -> float | None:
    if current_price is None:
        return None
    ep = float(pos.get("entry_price", 0))
    size = float(pos.get("size_usd", 0))
    side = str(pos.get("side", "BUY")).upper()
    if ep <= 0:
        return None
    if side == "BUY":
        shares = size / ep
        return round((current_price - ep) * shares, 2)
    else:
        entry_no = 1.0 - ep
        shares = size / max(entry_no, 1e-6)
        return round((entry_no - (1.0 - current_price)) * shares, 2)


def _metrics(trades: List[dict], equity_log: List[dict], initial_capital: float) -> dict:
    closes = [t for t in trades if t.get("action") == "CLOSE"]
    wins   = [t for t in closes if float(t.get("net_pnl", 0)) > 0]
    losses = [t for t in closes if float(t.get("net_pnl", 0)) <= 0]
    total_pnl  = sum(float(t.get("net_pnl", 0)) for t in closes)
    win_rate   = len(wins) / len(closes) if closes else 0.0
    avg_win    = sum(float(t.get("net_pnl", 0)) for t in wins)  / max(len(wins), 1)
    avg_loss   = sum(float(t.get("net_pnl", 0)) for t in losses) / max(len(losses), 1)
    profit_factor = (avg_win * len(wins)) / max(abs(avg_loss * len(losses)), 1e-6) if losses else float("inf")

    # Sharpe and max drawdown from equity log
    sharpe = None
    max_dd = None
    if len(equity_log) >= 2:
        equities = [float(r["equity"]) for r in equity_log]
        returns  = [(equities[i] - equities[i-1]) / max(equities[i-1], 1e-6)
                    for i in range(1, len(equities))]
        if returns:
            mean_r = sum(returns) / len(returns)
            std_r  = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))
            # Annualise assuming ~96 polls/day (60s interval)
            ann = math.sqrt(96 * 365)
            sharpe = round((mean_r / std_r) * ann, 2) if std_r > 1e-9 else None

        peak = equities[0]
        worst_dd = 0.0
        for e in equities:
            if e > peak:
                peak = e
            dd = (peak - e) / peak if peak > 0 else 0
            if dd > worst_dd:
                worst_dd = dd
        max_dd = round(worst_dd * 100, 1)

    current_equity = float(equity_log[-1]["equity"]) if equity_log else initial_capital
    total_return_pct = round((current_equity - initial_capital) / max(initial_capital, 1) * 100, 2)

    return {
        "total_trades":    len(closes),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(win_rate * 100, 1),
        "total_pnl":       round(total_pnl, 2),
        "total_return_pct": total_return_pct,
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
        "profit_factor":   round(profit_factor, 2) if profit_factor != float("inf") else "∞",
        "sharpe":          sharpe,
        "max_drawdown_pct": max_dd,
        "current_equity":  round(current_equity, 2),
    }


@app.route("/api/state")
def api_state():
    state    = _read_positions()
    trades   = _read_trades()
    eq_log   = _read_equity_log()
    capital  = float(state.get("total_capital", 0))

    # Enrich open positions with current price + P&L
    positions = []
    for pos in state.get("positions", []):
        mid   = pos.get("market_id", "")
        price = _fetch_price(mid)
        pnl   = _calc_pnl(pos, price)
        ep    = float(pos.get("entry_price", 0))
        size  = float(pos.get("size_usd", 0))
        side  = str(pos.get("side", "BUY")).upper()
        shares = round(size / max(ep if side == "BUY" else (1 - ep), 1e-6), 2)
        positions.append({
            **pos,
            "current_price": price,
            "unrealized_pnl": pnl,
            "shares": shares,
        })

    # Closed trades (most recent first)
    closed = sorted(
        [t for t in trades if t.get("action") == "CLOSE"],
        key=lambda t: str(t.get("ts", t.get("datetime", ""))),
        reverse=True,
    )[:50]

    # Equity curve (downsample to last 500 points)
    curve = eq_log[-500:] if len(eq_log) > 500 else eq_log

    metrics = _metrics(trades, eq_log, capital)

    return jsonify({
        "updated_at":  datetime.now(timezone.utc).isoformat(),
        "capital":     capital,
        "positions":   positions,
        "closed":      closed,
        "equity_curve": curve,
        "metrics":     metrics,
    })


@app.route("/api/equity")
def api_equity():
    eq_log = _read_equity_log()
    return jsonify(eq_log[-500:] if len(eq_log) > 500 else eq_log)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Whale Strategy Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body { background: #0d1117; color: #c9d1d9; font-family: 'Segoe UI', sans-serif; }
.card { background: #161b22; border: 1px solid #30363d; }
.card-header { background: #21262d; border-bottom: 1px solid #30363d; font-weight: 600; }
.badge-buy  { background: #238636; }
.badge-sell { background: #da3633; }
.pos-pnl    { font-weight: 600; }
.metric-val { font-size: 1.4rem; font-weight: 700; }
.metric-lbl { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; }
.green { color: #3fb950; }
.red   { color: #f85149; }
table { font-size: 0.85rem; }
.ts   { color: #8b949e; font-size: 0.75rem; }
</style>
</head>
<body>
<div class="container-fluid py-3">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h4 class="mb-0">🐳 Whale Strategy — Live Dashboard</h4>
    <span class="ts" id="updated">Loading…</span>
  </div>

  <!-- Metrics row -->
  <div class="row g-2 mb-3" id="metrics-row">
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-equity">—</div>
      <div class="metric-lbl">Equity</div>
    </div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-return">—</div>
      <div class="metric-lbl">Total Return</div>
    </div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-wr">—</div>
      <div class="metric-lbl">Win Rate</div>
    </div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-sharpe">—</div>
      <div class="metric-lbl">Sharpe</div>
    </div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-dd">—</div>
      <div class="metric-lbl">Max Drawdown</div>
    </div></div>
    <div class="col-6 col-md-2"><div class="card p-3 text-center">
      <div class="metric-val" id="m-pf">—</div>
      <div class="metric-lbl">Profit Factor</div>
    </div></div>
  </div>

  <!-- Equity curve -->
  <div class="card mb-3">
    <div class="card-header">Equity Curve</div>
    <div class="card-body" style="height:220px">
      <canvas id="equityChart"></canvas>
    </div>
  </div>

  <div class="row g-3">
    <!-- Open positions -->
    <div class="col-12 col-lg-7">
      <div class="card">
        <div class="card-header d-flex justify-content-between">
          Open Positions <span class="badge bg-secondary" id="pos-count">0</span>
        </div>
        <div class="card-body p-0">
          <table class="table table-dark table-hover mb-0">
            <thead><tr>
              <th>Market</th><th>Side</th><th>Entry</th><th>Shares</th>
              <th>Size $</th><th>Entry Date</th><th>Unreal. P&L</th>
            </tr></thead>
            <tbody id="pos-tbody"><tr><td colspan="7" class="text-center ts">No open positions</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Closed trades -->
    <div class="col-12 col-lg-5">
      <div class="card">
        <div class="card-header d-flex justify-content-between">
          Closed Trades <span class="ts" id="closed-summary"></span>
        </div>
        <div class="card-body p-0" style="max-height:400px; overflow-y:auto">
          <table class="table table-dark table-hover mb-0">
            <thead><tr><th>Market</th><th>Reason</th><th>Exit $</th><th>Net P&L</th></tr></thead>
            <tbody id="closed-tbody"><tr><td colspan="4" class="text-center ts">No closed trades</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = (n, dec=2) => n == null ? '—' : (n >= 0 ? '+' : '') + parseFloat(n).toFixed(dec);
const fmtUsd = n => n == null ? '—' : '$' + parseFloat(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const colorClass = n => n > 0 ? 'green' : n < 0 ? 'red' : '';

let chart = null;

function buildChart(curve) {
  const labels = curve.map(r => r.ts ? r.ts.slice(11,16) : '');
  const data   = curve.map(r => r.equity);
  const ctx = document.getElementById('equityChart').getContext('2d');
  if (chart) { chart.destroy(); }
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor: '#3fb950',
        backgroundColor: 'rgba(63,185,80,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8b949e', maxTicksLimit: 8 }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e', callback: v => '$'+v.toLocaleString() }, grid: { color: '#21262d' } },
      }
    }
  });
}

function refresh() {
  fetch('/api/state').then(r => r.json()).then(d => {
    $('updated').textContent = 'Updated ' + new Date(d.updated_at).toLocaleTimeString();

    const m = d.metrics;
    const retCls = m.total_return_pct >= 0 ? 'green' : 'red';
    $('m-equity').textContent  = fmtUsd(m.current_equity);
    $('m-return').innerHTML    = `<span class="${retCls}">${fmt(m.total_return_pct, 1)}%</span>`;
    $('m-wr').textContent      = m.win_rate + '%';
    $('m-sharpe').textContent  = m.sharpe != null ? m.sharpe : '—';
    $('m-dd').innerHTML        = m.max_drawdown_pct != null
      ? `<span class="red">${m.max_drawdown_pct}%</span>` : '—';
    $('m-pf').textContent      = m.profit_factor;

    // Equity curve
    if (d.equity_curve && d.equity_curve.length > 1) buildChart(d.equity_curve);

    // Open positions
    $('pos-count').textContent = d.positions.length;
    const posHtml = d.positions.length === 0
      ? '<tr><td colspan="7" class="text-center ts">No open positions</td></tr>'
      : d.positions.map(p => {
          const pnl = p.unrealized_pnl;
          const pnlHtml = pnl == null
            ? '<span class="ts">pending</span>'
            : `<span class="pos-pnl ${colorClass(pnl)}">${fmt(pnl)} $</span>`;
          const entryDate = p.entry_date ? p.entry_date.slice(0,16).replace('T',' ') : '—';
          const mktTitle  = (p.market_title || p.market_id || '').slice(0,35);
          const side      = (p.side || 'BUY').toUpperCase();
          const ep        = parseFloat(p.entry_price || 0);
          return `<tr>
            <td title="${p.market_id}">${mktTitle}</td>
            <td><span class="badge ${side==='BUY'?'badge-buy':'badge-sell'}">${side}</span></td>
            <td>${(ep*100).toFixed(1)}¢</td>
            <td>${p.shares || '—'}</td>
            <td>${fmtUsd(p.size_usd)}</td>
            <td class="ts">${entryDate}</td>
            <td>${pnlHtml}</td>
          </tr>`;
        }).join('');
    $('pos-tbody').innerHTML = posHtml;

    // Closed trades
    const totalPnl = m.total_pnl;
    $('closed-summary').textContent =
      `${m.total_trades} trades  ${fmt(totalPnl)}$`;
    const closedHtml = d.closed.length === 0
      ? '<tr><td colspan="4" class="text-center ts">No closed trades</td></tr>'
      : d.closed.map(t => {
          const pnl = parseFloat(t.net_pnl || 0);
          const ep  = parseFloat(t.exit_price || 0);
          const mid = (t.market_title || t.market_id || '').slice(0,30);
          return `<tr>
            <td title="${t.market_id}">${mid}</td>
            <td class="ts">${(t.reason||'').slice(0,18)}</td>
            <td>${(ep*100).toFixed(1)}¢</td>
            <td class="pos-pnl ${colorClass(pnl)}">${fmt(pnl)} $</td>
          </tr>`;
        }).join('');
    $('closed-tbody').innerHTML = closedHtml;
  }).catch(e => console.error('refresh error', e));
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(_TEMPLATE)


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
