"""The six charts of 9.4, built from metrics-service rows.

Rules: a finding title generated from the data; axis titles with units; a legend only for >1 series; a
tooltip on every mark; one neutral grey for data and one accent for what the title is about; bars start at
zero; no pie/donut/3D/dual axes/gauges; a 'View data' table; hidden with a one-line reason when the data is
insufficient. Each function returns {"id", "title", "hidden", "reason", "figure", "table"}.
"""
from __future__ import annotations

from decimal import Decimal

from ..engine.money import D
from . import fmt_money, fmt_pct

GREY = "#77818F"
ACCENT = "#1F5FD6"
RED = "#B4483B"
INK = "#1E2329"
GRID = "#E6E8EB"
FONT = dict(family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif", size=13, color=INK)


def _layout(xtitle, ytitle, **kw):
    lay = dict(font=FONT, paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF", margin=dict(l=10, r=40, t=36, b=64),
               xaxis=dict(title=dict(text=xtitle), gridcolor=GRID, zerolinecolor="#C9CED4", automargin=True),
               yaxis=dict(title=dict(text=ytitle), gridcolor=GRID, zerolinecolor="#C9CED4", automargin=True),
               showlegend=False, hoverlabel=dict(bgcolor="#FFFFFF", font=FONT), bargap=0.35)
    for k, v in kw.items():
        if k in ("xaxis", "yaxis") and isinstance(v, dict):
            lay[k] = {**lay[k], **v}
        else:
            lay[k] = v
    return lay


def _f(x):
    return float(D(x))  # chart coordinates only; every displayed value is formatted from Decimal


def hidden(cid, reason):
    return dict(id=cid, hidden=True, reason=reason, title=None, figure=None, table=None)


def h1(rows):
    """Controller home: are we recovering the leakage we confirm?"""
    if len(rows) < 2:
        return hidden("H1", "Recovery chart appears when at least 2 periods have results.")
    conf = sum((r["confirmed"] for r in rows), D(0))
    inv = sum((r["invoiced"] for r in rows), D(0))
    title = (f"{fmt_pct(inv / conf * 100)} of confirmed leakage invoiced over the last {len(rows)} periods ({fmt_money(inv, 0)} of {fmt_money(conf, 0)})"
             if conf else f"No leakage confirmed in the last {len(rows)} periods")
    x = [r["period"] for r in rows]
    fig = dict(data=[
        dict(type="bar", name="Confirmed", x=x, y=[_f(r["confirmed"]) for r in rows], marker=dict(color=GREY),
             customdata=[fmt_money(r["confirmed"]) for r in rows], hovertemplate="%{x}<br>Confirmed: %{customdata}<extra></extra>"),
        dict(type="bar", name="Invoiced", x=x, y=[_f(r["invoiced"]) for r in rows], marker=dict(color=ACCENT),
             customdata=[fmt_money(r["invoiced"]) for r in rows], hovertemplate="%{x}<br>Invoiced: %{customdata}<extra></extra>")],
        layout=_layout("Billing period", "USD", barmode="group", showlegend=True, legend=dict(orientation="h", y=1.12),
                       yaxis=dict(title=dict(text="USD"), rangemode="tozero", gridcolor=GRID, tickprefix="$", separatethousands=True)))
    return dict(id="H1", hidden=False, title=title, figure=fig,
                table=dict(columns=["Period", "Confirmed", "Invoiced"], rows=[[r["period"], fmt_money(r["confirmed"]), fmt_money(r["invoiced"])] for r in rows]))


def l1(rows):
    """Leakage review: which kind of leakage costs the most right now?"""
    if not rows:
        return hidden("L1", "No open leakage items for the current filter.")
    top = rows[0]
    title = f"{top['label']} is the largest open leakage: {fmt_money(top['amount'], 0)}"
    rr = list(reversed(rows))
    fig = dict(data=[dict(type="bar", orientation="h", y=[r["label"] for r in rr], x=[_f(r["amount"]) for r in rr],
                          marker=dict(color=[ACCENT if r is top else GREY for r in rr]), customdata=[[r["rule"], fmt_money(r["amount"])] for r in rr],
                          hovertemplate="%{y} (%{customdata[0]})<br>Open gap: %{customdata[1]}<extra></extra>")],
               layout=_layout("Open gap (USD)", "Rule", height=max(220, 46 * len(rows) + 120),
                              xaxis=dict(title=dict(text="Open gap (USD)"), rangemode="tozero", gridcolor=GRID, tickprefix="$", separatethousands=True)))
    return dict(id="L1", hidden=False, title=title, figure=fig, clickable="rule",
                table=dict(columns=["Rule", "Open gap"], rows=[[f"{r['label']} ({r['rule']})", fmt_money(r["amount"])] for r in rows]))


def c1(rows):
    """Customers: which customers earn the least, and why is the margin what it is?"""
    if rows and all(r["total_cost"] is None for r in rows):
        return hidden("C1", "Margins appear when expense data is imported for this period.")
    usable = [r for r in rows if r["revenue"] > 0 and r["total_cost"] is not None]
    if not usable:
        return hidden("C1", "No customer revenue in this period yet.")
    ordered = sorted(usable, key=lambda r: (r["margin_pct"] if r["margin_pct"] is not None else D(-10**9)))
    low = ordered[0]
    title = f"{low['customer_name'] or low['customer']} has the lowest margin: {fmt_pct(low['margin_pct'])}"
    rr = list(reversed(ordered))  # lowest at top
    y = [r["customer_name"] or r["customer"] for r in rr]
    cost_x = [_f(min(r["total_cost"], r["revenue"])) for r in rr]
    prof_x = [_f(max(r["site_profit"], 0)) for r in rr]
    loss_x = [_f(max(-r["site_profit"], 0)) for r in rr]
    data = [
        dict(type="bar", orientation="h", name="Allocated cost", y=y, x=cost_x, marker=dict(color="#C9CED4"),
             customdata=[[fmt_money(r["total_cost"]), fmt_money(r["revenue"])] for r in rr],
             hovertemplate="%{y}<br>Cost: %{customdata[0]}<br>Revenue: %{customdata[1]}<extra></extra>"),
        dict(type="bar", orientation="h", name="Site profit", y=y, x=prof_x,
             marker=dict(color=[ACCENT if r is low else GREY for r in rr]),
             customdata=[[fmt_money(r["site_profit"]), fmt_pct(r["margin_pct"])] for r in rr],
             text=[fmt_pct(r["margin_pct"]) if r["site_profit"] >= 0 else "" for r in rr], textposition="outside", cliponaxis=False,
             hovertemplate="%{y}<br>Site profit: %{customdata[0]}<br>Margin: %{customdata[1]}<extra></extra>"),
    ]
    if any(loss_x):
        data.append(dict(type="bar", orientation="h", name="Loss", y=y, x=loss_x, marker=dict(color=RED),
                         text=[f"Loss {fmt_pct(r['margin_pct'])}" if r["site_profit"] < 0 else "" for r in rr], textposition="outside",
                         customdata=[fmt_money(r["site_profit"]) for r in rr], hovertemplate="%{y}<br>Loss: %{customdata}<extra></extra>"))
    fig = dict(data=data, layout=_layout("USD (cost + site profit = revenue)", "Customer", barmode="stack", showlegend=True,
                                         legend=dict(orientation="h", y=1.1), height=max(260, 48 * len(rr) + 150),
                                         xaxis=dict(title=dict(text="USD (cost + site profit = revenue)"), rangemode="tozero", gridcolor=GRID, tickprefix="$", separatethousands=True)))
    return dict(id="C1", hidden=False, title=title, figure=fig,
                table=dict(columns=["Customer", "Revenue", "Total cost", "Site profit", "Margin"],
                           rows=[[r["customer_name"] or r["customer"], fmt_money(r["revenue"]), fmt_money(r["total_cost"]), fmt_money(r["site_profit"]),
                                  fmt_pct(r["margin_pct"])] for r in ordered]))


def c2(row, categories):
    """Customer detail: what turns this customer's revenue into its profit? (waterfall)"""
    if row is None or row["revenue"] == 0:
        return hidden("C2", "No revenue for this customer in this period.")
    if row["site_profit"] is None:
        return hidden("C2", "The revenue-to-profit bridge appears when expense data is imported for this period.")
    cats = sorted(categories, key=lambda c: -c["amount"])
    if len(cats) > 6:
        other = sum((c["amount"] for c in cats[5:]), D(0))
        cats = cats[:5] + [dict(label="Other", amount=other)]
    biggest = cats[0] if cats else None
    title = (f"{biggest['label']} takes the most of {row['customer_name'] or row['customer']}'s revenue: {fmt_money(biggest['amount'], 0)}"
             if biggest else "No cost assigned to this customer")
    x = ["Revenue"] + [c["label"] for c in cats] + ["Site profit"]
    y = [_f(row["revenue"])] + [-_f(c["amount"]) for c in cats] + [_f(row["site_profit"])]
    texts = [fmt_money(row["revenue"], 0)] + [fmt_money(-c["amount"], 0) for c in cats] + [fmt_money(row["site_profit"], 0)]
    fig = dict(data=[dict(type="waterfall", x=x, y=y, measure=["absolute"] + ["relative"] * len(cats) + ["total"], text=texts,
                          textposition="outside", customdata=texts, hovertemplate="%{x}: %{customdata}<extra></extra>",
                          decreasing=dict(marker=dict(color="#C98A82")), increasing=dict(marker=dict(color=GREY)),
                          totals=dict(marker=dict(color=GREY)), connector=dict(line=dict(color="#C9CED4", width=1)))],
               layout=_layout("", "USD", yaxis=dict(title=dict(text="USD"), rangemode="tozero", gridcolor=GRID, tickprefix="$", separatethousands=True)))
    return dict(id="C2", hidden=False, title=title, figure=fig, key="red = cost",
                table=dict(columns=["Step", "Amount"], rows=[[a, b] for a, b in zip(x, texts)]))


def c3(trend):
    """Customer detail: is this customer's margin getting better or worse?"""
    if len(trend) < 3:
        return hidden("C3", "Trend appears after 3 closed periods.")
    first, last = trend[0]["margin"], trend[-1]["margin"]
    word = "improved" if last > first else "worsened" if last < first else "held"
    title = f"Margin {word} from {fmt_pct(first)} to {fmt_pct(last)} over {len(trend)} closed periods"
    x = [t["period"] for t in trend]
    fig = dict(data=[
        dict(type="scatter", mode="lines+markers", name="Customer margin", x=x, y=[_f(t["margin"]) for t in trend], line=dict(color=ACCENT, width=2),
             marker=dict(size=8), customdata=[fmt_pct(t["margin"]) for t in trend], hovertemplate="%{x}<br>Margin: %{customdata}<extra></extra>"),
        dict(type="scatter", mode="lines", name="Site margin", x=x, y=[_f(t["site_margin"]) if t["site_margin"] is not None else None for t in trend],
             line=dict(color=GREY, width=2, dash="dash"), customdata=[fmt_pct(t["site_margin"]) for t in trend],
             hovertemplate="%{x}<br>Site margin: %{customdata}<extra></extra>")],
        layout=_layout("Closed period", "Margin (%)", showlegend=True, legend=dict(orientation="h", y=1.12),
                       yaxis=dict(title=dict(text="Margin (%)"), ticksuffix="%", gridcolor=GRID)))
    return dict(id="C3", hidden=False, title=title, figure=fig,
                table=dict(columns=["Period", "Margin", "Site margin"], rows=[[t["period"], fmt_pct(t["margin"]), fmt_pct(t["site_margin"])] for t in trend]))


def k1(cats, total):
    """Costs: where does the operating money go?"""
    if not cats:
        return hidden("K1", "No expense lines for this period.")
    top = cats[0]
    title = f"{top['label']} is the largest cost: {fmt_money(top['amount'], 0)} ({fmt_pct(top['share'])} of {fmt_money(total, 0)})"
    rr = list(reversed(cats))
    ylab = [c["label"] for c in rr]
    maxlines = max(len(c["lines"]) for c in rr)
    data = []
    for i in range(maxlines):
        xs, cd, colors = [], [], []
        for c in rr:
            line = c["lines"][i] if i < len(c["lines"]) else None
            xs.append(_f(line["amount"]) if line else 0)
            cd.append([line["description"] if line else "", fmt_money(line["amount"]) if line else "", c["label"]])
            colors.append(ACCENT if c is top else GREY)
        data.append(dict(type="bar", orientation="h", y=ylab, x=xs, customdata=cd, marker=dict(color=colors, line=dict(color="#FFFFFF", width=2)),
                         hovertemplate="%{customdata[2]}<br>%{customdata[0]}: %{customdata[1]}<extra></extra>", showlegend=False))
    data.append(dict(type="scatter", mode="text", y=ylab, x=[_f(c["amount"]) for c in rr], text=[f"  {fmt_money(c['amount'], 0)} · {fmt_pct(c['share'])}" for c in rr],
                     textposition="middle right", hoverinfo="skip", showlegend=False, cliponaxis=False))
    fig = dict(data=data, layout=_layout("USD", "Expense category", barmode="stack", height=max(300, 36 * len(rr) + 130),
                                         margin=dict(l=10, r=150, t=20, b=64),
                                         xaxis=dict(title=dict(text="USD"), rangemode="tozero", gridcolor=GRID, tickprefix="$", separatethousands=True)))
    return dict(id="K1", hidden=False, title=title, figure=fig,
                table=dict(columns=["Category", "Expense line", "Amount", "% of total"],
                           rows=[[c["label"], l["description"], fmt_money(l["amount"]), fmt_pct(c["share"]) if j == 0 else ""] for c in cats for j, l in enumerate(c["lines"])]))
