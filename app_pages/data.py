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

with ui.tile("data-counts", fill="yellow"):
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

ui.section("Source", "commit")
with st.container(horizontal=True, gap="small"):
    for c in s["commits"]:
        st.link_button(f"{c['repo']} @ {c['commit']}", c["url"], icon=":material/commit:")
st.caption(f"Pull requests merged since {s['source'].get('prs_since', '?')}.")
