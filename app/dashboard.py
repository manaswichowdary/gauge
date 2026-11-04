"""Streamlit dashboard: pick a run and see accuracy, latency and cost per setup."""
import sys
from pathlib import Path

# Streamlit puts only the app folder on the import path, so add the project folder too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import inspect
import json
import os

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import select

from gauge.config import ConfigError, load_settings
from gauge.db import Result, Run, init_db, make_engine, make_session_factory
from gauge.snapshot import load_snapshot
from gauge.stats import by_category, flaky_tasks, judge_agreement, summarize

# Newer Streamlit uses width="stretch", older versions use use_container_width=True. This works on both.
STRETCH = {"width": "stretch"} if "width" in inspect.signature(st.dataframe).parameters else {"use_container_width": True}

st.set_page_config(page_title="Gauge", page_icon="📊", layout="wide")

# Colors used by the charts. They match the theme in .streamlit/config.toml.
ACCENT = "#5eead4"   # teal
AMBER = "#fbbf24"
TEXT = "#e6edf7"
SOFT = "#c3d0e4"     # lighter than before, so axis labels and captions are easy to read
PALETTE = ["#5eead4", "#fbbf24", "#f472b6", "#60a5fa", "#a78bfa", "#fb923c", "#4ade80", "#f87171"]

# Chart text sizes and colors, applied to every chart through polish().
AXIS = dict(labelFontSize=14, titleFontSize=15, labelColor=SOFT, titleColor=TEXT,
            gridColor="#2a3a5c", domainColor="#4a5d85", tickColor="#4a5d85", titlePadding=10, labelLimit=260)
LEGEND = dict(labelFontSize=14, titleFontSize=15, labelColor=SOFT, titleColor=TEXT, symbolSize=180)


def polish(chart):
    """Bigger, brighter chart text on the dark background."""
    return chart.configure_view(strokeWidth=0).configure_axis(**AXIS).configure_legend(**LEGEND)


