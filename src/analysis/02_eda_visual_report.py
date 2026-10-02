# Databricks notebook source
# MAGIC %md
# MAGIC # Where customer service fails, and what we build
# MAGIC **Factored AI & Data Hackathon 2026 · Exploratory data analysis and workflow selection**
# MAGIC
# MAGIC This notebook tells the data story behind our choice of workflow. Every data chart in sections 1–4, the 5.3 chart and the baseline tiles in 5.4
# MAGIC are computed live from the Bronze layer (`workspace.bronze`, raw and pre-dedup, full history Jun 2023 – Jun 2026). The scorecard
# MAGIC (5.1–5.2), the issue table (4.5), the targets (5.5) and the plan (5.6–5.9) are fixed content taken from the written report. The written report is
# MAGIC `docs/02_eda_workflow_selection.md`; the full audit query set is `src/analysis/01_eda_workflow_selection.py`.
# MAGIC
# MAGIC **How to read it:** each section opens with its takeaway in one line, then the chart that proves it, then the table
# MAGIC behind the chart. Charts are interactive: hover for exact values.
# MAGIC
# MAGIC | # | Section | Question it answers |
# MAGIC |---|---|---|
# MAGIC | 1 | Demand and pain | Which contact reason fails customers most? |
# MAGIC | 2 | Complaints and disputes | Can historical cases ground a dispute agent? |
# MAGIC | 3 | Transactions and labels | Is there a trustworthy label to learn from? |
# MAGIC | 4 | Products, credit and data quality | What must Silver fix before an agent can answer? |
# MAGIC | 5 | Decision | Which workflow, what baseline, what learned component? |

# COMMAND ----------

# MAGIC %md
# MAGIC ### Setup
# MAGIC Plotting helpers and a colorblind-validated palette. Blue marks the element the story is about; grey is context.
# MAGIC The first cell installs Plotly, which newer serverless environments do not ship by default.

# COMMAND ----------

# MAGIC %pip install --quiet plotly==5.24.1

# COMMAND ----------

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# Categorical slots 1-3 pass all-pairs color-vision-deficiency checks on the light surface.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GREY = "#c3c2b7"  # context marks
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"  # status only, always with a label
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]  # sequential blue, light -> dark
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
B = "workspace.bronze"


def q(sql):
    """Run a read-only query and return a pandas DataFrame."""
    return spark.sql(sql).toPandas()


def style(fig, title, subtitle=None, height=360, legend=False, horizontal=False):
    """Apply the house style: left-aligned title + subtitle, hairline grid on the value axis, recessive axes.
    Use horizontal=True for horizontal bar charts (value axis = x)."""
    head = f"<b>{title}</b>"
    if subtitle:
        head += f"<br><span style='font-size:13px;color:{INK2}'>{subtitle}</span>"
    top = 76 if subtitle else 54
    if legend:
        top += 26
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK2, size=13),
        title=dict(text=head, x=0.01, xanchor="left", y=1, yanchor="top", pad=dict(t=16), font=dict(size=17, color=INK)),
        margin=dict(l=12, r=24, t=top, b=48),
        height=height,
        showlegend=legend,
        legend=dict(orientation="h", x=0, xanchor="left", y=1.0, yanchor="bottom", title_text="", font=dict(color=INK2)),
        hoverlabel=dict(bgcolor="white", bordercolor=GRID, font=dict(family=FONT, color=INK, size=12)),
        bargap=0.38,
        bargroupgap=0.08,
    )
    value_axis = dict(showgrid=True, gridcolor=GRID, gridwidth=1, showline=False)
    category_axis = dict(showgrid=False, showline=True, linecolor=AXIS)
    common = dict(ticks="", tickfont=dict(color=MUTED), title_font=dict(color=INK2, size=12), zeroline=False)
    fig.update_xaxes(**(value_axis if horizontal else category_axis), **common)
    fig.update_yaxes(**(category_axis if horizontal else value_axis), **common)
    return fig


def show(fig):
    fig.show(config={"displayModeBar": False, "responsive": True})


try:
    _native_display = display
except NameError:  # outside Databricks
    _native_display = print


def display(df):
    """Interactive table view. Mixed-type text columns are cast to str so Arrow can convert them."""
    if isinstance(df, pd.DataFrame):
        df = df.copy()
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].astype(str)
    _native_display(df)


def emphasize(values, highlight):
    """Blue for the highlighted categories, grey for the rest."""
    highlight = set(highlight if isinstance(highlight, (list, set, tuple)) else [highlight])
    return [BLUE if v in highlight else GREY for v in values]


def tiles(items):
    """KPI tiles. items: list of dicts with value, label and optional note."""
    cards = "".join(
        f"""<div style="flex:1;min-width:170px;background:{SURFACE};border:1px solid rgba(11,11,11,0.10);border-radius:10px;padding:14px 16px">
              <div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">{i['label']}</div>
              <div style="font-size:28px;font-weight:600;color:{INK};margin-top:4px">{i['value']}</div>
              <div style="font-size:12px;color:{INK2};margin-top:4px">{i.get('note', '')}</div>
            </div>"""
        for i in items
    )
    displayHTML(f'<div style="display:flex;gap:12px;flex-wrap:wrap;font-family:{FONT}">{cards}</div>')


def pct(num, den, digits=1):
    return f"{100 * num / den:.{digits}f}%" if den else "n/a"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 · Demand and pain
# MAGIC **Complaint contacts (`Queja`) are 17.1% of all contacts but 41.2% of the unresolved ones: this is where customer service fails.**
# MAGIC
# MAGIC Source: `call_center_interactions` (686,296 contacts, every channel, Jun 2023 – Jun 2026) joined to `satisfaction_surveys`.
# MAGIC Transactional contacts are the largest block, but they already resolve 91.5% at first contact. Satisfaction turns out to
# MAGIC depend only on resolution, so first-contact resolution (FCR) is the KPI that matters.

# COMMAND ----------

# Contact reasons keep their source (Spanish) names; the English gloss is added for readers.
S1_REASON = {
    "Queja": "Queja (complaint)",
    "Transaccional": "Transaccional (transactions)",
    "Producto": "Producto (product)",
    "Técnico": "Técnico (technical)",
    "Comercial": "Comercial (sales, credit)",
    "Retención": "Retención (retention)",
}


def s1_label(reason):
    return S1_REASON.get(reason, reason)


kpi = q(f"""
SELECT reason_category AS reason,
       count(*) AS contacts,
       sum(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END) AS resolved,
       percentile(try_cast(duration_seconds AS DOUBLE), 0.5) AS aht_p50
FROM {B}.call_center_interactions
GROUP BY reason_category
""").set_index("reason")
kpi["unresolved"] = kpi.contacts - kpi.resolved
all_c, all_r, all_u = int(kpi.contacts.sum()), int(kpi.resolved.sum()), int(kpi.unresolved.sum())
q_c, q_r, q_u = (int(kpi.loc["Queja", c]) for c in ("contacts", "resolved", "unresolved"))
q_aht, t_aht = float(kpi.loc["Queja", "aht_p50"]), float(kpi.loc["Transaccional", "aht_p50"])
tiles([
    {"value": f"{all_c:,}", "label": "Contacts analysed",
     "note": "Every channel, Jun 2023 – Jun 2026"},
    {"value": pct(q_r, q_c), "label": "Queja first-contact resolution",
     "note": f"{q_r:,} of {q_c:,} · all contacts {pct(all_r, all_c)}"},
    {"value": pct(q_u, all_u), "label": "Queja share of unresolved",
     "note": f"{q_u:,} of {all_u:,} · but {pct(q_c, all_c)} of all contacts"},
    {"value": f"{q_aht:.0f} s", "label": "Queja median handle time",
     "note": f"{q_aht / t_aht:.1f}× Transaccional ({t_aht:.0f} s) · voice and video"},
])

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1.1 · Which contact reasons hold the unresolved contacts?
# MAGIC `Queja` holds 41.2% of all unresolved contacts on only 17.1% of volume, the largest over-representation of any reason.
# MAGIC `Transaccional` is the mirror image: 35.0% of volume but only 12.7% of unresolved contacts.

# COMMAND ----------

d = q(f"""
SELECT reason_category AS reason,
       count(*) AS contacts,
       sum(CASE WHEN was_resolved = 'True' THEN 0 ELSE 1 END) AS unresolved
FROM {B}.call_center_interactions
GROUP BY reason_category
""")
tot_c, tot_u = int(d.contacts.sum()), int(d.unresolved.sum())
d["share_contacts"] = d.contacts / tot_c
d["share_unresolved"] = d.unresolved / tot_u
d = d.sort_values("share_unresolved", ascending=False).reset_index(drop=True)
d["label"] = d.reason.apply(s1_label)
focus = {"Queja", "Transaccional"}  # the two reasons the story contrasts get direct labels

fig = go.Figure()
for col, n_col, den, name, color in [
    ("share_contacts", "contacts", tot_c, "Share of all contacts", BLUE),
    ("share_unresolved", "unresolved", tot_u, "Share of unresolved contacts", ORANGE),
]:
    fig.add_trace(go.Bar(
        y=d.label, x=d[col], name=name, orientation="h", marker_color=color,
        text=[f"{v:.1%}" if r in focus else "" for r, v in zip(d.reason, d[col])],
        textposition="outside", cliponaxis=False, constraintext="none", textfont=dict(color=INK, size=12),
        customdata=d[[n_col]].values,
        hovertemplate="%{y}<br>" + name + ": %{x:.1%}<br>%{customdata[0]:,} of " + f"{den:,}" + "<extra></extra>",
    ))
