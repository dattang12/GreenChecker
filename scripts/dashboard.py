"""Cost and latency dashboard over the run log.

    streamlit run scripts/dashboard.py

Reads data/runs.sqlite, which both eval.py and the API write to.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pandas as pd
import streamlit as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from store import DB_PATH  # noqa: E402

st.set_page_config(page_title="repo-triage-agent", layout="wide")


@st.cache_data(ttl=10)
def load() -> pd.DataFrame:
    if not pathlib.Path(DB_PATH).exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        df = pd.read_sql_query("SELECT * FROM runs ORDER BY created_utc DESC", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["created_utc"] = pd.to_datetime(df["created_utc"], format="mixed", utc=True)
    df["date"] = df["created_utc"].dt.date
    df["succeeded"] = df["resolved"].fillna(df["status"].eq("fixed").astype(int)).astype(int)
    return df


df = load()

st.title("repo-triage-agent")
st.caption(
    "Agent reads a GitHub issue, patches the repo in a container, and is scored "
    "against the maintainer's own test suite."
)

if df.empty:
    st.warning(f"No runs logged yet. Expected a database at {DB_PATH}.")
    st.stop()

# -- filters ----------------------------------------------------------------
with st.sidebar:
    st.header("Filters")
    sources = st.multiselect("Source", sorted(df["source"].unique()),
                             default=list(df["source"].unique()))
    tags = st.multiselect("Tag", sorted(t for t in df["tag"].unique() if t))
    view = df[df["source"].isin(sources)]
    if tags:
        view = view[view["tag"].isin(tags)]

if view.empty:
    st.warning("No runs match the current filters.")
    st.stop()

# -- headline ---------------------------------------------------------------
resolved = int(view["succeeded"].sum())
total = len(view)
spend = float(view["cost_usd"].fillna(0).sum())

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Runs", total)
c2.metric("Resolved", f"{resolved}/{total}", f"{resolved / total:.0%}")
c3.metric("Cost / resolved fix", f"${spend / resolved:.3f}" if resolved else "n/a")
c4.metric("p50 latency", f"{view['duration_seconds'].quantile(0.5):.0f}s")
c5.metric("p95 latency", f"{view['duration_seconds'].quantile(0.95):.0f}s")

st.divider()

# -- trends -----------------------------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("Resolve rate over time")
    by_day = view.groupby("date").agg(
        runs=("id", "count"), resolved=("succeeded", "sum")
    )
    by_day["rate"] = by_day["resolved"] / by_day["runs"]
    st.line_chart(by_day[["rate"]], height=260)
    st.caption("Fraction of runs whose patch satisfied the upstream test suite.")

with right:
    st.subheader("Cost per run")
    st.bar_chart(
        view.sort_values("created_utc").set_index("created_utc")[["cost_usd"]],
        height=260,
    )
    st.caption(f"Total spend across the runs shown: ${spend:.2f}")

st.divider()

# -- where the budget goes --------------------------------------------------
left2, right2 = st.columns(2)

with left2:
    st.subheader("Outcome by stop reason")
    st.dataframe(
        view.groupby("stop_reason")
        .agg(
            runs=("id", "count"),
            resolved=("succeeded", "sum"),
            mean_cost=("cost_usd", "mean"),
            mean_calls=("tool_call_count", "mean"),
        )
        .sort_values("runs", ascending=False)
        .round(3),
        width="stretch",
    )
    st.caption(
        "Runs ending in tool_budget_exhausted are the ones to attack: they cost "
        "full price whether or not they produce anything."
    )

with right2:
    st.subheader("Spend by outcome")
    st.dataframe(
        view.groupby("status")
        .agg(runs=("id", "count"), total_cost=("cost_usd", "sum"),
             mean_duration=("duration_seconds", "mean"))
        .round(3),
        width="stretch",
    )
    wasted = float(view.loc[view["succeeded"] == 0, "cost_usd"].fillna(0).sum())
    st.caption(f"Spent on runs that resolved nothing: ${wasted:.2f} "
               f"({wasted / spend:.0%} of total)" if spend else "")

st.divider()

# -- recent runs ------------------------------------------------------------
st.subheader("Recent runs")
table = view.head(50).copy()
table["files"] = table["files_changed"].apply(
    lambda s: ", ".join(json.loads(s)) if s else ""
)
st.dataframe(
    table[[
        "created_utc", "source", "tag", "issue_id", "status", "succeeded",
        "judge_score", "tool_call_count", "duration_seconds", "cost_usd",
        "stop_reason", "files", "trace_url",
    ]],
    width="stretch",
    hide_index=True,
    column_config={
        "trace_url": st.column_config.LinkColumn("trace", display_text="open"),
        "cost_usd": st.column_config.NumberColumn("cost", format="$%.4f"),
        "duration_seconds": st.column_config.NumberColumn("sec", format="%.0f"),
        "succeeded": st.column_config.CheckboxColumn("resolved"),
    },
)