# Inter font and slightly larger text. Everything else comes from the Streamlit theme.
# st.html applies the CSS directly. st.markdown printed the style block as text on Streamlit Cloud.
st.html(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], [data-testid="stAppViewContainer"] { font-family: 'Inter', sans-serif; font-size: 18px; }
h1 { font-size: 2.8rem !important; font-weight: 700 !important; letter-spacing: -0.02em; }
h2, h3 { font-weight: 600 !important; margin-top: 1.8rem !important; font-size: 1.7rem !important; }
/* Captions were small and grey. Make them bigger and lighter. */
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * { font-size: 1.05rem !important; color: #c3d0e4 !important; opacity: 1 !important; line-height: 1.55 !important; }
[data-testid="stCaptionContainer"] { opacity: 1 !important; }
[data-testid="stMetricValue"] { font-size: 2.4rem; font-weight: 600; color: #5eead4; }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * { color: #c3d0e4 !important; font-size: 1.05rem !important; }
[data-testid="stMetric"] { background: #111a2e; border: 1px solid #2a3a5c; border-radius: 12px; padding: 16px 20px; }
[data-testid="stSidebar"] * { font-size: 1.05rem; }
[data-testid="stAlert"] p { font-size: 1.05rem; }
</style>
    """
)

st.title("Gauge")
st.caption("Compare agent setups on accuracy, speed and cost, and check how far the judge can be trusted.")

SNAPSHOT_PATH = Path("snapshot/run.json")
REGRESSION_PATH = Path("snapshot/regression.json")
LOAD_PATH = Path("snapshot/load.json")
AIPERF_PATH = Path("snapshot/aiperf.json")


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None

try:
    settings = None if os.getenv("GAUGE_READ_ONLY") else load_settings()
except ConfigError:
    settings = None  # no database configured, for example on Streamlit Community Cloud

if settings is None:
    # Read only mode: show the saved results from `make export`. No database and no AWS needed.
    if not SNAPSHOT_PATH.exists():
        st.error("No database is configured and snapshot/run.json was not found. Run `make export` first.")
        st.stop()
    run, results, agreement = load_snapshot(SNAPSHOT_PATH)
    is_fake = bool(run.notes and "FAKE" in run.notes)
    st.info(f"Read only demo. These are the saved results of run {run.id} ({run.started_at:%Y-%m-%d})"
            f"{'' if is_fake else ' on AWS Bedrock'}. Nothing here calls a model.")
else:
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        runs = list(s.execute(select(Run).order_by(Run.id.desc())).scalars())
        if not runs:
            st.info("No runs yet. Run `make run-dev` first.")
            st.stop()
        labels = {f"Run {r.id} ({r.mode}, {r.started_at:%Y-%m-%d %H:%M}, ${r.total_cost_usd:.3f})": r.id for r in runs}
        # Open on the latest full run, not on a seeded regression or a small dev run that came after it.
        newest_full = next((i for i, r in enumerate(runs) if r.mode == "full"), 0)
        run_id = labels[st.sidebar.selectbox("Run", list(labels), index=newest_full)]
        run = s.get(Run, run_id)
        results = list(s.execute(select(Result).where(Result.run_id == run_id)).scalars())
        agreement = judge_agreement(s)

if run.notes and "FAKE" in run.notes:
    st.warning("This run used the FAKE provider. The numbers are made up.")
if run.stopped_reason:
    st.error(f"Run stopped early: {run.stopped_reason}")

df = pd.DataFrame(summarize(results))
c1, c2, c3, c4 = st.columns(4)
c1.metric("Tasks run", len(results))
c2.metric("Setups", len(df))
c3.metric("Cost of all answers", f"${df['cost_usd'].sum():.4f}",
          help=f"This run paid ${run.total_cost_usd:.4f} for new calls. The rest of the answers were reused from the cache, "
               "and their cost is counted here too.")
c4.metric("Errors", int(df["errors"].sum()))

st.subheader("Accuracy, speed and cost by setup")
table = df.sort_values("accuracy", ascending=False).rename(columns={
    "setup": "Setup", "n": "Answers", "valid": "Counted", "accuracy": "Accuracy",
    "p50_ms": "p50 (ms)", "p95_ms": "p95 (ms)", "cost_usd": "Total cost", "cost_per_100": "Cost per 100 tasks", "errors": "Lost to errors",
})
st.dataframe(
    table, **STRETCH, hide_index=True,
    column_config={
        "Accuracy": st.column_config.ProgressColumn("Accuracy", min_value=0.0, max_value=1.0, format="percent"),
        "p50 (ms)": st.column_config.NumberColumn(format="%d"),
        "p95 (ms)": st.column_config.NumberColumn(format="%d"),
        "Total cost": st.column_config.NumberColumn(format="$%.4f"),
        "Cost per 100 tasks": st.column_config.NumberColumn(format="$%.4f"),
    },
)

st.subheader("Accuracy vs cost per 100 tasks")
st.caption("Top left is best: more accurate and cheaper. The cost axis is logarithmic so cheap setups do not bunch together.")
points = alt.Chart(df).mark_circle(size=220, opacity=0.9).encode(
    x=alt.X("cost_per_100:Q", title="Cost per 100 tasks (USD, log scale)", scale=alt.Scale(type="log")),
    y=alt.Y("accuracy:Q", title="Accuracy", scale=alt.Scale(domain=[0, 1.05]), axis=alt.Axis(format="%", values=[0, 0.2, 0.4, 0.6, 0.8, 1.0])),
    color=alt.Color("setup:N", title="Setup", scale=alt.Scale(range=PALETTE)),
    tooltip=["setup", alt.Tooltip("accuracy:Q", format=".1%"), alt.Tooltip("cost_per_100:Q", format="$.4f"),
             alt.Tooltip("p50_ms:Q", format=",.0f")],
)
labels_layer = alt.Chart(df).mark_text(align="left", dx=12, dy=-10, fontSize=14, color=SOFT).encode(
    x="cost_per_100:Q", y="accuracy:Q", text="setup:N")
st.altair_chart(polish((points + labels_layer).properties(height=380)), **STRETCH)

st.subheader("Accuracy by category")
cat = pd.DataFrame(by_category(results)).T
heat = cat.reset_index().melt(id_vars="index", var_name="category", value_name="accuracy").rename(columns={"index": "setup"})
base = alt.Chart(heat).encode(
    x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=0, orient="top")),
    y=alt.Y("setup:N", title=None, sort=list(table["Setup"])),
)
cells = base.mark_rect(cornerRadius=4).encode(
    color=alt.Color("accuracy:Q", scale=alt.Scale(domain=[0, 1], range=["#b91c1c", "#b45309", "#15803d"]), legend=None))
numbers = base.mark_text(fontSize=15, fontWeight=600, color="white").encode(
    text=alt.Text("accuracy:Q", format=".0%"))
st.altair_chart(polish((cells + numbers).properties(height=36 * len(cat) + 40)), **STRETCH)

flaky = flaky_tasks(results)
if flaky:
    st.subheader("Unstable tasks (repeats disagreed)")
    st.dataframe(pd.DataFrame(flaky, columns=["Setup", "Task"]), hide_index=True)

st.subheader("Judge trust")
if agreement["n"] == 0:
    st.write("No labels yet. Run `make label` and label about 30 outputs.")
else:
    st.caption(f"Checked against {agreement['n']} outputs I labeled by hand. Kappa is agreement corrected for chance (1 is perfect).")
    j1, j2, j3, j4 = st.columns(4)
    j1.metric("Judge agreement", f"{agreement['judge_percent_agreement']:.1f}%")
    j2.metric("Judge kappa", f"{agreement['judge_kappa']:.2f}")
    j3.metric("Rule agreement", f"{agreement['rule_percent_agreement']:.1f}%")
    j4.metric("Rule kappa", f"{agreement['rule_kappa']:.2f}")

regression = read_json(REGRESSION_PATH)
if regression:
    st.subheader("Seeded regression")
    st.caption(f"I broke {regression['setup']} on purpose with a bad system prompt and ran the normal gate. "
               f"{regression['tasks']} tasks, {regression['repeats']} repeat.")
    if regression.get("fake"):
        st.warning("This file came from the FAKE provider. The numbers are made up.")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Healthy accuracy", f"{regression['baseline_accuracy']:.0%}")
    g2.metric("Broken prompt", f"{regression['regressed_accuracy']:.0%}")
    g3.metric("Gate", "Caught it" if regression["caught"] else "Missed it")
    g4.metric("Time to detect", f"{regression['seconds_to_detect']:.0f} s")
    st.caption(f"Time to detect is the run plus the gate check, measured from the start of the broken run. "
               f"The gate limit is {regression['limit_points']:.0f} points. Cost ${regression['cost_usd']:.4f}.")

load = read_json(LOAD_PATH)
if load:
    st.subheader("Load test")
    st.caption(f"{load['setup']}, {load['requests_per_level']} requests at each concurrency level. "
               f"Retries are throttled calls that waited and tried again.")
    if load.get("fake"):
        st.warning("This file came from the FAKE provider. The numbers are made up.")
    lt = pd.DataFrame(load["levels"])
    lc1, lc2 = st.columns(2)
    with lc1:
        st.altair_chart(polish(alt.Chart(lt).mark_line(point=True, color=AMBER, strokeWidth=3).encode(
            x=alt.X("concurrency:O", title="Concurrent requests", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("p95_with_retries_ms:Q", title="p95 latency incl. retries (ms)"),
            tooltip=["concurrency", "p50_ms", "p95_ms", "p95_with_retries_ms", "retries", "errors"],
        ).properties(height=260)), **STRETCH)
    with lc2:
        st.altair_chart(polish(alt.Chart(lt).mark_bar(color=ACCENT).encode(
            x=alt.X("concurrency:O", title="Concurrent requests", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("throughput_per_s:Q", title="Answers per second"),
            tooltip=["concurrency", "throughput_per_s", "ok", "errors"],
        ).properties(height=260)), **STRETCH)
    knee = load.get("first_degraded_level")
    st.caption(f"First level that degraded: {knee}." if knee else "No level degraded in this test.")

aiperf = read_json(AIPERF_PATH)
if aiperf:
    st.subheader("AIPerf on a local model")
    st.caption(f"NVIDIA AIPerf against {aiperf['model']} served locally, {aiperf['requests_per_level']} streaming chat requests "
               f"at each concurrency level. Units are the ones AIPerf reports.")
    def stat(level, metric, key):
        value = level["metrics"].get(metric, {}).get(key)
        return None if value is None else round(value, 2)
    rows = [{"Concurrency": lv["concurrency"],
             "Time to first token avg": stat(lv, "time_to_first_token", "avg"),
             "Time to first token p99": stat(lv, "time_to_first_token", "p99"),
             "Request latency p50": stat(lv, "request_latency", "p50"),
             "Request latency p99": stat(lv, "request_latency", "p99"),
             "Output tokens per second": stat(lv, "output_token_throughput", "avg"),
             "Requests per second": stat(lv, "request_throughput", "avg"),
             "AIPerf exit code": lv["returncode"]} for lv in aiperf["levels"]]
    st.dataframe(pd.DataFrame(rows), **STRETCH, hide_index=True)

st.subheader("All results")
st.dataframe(
    pd.DataFrame([{"Setup": r.setup_name, "Task": r.task_id, "Passed": bool(r.passed), "Output": r.output_text[:200],
                   "ms": round(r.latency_ms), "Cost": r.cost_usd, "Error": r.error} for r in results]),
    **STRETCH, hide_index=True,
    column_config={"Cost": st.column_config.NumberColumn(format="$%.5f")},
)
