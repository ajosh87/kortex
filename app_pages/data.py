"""Data: what the graph holds, its schema as found in the data, and where it came from."""
import streamlit as st

import queries as Q
import ui
from db import traced


@st.cache_data(ttl=300, show_spinner="Reading the graph's statistics…")
def stats():
    return traced(Q.stats)


s, queries = stats()
ui.work("Data page: load statistics recorded by ingest.py in the (:Meta) node", queries,
        f"{s['nodes']:,} nodes, {s['relationships']:,} relationships", tag="page")
counts = s["counts"]

st.title("What the graph holds")
st.markdown(f"Loaded {s['source'].get('loaded_at', '?')} from public Kubernetes repositories. People are stored only "
            "as GitHub handles; names, emails and employers are not loaded.")
ui.section("Counts", "tag")

with ui.tile("data-counts", fill="accent"):
    with st.container(horizontal=True, gap="large"):
        st.metric("Nodes", f"{s['nodes']:,}")
        st.metric("Relationships", f"{s['relationships']:,}")
    st.space("small")
    with st.container(horizontal=True, gap="large", key="data-kinds"):  # every node kind, largest first; wraps when narrow
        for c in counts:
            st.metric(c["kind_label"], f"{c['n']:,}", width="content")

ui.section("Schema, as found in the data", "schema")
with ui.tile("data-schema"):
    ui.graph_view(s["schema"], caption="Every node kind and relationship type in the graph, with how many relationships of each.")

@st.cache_data(ttl=300, show_spinner="Grouping the whole graph by SIG…")
def sig_map():
    return traced(Q.sig_map)


@st.cache_data(ttl=300, show_spinner="Opening the cluster…")
def sig_inside(sig):
    return traced(Q.sig_inside, sig)


(graph, sigs, links), map_queries = sig_map()
ui.work("Data page: the whole graph grouped by SIG", map_queries, f"{len(sigs)} SIGs, {len(links)} collaborations")
picked = st.session_state.get("data-sig-map-pick") or ""  # the node clicked last (set before this run)
if picked.startswith("SIG:"):
    st.session_state["sig-open"] = picked.split(":", 1)[1]  # clicking a SIG opens its cluster; other nodes keep it open
opened = st.session_state.get("sig-open")
if opened:
    members, inside_queries = sig_inside(opened)
    ui.work(f"Data page: opening the {opened} cluster", inside_queries, f"{len(members)} members shown")
    known = {n["id"] for n in graph["nodes"]}
    extra = {n["id"]: n for n, _ in members if n["id"] not in known}  # once each (a chair can also be a tech lead)
    graph = {**graph, "nodes": graph["nodes"] + list(extra.values()),
             "edges": graph["edges"] + [{"src": n["id"], "rel": rel, "dst": f"SIG:{opened}"} for n, rel in members]}
    graph |= {"total_nodes": len(graph["nodes"]), "shown_nodes": len(graph["nodes"])}
ui.section("The whole graph, by SIG", "bubble_chart")
with ui.tile("data-sig-map"):
    st.markdown("Each SIG is a cluster: the folders labelled for it, the KEPs it owns and the people who lead it. "
                "**Click a SIG to open it**: its leads, subprojects, latest KEPs, folders and annual reports appear "
                "around it, and clicking any of them shows its details. A line from one SIG to another counts the "
                "KEPs the first owns and the second takes part in (4 or more).")
    if opened:
        row = next((r for r in sigs if r["sig"] == opened), {})
        st.caption(f":material/open_in_full: {opened} is open: up to 6 of its {row.get('keps', '?')} KEPs, "
                   f"{row.get('folders', '?')} folders and {row.get('leads', '?')} leads are shown.")
    ui.graph_view(graph, key="data-sig-map", caption=f"All {len(sigs)} SIGs; {len(links)} SIG pairs share 4 or more KEPs.")

ui.section("Source", "commit")
with st.container(horizontal=True, gap="small"):
    for c in s["commits"]:
        st.link_button(f"{c['repo']} @ {c['commit']}", c["url"], icon=":material/commit:")
st.caption(f"Pull requests merged since {s['source'].get('prs_since', '?')}.")