fig.update_layout(barmode="group")
fig.update_yaxes(autorange="reversed")
fig.update_xaxes(tickformat=".0%", range=[0, d[["share_contacts", "share_unresolved"]].values.max() * 1.12])
qrow = d[d.reason == "Queja"].iloc[0]
style(
    fig,
    f"Complaints are {qrow.share_contacts:.0%} of contacts but {qrow.share_unresolved:.0%} of unresolved ones",
    f"Share of all contacts (n = {tot_c:,}) and of unresolved contacts (n = {tot_u:,}) by contact reason",
    height=420, legend=True, horizontal=True,
)
fig.update_layout(margin=dict(t=90))  # legend sits right under the subtitle, no empty band
show(fig)
display(pd.DataFrame({
    "Reason": d.reason,
    "Contacts": d.contacts,
    "Share of contacts (%)": (100 * d.share_contacts).round(1),
    "Unresolved": d.unresolved,
    "Share of unresolved (%)": (100 * d.share_unresolved).round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1.2 · How often is each reason resolved at first contact?
# MAGIC `Queja` resolves 43.6% of contacts, the lowest of any reason and 33 points below the 76.6% average.
# MAGIC `Transaccional` already resolves 91.5%, which leaves only 8.5 points of headroom.

# COMMAND ----------

d = q(f"""
SELECT reason_category AS reason,
       count(*) AS contacts,
       sum(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END) AS resolved
FROM {B}.call_center_interactions
GROUP BY reason_category
""")
d["fcr"] = d.resolved / d.contacts
overall = d.resolved.sum() / d.contacts.sum()
d = d.sort_values("fcr").reset_index(drop=True)
d["label"] = d.reason.apply(s1_label)

fig = go.Figure(go.Bar(
    y=d.label, x=d.fcr, orientation="h", marker_color=emphasize(d.reason, "Queja"),
    text=[f"{v:.1%}" for v in d.fcr], textposition="outside", cliponaxis=False, constraintext="none", textfont=dict(color=INK, size=12),
    customdata=d[["resolved", "contacts"]].values,
    hovertemplate="%{y}<br>FCR %{x:.1%}<br>%{customdata[0]:,} of %{customdata[1]:,} contacts resolved<extra></extra>",
))
fig.add_vline(x=overall, line_width=1, line_color=MUTED)
fig.add_annotation(x=overall, y=1, xref="x", yref="paper", yanchor="bottom", xanchor="left", xshift=4, showarrow=False,
                   text=f"All contacts {overall:.1%}", font=dict(size=11, color=MUTED))
fig.update_yaxes(autorange="reversed")
fig.update_xaxes(tickformat=".0%", range=[0, 1.08])
qfcr = d.loc[d.reason == "Queja", "fcr"].iloc[0]
show(style(
    fig,
    f"Complaints have the lowest first-contact resolution: {qfcr:.1%} vs {overall:.1%} overall",
    f"FCR = share of contacts with was_resolved = True, by contact reason (n = {int(d.contacts.sum()):,} contacts)",
    height=380, horizontal=True,
))
display(pd.DataFrame({
    "Reason": d.reason,
    "Contacts": d.contacts,
    "Resolved at first contact": d.resolved,
    "FCR (%)": (100 * d.fcr).round(1),
    "Gap to average (pp)": (100 * (d.fcr - overall)).round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1.3 · Which contacts take longest to handle?
# MAGIC A median `Queja` contact takes 431 s, 2.1× a transactional one (205 s); complaints use 23.1% of all handle-hours.
# MAGIC `Comercial` calls are longer still (540 s), but they are a much smaller block (8.0% of contacts).

# COMMAND ----------

d = q(f"""
SELECT coalesce(reason_category, 'ALL') AS reason,
       count(try_cast(duration_seconds AS DOUBLE)) AS timed,
       percentile(try_cast(duration_seconds AS DOUBLE), 0.5) AS p50,
       percentile(try_cast(duration_seconds AS DOUBLE), 0.9) AS p90,
       sum(try_cast(duration_seconds AS DOUBLE)) / 3600 AS handle_h
FROM {B}.call_center_interactions
GROUP BY ROLLUP(reason_category)
""")
allr = d[d.reason == "ALL"].iloc[0]
d = d[d.reason != "ALL"].sort_values("p50", ascending=False).reset_index(drop=True)
d["label"] = d.reason.apply(s1_label)
d["share_h"] = d.handle_h / allr.handle_h
colors = emphasize(d.reason, "Queja")

fig = go.Figure()
for (_, r), c in zip(d.iterrows(), colors):  # thin range line from median to 90th percentile
    fig.add_trace(go.Scatter(x=[r.p50, r.p90], y=[r.label, r.label], mode="lines", line=dict(color=c, width=2),
                             hoverinfo="skip", showlegend=False))
hover = ("%{y}<br>Median %{customdata[1]:.0f} s · 90th percentile %{customdata[2]:.0f} s"
         "<br>n = %{customdata[0]:,} timed contacts<extra></extra>")
cd = d[["timed", "p50", "p90"]].values
fig.add_trace(go.Scatter(
    x=d.p50, y=d.label, mode="markers+text", marker=dict(size=12, color=colors),
    text=[f"{v:.0f} s" for v in d.p50], textposition="middle left", textfont=dict(color=INK, size=12),
    customdata=cd, hovertemplate=hover, showlegend=False,
))
fig.add_trace(go.Scatter(
    x=d.p90, y=d.label, mode="markers+text", marker=dict(size=10, color=SURFACE, line=dict(color=colors, width=2)),
    text=[f"{v:.0f} s" if r == "Queja" else "" for r, v in zip(d.reason, d.p90)],
    textposition="middle right", textfont=dict(color=INK, size=12),
    customdata=cd, hovertemplate=hover, showlegend=False,
))
top = d.iloc[0]  # key the two marks once, on the top row
fig.add_annotation(x=top.p50, y=top.label, yshift=15, text="median", showarrow=False, font=dict(size=11, color=MUTED))
fig.add_annotation(x=top.p90, y=top.label, yshift=15, text="90th percentile", showarrow=False, font=dict(size=11, color=MUTED))
fig.add_vline(x=allr.p50, line_width=1, line_color=MUTED)
fig.add_annotation(x=allr.p50, y=1, xref="x", yref="paper", yanchor="bottom", xanchor="left", xshift=4, showarrow=False,
                   text=f"All contacts: median {allr.p50:.0f} s", font=dict(size=11, color=MUTED))
fig.update_yaxes(autorange="reversed")
fig.update_xaxes(range=[0, d.p90.max() * 1.1], title_text="seconds per contact")
qa = d[d.reason == "Queja"].iloc[0]
ta = d[d.reason == "Transaccional"].iloc[0]
show(style(
    fig,
    f"Complaint contacts take {qa.p50 / ta.p50:.1f}× as long to handle as transactional ones",
    f"Handle time in seconds, voice and video contacts only (n = {int(allr.timed):,}); dot = median, ring = 90th percentile",
    height=400, horizontal=True,
))
display(pd.DataFrame({
    "Reason": d.reason,
    "Timed contacts": d.timed,
    "Median (s)": d.p50.round(0).astype(int),
    "90th percentile (s)": d.p90.round(0).astype(int),
    "Handle-hours": d.handle_h.round(0).astype(int),
    "Share of handle-hours (%)": (100 * d.share_h).round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1.4 · Is satisfaction a separate problem from resolution?
# MAGIC No. Resolved contacts only ever score CSAT 2–4 and unresolved contacts only 1–3, with the same split in every reason.
# MAGIC Satisfaction is a consequence of resolution (+1 pp FCR ≈ +0.01 CSAT), so FCR is the KPI and CSAT is never used as a label.

# COMMAND ----------

d = q(f"""
SELECT i.reason_category AS reason, i.was_resolved AS resolved, try_cast(s.main_score AS INT) AS score, count(*) AS surveys
FROM {B}.satisfaction_surveys s
JOIN {B}.call_center_interactions i ON s.interaction_id = i.interaction_id
WHERE s.survey_type = 'CSAT'
GROUP BY 1, 2, 3
""")
pool = d.pivot_table(index="score", columns="resolved", values="surveys", aggfunc="sum").reindex([1, 2, 3, 4]).fillna(0)
n_res, n_unr = int(pool["True"].sum()), int(pool["False"].sum())
mean_res = (pool["True"] * pool.index.values).sum() / n_res
mean_unr = (pool["False"] * pool.index.values).sum() / n_unr
scores = [str(int(s)) for s in pool.index]

fig = go.Figure()
for col, n, name, color in [("True", n_res, "Resolved at first contact", BLUE), ("False", n_unr, "Unresolved", ORANGE)]:
    share = pool[col] / n
    fig.add_trace(go.Bar(
        x=scores, y=share, name=name, marker_color=color,
        text=[f"{v:.0%}" for v in share], textposition="outside", cliponaxis=False, constraintext="none", textfont=dict(color=INK, size=12),
        customdata=pool[[col]].astype(int).values,
        hovertemplate="CSAT %{x} · " + name + "<br>%{y:.1%} of surveys (%{customdata[0]:,} of " + f"{n:,}" + ")<extra></extra>",
    ))
fig.update_layout(barmode="group")
fig.update_yaxes(tickformat=".0%", range=[0, 0.82])
fig.update_xaxes(title_text="CSAT score (1 = lowest, 4 = highest)")
style(
    fig,
    "Satisfaction depends only on resolution: resolved contacts score 2–4, unresolved ones 1–3",
    f"CSAT, % of surveys in each group · resolved n = {n_res:,} (mean {mean_res:.3f}) · unresolved n = {n_unr:,} (mean {mean_unr:.3f})",
    height=390, legend=True,
)
fig.update_layout(margin=dict(t=90))  # legend sits right under the subtitle, no empty band
show(fig)
display(pd.DataFrame({
    "CSAT score": pool.index,
    "Resolved: surveys": pool["True"].astype(int).values,
    "Resolved: share (%)": (100 * pool["True"] / n_res).round(1).values,
    "Unresolved: surveys": pool["False"].astype(int).values,
    "Unresolved: share (%)": (100 * pool["False"] / n_unr).round(1).values,
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1.5 · When does demand arrive?
# MAGIC Tuesday to Friday carry 725–731 contacts a day, Monday 614, Saturday 487 and Sunday half the peak (367). Within the day
# MAGIC demand is flat: every hour holds 4.1–4.2% of contacts, night hours included (timestamp timezone unconfirmed).
# MAGIC This is a pattern of the synthetic data, not a staffing recommendation.

# COMMAND ----------

from plotly.subplots import make_subplots

wd = q(f"""
WITH daily AS (
  SELECT to_date(try_cast(interaction_date AS TIMESTAMP)) AS dt, count(*) AS n
  FROM {B}.call_center_interactions GROUP BY 1)
SELECT date_format(dt, 'E') AS weekday, dayofweek(dt) AS k, count(*) AS days, sum(n) AS contacts, avg(n) AS per_day
FROM daily
WHERE dt BETWEEN DATE'2023-06-18' AND DATE'2026-06-17'
GROUP BY 1, 2
""")
wd = wd.assign(order=(wd.k + 5) % 7).sort_values("order").reset_index(drop=True)  # Monday first
hr = q(f"""
SELECT hour(try_cast(interaction_date AS TIMESTAMP)) AS hour, count(*) AS contacts
FROM {B}.call_center_interactions
GROUP BY 1
""").sort_values("hour").reset_index(drop=True)
hr["share"] = hr.contacts / hr.contacts.sum()
peak = {"Tue", "Wed", "Thu", "Fri"}

fig = make_subplots(rows=1, cols=2, column_widths=[0.42, 0.58], horizontal_spacing=0.08,
                    subplot_titles=("Average contacts per day, by weekday", "Share of contacts, by hour of day"))
fig.add_trace(go.Bar(
    x=wd.weekday, y=wd.per_day, marker_color=emphasize(wd.weekday, peak),
    text=[f"{v:.0f}" for v in wd.per_day], textposition="outside", cliponaxis=False, constraintext="none", textfont=dict(color=INK, size=12),
    customdata=wd[["contacts", "days"]].values,
    hovertemplate="%{x}: %{y:.1f} contacts per day<br>%{customdata[0]:,} contacts over %{customdata[1]} days<extra></extra>",
), row=1, col=1)
fig.add_trace(go.Bar(
    x=hr.hour, y=hr.share, marker_color=GREY,
    customdata=hr[["contacts"]].values,
    hovertemplate="%{x}:00–%{x}:59<br>%{y:.2%} of contacts (%{customdata[0]:,})<extra></extra>",
), row=1, col=2)
fig.add_annotation(x=11.5, y=hr.share.max() * 1.1, xref="x2", yref="y2", yanchor="bottom", showarrow=False,
                   text=f"Every hour holds {hr.share.min():.2%}–{hr.share.max():.2%} of contacts",
                   font=dict(size=12, color=INK))
fig.update_yaxes(range=[0, wd.per_day.max() * 1.15], row=1, col=1)
fig.update_yaxes(tickformat=".0%", range=[0, hr.share.max() * 1.3], row=1, col=2)
fig.update_xaxes(tickmode="array", tickvals=list(range(0, 24, 3)), ticktext=[f"{h:02d}:00" for h in range(0, 24, 3)], row=1, col=2)
style(
    fig,
    "Demand follows the week, not the clock",
    f"Weekday panel: full days Jun 18, 2023 – Jun 17, 2026 ({int(wd.days.min())}–{int(wd.days.max())} days each) · hour panel: n = {int(hr.contacts.sum()):,} contacts",
    height=380,
)
for a in fig.layout.annotations[:2]:  # left-align the two panel titles over their panels
    a.update(x=fig.layout.xaxis.domain[0] if "weekday" in a.text else fig.layout.xaxis2.domain[0],
             xanchor="left", font=dict(size=13, color=INK2))
show(fig)
display(pd.DataFrame({
    "Weekday": wd.weekday,
    "Days": wd.days,
    "Contacts": wd.contacts,
    "Contacts per day": wd.per_day.round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC **What this means for the build.** The workflow with the most room to improve is the one behind `Queja`: FCR 43.6% is the
# MAGIC baseline to beat, and 431 s median handle time is the secondary one. `Transaccional` is big but already works (91.5% FCR).
# MAGIC Section 2 checks whether complaint records can support a dispute agent.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 · Complaints and dispute grounding
# MAGIC **Disputes are 40% of formal complaint cases, but no historical case links to the complainant's own products, transactions or calls, so each new case must be built from the customer's own transactions.**
# MAGIC
# MAGIC `complaints` holds 67,095 formal cases (Jun 2023 – Jun 2026). The *dispute slice* is the two categories that describe a contested charge:
# MAGIC Transactions (*Cargo no reconocido*, unrecognized charge) and Fees (*Cobro indebido*, incorrect charge).
# MAGIC This section sizes the slice, sets its process baseline, and tests whether old cases can ground an agent.

# COMMAND ----------

k = q(f"""
WITH c AS (
  SELECT category IN ('Transactions', 'Fees') AS is_dispute, status,
         try_cast(creation_date AS TIMESTAMP) AS created
  FROM {B}.complaints),
monthly AS (
  SELECT date_format(created, 'yyyy-MM') AS month, count(*) AS n
  FROM c
  WHERE is_dispute AND created >= TIMESTAMP'2023-07-01' AND created < TIMESTAMP'2026-06-01'
  GROUP BY 1)
SELECT (SELECT count(*) FROM c) AS cases,
       (SELECT date_format(min(created), 'MMM yyyy') FROM c) AS first_month,
       (SELECT date_format(max(created), 'MMM yyyy') FROM c) AS last_month,
       (SELECT count_if(is_dispute) FROM c) AS disputes,
       (SELECT count_if(is_dispute AND status IN ('Open', 'In Process', 'Escalated')) FROM c) AS disputes_open,
       (SELECT CAST(avg(n) AS DOUBLE) FROM monthly) AS per_month,
       (SELECT min(n) FROM monthly) AS month_min,
       (SELECT max(n) FROM monthly) AS month_max,
       (SELECT count(*) FROM monthly) AS months
""").to_dict("records")[0]

tiles([
    {"value": f"{int(k['cases']):,}", "label": "Formal complaint cases", "note": f"{k['first_month']} – {k['last_month']} · all categories"},
    {"value": f"{int(k['disputes']):,}", "label": "Dispute cases",
     "note": f"{pct(k['disputes'], k['cases'])} of all cases · Transactions + Fees"},
    {"value": f"{k['per_month']:,.0f}", "label": "Dispute cases per month",
     "note": f"{int(k['months'])} full months · range {int(k['month_min']):,}–{int(k['month_max']):,}"},
    {"value": pct(k["disputes_open"], k["disputes"]), "label": "Disputes still open",
     "note": f"{int(k['disputes_open']):,} Open, In Process or Escalated"},
])

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2.1 · How many disputes arrive each month?
# MAGIC About 754 per full month, every month between 676 and 812, and no trend: the monthly average is 750–758 in each year of the window.
# MAGIC A stable intake means a pilot can be sized straight from this line.

# COMMAND ----------

m = q(f"""
SELECT date_format(try_cast(creation_date AS TIMESTAMP), 'yyyy-MM') AS month,
       count_if(category IN ('Transactions', 'Fees')) AS dispute_cases,
       count(*) AS all_cases
FROM {B}.complaints
WHERE try_cast(creation_date AS TIMESTAMP) >= TIMESTAMP'2023-07-01'
  AND try_cast(creation_date AS TIMESTAMP) <  TIMESTAMP'2026-06-01'
GROUP BY 1
ORDER BY 1
""")
m["date"] = pd.to_datetime(m["month"] + "-01")
m["share"] = m["dispute_cases"] / m["all_cases"]
avg = m["dispute_cases"].mean()
lo = m.loc[m["dispute_cases"].idxmin()]
hi = m.loc[m["dispute_cases"].idxmax()]

fig = go.Figure(go.Scatter(
    x=m["date"], y=m["dispute_cases"], mode="lines", line=dict(color=BLUE, width=2),
    customdata=m[["all_cases", "share"]].values,
    hovertemplate="%{x|%b %Y}: <b>%{y:,}</b> dispute cases<br>%{customdata[1]:.1%} of %{customdata[0]:,} complaint cases<extra></extra>",
))
ext = pd.DataFrame([hi, lo])
fig.add_trace(go.Scatter(
    x=ext["date"], y=ext["dispute_cases"], mode="markers+text", marker=dict(color=BLUE, size=8),
    text=[f"High: {int(hi['dispute_cases']):,} ({hi['date']:%b %Y})", f"Low: {int(lo['dispute_cases']):,} ({lo['date']:%b %Y})"],
    textposition=["top center", "bottom center"], textfont=dict(color=INK2, size=12),
    customdata=ext[["all_cases", "share"]].values,
    hovertemplate="%{x|%b %Y}: <b>%{y:,}</b> dispute cases<br>%{customdata[1]:.1%} of %{customdata[0]:,} complaint cases<extra></extra>",
))
fig.add_hline(y=avg, line=dict(color=MUTED, width=1))
fig.add_annotation(x=1, xref="paper", xanchor="left", xshift=6, y=avg, yref="y", showarrow=False, align="left",
                   text=f"Average<br>{avg:,.0f}/month", font=dict(color=MUTED, size=11))
fig.update_yaxes(range=[0, hi["dispute_cases"] * 1.16], tickformat=",")
fig.update_xaxes(dtick="M6", tickformat="%b %Y")
style(fig, f"Dispute intake is steady at about {avg:,.0f} cases a month",
      f"New dispute cases per month (Transactions + Fees) · {len(m)} full months, Jul 2023 – May 2026 · n = {int(m.dispute_cases.sum()):,}",
      height=340)
fig.update_layout(margin=dict(r=92))
show(fig)

t = m.assign(Year=m["date"].dt.year.astype(str), Month=m["date"].dt.strftime("%b"), mnum=m["date"].dt.month)
tbl = t.pivot_table(index=["mnum", "Month"], columns="Year", values="dispute_cases", aggfunc="sum")
tbl = tbl.applymap(lambda v: "–" if pd.isna(v) else f"{int(v):,}").reset_index().drop(columns="mnum")
year_avg = t.groupby("Year")["dispute_cases"].mean()
tbl.loc[len(tbl)] = ["Monthly average"] + [f"{year_avg[y]:,.0f}" for y in tbl.columns[1:]]
tbl.columns.name = None
display(tbl)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2.2 · How much of the dispute backlog is still open?
# MAGIC Three in four dispute cases (74.9%) are Open, In Process or Escalated, exactly the same share as the other categories, and across all complaints the status mix does not change with case age.
# MAGIC Backlog is a process baseline, not a sign that disputes are harder.

# COMMAND ----------

s = q(f"""
SELECT coalesce(status, 'Missing') AS status,
       count_if(category IN ('Transactions', 'Fees')) AS disputes,
       count(*) - count_if(category IN ('Transactions', 'Fees')) AS others
FROM {B}.complaints
GROUP BY 1
""")
OPEN = ["Open", "In Process", "Escalated"]
s["is_open"] = s["status"].isin(OPEN)
s = s.sort_values(["is_open", "disputes"], ascending=[False, False]).reset_index(drop=True)
n_d, n_o = int(s["disputes"].sum()), int(s["others"].sum())
s["share"] = s["disputes"] / n_d
s["share_other"] = s["others"] / n_o
open_share = s.loc[s["is_open"], "disputes"].sum() / n_d
open_other = s.loc[s["is_open"], "others"].sum() / n_o

fig = go.Figure(go.Bar(
    y=s["status"], x=s["disputes"], orientation="h", marker_color=emphasize(s["status"], OPEN),
    text=[f"{n:,} · {p:.1%}" for n, p in zip(s["disputes"], s["share"])], textposition="outside", cliponaxis=False,
    textfont=dict(color=INK2, size=12),
    customdata=s["share"],
    hovertemplate="%{y}: <b>%{x:,}</b> dispute cases (%{customdata:.1%} of " + f"{n_d:,})<extra></extra>",
))
fig.update_yaxes(categoryorder="array", categoryarray=list(s["status"])[::-1])
fig.update_xaxes(range=[0, s["disputes"].max() * 1.2], tickformat=",")
style(fig, f"Three in four dispute cases are still open ({open_share:.1%})",
      f"Dispute cases by current status · n = {n_d:,} · blue = open backlog · other categories: {open_other:.1%} open",
      height=340, horizontal=True)
show(fig)

display(pd.DataFrame({
    "Status": s["status"],
    "Dispute cases": s["disputes"],
    "Share of disputes (%)": (100 * s["share"]).round(1),
    "Share of other categories (%)": (100 * s["share_other"]).round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2.3 · How long does a dispute wait for its first response?
# MAGIC The median answered case waits 37 h and 26.2% wait more than 48 h; another 38.7% of dispute cases have no first response yet, almost all of them still open.
# MAGIC Timing is the same for every complaint category, so this is the baseline an instant intake is compared against.

# COMMAND ----------

FR = f"""
WITH d AS (
  SELECT status IN ('Open', 'In Process', 'Escalated') AS is_open,
         (unix_timestamp(try_cast(first_response_date AS TIMESTAMP))
          - unix_timestamp(try_cast(creation_date AS TIMESTAMP))) / 3600.0 AS hours
  FROM {B}.complaints
  WHERE category IN ('Transactions', 'Fees'))
"""
fr = q(FR + """
SELECT CAST(greatest(ceil(hours / 4), 1) * 4 AS INT) AS upper_h, count(*) AS cases
FROM d WHERE hours IS NOT NULL
GROUP BY 1 ORDER BY 1
""")
st = q(FR + """
SELECT count(*) AS disputes, count(hours) AS answered,
       CAST(percentile(hours, 0.5) AS DOUBLE) AS p50, CAST(percentile(hours, 0.9) AS DOUBLE) AS p90,
       count_if(hours > 48) AS over_48, count_if(hours IS NULL AND is_open) AS none_open
FROM d
""").to_dict("records")[0]
disputes, answered, over_48 = int(st["disputes"]), int(st["answered"]), int(st["over_48"])
none_open = int(st["none_open"])
no_resp = disputes - answered
fr["lower_h"] = fr["upper_h"] - 4
fr["share"] = fr["cases"] / answered

fig = go.Figure(go.Bar(
    x=fr["upper_h"] - 2, y=fr["cases"], width=3.4,
    marker_color=[BLUE if u > 48 else GREY for u in fr["upper_h"]],
    customdata=fr[["lower_h", "upper_h", "share"]].values,
    hovertemplate="%{customdata[0]:.0f}–%{customdata[1]:.0f} h: <b>%{y:,}</b> cases<br>%{customdata[2]:.1%} of "
                  + f"{answered:,} answered cases<extra></extra>",
))
peak = fr["cases"].max()
fig.add_vline(x=48, line=dict(color=MUTED, width=1))
fig.add_annotation(x=48, y=peak * 1.13, text="48 h", showarrow=False, xanchor="right", xshift=-4, font=dict(color=MUTED, size=11))
fig.add_annotation(x=49.5, y=peak * 1.13, xanchor="left", showarrow=False, align="left",
                   text=f"<b>Over 48 h: {over_48:,} cases ({over_48 / answered:.1%} of answered)</b>",
                   font=dict(color=BLUE, size=12))
fig.add_annotation(x=1.5, y=peak * 1.13, xanchor="left", yanchor="top", showarrow=False, align="left",
                   text=f"<b>+{no_resp:,} cases ({no_resp / disputes:.1%})</b><br>have no first response yet,<br>"
                        f"{none_open:,} of them still open<br>(not in the bars)",
                   font=dict(color=INK2, size=12))
fig.update_xaxes(tickvals=[0, 12, 24, 36, 48, 60, 72], ticksuffix=" h", range=[0, 74], title_text="Hours from case creation to first response")
fig.update_yaxes(tickformat=",", range=[0, peak * 1.2])
style(fig, f"First response takes a median {st['p50']:.0f} h, and {no_resp / disputes:.1%} of dispute cases have none yet",
      f"Dispute cases with a first response, by hours to first response (4-hour bins) · n = {answered:,} of {disputes:,} · p90 {st['p90']:.0f} h",
      height=380)
show(fig)

tbl = pd.DataFrame({
    "Hours to first response": [f"{a}–{b} h" for a, b in zip(fr["lower_h"], fr["upper_h"])] + ["No first response yet"],
    "Dispute cases": list(fr["cases"]) + [no_resp],
})
tbl["Share of all dispute cases (%)"] = (100 * tbl["Dispute cases"] / disputes).round(1)
display(tbl)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2.4 · Can a historical case tell the agent which product, transaction or call is disputed?
# MAGIC No. Every complaint names a real customer and every product ID exists, but the product on 0 of 44,570 cases belongs to the complainant,
# MAGIC 0 of 21,751 claimed amounts match any of the customer's transactions, and no case records its originating contact.
# MAGIC The same ownership test passes for all 4.4M transactions, so the break is in the complaint links, not in our joins.

# COMMAND ----------

g = q(f"""
WITH p AS (SELECT product_id, customer_id AS owner FROM {B}.products),
tx AS (
  SELECT count_if(t.product_id IS NOT NULL) AS n, count_if(p.owner = t.customer_id) AS passed
  FROM {B}.transactions t LEFT JOIN p ON t.product_id = p.product_id),
cp AS (
  SELECT count_if(c.affected_product_id IS NOT NULL) AS n,
         count_if(p.product_id IS NOT NULL) AS found,
         count_if(p.owner = c.customer_id) AS owned
  FROM {B}.complaints c LEFT JOIN p ON c.affected_product_id = p.product_id),
ca AS (
  SELECT complaint_id, customer_id, try_cast(claimed_amount AS DOUBLE) AS amt
  FROM {B}.complaints
  WHERE try_cast(claimed_amount AS DOUBLE) IS NOT NULL),
am AS (
  SELECT ca.complaint_id,
         max(CASE WHEN abs(try_cast(t.amount AS DOUBLE) - ca.amt) < 0.01 THEN 1 ELSE 0 END) AS hit
  FROM ca LEFT JOIN {B}.transactions t ON t.customer_id = ca.customer_id
  GROUP BY ca.complaint_id),
cu AS (
  SELECT count(*) AS n, count_if(k.customer_id IS NOT NULL) AS passed
  FROM {B}.complaints c LEFT JOIN (SELECT DISTINCT customer_id FROM {B}.customers) k ON c.customer_id = k.customer_id),
oi AS (SELECT count(*) AS n, count(origin_interaction_id) AS passed FROM {B}.complaints)
SELECT 'tx_owner' AS chk, passed, n FROM tx
UNION ALL SELECT 'cu_found', passed, n FROM cu
UNION ALL SELECT 'cp_found', found, n FROM cp
UNION ALL SELECT 'cp_owner', owned, n FROM cp
UNION ALL SELECT 'amount', CAST(sum(hit) AS BIGINT), count(*) FROM am
UNION ALL SELECT 'origin', passed, n FROM oi
""")
CHECKS = [
    ("tx_owner", "Transaction → owner of its product (control)"),
    ("cu_found", "Complainant → exists in customers"),
    ("cp_found", "Complaint product → exists in products"),
    ("cp_owner", "Complaint product → owned by the complainant"),
    ("amount", "Claimed amount → one of the complainant's transactions"),
    ("origin", "Complaint → originating contact recorded"),
]
g = g.set_index("chk").loc[[c for c, _ in CHECKS]].reset_index()
g["check"] = [label for _, label in CHECKS]
g["rate"] = g["passed"] / g["n"]
g["result"] = ["PASS" if r >= 0.99 else "FAIL" for r in g["rate"]]
status_color = [GOOD if r == "PASS" else CRITICAL for r in g["result"]]
cd = g[["result", "passed", "n"]].values
HT = "%{y}<br><b>%{customdata[0]}</b>: %{customdata[1]:,} of %{customdata[2]:,} rows pass<extra></extra>"

fig = go.Figure([
    go.Bar(y=g["check"], x=[1] * len(g), orientation="h", marker_color="#efeee9", customdata=cd, hovertemplate=HT),
    go.Bar(y=g["check"], x=g["rate"], orientation="h", marker_color=status_color, customdata=cd, hovertemplate=HT),
])
for r, c in zip(g.itertuples(), status_color):
    fig.add_annotation(x=1, xanchor="left", xshift=10, y=r.check, showarrow=False, align="left",
                       text=f"<span style='color:{c}'><b>{r.result}</b></span>   {r.rate:.0%}  ·  {int(r.passed):,} / {int(r.n):,}",
                       font=dict(color=INK2, size=12))
fig.update_layout(barmode="overlay")
fig.update_yaxes(categoryorder="array", categoryarray=list(g["check"])[::-1])
fig.update_xaxes(range=[0, 1.55], tickvals=[0, 0.25, 0.5, 0.75, 1], tickformat=".0%")
style(fig, "Complaint IDs are valid, but every link to the complainant's own data fails",
      "Share of rows passing each integrity check · n = rows where the check applies · first row is the control on transactions",
      height=420, horizontal=True)
show(fig)

display(pd.DataFrame({
    "Check": g["check"],
    "Result": g["result"],
    "Rows passing": g["passed"],
    "Rows checked": g["n"],
    "Pass rate (%)": (100 * g["rate"]).round(1),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC **What this means for the build.** Complaints size the demand (about 754 disputes a month) and set the process baseline (37 h median first response, three in four cases open).
# MAGIC They cannot supply the disputed transaction, the product, the originating call or an outcome label (the SLA-breach flag, for one, sits at 18.5–21.4% whatever the
# MAGIC resolution time; report Q3.7). The dispute intake therefore identifies the customer and builds each case from their own products and transactions, the only links that pass.
# MAGIC Because no field links a contact to a case, "complaint contacts are disputes" stays an explicit assumption. Section 3 checks whether those transactions are clean enough to ground on.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 · Transactions and labels
# MAGIC **Transaction keys are clean enough to ground a dispute on, but their fraud label cannot be learned.**
# MAGIC
# MAGIC `transactions` holds 4.4M rows (Jun 2023 – Jun 2026). We check their integrity, test whether `is_fraud` carries any signal,
# MAGIC whether `fraud_score` is a fair baseline, what `process_date` really means, and how many candidates a dispute lookup would face.

# COMMAND ----------

# Headline numbers: grain integrity, label prevalence, temporal predictability and the fraud_score leak (report Q4.1, Q4.5, Q4.7).
g = q(f"""
SELECT count(*)                                                                   AS n,
       count_if(p.customer_id = t.customer_id)                                    AS owned,
       count_if(t.is_fraud = 'True')                                              AS fraud,
       count_if(try_cast(t.fraud_score AS DOUBLE) > 30)                           AS flagged,
       count_if(try_cast(t.fraud_score AS DOUBLE) > 30 AND t.is_fraud = 'True')   AS flagged_fraud,
       (SELECT count_if(k > 1) FROM (SELECT count(*) AS k FROM {B}.transactions
                                     GROUP BY customer_id, product_id, transaction_date, amount)) AS dup_groups
FROM {B}.transactions t LEFT JOIN {B}.products p ON t.product_id = p.product_id
""").iloc[0]

# Temporal test (Q4.5): a smoothed target-encoded cell model trained before 2025-07-01, scored on later transactions.
auc = q(f"""
WITH raw AS (
  SELECT t.channel ch, t.transaction_type ty, coalesce(t.merchant_category, '<NULL>') mc,
         translate(t.transaction_country, 'é', 'e') tc, translate(c.country, 'é', 'e') cc,
         try_cast(t.amount AS DOUBLE) / CASE t.currency WHEN 'ARS' THEN 350.0 WHEN 'COP' THEN 4000.0 ELSE 1.0 END usd,
         try_cast(t.transaction_date AS TIMESTAMP) ts, CASE WHEN t.is_fraud = 'True' THEN 1 ELSE 0 END y
  FROM {B}.transactions t LEFT JOIN {B}.customers c ON c.customer_id = t.customer_id),
f AS (SELECT ch, ty, mc, tc, CASE WHEN usd < 200 THEN 'lo' WHEN usd < 3000 THEN 'mid' ELSE 'hi' END ab,
             CASE WHEN hour(ts) < 6 THEN 'night' ELSE 'day' END nb, CASE WHEN tc <> cc THEN 'x' ELSE 'd' END xb, y,
             CASE WHEN ts < TIMESTAMP'2025-07-01 00:00:00' THEN 'train' ELSE 'test' END split
      FROM raw),
pr AS (SELECT sum(y) / count(*) p0 FROM f WHERE split = 'train'),
cell AS (SELECT ch, ty, mc, tc, ab, nb, xb, (sum(y) + 200 * max(p0)) / (count(*) + 200) s
         FROM f CROSS JOIN pr WHERE split = 'train' GROUP BY 1, 2, 3, 4, 5, 6, 7),
sc AS (SELECT round(coalesce(cell.s, (SELECT p0 FROM pr)), 9) s, f.y
       FROM f LEFT JOIN cell ON f.ch = cell.ch AND f.ty = cell.ty AND f.mc = cell.mc AND f.tc = cell.tc
                            AND f.ab = cell.ab AND f.nb = cell.nb AND f.xb = cell.xb
       WHERE f.split = 'test'),
gr AS (SELECT s, sum(y) pos, count(*) - sum(y) neg FROM sc GROUP BY s),
r AS (SELECT s, pos, neg, sum(neg) OVER (ORDER BY s RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - neg nlow FROM gr)
SELECT sum(pos) AS test_frauds, sum(neg) AS test_non_frauds,
       CAST(sum(pos * (nlow + 0.5 * neg)) / (sum(pos) * sum(neg)) AS DOUBLE) AS auc
FROM r
""").iloc[0]

tiles([
    {"value": f"{int(g.n):,}", "label": "Transactions",
     "note": f"{pct(g.owned, g.n)} owned by the product's customer · {int(g.dup_groups)} duplicate groups"},
    {"value": pct(g.fraud, g.n, 4), "label": "is_fraud prevalence",
     "note": f"{int(g.fraud):,} frauds of {int(g.n):,} transactions"},
    {"value": f"{auc.auc:.3f}", "label": "Temporal fraud AUC",
     "note": f"trained before Jul 2025 · {int(auc.test_frauds):,} test frauds · 0.500 = chance"},
    {"value": f"{g.flagged_fraud / g.flagged:.3f}", "label": "fraud_score precision",
     "note": f"scores above 30: {int(g.flagged):,} flagged, {int(g.flagged - g.flagged_fraud)} non-fraud · a label leak"},
])

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3.1 · Is fraud more likely on some channel or transaction type?
# MAGIC No. Every channel and type sits within ±12% of the overall 97.5 frauds per 100k, and neither dimension beats chance
# MAGIC (χ² p ≥ 0.08). Only the Transfer type's interval misses the average, which 12 intervals at 95% do by chance about half the time.
# MAGIC Hour (χ² 17.2 on 23 df), month (41.6 on 36) and 139 channel × type × merchant × country cells (135.2 on 138) are just as flat
# MAGIC (report Q4.3–Q4.4).

# COMMAND ----------

import math
import numpy as np
from plotly.subplots import make_subplots


def chi2_p(x, df):
    """Upper-tail p-value of a chi-square statistic (series for the regularized lower incomplete gamma)."""
    a, h = df / 2, x / 2
    term = total = 1 / a
    n = 1
    while term > 1e-12 * total:
        term *= h / (a + n)
        total += term
        n += 1
    return max(0.0, 1 - total * math.exp(-h + a * math.log(h) - math.lgamma(a)))


fr = q(f"""
SELECT dim, coalesce(val, '(missing)') AS val, count(*) AS n, sum(y) AS frauds
FROM (SELECT stack(2, 'Channel', channel, 'Transaction type', transaction_type) AS (dim, val),
             CASE WHEN is_fraud = 'True' THEN 1 ELSE 0 END AS y
      FROM {B}.transactions)
GROUP BY 1, 2
""")
N = int(fr[fr.dim == "Channel"].n.sum())
F = int(fr[fr.dim == "Channel"].frauds.sum())
p0 = F / N
fr["rate"] = fr.frauds / fr.n * 1e5
fr["se"] = np.sqrt((fr.frauds / fr.n) * (1 - fr.frauds / fr.n) / fr.n) * 1e5
fr["lo"], fr["hi"] = fr.rate - 1.96 * fr.se, fr.rate + 1.96 * fr.se
fr["lift"] = fr.rate / (p0 * 1e5)
fr["chi"] = (fr.frauds - fr.n * p0) ** 2 / (fr.n * p0 * (1 - p0))
chi2 = fr.groupby("dim").chi.sum()
dof = fr.groupby("dim").size() - 1
pval = {d: chi2_p(chi2[d], dof[d]) for d in chi2.index}

dims = ["Channel", "Transaction type"]
fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.12,
                    subplot_titles=[f"<b>By {d.lower()}</b>  (χ² {chi2[d]:.1f} on {dof[d]} df, p = {pval[d]:.2f})" for d in dims])
for c, d in enumerate(dims, start=1):
    s = fr[fr.dim == d].sort_values("rate").reset_index(drop=True)
    extremes = [s.val.iloc[0], s.val.iloc[-1]]
    fig.add_trace(go.Bar(
        y=s.val, x=s.rate, orientation="h", marker_color=emphasize(s.val, extremes),
        error_x=dict(type="data", symmetric=False, array=s.hi - s.rate, arrayminus=s.rate - s.lo,
                     color=INK2, thickness=1.2, width=4),
        customdata=np.stack([s.lo, s.hi, s.frauds, s.n, s.lift], axis=-1),
        hovertemplate="<b>%{y}</b><br>Fraud per 100k: %{x:.1f} (95% CI %{customdata[0]:.1f}–%{customdata[1]:.1f})"
                      "<br>%{customdata[2]:,.0f} frauds of %{customdata[3]:,.0f} transactions"
                      "<br>%{customdata[4]:.2f}× the overall rate<extra></extra>"), row=1, col=c)
    fig.add_trace(go.Scatter(
        y=s.val, x=s.hi + 3, mode="text", text=[f"{v:.1f}" for v in s.rate], textposition="middle right",
        textfont=dict(color=[INK if v in extremes else MUTED for v in s.val], size=12),
        hoverinfo="skip", cliponaxis=False), row=1, col=c)
    fig.add_vline(x=p0 * 1e5, line_width=1, line_color=MUTED, row=1, col=c)
    fig.add_annotation(x=p0 * 1e5, y=1.0, xref=f"x{c if c > 1 else ''}", yref=f"y{c if c > 1 else ''} domain",
                       text=f"overall {p0 * 1e5:.1f}", showarrow=False, xanchor="left", yanchor="bottom",
                       xshift=4, font=dict(size=11, color=MUTED))
fig.update_xaxes(range=[0, 150], title_text="Fraud per 100k transactions")
for a, dom in zip(fig.layout.annotations[:2], [fig.layout.xaxis.domain, fig.layout.xaxis2.domain]):  # panel titles
    a.update(font=dict(size=13, color=INK2), x=dom[0], xanchor="left", yshift=6)
show(style(fig, f"No channel or type moves the fraud rate more than {100 * (fr.lift - 1).abs().max():.0f}% from average",
           f"Fraud per 100k transactions with 95% CI · n = {N:,} ({F:,} frauds) · line = overall rate "
           f"· blue = highest and lowest per panel",
           height=400, horizontal=True))

t = fr.sort_values(["dim", "rate"], ascending=[True, False])[["dim", "val", "n", "frauds", "rate", "lo", "hi", "lift"]]
t.columns = ["Dimension", "Value", "Transactions", "Frauds", "Fraud per 100k", "CI low", "CI high", "× overall"]
display(t.round({"Fraud per 100k": 1, "CI low": 1, "CI high": 1, "× overall": 2}).reset_index(drop=True))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3.2 · Can `fraud_score` serve as a feature or as the baseline to beat?
# MAGIC No. Non-fraud scores stop at exactly 30.00 while fraud scores spread evenly over 0–100, so every score above 30 is a fraud
# MAGIC (precision 1.000) and the raw score alone reaches AUC 0.847. The score was generated from the label: as a feature it leaks
# MAGIC the answer, and as a baseline it sets a bar no honest model can be compared against (report Q4.6–Q4.7).

# COMMAND ----------

fs = q(f"""
SELECT CASE WHEN is_fraud = 'True' THEN 'Fraud' ELSE 'Non-fraud' END AS cls,
       CASE WHEN s IS NOT NULL THEN CAST(greatest(ceil(s / 5) - 1, 0) * 5 AS INT) END AS bin_lo,  -- bins (lo, lo + 5]
       count(*) AS n, max(s) AS s_max
FROM (SELECT is_fraud, try_cast(fraud_score AS DOUBLE) AS s FROM {B}.transactions)
GROUP BY 1, 2
""")
# AUC of the raw score (Q4.6): pairwise fraud > non-fraud over the 5,014 distinct score values, ties count half.
fs_auc = q(f"""
WITH gr AS (SELECT s, sum(y) AS pos, count(*) - sum(y) AS neg
            FROM (SELECT try_cast(fraud_score AS DOUBLE) AS s, CASE WHEN is_fraud = 'True' THEN 1 ELSE 0 END AS y
                  FROM {B}.transactions)
            WHERE s IS NOT NULL GROUP BY s),
r AS (SELECT pos, neg, sum(neg) OVER (ORDER BY s RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - neg AS nlow FROM gr)
SELECT CAST(sum(pos * (nlow + 0.5 * neg)) / (sum(pos) * sum(neg)) AS DOUBLE) AS auc FROM r
""").auc.iloc[0]
scored = fs[fs.bin_lo.notna()].copy()
scored["bin_lo"] = scored.bin_lo.astype(int)
n_total, n_null = int(fs.n.sum()), int(fs[fs.bin_lo.isna()].n.sum())
ncls = scored.groupby("cls").n.sum()
nonfraud_max = scored[scored.cls == "Non-fraud"].s_max.max()
above = scored[scored.bin_lo >= 30].groupby("cls").n.sum().reindex(["Fraud", "Non-fraud"]).fillna(0).astype(int)

bins = list(range(0, 100, 5))
fig = go.Figure()
for cls, color in [("Fraud", BLUE), ("Non-fraud", ORANGE)]:
    s = scored[scored.cls == cls].set_index("bin_lo").n.reindex(bins).fillna(0)
    share = s / ncls[cls]
    fig.add_trace(go.Bar(
        x=[b + 2.5 for b in bins], y=share, name=f"{cls} (n = {ncls[cls]:,})", marker_color=color,
        customdata=np.stack([bins, [b + 5 for b in bins], s], axis=-1),
        hovertemplate=f"<b>{cls}</b><br>Score (%{{customdata[0]:.0f}}, %{{customdata[1]:.0f}}]: %{{y:.1%}}"
                      f"<br>%{{customdata[2]:,.0f}} of {ncls[cls]:,} scored<extra></extra>"))
fig.add_vline(x=nonfraud_max, line_width=1, line_color=MUTED)
fig.add_annotation(x=nonfraud_max, y=0.205, text=f"non-fraud max {nonfraud_max:.2f}", showarrow=False,
                   xanchor="left", xshift=4, font=dict(size=11, color=MUTED))
fig.add_annotation(x=65, y=0.12, showarrow=False, align="center", font=dict(size=12, color=INK2),
                   text=f"Above 30: {int(above.sum()):,} transactions, <b>{above['Non-fraud']} non-fraud</b>"
                        f"<br>precision {above['Fraud'] / above.sum():.3f} · raw-score AUC {fs_auc:.3f}")
fig.update_layout(barmode="group")
fig.update_xaxes(range=[0, 100], tickvals=list(range(0, 101, 10)), title_text="fraud_score (5-point bins)")
fig.update_yaxes(range=[0, 0.22], tickformat=".0%", title_text="Share of the class")
style(fig, "fraud_score is built from the label: no non-fraud transaction scores above 30",
      f"Share of each class's scored transactions per 5-point bin · {pct(n_null, n_total)} of {n_total:,} rows "
      f"have no score and are excluded", height=400, legend=True)
fig.update_layout(margin=dict(t=90))  # legend sits right under the subtitle, no empty band
show(fig)

t = scored.pivot_table(index="bin_lo", columns="cls", values="n", aggfunc="sum").reindex(bins).fillna(0).astype(int)
t = pd.DataFrame({"Score bin": [f"({b}, {b + 5}]" for b in bins],
                  "Fraud": t["Fraud"].values, "Fraud share": (t["Fraud"] / ncls["Fraud"]).round(3).values,
                  "Non-fraud": t["Non-fraud"].values,
                  "Non-fraud share": (t["Non-fraud"] / ncls["Non-fraud"]).round(3).values})
display(t)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3.3 · Does `process_date` reveal late-arriving transactions?
# MAGIC No. It is a business day that closes at 06:00: every event between 00:00 and 05:59 is booked on the previous day, which is
# MAGIC exactly 6 of 24 hours (25%), and no row carries a later day. Interactions and complaints do the same with an 08:00 cut-off.
# MAGIC All trends and splits in this notebook use the event timestamp.

# COMMAND ----------

pdq = q(f"""
SELECT hour(ts) AS hour, count(*) AS n, count_if(lag < 0) AS previous_day, count_if(lag > 0) AS later_day
FROM (SELECT try_cast(transaction_date AS TIMESTAMP) AS ts,
             datediff(try_cast(process_date AS DATE), to_date(try_cast(transaction_date AS TIMESTAMP))) AS lag
      FROM {B}.transactions)
GROUP BY 1 ORDER BY 1
""")
pdq["share"] = pdq.previous_day / pdq.n
overall = pdq.previous_day.sum() / pdq.n.sum()
exc = pdq[pdq.hour >= 6].previous_day.sum()

fig = go.Figure(go.Bar(
    x=pdq.hour, y=pdq.share, marker_color=[BLUE if h < 6 else GREY for h in pdq.hour],
    customdata=[[f"{h:02d}:00–{h:02d}:59", int(p), int(n)] for h, p, n in zip(pdq.hour, pdq.previous_day, pdq.n)],
    hovertemplate="<b>%{customdata[0]}</b><br>Previous-day process_date: %{y:.2%}"
                  "<br>%{customdata[1]:,} of %{customdata[2]:,} transactions<extra></extra>"))
fig.add_vline(x=5.5, line_width=1, line_color=MUTED)
fig.add_annotation(x=5.5, y=1.08, text="06:00 cut-off", showarrow=False, xanchor="left", xshift=4,
                   font=dict(size=11, color=MUTED))
fig.add_hline(y=overall, line_width=1, line_color=MUTED)
fig.add_annotation(x=23.4, y=overall, text=f"all hours: {overall:.1%}", showarrow=False, xanchor="right",
                   yanchor="bottom", font=dict(size=11, color=MUTED))
fig.add_annotation(x=2.5, y=1.0, text="<b>100%</b> of events 00:00–05:59", showarrow=False, yanchor="bottom",
                   font=dict(size=12, color=INK))
fig.add_annotation(x=14.5, y=0.08, showarrow=False, font=dict(size=12, color=INK2),
                   text=f"06:00–23:59: {exc:,} of {int(pdq[pdq.hour >= 6].n.sum()):,} rows "
                        f"({pct(exc, pdq[pdq.hour >= 6].n.sum(), 3)}) carry the previous day")
fig.update_xaxes(tickvals=list(range(24)), ticktext=[f"{h:02d}" for h in range(24)], title_text="Hour of the event")
fig.update_yaxes(range=[0, 1.1], tickformat=".0%", tickvals=[0, 0.25, 0.5, 0.75, 1])
show(style(fig, "process_date is a business day that closes at 06:00, not a late-arrival signal",
           f"Share of transactions whose process_date is the day before the event date, by event hour · n = {int(pdq.n.sum()):,} "
           f"· {int(pdq.later_day.sum())} rows carry a later day", height=380))

t = pdq[["hour", "n", "previous_day", "share"]].copy()
t.columns = ["Event hour", "Transactions", "Previous-day process_date", "Share"]
display(t.round({"Share": 4}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3.4 · How many transactions would a dispute intake have to choose from?
# MAGIC Very few. In the 30 days before a reference date, the median customer-date has 1 own transaction, 9 in 10 have at most 2
# MAGIC and 99 in 100 at most 4 (max 10). Finding the disputed charge is a lookup plus a customer confirmation, not a learning
# MAGIC problem (report Q4.11).

# COMMAND ----------

cs = q(f"""
WITH ref AS (SELECT explode(array(DATE'2024-03-15', DATE'2025-03-15', DATE'2026-03-15')) AS d),
cust AS (SELECT DISTINCT customer_id FROM {B}.products),
t AS (SELECT customer_id, to_date(try_cast(transaction_date AS TIMESTAMP)) AS td FROM {B}.transactions),
hits AS (SELECT ref.d, t.customer_id, count(*) AS n30
         FROM t JOIN ref ON t.td > date_sub(ref.d, 30) AND t.td <= ref.d GROUP BY 1, 2),
k AS (SELECT ref.d, cust.customer_id, coalesce(hits.n30, 0) AS n30
      FROM ref CROSS JOIN cust LEFT JOIN hits ON hits.d = ref.d AND hits.customer_id = cust.customer_id)
SELECT least(n30, 6) AS bucket, count(*) AS n, max(n30) AS max_n30, (SELECT count(*) FROM cust) AS n_cust
FROM k GROUP BY 1 ORDER BY 1
""")
n_cd, n_cust = int(cs.n.sum()), int(cs.n_cust.iloc[0])
cs["share"] = cs.n / n_cd
cs["cum"] = cs.share.cumsum()
pq = {p_: int(cs[cs.cum >= p_].bucket.iloc[0]) for p_ in (0.5, 0.9, 0.99)}  # smallest count reaching each percentile
mx = int(cs.max_n30.max())
plain = [f"{b}–{mx}" if b == 6 else str(b) for b in cs.bucket]
tag = {pq[0.5]: "p50", pq[0.9]: "p90", pq[0.99]: "p99"}
labels = [lab + (f"<br><b>{tag[b]}</b>" if b in tag else "") for lab, b in zip(plain, cs.bucket)]

fig = go.Figure(go.Bar(
    x=labels, y=cs.share, marker_color=[BLUE if b <= pq[0.9] else GREY for b in cs.bucket],
    text=[f"{v:.1%}" for v in cs.share], textposition="outside", cliponaxis=False,
    textfont=dict(color=INK2, size=12),
    customdata=[[lab, int(n), c] for lab, n, c in zip(plain, cs.n, cs.cum)],
    hovertemplate="<b>%{customdata[0]} transactions</b><br>%{y:.1%} of customer-dates (%{customdata[1]:,} of "
                  f"{n_cd:,})<br>cumulative %{{customdata[2]:.1%}}<extra></extra>"))
fig.update_xaxes(title_text="Own transactions in the 30 days before the reference date")
fig.update_yaxes(range=[0, 0.52], tickformat=".0%")
show(style(fig, f"In {cs[cs.bucket <= pq[0.9]].share.sum():.0%} of customer-dates, the customer has at most {pq[0.9]} candidate "
                f"transactions in a 30-day window",
           f"Share of customer-dates by own transactions in the 30 days before 15 Mar 2024, 2025 and 2026 · "
           f"n = {n_cust:,} customers with products × 3 dates = {n_cd:,}",
           height=380))

t = cs[["bucket", "n", "share", "cum"]].copy()
t["bucket"] = plain
t.columns = ["Own transactions (30 d)", "Customer-dates", "Share", "Cumulative share"]
display(t.round({"Share": 3, "Cumulative share": 3}))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 · Products, credit and data quality
# MAGIC **Product status cannot be trusted, credit data carries no risk signal and two documented keys are broken, so Silver has to repair the data before an agent can answer from it.**
# MAGIC
# MAGIC Tables: `products` (n = 400,000), `customers` (n = 150,000) and 22 of the 24 foreign keys in the data dictionary (the other 2 belong to the unloaded `digital_events`). Because no credit outcome is learnable,
# MAGIC credit eligibility can only be a documented rule policy served by a synthetic policy service, never a model.

# COMMAND ----------

k = q(f"""
WITH cards AS (
  SELECT count_if(product_type = 'Tarjeta Crédito' AND product_status = 'Active' AND try_cast(expiration_date AS DATE) IS NOT NULL) AS cc_n,
         count_if(product_type = 'Tarjeta Crédito' AND product_status = 'Active' AND try_cast(expiration_date AS DATE) < DATE'2026-06-17') AS cc_exp,
         count_if(product_type = 'Tarjeta Débito' AND product_status = 'Active' AND try_cast(expiration_date AS DATE) IS NOT NULL) AS dc_n,
         count_if(product_type = 'Tarjeta Débito' AND product_status = 'Active' AND try_cast(expiration_date AS DATE) < DATE'2026-06-17') AS dc_exp
  FROM {B}.products),
elig AS (
  SELECT count(*) AS cust_n,
         count_if(try_cast(credit_score AS DOUBLE) IS NULL OR try_cast(estimated_monthly_income AS DOUBLE) IS NULL) AS either_null
  FROM {B}.customers),
cp AS (
  SELECT IF(try_cast(p.days_past_due AS DOUBLE) > 90, 1, 0) AS y, -try_cast(c.credit_score AS DOUBLE) AS s
  FROM {B}.products p JOIN {B}.customers c ON p.customer_id = c.customer_id
  WHERE p.product_type IN ('Tarjeta Crédito', 'Préstamo Personal', 'Préstamo Hipotecario')
    AND try_cast(p.days_past_due AS DOUBLE) IS NOT NULL AND try_cast(c.credit_score AS DOUBLE) IS NOT NULL),
r AS (SELECT y, rank() OVER (ORDER BY s) + (count(*) OVER (PARTITION BY s) - 1) / 2.0 AS ar FROM cp),
auc AS (
  SELECT sum(y) AS n_pos, count(*) - sum(y) AS n_neg,
         (sum(IF(y = 1, ar, 0)) - sum(y) * (sum(y) + 1) / 2.0) / (sum(y) * (count(*) - sum(y))) AS auc
  FROM r),
br AS (SELECT DISTINCT branch_id AS k FROM {B}.branches),
fk_c AS (
  SELECT count_if(x.v IS NOT NULL) AS c_nn, count_if(x.v IS NOT NULL AND br.k IS NULL) AS c_orph
  FROM (SELECT registration_branch_id AS v FROM {B}.customers) x LEFT JOIN br ON x.v = br.k),
fk_a AS (
  SELECT count_if(x.v IS NOT NULL) AS a_nn, count_if(x.v IS NOT NULL AND br.k IS NULL) AS a_orph
  FROM (SELECT assigned_branch_id AS v FROM {B}.service_agents) x LEFT JOIN br ON x.v = br.k)
SELECT * FROM cards, elig, auc, fk_c, fk_a
""").iloc[0]
k = {c: (float(v) if c == "auc" else int(v)) for c, v in k.items()}

broken = int(k["c_orph"] > 0) + int(k["a_orph"] > 0)
tiles([
    {"value": pct(k["cc_exp"], k["cc_n"]), "label": "Active credit cards past expiry",
     "note": f"{k['cc_exp']:,} of {k['cc_n']:,} with an expiry date · debit cards {pct(k['dc_exp'], k['dc_n'])}"},
    {"value": f"{k['auc']:.3f}", "label": "Credit score vs. delinquency (AUC)",
     "note": f"0.500 is a coin flip · {k['n_pos']:,} of {k['n_pos'] + k['n_neg']:,} scored products > 90 days past due"},
    {"value": pct(k["either_null"], k["cust_n"]), "label": "Customers missing score or income",
     "note": f"{k['either_null']:,} of {k['cust_n']:,} cannot be assessed by a credit policy"},
    {"value": f"{broken} of 22", "label": "Checked foreign keys broken",
     "note": f"22 of the 24 documented keys checked · both point to branches: {k['c_orph']:,} / {k['c_nn']:,} and {k['a_orph']:,} / {k['a_nn']:,} orphans"},
])

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4.1 · Is an "Active" card actually usable?
# MAGIC Not necessarily. Half of the Active cards with an expiry date expired before the data ends, so the status field alone cannot answer "is my card working?".
# MAGIC Silver derives `effective_status = 'Expired'` from the expiry date (issue 6 in 4.5).

# COMMAND ----------

df = q(f"""
SELECT CASE product_type WHEN 'Tarjeta Crédito' THEN 'Credit cards' ELSE 'Debit cards' END AS card,
       count_if(try_cast(expiration_date AS DATE) IS NOT NULL) AS with_expiry,
       count_if(try_cast(expiration_date AS DATE) < DATE'2026-06-17') AS past_expiry
FROM {B}.products
WHERE product_type IN ('Tarjeta Crédito', 'Tarjeta Débito') AND product_status = 'Active'
GROUP BY 1 ORDER BY 1
""")
df["not_expired"] = df.with_expiry - df.past_expiry
df["past_share"] = df.past_expiry / df.with_expiry
df["valid_share"] = df.not_expired / df.with_expiry

fig = go.Figure()
fig.add_trace(go.Bar(
    y=df.card, x=df.past_share, orientation="h", marker_color=BLUE, name="Past expiry",
    text=[f"<b>Past expiry {s:.1%}</b>  ·  {n:,}" for s, n in zip(df.past_share, df.past_expiry)],
    textposition="inside", insidetextanchor="start", insidetextfont=dict(color="white", size=13),
    customdata=list(zip(df.past_expiry, df.with_expiry)),
    hovertemplate="%{y} · past expiry but Active<br>%{x:.1%} (%{customdata[0]:,} of %{customdata[1]:,})<extra></extra>"))
fig.add_trace(go.Bar(
    y=df.card, x=df.valid_share, orientation="h", marker_color=GREY, name="Not yet expired",
    text=[f"Not yet expired {s:.1%}  ·  {n:,}" for s, n in zip(df.valid_share, df.not_expired)],
    textposition="inside", insidetextanchor="end", insidetextfont=dict(color=INK, size=13),
    customdata=list(zip(df.not_expired, df.with_expiry)),
    hovertemplate="%{y} · not yet expired<br>%{x:.1%} (%{customdata[0]:,} of %{customdata[1]:,})<extra></extra>"))
fig.update_layout(barmode="stack")
fig.update_xaxes(range=[0, 1], tickformat=".0%", dtick=0.25)
fig.update_yaxes(autorange="reversed")
n_exp = dict(zip(df.card, df.with_expiry.astype(int)))
show(style(fig, "Half of the Active cards with an expiry date are already past it",
           f"Active cards with an expiration date, checked against data end (2026-06-17) · credit n = {n_exp['Credit cards']:,} · debit n = {n_exp['Debit cards']:,}",
           height=300, horizontal=True))

display(pd.DataFrame({
    "Card": df.card,
    "Active with expiry date": df.with_expiry,
    "Past expiry": df.past_expiry,
    "Past expiry %": (100 * df.past_share).round(1),
    "Not yet expired": df.not_expired,
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4.2 · Does credit score predict delinquency?
# MAGIC No. The share of credit products more than 90 days past due stays between 4.76% and 5.05% across score bands, and the score ranks delinquency at
# MAGIC AUC 0.504 (0.500 is chance). There is no outcome to learn eligibility from.

# COMMAND ----------

df = q(f"""
WITH cu AS (SELECT customer_id, try_cast(credit_score AS DOUBLE) AS score FROM {B}.customers),
cp AS (SELECT try_cast(p.days_past_due AS DOUBLE) AS dpd, cu.score
       FROM {B}.products p LEFT JOIN cu ON p.customer_id = cu.customer_id
       WHERE p.product_type IN ('Tarjeta Crédito', 'Préstamo Personal', 'Préstamo Hipotecario')
         AND try_cast(p.days_past_due AS DOUBLE) IS NOT NULL)
SELECT CASE WHEN score IS NULL THEN 6 WHEN score < 580 THEN 1 WHEN score < 650 THEN 2
            WHEN score < 720 THEN 3 WHEN score < 780 THEN 4 ELSE 5 END AS band_order,
       count(*) AS products, sum(IF(dpd > 90, 1, 0)) AS dpd90
FROM cp GROUP BY 1 ORDER BY 1
""")
df["band"] = df.band_order.map({1: "Under 580", 2: "580–649", 3: "650–719", 4: "720–779", 5: "780+", 6: "No score"})
z = 1.96
p, n = df.dpd90 / df.products, df.products
centre = (p + z**2 / (2 * n)) / (1 + z**2 / n)
half = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / (1 + z**2 / n)
df["rate"], df["lo"], df["hi"] = p, centre - half, centre + half
overall = df.dpd90.sum() / df.products.sum()
focus = ["Under 580", "780+"]

fig = go.Figure(go.Bar(
    x=df.band, y=df.rate, marker_color=emphasize(df.band, focus),
    error_y=dict(type="data", symmetric=False, array=df.hi - df.rate, arrayminus=df.rate - df.lo,
                 color=INK2, thickness=1.5, width=6),
    customdata=list(zip(df.dpd90, df.products, df.lo, df.hi)),
    hovertemplate="Credit score %{x}<br>%{y:.2%} more than 90 days past due (%{customdata[0]:,} of %{customdata[1]:,})"
                  "<br>95% CI %{customdata[2]:.2%} – %{customdata[3]:.2%}<extra></extra>"))
for b, r, h in zip(df.band, df.rate, df.hi):
    fig.add_annotation(x=b, y=h, text=f"<b>{r:.2%}</b>" if b in focus else f"{r:.2%}", showarrow=False,
                       yshift=11, font=dict(color=INK if b in focus else INK2, size=13))
fig.add_shape(type="line", xref="paper", x0=0, x1=1, y0=overall, y1=overall, line=dict(color=MUTED, width=1))
fig.add_annotation(xref="paper", x=1.005, xanchor="left", y=overall, yanchor="middle", showarrow=False, align="left",
                   text=f"All credit<br>products {overall:.2%}", font=dict(color=MUTED, size=11))
fig.update_yaxes(range=[0, 0.068], tickformat=".0%", dtick=0.01, title_text="More than 90 days past due")
fig.update_xaxes(title_text="Owner's credit score")
fig = style(fig, "A credit score under 580 carries the same delinquency as a score of 780+",
            f"Credit cards, personal loans and mortgages more than 90 days past due, by the owner's credit score · whiskers show 95% CIs · n = {df.products.sum():,}",
            height=380)
fig.update_layout(margin=dict(r=120))
show(fig)

display(pd.DataFrame({
    "Credit score": df.band,
    "Credit products": df.products,
    "> 90 days past due": df.dpd90,
    "Rate %": (100 * df.rate).round(2),
    "95% CI low %": (100 * df.lo).round(2),
    "95% CI high %": (100 * df.hi).round(2),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4.3 · Could a credit policy even run on this data?
# MAGIC Only for two customers in three. The credit score is missing for 15.0% of customers and income for 20.0%, and 32.0% lack at least one of them,
# MAGIC at the same rate in every segment. The agent needs an explicit insufficient-data path (issue 21).

# COMMAND ----------

df = q(f"""
SELECT CASE WHEN grouping(segment) = 1 THEN 'All customers' ELSE coalesce(segment, 'Unknown') END AS segment,
       count(*) AS customers,
       count_if(try_cast(credit_score AS DOUBLE) IS NULL) AS score_missing,
       count_if(try_cast(estimated_monthly_income AS DOUBLE) IS NULL) AS income_missing,
       count_if(try_cast(credit_score AS DOUBLE) IS NULL OR try_cast(estimated_monthly_income AS DOUBLE) IS NULL) AS either_missing
FROM {B}.customers GROUP BY ROLLUP(segment) ORDER BY customers DESC
""")
for c in ["score", "income", "either"]:
    df[f"{c}_pct"] = df[f"{c}_missing"] / df.customers
allc = df[df.segment == "All customers"].iloc[0]
n_cust = int(allc.customers)
seg = df[df.segment != "All customers"]

bars = pd.DataFrame({
    "input": ["Credit score", "Monthly income", "Score or income (policy cannot run)"],
    "missing": [int(allc.score_missing), int(allc.income_missing), int(allc.either_missing)],
})
bars["share"] = bars.missing / n_cust
fig = go.Figure(go.Bar(
    y=bars.input, x=bars.share, orientation="h", marker_color=emphasize(bars.input, "Score or income (policy cannot run)"),
    text=[f"<b>{s:.1%}</b>  ·  {m:,} customers" for s, m in zip(bars.share, bars.missing)],
    textposition="outside", cliponaxis=False, textfont=dict(color=INK2, size=13),
    customdata=list(zip(bars.missing, [n_cust] * 3)),
    hovertemplate="%{y} missing<br>%{x:.1%} (%{customdata[0]:,} of %{customdata[1]:,} customers)<extra></extra>"))
fig.update_xaxes(range=[0, 0.45], tickformat=".0%", dtick=0.1)
fig.update_yaxes(autorange="reversed")
show(style(fig, "One customer in three lacks a credit score or an income",
           f"Customers with a missing value, n = {n_cust:,} · 'score or income' ranges "
           f"{seg.either_pct.min():.1%}–{seg.either_pct.max():.1%} across the {len(seg)} segments",
           height=300, horizontal=True))

display(pd.DataFrame({
    "Segment": df.segment,
    "Customers": df.customers,
    "Score missing %": (100 * df.score_pct).round(1),
    "Income missing %": (100 * df.income_pct).round(1),
    "Either missing %": (100 * df.either_pct).round(1),
    "Either missing (n)": df.either_missing,
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4.4 · Do the documented joins hold?
# MAGIC Almost all. 20 of the 22 checked foreign keys have no orphans, so Silver can enforce them as hard expectations (the data dictionary
# MAGIC documents 24; the other 2 belong to `digital_events`, which is not loaded). The two that fail both point to
# MAGIC `branches` and almost none of their values has a parent, so "your branch" cannot be grounded (issue 14). `complaints.origin_interaction_id` passes only
# MAGIC because it is always empty (issue 1).

# COMMAND ----------

df = q(f"""
WITH cu AS (SELECT DISTINCT customer_id AS k FROM {B}.customers),
br AS (SELECT DISTINCT branch_id AS k FROM {B}.branches),
ag AS (SELECT DISTINCT agent_id AS k FROM {B}.service_agents),
pr AS (SELECT DISTINCT product_id AS k FROM {B}.products),
it AS (SELECT DISTINCT interaction_id AS k FROM {B}.call_center_interactions),
ca AS (SELECT DISTINCT campaign_id AS k FROM {B}.marketing_campaigns),
fk_cu AS (SELECT 'products.customer_id' AS fk, customer_id AS v FROM {B}.products
          UNION ALL SELECT 'transactions.customer_id', customer_id FROM {B}.transactions
          UNION ALL SELECT 'call_center_interactions.customer_id', customer_id FROM {B}.call_center_interactions
          UNION ALL SELECT 'call_transcripts.customer_id', customer_id FROM {B}.call_transcripts
          UNION ALL SELECT 'satisfaction_surveys.customer_id', customer_id FROM {B}.satisfaction_surveys
          UNION ALL SELECT 'complaints.customer_id', customer_id FROM {B}.complaints
          UNION ALL SELECT 'campaign_sends.customer_id', customer_id FROM {B}.campaign_sends),
fk_br AS (SELECT 'customers.registration_branch_id' AS fk, registration_branch_id AS v FROM {B}.customers
          UNION ALL SELECT 'products.opening_branch_id', opening_branch_id FROM {B}.products
          UNION ALL SELECT 'service_agents.assigned_branch_id', assigned_branch_id FROM {B}.service_agents
          UNION ALL SELECT 'transactions.branch_id', branch_id FROM {B}.transactions
          UNION ALL SELECT 'complaints.related_branch_id', related_branch_id FROM {B}.complaints),
fk_ag AS (SELECT 'call_center_interactions.agent_id' AS fk, agent_id AS v FROM {B}.call_center_interactions
          UNION ALL SELECT 'call_transcripts.agent_id', agent_id FROM {B}.call_transcripts
          UNION ALL SELECT 'satisfaction_surveys.agent_id', agent_id FROM {B}.satisfaction_surveys
          UNION ALL SELECT 'complaints.assigned_agent_id', assigned_agent_id FROM {B}.complaints),
fk_pr AS (SELECT 'transactions.product_id' AS fk, product_id AS v FROM {B}.transactions
          UNION ALL SELECT 'complaints.affected_product_id', affected_product_id FROM {B}.complaints),
fk_it AS (SELECT 'call_transcripts.interaction_id' AS fk, interaction_id AS v FROM {B}.call_transcripts
          UNION ALL SELECT 'satisfaction_surveys.interaction_id', interaction_id FROM {B}.satisfaction_surveys
          UNION ALL SELECT 'complaints.origin_interaction_id', origin_interaction_id FROM {B}.complaints),
fk_ca AS (SELECT 'campaign_sends.campaign_id' AS fk, campaign_id AS v FROM {B}.campaign_sends)
SELECT fk, 'customers' AS parent, count(*) AS n, count_if(v IS NULL) AS n_null, count_if(v IS NOT NULL AND cu.k IS NULL) AS orphans
  FROM fk_cu LEFT JOIN cu ON fk_cu.v = cu.k GROUP BY fk
UNION ALL SELECT fk, 'branches', count(*), count_if(v IS NULL), count_if(v IS NOT NULL AND br.k IS NULL) FROM fk_br LEFT JOIN br ON fk_br.v = br.k GROUP BY fk
UNION ALL SELECT fk, 'service_agents', count(*), count_if(v IS NULL), count_if(v IS NOT NULL AND ag.k IS NULL) FROM fk_ag LEFT JOIN ag ON fk_ag.v = ag.k GROUP BY fk
UNION ALL SELECT fk, 'products', count(*), count_if(v IS NULL), count_if(v IS NOT NULL AND pr.k IS NULL) FROM fk_pr LEFT JOIN pr ON fk_pr.v = pr.k GROUP BY fk
UNION ALL SELECT fk, 'call_center_interactions', count(*), count_if(v IS NULL), count_if(v IS NOT NULL AND it.k IS NULL) FROM fk_it LEFT JOIN it ON fk_it.v = it.k GROUP BY fk
UNION ALL SELECT fk, 'marketing_campaigns', count(*), count_if(v IS NULL), count_if(v IS NOT NULL AND ca.k IS NULL) FROM fk_ca LEFT JOIN ca ON fk_ca.v = ca.k GROUP BY fk
""")
df["non_null"] = df.n - df.n_null
df["orphan_rate"] = [o / nn if nn else 0.0 for o, nn in zip(df.orphans, df.non_null)]
df["status"] = ["FAIL" if o > 0 else "PASS" for o in df.orphans]
df["label"] = df.fk + "  →  " + df.parent
df = pd.concat([df[df.status == "FAIL"].sort_values("orphans", ascending=False),
                df[df.status == "PASS"].sort_values("fk")], ignore_index=True)


def rate_txt(r):
    return f"{100 * r:.3f}%" if r > 0.999 else f"{100 * r:.2f}%"


fail, ok = df[df.status == "FAIL"], df[df.status == "PASS"]
fig = go.Figure()
fig.add_trace(go.Bar(
    y=fail.label, x=fail.orphan_rate, orientation="h", marker_color=CRITICAL, width=0.78, constraintext="none",
    text=[f"<b>FAIL</b>  ·  {o:,} of {nn:,} values have no parent ({rate_txt(r)})"
          for o, nn, r in zip(fail.orphans, fail.non_null, fail.orphan_rate)],
    textposition="inside", insidetextanchor="start", insidetextfont=dict(color="white", size=12.5),
    customdata=list(zip(fail.orphans, fail.non_null, fail.n_null / fail.n)),
    hovertemplate="%{y}<br>FAIL · orphans %{customdata[0]:,} of %{customdata[1]:,} non-null values"
                  "<br>Null share %{customdata[2]:.1%}<extra></extra>"))
empty = ok.non_null == 0
ok_text = [
    f" <span style='color:{GOOD}'><b>PASS</b></span>  ·  no values to check: {n:,} of {n:,} are null" if e
    else f" <span style='color:{GOOD}'><b>PASS</b></span>  ·  0 orphans in {nn:,} values"
    for e, n, nn in zip(empty, ok.n, ok.non_null)
]
fig.add_trace(go.Scatter(
    y=ok.label, x=[0] * len(ok), mode="markers+text", cliponaxis=False,
    marker=dict(color=GOOD, size=9, symbol=["circle-open" if e else "circle" for e in empty], line=dict(color=GOOD, width=2)),
    text=ok_text, textposition="middle right", textfont=dict(color=INK2, size=12),
    customdata=list(zip(ok.orphans, ok.non_null, ok.n_null / ok.n)),
    hovertemplate="%{y}<br>PASS · orphans %{customdata[0]:,} of %{customdata[1]:,} non-null values"
                  "<br>Null share %{customdata[2]:.1%}<extra></extra>"))
fig.update_xaxes(range=[0, 1], tickformat=".0%", dtick=0.25, title_text="Orphans, % of non-null key values")
fig.update_yaxes(autorange="reversed", tickfont=dict(color=INK2, size=12), ticksuffix="     ")
fig = style(fig, f"{len(ok)} of the {len(df)} checked keys are intact; the {len(fail)} that fail both point to branches",
            "Orphan = non-null key value with no parent row · PASS = 0 orphans · "
            f"{len(df)} of the 24 dictionary keys (2 are in digital_events, not loaded)",
            height=640, horizontal=True)
fig.update_yaxes(showline=False)  # the 0% gridline marks the baseline; an axis line would cut through the PASS markers
show(fig)

display(pd.DataFrame({
    "Foreign key": df.fk,
    "Parent table": df.parent,
    "Rows": df.n,
    "Null %": (100 * df.n_null / df.n).round(2),
    "Non-null values": df.non_null,
    "Orphans": df.orphans,
    "Orphan %": (100 * df.orphan_rate).round(3),
    "Status": df.status,
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4.5 · What must Silver fix before an agent can answer?
# MAGIC 22 issues came out of the profiling. Each one has its evidence, its impact on the workflow and the Silver rule that handles it; the chip shows the kind of rule.
# MAGIC This table is the written report's section 6 (static content; † marks figures that no notebook query reproduces).

# COMMAND ----------

import html as _html
import re as _re

RULES = {  # kind: meaning. Rule kinds are nominal categories, so every chip shares one neutral style and the text carries the meaning.
    "FIX": "derive a corrected column; the raw value is kept",
    "FLAG": "keep the value and add a boolean flag",
    "QUARANTINE": "null the value (raw copy kept) and exclude it from joins",
    "DROP": "remove the column from Silver and from feature sets",
    "CONTRACT": "a documented expectation or usage rule that Silver enforces",
}
ISSUES = [
    (1, "Complaint ↔ contact link missing", "`origin_interaction_id` 0 / 67,095; same-day re-link 0.46% vs 0.42% chance",
     "No call-to-case trace", ["FLAG"], "`link_status = 'unlinked'`; no fuzzy re-linking"),
    (2, "Complaint product belongs to someone else", "44,570 / 44,570 linked products not owned by the complainant",
     "Wrong product cited in a dispute", ["QUARANTINE"], "`affected_product_id`"),
    (3, "Claimed amount and currency are random", "0 / 21,751 amount matches; currency neither home nor USD 10,916 / 21,776",
     "Wrong amount or currency", ["FLAG"],
     "`claim_untraceable`; never FX-convert; intake takes amount and currency from the selected transaction"),
    (4, "Complaint outcome fields random or stale",
     "SLA flag flat 18.5–21.4%; 45,629 / 46,948 Open or In Process cases > 30 days; `compensation_granted` holds amounts in 4,641 rows†",
     "Invalid labels; wrong status answers", ["FIX", "FLAG"],
     "FIX `compensation_amount` + `compensation_flag`; FIX `sla_derived` from dates; FLAG `stale_status`; never use as labels"),
    (5, "Text reveals the label", "5 descriptions for 67,095 rows (1 per category); subcategory 1:1 with category",
     "Label leakage", ["FIX", "DROP"], "FIX: impute subcategory from category; DROP `description` from features"),
    (6, "Active cards past expiry", "40,484 / 80,864 credit; 16,180 / 32,141 debit",
     "\"Your card is active\" would be wrong", ["FIX"], "`effective_status = 'Expired'` when `expiration_date < as_of_date`"),
    (7, "Stale last-movement field", "`last_transaction_date` matches 427 / 305,721",
     "Wrong \"last movement\" answer", ["FIX"], "Recompute from `silver.transactions`"),
    (8, "Activity before opening or registration", "827,610 / 4,425,008 before opening†; 829,540 before registration†",
     "Incoherent timelines", ["FLAG", "FIX"], "FLAG; `effective_opening_date = least(opening_date, first transaction)`"),
    (9, "Email is not an identity key",
     "79,930 / 147,016 customers share an email (54.4%); `document_number` unique 150,000 / 150,000",
     "Wrong-customer authentication", ["CONTRACT"], "Email is non-unique; authenticate on document type + number + a second factor"),
    (10, "Product-number collisions", "12 / 400,000 (6 pairs, different customers)",
     "Card/account-number lookup collision", ["QUARANTINE"], "Exclude from number lookup"),
    (11, "Mexico in USD, document type `DNI`", "200,398 / 200,398 products in USD; 74,907 / 74,907 `DNI`",
     "Currency and ID display", ["FLAG", "CONTRACT"], "FLAG + documented assumption: quote Mexican balances in USD"),
    (12, "`amount_usd` fixed-rate and partly missing", "100% equal amount / 350 or / 4000; 99,477 / 1,987,029 missing",
     "Inconsistent USD equivalents", ["FIX"],
     "Fill with the same fixed rate; daily FX only for customer-facing conversions, labeled as such"),
    (13, "Two spellings of Mexico", "`Mexico` 40,515 rows: 18,412 from Mexican customers, 22,103 from Colombian and Argentine customers†",
     "Comparing countries as text would mark the 18,412 Mexican customers' rows as international (Bronze has no such flag); "
     "the other 22,103 are genuine foreign transactions", ["FIX"], "Normalize accents; derive `is_international`"),
    (14, "Branch foreign keys broken", "`registration_branch_id` 149,995 / 150,000 orphans; `assigned_branch_id` 831 / 833",
     "\"Your branch\" cannot be grounded", ["QUARANTINE", "CONTRACT"],
     "QUARANTINE both keys; the other 20 checked keys have 0 orphans → hard expectation"),
    (15, "`mentioned_products` is random", "545,118 / 548,680 mentioned IDs match no product; the 3,562 that exist all belong to another customer",
     "Privacy and grounding hazard", ["DROP"], "Remove the column"),
    (16, "Fields that copy the label",
     "`main_topics` = reason 171,321 / 171,321; sentiment label = bins of the score; surveys = f(`was_resolved`)",
     "Leakage", ["DROP"], "Exclude from features; keep `sentiment_score` only"),
    (17, "Truncated survey scales", "CSAT 1–4; NPS 2–7 with 0 promoters; `nps_category` null 3,274 / 63,668†",
     "Degenerate KPIs", ["FIX"], "`nps_category` from score; report relative KPIs only"),
    (18, "Implausible type × channel", "1,240,000 / 4,425,008 (28.0%)",
     "Narration sounds wrong", ["FLAG"], "Omit the channel when narrating flagged rows"),
    (19, "`process_date` is a business-day cut-off", "Previous day for 25.0% of transactions, 33.3% of contacts; 0 later",
     "\"When\" answers off by a day; split leakage", ["FIX", "CONTRACT"],
     "`event_date` from the timestamp; `process_date` as partition key only"),
    (20, "Customer status conflicts with products", "2,694 / 2,979 Closed customers hold an Active product",
     "Authorization ambiguity", ["FLAG", "CONTRACT"], "FLAG + precedence rule (Closed/Suspended → human handoff)"),
    (21, "Random missingness in key inputs",
     "score 15.0%, income 20.0%, either 32.0%; `fraud_score` 20.0%; `response_code` ~5%",
     "Eligibility and decline answers", ["FLAG"], "`null_reason` (not applicable vs missing); explicit insufficient-data path"),
    (22, "Duplicates, typing, lineage", "0 duplicate groups on 6 natural keys; 0 rescued rows; 0 invalid amounts",
     "None today", ["CONTRACT", "QUARANTINE"], "Keep natural-key dedup with an expectation of 0; strict casting with quarantine"),
]


def _fmt(s):
    s = _html.escape(s, quote=False)
    return _re.sub(r"`([^`]+)`", r"<code style='font-size:11.5px;background:#f1f0ec;padding:0 3px;border-radius:3px;color:" + INK + r"'>\1</code>", s)


def _chip(kind):
    return (f"<span style='display:inline-block;padding:1px 7px;margin:0 4px 3px 0;border-radius:4px;background:#efefeb;"
            f"border:1px solid {AXIS};color:{INK};font-size:10.5px;font-weight:700;letter-spacing:.04em'>{kind}</span>")


counts = {kind: sum(kind in r[4] for r in ISSUES) for kind in RULES}
legend = "".join(
    f"<div style='margin:0 18px 6px 0;font-size:12px;color:{INK2}'>{_chip(kind)} <b>{counts[kind]}</b> · {RULES[kind]}</div>"
    for kind in RULES)
cell = f"padding:8px 10px;border-bottom:1px solid {GRID};vertical-align:top"
rows = "".join(
    f"<tr style='background:{'#ffffff' if i % 2 else SURFACE}'>"
    f"<td style='{cell};color:{MUTED};text-align:right'>{n}</td>"
    f"<td style='{cell};color:{INK};font-weight:600'>{_fmt(issue)}</td>"
    f"<td style='{cell}'>{_fmt(ev)}</td>"
    f"<td style='{cell}'>{_fmt(imp)}</td>"
    f"<td style='{cell}'>{''.join(_chip(k) for k in kinds)}<div style='color:{INK2}'>{_fmt(rule)}</div></td></tr>"
    for i, (n, issue, ev, imp, kinds, rule) in enumerate(ISSUES))
head = "".join(
    f"<th style='padding:8px 10px;text-align:{a};font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:{MUTED};"
    f"font-weight:600;border-bottom:2px solid {AXIS};width:{w}'>{t}</th>"
    for t, w, a in [("#", "4%", "right"), ("Issue", "17%", "left"), ("Evidence (n / denominator)", "29%", "left"),
                    ("Impact on the workflow", "17%", "left"), ("Silver rule", "33%", "left")])
displayHTML(f"""
<div style="font-family:{FONT};color:{INK2};font-size:13px;line-height:1.45;max-width:1100px">
  <div style="font-size:17px;font-weight:700;color:{INK}">22 data-quality issues, each with a Silver rule</div>
  <div style="font-size:13px;color:{INK2};margin:2px 0 12px">Rule kinds and how many issues use each one (an issue can use two)</div>
  <div style="display:flex;flex-wrap:wrap;margin-bottom:10px">{legend}</div>
  <table style="width:100%;border-collapse:collapse;table-layout:fixed;background:{SURFACE}">
    <thead><tr>{head}</tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div style="font-size:11.5px;color:{MUTED};margin-top:8px">† Not reproduced by a query in either EDA notebook. Source: docs/02_eda_workflow_selection.md, section 6.</div>
</div>
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5 · Decision: workflow, baseline, learned component
# MAGIC **We build transaction-dispute intake: it has the widest service gap to close, it can be grounded on transactions whose keys are clean, and it supports a learned component whose labels are valid by construction.**
# MAGIC
# MAGIC The scorecard below condenses sections 1–4 into eight weighted criteria. The baseline to beat is recomputed live from Bronze;
# MAGIC targets are measured later on a held-out Spanish/Portuguese scenario set.

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.1 · How do the four hackathon workflows compare on the evidence?
# MAGIC Transaction-dispute intake scores 4.05 of 5, ahead of account/payment inquiries (3.90). Card support (2.45) and credit eligibility (2.30) trail far behind:
# MAGIC card demand cannot even be measured, and credit has no outcome label at all. Hover a cell for the evidence behind its score.

# COMMAND ----------

import math

# Scores (1-5, 5 = most favourable; for risk, 5 = least complex) and weights from the written scorecard (report section 7).
WORKFLOWS = ["Transaction-dispute intake", "Account/payment inquiries", "Card support", "Credit info & eligibility"]
CHOSEN = "Transaction-dispute intake"
SCORECARD = [  # (criterion, weight, scores in WORKFLOWS order, evidence in WORKFLOWS order)
    ("Service pain", 0.25, [5, 2, 2, 3], [
        "FCR 43.6%; 41.2% of unresolved; CSAT 2.43; 74.9% of cases open; first response p50 37 h",
        "FCR 91.5%; 12.7% of unresolved; CSAT 2.91", "Not measurable", "FCR 65.2%; 11.9% of unresolved"]),
    ("Demand", 0.15, [3, 5, 2, 2], [
        "Queja 117,021 contacts (17.1%); 40.4% of formal cases are disputes", "240,056 contacts (35.0%)",
        "Not measurable: no card reason code; product links random", "54,879 contacts (8.0%)"]),
    ("Grounding data", 0.15, [4, 5, 3, 2], [
        "Customer, products, transactions 100% consistent; complaint history unusable",
        "Balances 100%; transactions 100% consistent",
        "Status, expiry, declines; but 50.1% of Active credit cards with an expiry date are past it, no card-event data",
        "Score 85% and income 80% present; no underwriting history"]),
    ("Valid labels + baseline", 0.15, [4, 3, 2, 1], [
        "Labels by construction anchored on real transactions; semantic classes leave room over keywords",
        "Labels by construction; simple intents, keyword baseline likely near ceiling",
        "Fraud unlearnable (AUC 0.504); fraud_score leaks the label", "No outcome label; delinquency AUC 0.504"]),
    ("Risk / compliance", 0.10, [3, 5, 3, 2], [
        "Regulated claim intake; the decision stays with humans", "Read-only", "Block/replace actions, fraud",
        "Fair-lending exposure; rules only from a policy service"]),
    ("Feasibility in ~7 days", 0.10, [4, 5, 3, 3], [
        "Reuses lookups; mocked case-write tool", "Lookups only", "Mocked action tools and a state machine",
        "Policy service, FX-converted income, insufficient-data branch"]),
    ("Cost", 0.05, [5, 5, 2, 3], [
        "12,160 handle-hours (23.1%); AHT p50 431 s", "12,663 handle-hours (24.0%)", "Not measurable",
        "7,061 handle-hours (13.4%); AHT p50 540 s"]),
    ("ES/PT demo", 0.05, [4, 4, 4, 3], [
        "Portuguese is team-generated for every option", "Portuguese is team-generated for every option",
        "Portuguese is team-generated for every option", "Product and regulatory terms vary by country"]),
]


def round_half_up(x, digits=2):
    return math.floor(x * 10 ** digits + 0.5 + 1e-9) / 10 ** digits


WEIGHTED = [round_half_up(sum(w * s[j] for _, w, s, _ in SCORECARD)) for j in range(len(WORKFLOWS))]
EQUAL = [round_half_up(sum(s[j] for _, _, s, _ in SCORECARD) / len(SCORECARD)) for j in range(len(WORKFLOWS))]

z = [row[2] for row in SCORECARD]
y_labels = [f"{name} ({w:.0%})" for name, w, _, _ in SCORECARD]
TWO_LINE = {  # column headers broken over two lines so they never rotate on narrower screens
    "Transaction-dispute intake": "Transaction-dispute<br>intake", "Account/payment inquiries": "Account/payment<br>inquiries",
    "Card support": "Card<br>support", "Credit info & eligibility": "Credit info<br>& eligibility",
}
x_labels = [
    (f"<b>{TWO_LINE[wf]}</b><br><b>{t:.2f}</b> weighted · chosen" if wf == CHOSEN else f"{TWO_LINE[wf]}<br>{t:.2f} weighted")
    for wf, t in zip(WORKFLOWS, WEIGHTED)
]
hover = [[f"<b>{WORKFLOWS[j]}</b><br>{name} · weight {w:.0%}<br>Score {s[j]} of 5<br>{ev[j]}" for j in range(len(WORKFLOWS))]
         for name, w, s, ev in SCORECARD]

fig = go.Figure(go.Heatmap(
    z=z, x=x_labels, y=y_labels, zmin=1, zmax=5,
    colorscale=[[i / (len(SEQ) - 2), c] for i, c in enumerate(SEQ[:-1])], showscale=False,
    xgap=3, ygap=3, customdata=hover, hovertemplate="%{customdata}<extra></extra>",
))
for i, (_, _, scores, _) in enumerate(SCORECARD):
    for j, s in enumerate(scores):
        fig.add_annotation(x=x_labels[j], y=y_labels[i], text=f"<b>{s}</b>" if WORKFLOWS[j] == CHOSEN else str(s),
                           showarrow=False, font=dict(size=15, color="white" if s >= 4 else INK))
# Outline the chosen column.
k = WORKFLOWS.index(CHOSEN)
fig.add_shape(type="rect", x0=k - 0.5, x1=k + 0.5, y0=-0.5, y1=len(SCORECARD) - 0.5, line=dict(color=INK, width=2.5))
style(fig, f"Transaction-dispute intake scores highest: {WEIGHTED[k]:.2f} vs {WEIGHTED[1]:.2f} for account/payment",
      "Score per criterion, 1 to 5; darker = more favourable (for risk, 5 = least complex) · weight in parentheses · 4 workflows × 8 criteria",
      height=440)
fig.update_xaxes(showline=False, tickangle=0, tickfont=dict(color=INK2, size=13))
fig.update_yaxes(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=13))
fig.update_layout(margin=dict(t=74))
show(fig)

scorecard_table = pd.DataFrame(
    [[name, f"{w:.0%}"] + s for name, w, s, _ in SCORECARD]
    + [["Weighted score", "100%"] + [f"{t:.2f}" for t in WEIGHTED],
       ["Equal-weight score", "8 × 12.5%"] + [f"{t:.2f}" for t in EQUAL]],
    columns=["Criterion", "Weight"] + WORKFLOWS,
)
display(scorecard_table.astype(str))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.2 · Does the choice survive a different weighting?
# MAGIC Only partly, and we say so.
# MAGIC With equal weights, account/payment leads 4.25 to 4.00. The choice rests on giving service pain the largest weight, which we justify with
# MAGIC the rubric's "problem supported by data" requirement and its call to "establish a baseline". The weights are the team's own: the hackathon
# MAGIC publishes no weights. Card support and credit eligibility rank last under both weightings.

# COMMAND ----------

fig = go.Figure()
for name, values, color, rule in [
    ("Team weights (service pain 25%)", WEIGHTED, BLUE, "team weights"),
    ("Equal weights (12.5% each)", EQUAL, ORANGE, "equal weights"),
]:
    fig.add_bar(
        name=name, x=WORKFLOWS, y=values, marker_color=color,
        text=[f"{v:.2f}" for v in values], textposition="outside", cliponaxis=False,
        textfont=dict(color=INK2, size=13),
        hovertemplate="<b>%{x}</b><br>Score with " + rule + ": %{y:.2f} of 5<br>n = 8 criteria<extra></extra>",
    )
fig.update_layout(barmode="group")
fig.update_yaxes(range=[0, 5.4], dtick=1, title_text="Total score (1–5)")
fig.update_xaxes(tickfont=dict(color=INK2, size=13))
show(style(fig, "The weighting decides the top spot: equal weights put account/payment first",
           "Total score per workflow, 1–5, over 8 criteria · team weights: pain 25%; demand, grounding, labels 15% each; risk, feasibility 10%; cost, ES/PT 5%",
           height=400, legend=True))

sensitivity = pd.DataFrame({
    "Workflow": WORKFLOWS,
    "Team-weighted score": [f"{v:.2f}" for v in WEIGHTED],
    "Equal-weight score": [f"{v:.2f}" for v in EQUAL],
    "Rank (team weights)": pd.Series(WEIGHTED).rank(ascending=False, method="min").astype(int),
    "Rank (equal)": pd.Series(EQUAL).rank(ascending=False, method="min").astype(int),
})
display(sensitivity)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.3 · Why give service pain the largest weight?
# MAGIC Because it is the gap a new workflow can close.
# MAGIC More than half of complaint contacts are left unresolved; transactional inquiries 8.5%, so account/payment has little left to beat.

# COMMAND ----------

pain = q(f"""
SELECT reason_category AS reason, count(*) AS contacts,
       sum(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END) AS resolved
FROM {B}.call_center_interactions
GROUP BY reason_category
""")
pain["unresolved"] = pain.contacts - pain.resolved
overall = pain.unresolved.sum() / pain.contacts.sum()
proxies = [  # (workflow label, proxy reason)
    ("Transaction-dispute intake · Queja", "Queja"),
    ("Credit info & eligibility · Comercial", "Comercial"),
    ("Account/payment inquiries · Transaccional", "Transaccional"),
    ("Card support · no proxy", None),
]
rows = []
for label, reason in proxies:
    r = pain[pain.reason == reason]
    if len(r):
        rows.append(dict(workflow=label, reason=reason, contacts=int(r.contacts.iloc[0]), unresolved=int(r.unresolved.iloc[0]),
                         rate=r.unresolved.iloc[0] / r.contacts.iloc[0]))
    else:
        rows.append(dict(workflow=label, reason="none", contacts=None, unresolved=None, rate=None))
gap = pd.DataFrame(rows)
q_rate = gap.rate.iloc[0]
t_rate = gap.rate.iloc[2]

fig = go.Figure(go.Bar(
    y=gap.workflow, x=gap.rate, orientation="h",
    marker_color=emphasize(gap.workflow, gap.workflow.iloc[0]),
    text=[f"{v:.1%}" if pd.notna(v) else "" for v in gap.rate], textposition="outside", cliponaxis=False,
    textfont=dict(color=INK2, size=13),
    customdata=gap[["unresolved", "contacts"]].fillna(0).astype(int).values,
    hovertemplate="<b>%{y}</b><br>Unresolved at first contact: %{x:.1%}<br>%{customdata[0]:,} of %{customdata[1]:,} contacts<extra></extra>",
))
fig.add_annotation(x=0, y=gap.workflow.iloc[3], xanchor="left", showarrow=False, xshift=6,
                   text="not measurable: no contact reason for cards", font=dict(color=MUTED, size=12), bgcolor=SURFACE)
fig.add_vline(x=overall, line_color=MUTED, line_width=1, layer="below")
fig.add_annotation(x=overall, y=1.0, yref="paper", yanchor="bottom", xanchor="left", xshift=4, showarrow=False,
                   text=f"All contacts {overall:.1%}", font=dict(color=MUTED, size=12))
n_bars = " / ".join(f"{int(v):,}" for v in gap.contacts.dropna())
style(fig, f"Complaint contacts: {q_rate:.1%} unresolved, {q_rate / t_rate:.1f}× the account/payment rate",
      f"Unresolved at first contact, % of each proxy reason's contacts · n = {n_bars} · line = all {int(pain.contacts.sum()):,}",
      height=340, horizontal=True)
fig.update_yaxes(categoryorder="array", categoryarray=list(gap.workflow)[::-1], tickfont=dict(color=INK2, size=13))
fig.update_xaxes(tickformat=".0%", range=[0, 0.68])
show(fig)

display(pd.DataFrame({
    "Workflow": gap.workflow.str.split(" · ").str[0],
    "Proxy reason": gap.reason,
    "Contacts": gap.contacts.apply(lambda v: f"{int(v):,}" if pd.notna(v) else "n/a"),
    "Unresolved": gap.unresolved.apply(lambda v: f"{int(v):,}" if pd.notna(v) else "n/a"),
    "Unresolved (%)": gap.rate.apply(lambda v: f"{100 * v:.1f}" if pd.notna(v) else "n/a"),
    "FCR (%)": gap.rate.apply(lambda v: f"{100 * (1 - v):.1f}" if pd.notna(v) else "n/a"),
}))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.4 · What is the baseline to beat?
# MAGIC Complaint contacts (`Queja`) are the contact-level proxy; dispute-type complaint cases (Transactions + Fees) give the case level.
# MAGIC First row: contacts. Second row: cases. Both are computed live over the full history.

# COMMAND ----------

base = q(f"""
WITH b AS (
  SELECT reason_category AS r,
         CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END AS res,
         CASE WHEN was_resolved = 'True' AND was_escalated = 'False' AND requires_followup = 'False' THEN 1 ELSE 0 END AS strict_res,
         try_cast(duration_seconds AS DOUBLE) AS dur
  FROM {B}.call_center_interactions)
SELECT count_if(r = 'Queja') AS n,
       sum(CASE WHEN r = 'Queja' THEN res END) AS resolved,
       sum(CASE WHEN r = 'Queja' THEN strict_res END) AS strict_resolved,
       count(CASE WHEN r = 'Queja' THEN dur END) AS n_dur,
       percentile(CASE WHEN r = 'Queja' THEN dur END, 0.5) AS aht_p50,
       percentile(CASE WHEN r = 'Queja' THEN dur END, 0.9) AS aht_p90,
       sum(CASE WHEN r = 'Queja' THEN 1 - res END) AS unresolved,
       sum(1 - res) AS unresolved_all
FROM b
""").iloc[0]
n, res, strict = int(base.n), int(base.resolved), int(base.strict_resolved)
tiles([
    {"value": pct(res, n), "label": "Queja FCR", "note": f"{res:,} of {n:,} complaint contacts resolved"},
    {"value": pct(strict, n), "label": "Queja strict FCR", "note": f"{strict:,} of {n:,}; also no escalation or follow-up"},
    {"value": f"{base.aht_p50:,.0f} s", "label": "Queja handle time p50",
     "note": f"p90 {base.aht_p90:,.0f} s · n = {int(base.n_dur):,} voice/video"},
    {"value": pct(base.unresolved, base.unresolved_all), "label": "Share of all unresolved",
     "note": f"{int(base.unresolved):,} of {int(base.unresolved_all):,} unresolved contacts"},
])

# COMMAND ----------

cases = q(f"""
WITH c AS (
  SELECT status, claimed_amount, currency,
         try_cast(creation_date AS TIMESTAMP) AS cd,
         (unix_timestamp(try_cast(first_response_date AS TIMESTAMP)) - unix_timestamp(try_cast(creation_date AS TIMESTAMP))) / 3600.0 AS h_first
  FROM {B}.complaints
  WHERE category IN ('Transactions', 'Fees'))
SELECT count(*) AS n,
       count(h_first) AS n_first,
       percentile(h_first, 0.5) AS first_p50_h,
       percentile(h_first, 0.9) AS first_p90_h,
       count_if(claimed_amount IS NOT NULL AND currency IS NOT NULL) AS n_complete,
       count_if(status IN ('Open', 'In Process', 'Escalated')) AS n_open,
       count_if(cd >= TIMESTAMP'2023-07-01' AND cd < TIMESTAMP'2026-06-01') AS n_full_months,
       CAST(count_if(cd >= TIMESTAMP'2023-07-01' AND cd < TIMESTAMP'2026-06-01') AS DOUBLE) / 35 AS per_full_month
FROM c
""").iloc[0]
cn = int(cases.n)
tiles([
    {"value": f"{cases.first_p50_h:.0f} h", "label": "Dispute first response p50",
     "note": f"p90 {cases.first_p90_h:.0f} h · n = {int(cases.n_first):,} ({pct(cases.n_first, cn)} have one)"},
    {"value": pct(cases.n_complete, cn), "label": "Dispute case completeness",
     "note": f"amount + currency present · {int(cases.n_complete):,} of {cn:,}"},
    {"value": pct(cases.n_open, cn), "label": "Dispute case backlog", "note": f"Open, In Process or Escalated · {int(cases.n_open):,} of {cn:,}"},
    {"value": f"{cases.per_full_month:.1f}", "label": "Dispute cases per month",
     "note": f"{int(cases.n_full_months):,} cases in 35 full months (Jul 2023 – May 2026) · {cn:,} in total"},
])

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.5 · What do we commit to beat?
# MAGIC Targets are measured on a held-out ES/PT scenario set, not on live traffic, so comparisons with the historical baseline are directional.
# MAGIC
# MAGIC | Outcome | Target | Baseline it beats |
# MAGIC |---|---|---|
# MAGIC | First-contact case completion: in-scope scenarios that end with a complete, verified case | **≥ 80%** | Queja FCR 43.6% |
# MAGIC | Correct transaction linked to the case | **≥ 95%** of completed cases | 0% valid product links in historical cases |
# MAGIC | Required fields present (transaction, owned product, amount, currency) | **100%** of created cases | 31.4% amount + currency |
# MAGIC | Grounding violations (product or transaction not owned by the customer) | **0** | 44,570 of 44,570 historical complaint links |
# MAGIC | Must-handoff scenarios handed off, with a reason code | **100%** recall | Escalation today is a random 10% |
# MAGIC | Case number and next step given | **Within the conversation** | First response p50 37 h |
# MAGIC | Intake duration | **Median below 431 s** | Queja handle time p50 |
# MAGIC | ES vs PT gap in completion and macro-F1 | **≤ 5 pp** | New capability (no Portuguese today) |
# MAGIC
# MAGIC **Deliberately not targeted:** recontact (random arrivals), escalation (a 10% coin flip), the SLA flag (random),
# MAGIC CSAT on its own (tied to FCR) and money at stake (claimed amounts are random).

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.6 · What does the agent actually do?
# MAGIC One conversation that builds the case from the customer's own, verified transactions.
# MAGIC It contains the runner-up: the account/payment lookups (steps 1–2) ship on their own if the dispute scope slips.

# COMMAND ----------

FLOW_STEPS = [  # (title, detail, kind)
    ("Authenticate", "Document type + number + a second factor. Never email: 54.4% of customers with one share it.", "Tool"),
    ("List own products &amp; recent transactions", "From Silver, on event time. Typically 1 candidate in 30 days (p90 2).", "Tool"),
    ("Customer confirms the movement", "Amount and currency come from the transaction, never from the claim.", "Customer"),
    ("Classify dispute type", "Unrecognized charge vs incorrect charge or fee, in Spanish or Portuguese.", "Model"),
    ("Create structured case", "Type, category, product, transaction, amount, currency, channel, rule-based priority.", "Tool"),
    ("Case number &amp; next steps", "Given in the conversation. Today the first response takes 37 h (p50).", "Reply"),
]
HANDOFF = ["Suspected fraud or card compromise", "Amount above threshold", "Closed or Suspended customer",
           "Low classifier confidence", "Customer asks for a person"]


def flow_card(i, title, detail, kind):
    learned = kind == "Model"
    return f"""
    <div class="d5-step" style="background:{'#eef5fd' if learned else '#ffffff'};
                border:{'2px solid ' + BLUE if learned else '1px solid rgba(11,11,11,0.12)'};border-radius:10px;padding:12px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="flex:0 0 22px;width:22px;height:22px;border-radius:50%;background:{BLUE if learned else INK2};color:#fff;font-size:12px;
                     font-weight:600;display:inline-flex;align-items:center;justify-content:center">{i}</span>
        <span style="font-size:11px;color:{BLUE if learned else MUTED};font-weight:{600 if learned else 400};
                     text-transform:uppercase;letter-spacing:.04em">{kind}</span>
      </div>
      <div style="font-size:14px;font-weight:600;color:{INK};margin-top:8px;line-height:1.3">{title}</div>
      <div style="font-size:12px;color:{INK2};margin-top:6px;line-height:1.45">{detail}</div>
    </div>"""


arrow = f"""<div class="d5-arrow" style="align-items:center;justify-content:center;color:{MUTED};font-size:16px">&rarr;</div>"""
chips = "".join(
    f"""<span style="display:inline-block;border:1px solid rgba(11,11,11,0.14);border-radius:999px;padding:3px 10px;
                     font-size:12px;color:{INK};background:#ffffff;margin:3px 6px 3px 0">{t}</span>"""
    for t in HANDOFF
)
displayHTML(f"""
<style>
  .d5-step {{ flex: 1 1 0; min-width: 118px; box-sizing: border-box; }}
  .d5-arrow {{ flex: 0 0 12px; display: flex; }}
  @media (max-width: 880px) {{ .d5-step {{ flex: 1 1 28%; }} }}
  @media (max-width: 520px) {{ .d5-step {{ flex: 1 1 100%; }} .d5-arrow {{ display: none; }} }}
</style>
<div style="font-family:{FONT};background:{SURFACE};border:1px solid rgba(11,11,11,0.10);border-radius:12px;padding:18px 18px 14px">
  <div style="font-size:17px;font-weight:700;color:{INK}">Proposed dispute-intake flow: six steps, one conversation</div>
  <div style="font-size:13px;color:{INK2};margin-top:4px">Scope: unrecognized charges and incorrect charges or fees · Spanish and Portuguese ·
    Mexico, Colombia, Argentina. Blue = the only learned step (the intent classifier); the other five are deterministic tools,
    the customer's own confirmation, or the reply.</div>
  <div style="display:flex;align-items:stretch;gap:4px;flex-wrap:wrap;margin-top:16px">
    {arrow.join(flow_card(i + 1, *s) for i, s in enumerate(FLOW_STEPS))}
  </div>
  <div style="margin-top:14px;padding-top:12px;border-top:1px solid {GRID};display:flex;gap:14px;align-items:baseline;flex-wrap:wrap">
    <div style="font-size:14px;font-weight:600;color:{INK};white-space:nowrap">&#8627; Hand off to a human</div>
    <div style="font-size:12px;color:{INK2};white-space:nowrap">from any step, with a reason code, when:</div>
    <div style="flex:1 1 400px">{chips}</div>
  </div>
</div>
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.7 · Learned component: a Spanish/Portuguese intake intent classifier
# MAGIC **Every historical label we tested is noise or a copy of another field**, so the labels are produced by construction and the model must beat two simple baselines.
# MAGIC Negative controls kept on file: `is_fraud` temporal AUC 0.504 and 0.4997 · `dpd > 90` AUC 0.504 against credit score · FCR by agent equals binomial noise ·
# MAGIC CSAT depends only on `was_resolved` · the SLA flag is flat at 18.5–21.4% · complaint descriptions copy the category.
# MAGIC
# MAGIC | Plan element | Choice |
# MAGIC |---|---|
# MAGIC | **Task** | Intent classes `dispute_unrecognized_charge`, `dispute_incorrect_charge_or_fee`, `account_payment_inquiry`, `card_lost_or_block`, `other_complaint`, `out_of_scope`. Slots (amount, currency, relative date, merchant hint) by rules first. |
# MAGIC | **Labels, valid by construction** | A scenario generator samples a real customer, product and transaction from Silver (event time) and records the gold intent, dispute type, transaction, amount, currency and date. ES and PT messages come from paraphrase families with controlled noise: rounded amounts, relative dates ("el martes pasado" / "na terça passada"), 1.234,56 vs 1,234.56, partial merchant names. |
# MAGIC | **Final test** | The independent holdout in `eval/holdout/`: 300 free-form messages (150 per language) produced by a generation process separate from the scenario generator; no person wrote them. The plan also calls for at least 100 hand-written messages per language by both team members, reported separately; none exist yet. |
# MAGIC | **Baselines** | (a) majority class; (b) keyword/regex router written from a glossary before the test set is seen. Transaction resolution stays a deterministic tool (amount ±1%, date ±2 days, then customer confirmation), reported as top-1 accuracy. |
# MAGIC | **Candidate model** | Character n-gram TF-IDF + logistic regression (optionally sentence embeddings + logistic regression). Calibrated probabilities drive an abstain-and-handoff threshold. |
# MAGIC | **Splits** | (1) group split by paraphrase family and `customer_id`; (2) temporal on the anchor transaction's event time, train before 2025-07-01; (3) train on ES only, test on PT; (4) the independent holdout, plus the hand-written messages once they exist. |
# MAGIC | **Metrics** | Macro-F1 per language, recall on the two dispute classes, out-of-scope false-accept rate, abstention rate, slot exact-match, top-1 transaction resolution; 95% bootstrap intervals against both baselines on the same test sets. |

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5.8 · Leakage checklist
# MAGIC Never used as features, split keys or evaluation shortcuts:
# MAGIC
# MAGIC - **Copies of the label:** `reason_category`, `contact_reason`, transcript `main_topics` and `detected_intents`; complaint `description` (5 strings, one per category) and `subcategory` (1:1 with category).
# MAGIC - **Built from the label:** `fraud_score` (precision 1.000 above 30).
# MAGIC - **Known only after the contact:** `was_resolved`, `requires_followup`, `was_escalated`, duration, sentiment, survey scores.
# MAGIC - **Known only after the intake:** case status, resolution dates and days, closing date, compensation, `sla_breached`.
# MAGIC - **Wrong clock:** `process_date` as a split key; 25–34% of events carry the previous day.
# MAGIC - **Train/test overlap:** shared templates or paraphrase families, verbatim `transaction_id` or amount strings in the text, the same generator seed on both sides.
# MAGIC
# MAGIC ### 5.9 · Limitations
# MAGIC - **Synthetic-data artifacts:** uniform categories, scores and timings; FCR driven only by reason; satisfaction set by resolution; random cross-table links; flat demand. The data sizes the problem, sets baselines and grounds the flow; it supports no causal claims about banking behavior.
# MAGIC - **Spanish only:** all historical text is Spanish; Portuguese evaluation data is team-generated, labeled as such and reported separately.
# MAGIC - **Bronze-level numbers:** raw and pre-dedup. With 0 duplicates found, Silver should reproduce them except for the fields its rules recompute.
# MAGIC - **Sample and cost:** interactions look like a sample, so savings are stated as rates, not FTE; handle time exists only for voice and video (590,062 of 686,296 contacts).
# MAGIC - **Assumptions:** the Queja-to-dispute mapping cannot be verified (calls do not link to cases); historical baselines and scenario targets measure different populations.
# MAGIC - **Open questions:** the timezone of event timestamps; `digital_events` is not loaded; the announced ~2% duplicates were not found under the 6 natural keys tested.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Next steps
# MAGIC 1. **Build Silver** with the rules in section 4 (FIX, FLAG, QUARANTINE, DROP) and hard expectations: 0 duplicates on natural keys, 0 orphans on the documented keys, event time instead of `process_date`.
# MAGIC 2. **Scenario generator:** sample real customers, products and transactions from Silver and render ES/PT messages with gold labels; keep the independent holdout (`eval/holdout/`) apart from it and add the hand-written messages before training.
# MAGIC 3. **Intent classifier + baselines:** majority class and keyword router first, then the TF-IDF model, on the group, temporal and ES→PT splits with bootstrap intervals.
# MAGIC 4. **Agent tools:** authenticate, list products and transactions, explain decline codes, create the case (mocked write) and hand off with a reason code; then score the scenario set against the targets above.
