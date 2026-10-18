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


@st.cache_data(ttl=300, show_spinner="Opening it…")
def neighbors(kind, key):
    return traced(Q.neighbors, kind, key)


BROWSE = "data-sig-map"  # the graph's key; its last clicked node is in session state under BROWSE + "-pick"
TITLED = {"KEP", "Doc", "PR"}  # kinds whose title is worth showing under the name


def back():
    trail = st.session_state["browse-trail"]
    if trail:
        trail.pop()
    st.session_state[f"{BROWSE}-pick"] = st.session_state["browse-last"] = trail[-1] if trail else None


def start_over():
    st.session_state["browse-trail"] = []
    st.session_state[f"{BROWSE}-pick"] = st.session_state["browse-last"] = None


(graph, sigs, links), map_queries = sig_map()
ui.work("Data page: the whole graph grouped by SIG", map_queries, f"{len(sigs)} SIGs, {len(links)} collaborations")

# Browsing: every click opens that node (its neighbours join the picture); clicking an earlier node on the trail
# goes back to it. The trail is the list of opened nodes, in order.
trail = st.session_state.setdefault("browse-trail", [])
picked = st.session_state.get(f"{BROWSE}-pick")
if picked and picked != st.session_state.get("browse-last"):
    st.session_state["browse-last"] = picked
    if picked in trail:
        del trail[trail.index(picked) + 1:]
    else:
        trail.append(picked)

nodes = {n["id"]: n for n in graph["nodes"]}
edges = {(e["src"], e["rel"], e["dst"]): e for e in graph["edges"]}
opened_counts = []
for opened in trail:
    kind, key = opened.split(":", 1)
    rows, queries = neighbors(kind, key)
    ui.work(f"Data page: opening {kind} {key}", queries, f"{len(rows)} neighbours shown")
    if opened not in nodes:
        nodes[opened] = Q.node(kind, key)
    for r in rows:
        n = Q.node(r["kind"], r["key"], title=r["title"] if r["kind"] in TITLED else None)
        nodes.setdefault(n["id"], n)
        a, b = (opened, n["id"]) if r["out"] else (n["id"], opened)
        rel = r["rel"].lower().replace("_", " ")
        edges.setdefault((a, rel, b), {"src": a, "rel": rel, "dst": b})
    opened_counts.append(sorted({(r["rel"].lower().replace("_", " "), min(r["total"], 6), r["total"]) for r in rows}))
graph = {"nodes": list(nodes.values()), "edges": list(edges.values()), "total_nodes": len(nodes), "shown_nodes": len(nodes)}

ui.section("The whole graph, by SIG", "bubble_chart")
with ui.tile("data-sig-map"):
    st.markdown("Each SIG is a cluster: the folders labelled for it, the KEPs it owns and the people who lead it. A "
                "line from one SIG to another counts the KEPs the first owns and the second takes part in (4 or more). "
                "**Click any node to open it**: its neighbours join the picture (up to 6 of each kind of link), and "
                "its details appear below. Keep clicking to go deeper.")
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.button("Back", icon=":material/arrow_back:", on_click=back, disabled=not trail, key="browse-back")
        st.button("Start over", icon=":material/restart_alt:", on_click=start_over, disabled=not trail, key="browse-reset")
        st.caption(":material/route: " + (" › ".join(Q.node(*i.split(":", 1))["label"] for i in trail)
                                           if trail else "Nothing opened yet: click a SIG."))
    if trail:
        shown = ", ".join(f"{rel} {k} of {t}" if t > k else f"{rel} {t}" for rel, k, t in opened_counts[-1])
        st.caption(f"{Q.node(*trail[-1].split(':', 1))['label']}: {shown}.")
    alone = sorted({r["sig"] for r in sigs} - {x for r in links for x in (r["owner"], r["partner"])})
    ui.graph_view(graph, key=BROWSE, caption=f"All {len(sigs)} SIGs; lines join SIGs sharing 4 or more KEPs, and each "
                                             f"SIG to its strongest partner; {len(graph['nodes'])} nodes shown."
                                             + (f" No shared KEPs or leads: {', '.join(alone)}." if alone else ""))

ui.section("Source", "commit")
with st.container(horizontal=True, gap="small"):
    for c in s["commits"]:
        st.link_button(f"{c['repo']} @ {c['commit']}", c["url"], icon=":material/commit:")
st.caption(f"Pull requests merged since {s['source'].get('prs_since', '?')}.")
