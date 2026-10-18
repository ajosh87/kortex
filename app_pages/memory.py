"""Memory: what the agent remembers of the open chat (pick another in the sidebar), read back from the graph."""
import streamlit as st

import agent as A
import ui
from db import traced

with st.spinner("Reading this chat's memory from the graph…"):
    m, queries = traced(A.memory, st.session_state.user, st.session_state.session)
ui.work("Memory page: this chat's turns and the Kubernetes nodes each one points at, from your memory graph", queries,
        f"{len(m['turns'])} turns, {len(m['graph']['nodes'])} linked nodes", tag="memory")

st.title("Memory")
st.markdown("Your chats are stored in FalkorDB in a graph of their own, one per name and separate from the Kubernetes "
            "graph, so nobody else's queries can see them. Each question is a `(:Turn)` on your timeline, pointing at "
            "the folders, people and KEPs its answer used; the agent recalls them by topic in later chats.")
ui.section("This chat", "forum")

if not m["turns"]:
    st.caption("Nothing remembered in this chat yet. Ask something, or open an earlier chat in the sidebar.")
    st.stop()

turns_col, graph_col = st.columns([2, 3], gap="large")
with turns_col:
    for t in m["turns"]:
        with ui.tile(f"turn-{t['n']}"):
            st.markdown(f"**Q{t['n']}.** {t['question']}")
            st.caption(f"{t['when']} · linked to {t['linked']} graph node{'s' if t['linked'] != 1 else ''}")
            with st.expander("Answer"):
                st.markdown(t["answer"])
with graph_col, ui.tile("memory-graph"):
    st.markdown("**:material/hub: What this chat touched**")
    if m["graph"]["edges"]:
        ui.graph_view(m["graph"], key=f"memory-graph-{st.session_state.session}", layout="neato", edge_labels=False,
                      caption="Each question node links to the nodes its answer used.")
    else:
        st.caption("This chat is not linked to any graph nodes.")
