#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard.py — Le desk du fonds : une page qui montre ce que le fonds a fait et pourquoi
=======================================================================================

Contenu, du plus important au plus détaillé :
  * l'état du fonds (rendement, pire perte, Sharpe, lignes) et sa courbe face au marché ;
  * les dernières décisions, chacune sous forme de fiche (quoi, pourquoi, historique, aujourd'hui) ;
  * le radar des grands acteurs (heure, jour, semaine, mois) ;
  * la position des banques sur les contrats à terme (CFTC) ;
  * le portefeuille et le signal de chaque ligne.

La page est autonome (HTML + CSS, quelques lignes de JavaScript pour filtrer les fiches) et
s'adapte au thème clair ou sombre. Elle se publie telle quelle comme page web privée.

Usage :
    python backtest/dashboard.py                                  # démonstration (données simulées)
    python backtest/dashboard.py --data-dir data/phase1/engine    # données réelles
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import flow_backtest as fb
import trade_cards as tc

ROOT = Path(__file__).resolve().parents[1]
CHIP = {"ACHAT": "buy", "RENFORCEMENT": "buy", "ALLÈGEMENT": "warn", "VENTE": "sell"}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _pct(x: float, digits: int = 1, signed: bool = True) -> str:
    if x is None or not np.isfinite(x):
        return "n.d."
    return (f"{x:+.{digits}%}" if signed else f"{x:.{digits}%}").replace(".", ",")


def _chart_svg(equity: pd.Series, bench: pd.Series) -> str:
    """Courbe base 100 du fonds (bleu) et du marché (gris), échelle linéaire, étiquettes aux extrémités."""
    s = (equity / equity.iloc[0] * 100).resample("W").last().dropna()
    b = (bench / bench.iloc[0] * 100).resample("W").last().reindex(s.index).ffill()
    w, h, left, right, top, bottom = 640, 200, 44, 70, 12, 26
    lo, hi = float(min(s.min(), b.min())), float(max(s.max(), b.max()))
    step = 50 if hi - lo > 150 else 25
    y0, y1 = step * np.floor(lo / step), step * np.ceil(hi / step)

    def x(i):
        return left + (w - left - right) * i / max(1, len(s) - 1)

    def y(v):
        return top + (h - top - bottom) * (1 - (v - y0) / (y1 - y0))

    grid = []
    for v in np.arange(y0, y1 + 0.1, step):
        grid.append(f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                    f'<text x="{left - 6}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">{v:.0f}</text>')
    years = sorted(set(s.index.year))
    for yr in years[:: max(1, len(years) // 6)]:
        i = int(np.argmax(s.index.year == yr))
        grid.append(f'<text x="{x(i):.1f}" y="{h - 6}" class="axis" text-anchor="middle">{yr}</text>')

    def path(series):
        return " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series.to_numpy()))

    end = len(s) - 1
    return f"""<svg viewBox="0 0 {w} {h}" role="img" aria-label="Valeur du fonds et du marché, base 100">
  {''.join(grid)}
  <path d="{path(b)}" class="line-bench"/>
  <path d="{path(s)}" class="line-fund"/>
  <circle cx="{x(end):.1f}" cy="{y(s.iloc[-1]):.1f}" r="3.5" class="dot-fund"/>
  <text x="{x(end) + 8:.1f}" y="{y(s.iloc[-1]) + 4:.1f}" class="end-label">{s.iloc[-1]:.0f}</text>
  <text x="{x(end) + 8:.1f}" y="{y(b.iloc[-1]) + 4:.1f}" class="end-label muted">{b.iloc[-1]:.0f}</text>
</svg>"""


def _card_html(card: tc.TradeCard) -> str:
    kind = CHIP.get(card.title.split(" ")[0], "warn")
    reasons = "".join(f"<li>{esc(r)}</li>" for r in card.reasons)
    return f"""<article class="ticket" data-kind="{kind}">
  <header class="ticket-head">
    <span class="chip {kind}">{esc(card.title)}</span>
    <span class="ticker">{esc(card.asset)}</span>
    <time>{card.date:%d/%m/%Y}</time>
  </header>
  <ul class="reasons">{reasons}</ul>
  <p class="history"><span class="label">Historique</span>{esc(card.history)}</p>
  <p class="status"><span class="label">Aujourd'hui</span>{esc(card.status)}</p>
</article>"""


def _radar_html(result, lookback: int = 10) -> str:
    radar = result.signals.radar
    if radar is None:
        return '<p class="empty">Radar indisponible : pas de volume dans les données.</p>'
    rows = []
    n = len(radar.score)
    for i in range(n - 1, max(-1, n - 1 - lookback), -1):
        alerts = radar.alerts(date=radar.score.index[i])
        for a in alerts.itertuples():
            rows.append(a)
        if len(rows) >= 8:
            break
    if not rows:
        return f'<p class="empty">Aucune trace anormale sur les {lookback} dernières séances.</p>'
    items = []
    for a in rows[:8]:
        kind = "buy" if a.score > 0 else "sell"
        items.append(f"""<li class="alert">
  <div class="alert-head"><span class="chip {kind}">{esc(a.sens)}</span><span class="ticker">{esc(a.asset)}</span>
  <span class="score">score {a.score:+.1f}</span><time>{a.date:%d/%m}</time></div>
  <p>{esc(a.explication)}</p></li>""")
    return f'<ul class="alerts">{"".join(items)}</ul>'


def _banks_html(cot: Optional[pd.DataFrame]) -> str:
    if cot is None or cot.empty:
        return '<p class="empty">Données CFTC non téléchargées (étape <code>cot</code>).</p>'
    rows = []
    for market, grp in cot.sort_values("report_date").groupby("market"):
        last = grp.iloc[-1]
        habit = grp["dealer_net_pct_oi"].median()
        rank = (grp["dealer_net_pct_oi"] <= last["dealer_net_pct_oi"]).mean()
        more = last["dealer_net_pct_oi"] < habit
        rows.append(f"""<div class="bank">
  <div class="bank-name">{esc(market)}<span class="chip {'warn' if more else 'neutral'}">{'plus vendeuses' if more else 'moins vendeuses'} que d'habitude</span></div>
  <div class="bank-figures"><span class="big">{_pct(last['dealer_net_pct_oi'], 0)}</span>
  <span class="muted">des positions ouvertes, habituellement {_pct(habit, 0)} · rang historique {rank:.0%}</span></div>
  <div class="muted small">Rapport du {pd.Timestamp(last['report_date']):%d/%m/%Y}</div></div>""")
    return ("".join(rows) + '<p class="note">Les banques couvrent les achats de leurs clients : elles sont presque '
            "toujours vendeuses nettes. Seul l'écart à leur habitude compte.</p>")


def _portfolio_html(result, review: pd.DataFrame) -> str:
    if review.empty:
        return '<p class="empty">Aucune ligne en portefeuille.</p>'
    trades = result.trades[result.trades["exit_reason"] == "EN_COURS"].set_index("asset")
    rows = []
    for _, r in review.sort_values("weight", ascending=False).iterrows():
        ret = trades["return"].get(r["asset"], np.nan)
        signal = r["exit_signal"].split(" (")[0]
        kind = "buy" if signal.startswith(("CONSERVER", "RENFORCER")) else ("sell" if signal.startswith("VENTE") else "warn")
        rows.append(f"<tr><td class='ticker'>{esc(r['asset'])}</td><td class='num'>{_pct(r['weight'], 1, False)}</td>"
                    f"<td class='num'>{_pct(ret)}</td><td class='num'>{_pct(r['p_up_M+3'], 0, False)}</td>"
                    f"<td><span class='chip {kind}'>{esc(signal.lower())}</span></td></tr>")
    return f"""<div class="table-wrap"><table>
<thead><tr><th>Ligne</th><th class="num">Poids</th><th class="num">Depuis l'achat</th><th class="num">Hausse à 3 mois</th><th>Signal</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


CSS = """
:root {
  --bg: #f4f6f9; --surface: #ffffff; --ink: #121a24; --muted: #5a6573; --line: #dde3ea;
  --accent: #2a78d6; --bench: #8a8f98; --buy: #147a47; --buy-bg: #e3f3ea; --sell: #b8352a; --sell-bg: #fbe7e4;
  --warn: #9a5b00; --warn-bg: #fbefd9; --neutral-bg: #eceff3;
  --display: "IBM Plex Sans Condensed", "Arial Narrow", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0e131a; --surface: #151c25; --ink: #e5eaf0; --muted: #98a3b1; --line: #263140;
    --accent: #5b9bf0; --bench: #7d8590; --buy: #4cc98a; --buy-bg: #12301f; --sell: #ff7a6b; --sell-bg: #3a1a17;
    --warn: #f0b04a; --warn-bg: #36280e; --neutral-bg: #1e2733;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0e131a; --surface: #151c25; --ink: #e5eaf0; --muted: #98a3b1; --line: #263140;
  --accent: #5b9bf0; --bench: #7d8590; --buy: #4cc98a; --buy-bg: #12301f; --sell: #ff7a6b; --sell-bg: #3a1a17;
  --warn: #f0b04a; --warn-bg: #36280e; --neutral-bg: #1e2733;
}
body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
.wrap { max-width: 1180px; margin: 0 auto; padding-inline: 16px; padding-block: 20px 40px; display: grid; gap: 20px; }
h1, h2 { font-family: var(--display); font-weight: 600; text-wrap: balance; margin: 0; }
h1 { font-size: 1.9rem; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.top { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 16px; }
.source { font-size: 0.85rem; padding: 4px 10px; border-radius: 999px; background: var(--warn-bg); color: var(--warn); font-weight: 600; }
.source.real { background: var(--buy-bg); color: var(--buy); }
.asof { color: var(--muted); font-size: 0.9rem; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.kpi { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: grid; gap: 2px; }
.kpi .value { font-family: var(--mono); font-size: 1.45rem; font-variant-numeric: tabular-nums; }
.kpi .label, .label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.kpi .sub { font-size: 0.82rem; color: var(--muted); }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; min-width: 0; }
.chart svg { width: 100%; height: auto; display: block; }
.grid { stroke: var(--line); stroke-width: 1; }
.axis { fill: var(--muted); font: 11px var(--mono); }
.line-fund { fill: none; stroke: var(--accent); stroke-width: 2.2; }
.line-bench { fill: none; stroke: var(--bench); stroke-width: 1.4; }
.dot-fund { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
.end-label { fill: var(--ink); font: 600 12px var(--mono); }
.end-label.muted { fill: var(--muted); font-weight: 400; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 0.85rem; color: var(--muted); }
.legend span::before { content: ""; display: inline-block; width: 14px; height: 3px; margin-right: 6px; vertical-align: middle; background: var(--accent); }
.legend span.bench::before { background: var(--bench); }
.main { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 20px; align-items: start; }
.side { display: grid; gap: 20px; }
@media (max-width: 860px) { .main { grid-template-columns: 1fr; } }
.filters { display: flex; flex-wrap: wrap; gap: 8px; }
.filters button { font: 600 0.85rem var(--body); color: var(--ink); background: var(--neutral-bg); border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px; cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.filters button:focus-visible, .ticket:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tickets { display: grid; gap: 12px; }
.ticket { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: grid; gap: 8px; background: var(--surface); }
.ticket-head, .alert-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.ticket-head time, .alert-head time { margin-left: auto; color: var(--muted); font: 0.85rem var(--mono); }
.ticker { font: 600 1rem var(--mono); letter-spacing: 0.02em; }
.chip { font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; padding: 3px 9px; border-radius: 999px; background: var(--neutral-bg); color: var(--muted); white-space: nowrap; }
.chip.buy { background: var(--buy-bg); color: var(--buy); }
.chip.sell { background: var(--sell-bg); color: var(--sell); }
.chip.warn { background: var(--warn-bg); color: var(--warn); }
.reasons { margin: 0; padding-left: 18px; display: grid; gap: 4px; font-size: 0.92rem; }
.history, .status { margin: 0; font-size: 0.92rem; display: grid; gap: 2px; }
.alerts { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.alert { border-top: 1px solid var(--line); padding-top: 10px; }
.alert:first-child { border-top: 0; padding-top: 0; }
.alert p { margin: 6px 0 0; font-size: 0.9rem; }
.score { font: 0.85rem var(--mono); color: var(--muted); }
.bank { display: grid; gap: 4px; border-top: 1px solid var(--line); padding-top: 10px; }
.bank:first-child { border-top: 0; padding-top: 0; }
.bank-name { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-weight: 600; }
.bank-figures { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; }
.big { font: 600 1.35rem var(--mono); }
.muted { color: var(--muted); }
.small, .note { font-size: 0.82rem; }
.note { color: var(--muted); margin: 0; }
.empty { color: var(--muted); margin: 0; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th { text-align: left; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--line); }
td { padding: 7px 8px; border-bottom: 1px solid var(--line); }
.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
footer { color: var(--muted); font-size: 0.82rem; }
@media (prefers-reduced-motion: no-preference) { .ticket { transition: border-color .15s; } .ticket:hover { border-color: var(--accent); } }
"""

SCRIPT = """
<script>
(function () {
  var buttons = document.querySelectorAll('.filters button');
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'); });
      var kind = btn.getAttribute('data-filter');
      document.querySelectorAll('.ticket').forEach(function (t) {
        t.hidden = !(kind === 'all' || t.getAttribute('data-kind') === kind);
      });
    });
  });
})();
</script>
"""


def render_dashboard(result, review: pd.DataFrame, cards: list[tc.TradeCard], cot: Optional[pd.DataFrame],
                     synthetic: bool, label: str, benchmark: Optional[pd.Series] = None,
                     benchmark_label: str = "univers équipondéré") -> str:
    """`benchmark` : indice de comparaison (S&P 500 via SPY sur données réelles) ; à défaut,
    l'univers équipondéré du moteur, qui hérite du biais du survivant de l'univers."""
    m, b = result.metrics, result.benchmark_metrics
    bench = result.benchmark
    if benchmark is not None:
        bench = benchmark.reindex(result.equity.index).ffill().dropna()
        b = fb.compute_metrics(bench, fb.StrategyConfig().risk_free_rate)
    asof = result.equity.index[-1]
    n_lines = int((result.scales.iloc[-1] > 0).sum())
    source = ('<span class="source">Données simulées — démonstration</span>' if synthetic
              else '<span class="source real">Données réelles</span>')
    kpis = [
        ("Rendement annuel", _pct(m["cagr"]), f"marché : {_pct(b['cagr'])}"),
        ("Pire perte", _pct(m["max_drawdown"]), f"marché : {_pct(b['max_drawdown'])}"),
        ("Rendement / risque", f"{m['sharpe']:.2f}".replace(".", ","), f"Sharpe · marché : {b['sharpe']:.2f}".replace(".", ",")),
        ("Lignes détenues", str(n_lines), f"part investie : {_pct(result.gross.iloc[-1], 0, False)}"),
    ]
    kpi_html = "".join(f'<div class="kpi"><span class="label">{esc(k)}</span><span class="value">{esc(v)}</span>'
                       f'<span class="sub">{esc(s)}</span></div>' for k, v, s in kpis)
    tickets = "".join(_card_html(c) for c in cards)
    return f"""<title>Desk Smart Money</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <div class="top">
    <h1>Desk Smart Money</h1>
    <div class="top">{source}<span class="asof">Situation au {asof:%d/%m/%Y} · {esc(label)}</span></div>
  </div>
  <section class="kpis" aria-label="État du fonds">{kpi_html}</section>
  <section class="panel chart" aria-label="Courbe du fonds">
    <h2>Le fonds face au marché</h2>
    {_chart_svg(result.equity, bench)}
    <div class="legend"><span>Fonds (après frais)</span><span class="bench">Marché ({esc(benchmark_label)})</span></div>
  </section>
  <div class="main">
    <section class="panel" aria-label="Dernières décisions">
      <h2>Dernières décisions</h2>
      <div class="filters" role="group" aria-label="Filtrer les décisions">
        <button type="button" id="f-all" data-filter="all" aria-pressed="true">Toutes</button>
        <button type="button" id="f-buy" data-filter="buy" aria-pressed="false">Achats</button>
        <button type="button" id="f-warn" data-filter="warn" aria-pressed="false">Allègements</button>
        <button type="button" id="f-sell" data-filter="sell" aria-pressed="false">Ventes</button>
      </div>
      <div class="tickets">{tickets}</div>
    </section>
    <div class="side">
      <section class="panel" aria-label="Radar des grands acteurs">
        <h2>Radar des grands acteurs</h2>
        <p class="note">Volumes anormaux recoupés sur l'heure, le jour, la semaine et le mois.</p>
        {_radar_html(result)}
      </section>
      <section class="panel" aria-label="Banques">
        <h2>Banques · contrats à terme</h2>
        <p class="note">Données réelles de la CFTC, publiées chaque vendredi.</p>
        {_banks_html(cot)}
      </section>
      <section class="panel" aria-label="Portefeuille">
        <h2>Portefeuille</h2>
        {_portfolio_html(result, review)}
      </section>
    </div>
  </div>
  <footer>Aucun ordre réel n'est passé : le fonds montre ce qu'il ferait et pourquoi. Les résultats passés,
  simulés ou réels, ne garantissent pas les résultats futurs.</footer>
</div>
{SCRIPT}"""


def load_optional(path: Path, dates: tuple[str, ...]) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    for col in dates:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    return df


def main(argv: Optional[list[str]] = None) -> Path:
    parser = argparse.ArgumentParser(description="Page « Desk Smart Money »")
    parser.add_argument("--data-dir", help="fichiers du moteur (sinon démonstration simulée)")
    parser.add_argument("--out", default=str(ROOT / "outputs" / "desk_smart_money.html"))
    parser.add_argument("--cards", type=int, default=12)
    args = parser.parse_args(argv)
    if args.data_dir:
        data = fb.load_market_from_csv(args.data_dir)
        cfg = fb.StrategyConfig(entry_flow_threshold=0.0, exit_flow_threshold=-0.05)
        synthetic, label = False, "actions américaines"
    else:
        data, cfg = fb.generate_synthetic_market(seed=7), fb.StrategyConfig()
        synthetic, label = True, "24 actifs simulés"
    result = fb.run_backtest(data, cfg)
    review = fb.review_positions(result)
    phase1 = ROOT / "data" / "phase1"
    cot = load_optional(phase1 / "cot_dealers.csv", ("report_date", "available_date"))
    buyers = load_optional(Path(args.data_dir) / "smart_money_buyers.csv", ("period_end", "available_date")) \
        if args.data_dir else None
    # Données simulées : les fiches n'utilisent pas la position réelle des banques (dates sans rapport).
    cards = tc.build_trade_cards(result, n=args.cards, buyers=buyers, cot=None if synthetic else cot,
                                 synthetic=synthetic)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    spy_path = phase1 / "prices" / "SPY.csv"
    benchmark, bench_label = None, "univers équipondéré"
    if not synthetic and spy_path.exists():
        spy = pd.read_csv(spy_path, parse_dates=["date"]).set_index("date")["adjClose"]
        spy.index = pd.DatetimeIndex(spy.index).tz_localize(None) if spy.index.tz is not None else spy.index
        benchmark, bench_label = spy, "S&P 500 · SPY"
    out.write_text(render_dashboard(result, review, cards, cot, synthetic, label, benchmark, bench_label),
                   encoding="utf-8")
    print(f"Page écrite : {out}")
    return out


if __name__ == "__main__":
    main()
