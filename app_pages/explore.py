"""Explore: the agent's graph walks, run directly on any folder, KEP, person or SIG. No LLM involved."""
import streamlit as st

import tools as T
import ui
from db import traced


@st.cache_data(ttl=600, max_entries=200, show_spinner="Searching the graph…")
def search(text):
    return traced(T.explore_search, text)


@st.cache_data(ttl=600, max_entries=200, show_spinner="Walking the graph…")
def run(tool, kind, key):
    return T.explore(tool, kind, key)


st.title("Explore")
st.markdown("See exactly what the agent sees. Pick anything in the Kubernetes graph, then one of the walks the agent "
            "uses to answer questions: the same tools and the same Cypher, without a model or an API key.")
ui.section("Pick what to explore", "tune")

with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
    text = st.text_input("1 · Find a folder, KEP, person or SIG", value="eviction", type="search", live="300ms",
                         placeholder="e.g. pkg/scheduler, sidecar, pohly, sig-node")
    matches, search_queries = search(text.strip()) if len(text.strip()) >= 2 else ([], [])
    choice = st.selectbox("Match", matches, index=0 if matches else None, key="explore-match",
                          format_func=lambda n: f"{n['kind_label']} · {n['label']}" + (f" · {n['title']}" if n.get("title") else ""),
                          placeholder="No matches" if text.strip() else "Type to search")
if search_queries:
    ui.work(f"search for “{text.strip()}”", search_queries, f"{len(matches)} matches")

actions = T.actions_for(choice["kind"]) if choice else []
picked = st.segmented_control("2 · Pick a question", [a["tool"] for a in actions],
                              default=actions[0]["tool"] if actions else None, required=bool(actions),
                              key=f"view-{choice['kind'] if choice else 'none'}",
                              format_func=lambda t: next(f"{a['icon']} {a['label']}" for a in actions if a["tool"] == t))
if not choice or not picked:
    st.caption("Type at least two characters and pick a match.")
    st.stop()
st.caption(f":material/conversion_path: **The walk:** {next(a['how'] for a in actions if a['tool'] == picked)}")
ui.section("Result", "task_alt")

r = run(picked, choice["kind"], choice["key"])
ui.work(f"▶ {ui.call(r['tool'], r['args'])} (Explore, no model)", r["queries"], r["result"], r["ms"])
with ui.tile("explore-summary", fill="yellow"):
    st.markdown(f"#### {r['title']}")
    st.markdown(r["result"])
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.badge(f"Graph {r['graph_ms']:g} ms", icon=":material/bolt:", color="green", help="Time spent in FalkorDB")
        st.badge(f"{len(r['queries'])} Cypher quer{'y' if len(r['queries']) == 1 else 'ies'} · in the console",
                 icon=":material/terminal:", color="gray")
        st.badge(f"{r['found']} rows found", icon=":material/table_rows:", color="gray")
        if choice.get("url"):
            st.link_button("Open source", choice["url"], icon=":material/open_in_new:", type="tertiary")

if r["graph"]["edges"]:
    ui.section("Graph", "hub")
    with ui.tile("explore-graph"):
        ui.graph_view(r["graph"], key=f"explore-graph-{picked}-{choice['id']}")

if r["tables"]:
    ui.section("What was found", "table_rows")
    ui.tables(r["tables"], key=f"explore-{picked}-{choice['id']}")
