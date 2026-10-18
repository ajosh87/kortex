"""Shared rendering for every page. These functions only turn pipeline output (step records, graphs,
done events) into Streamlit elements: no data is computed or reshaped here.

Layout is a bento grid of tiles (always), kept to a readable width with room between them. On top of it an
optional neo-brutalist style (switch in the top bar): thick ink borders, hard shadows, square corners, flat colour fills
and bold type. Only tiles whose content we control get colour fills; tiles with graphs, tables or code keep the
theme background.

Like a desktop chat app, the page has three parts: chats on the left (sidebar), the conversation in the middle, and a
console panel on the right: a log of what ran in the background (model calls, tool calls, and every Cypher query
with its parameters and response), written as it happens. The panel is open by default (on narrow screens it
starts closed) and is shown or hidden from the top bar."""
import json
import re
from datetime import datetime

import streamlit as st

import tools as T
import ui_graph

# ---------------- palette (presentation only) ----------------

# Node colours by kind. Light fills with dark text read on light and dark themes.
SOFT = {"Person": "#93c5fd", "Dir": "#fcd34d", "KEP": "#c4b5fd", "SIG": "#86efac", "Team": "#5eead4",
        "FeatureGate": "#fda4af", "PR": "#cbd5e1", "Component": "#fdba74", "Subproject": "#a5b4fc", "Turn": "#4f46e5"}
# Neo-brutalist: flat, saturated.
BOLD = {"Person": "#7DE3F0", "Dir": "#FFDE59", "KEP": "#C3A6FF", "SIG": "#B4F461", "Team": "#5EEAD4",
        "FeatureGate": "#FF9ACD", "PR": "#E5E7EB", "Component": "#FFB25B", "Subproject": "#9DC4FF", "Turn": "#111111"}
FONT = {"Turn": "#ffffff"}  # dark fills get light text

# Tile fills (neo-brutalist mode only).
FILL = {"yellow": "#FFDE59", "pink": "#FF9ACD", "cyan": "#7DE3F0", "lime": "#B4F461", "violet": "#C3A6FF", "orange": "#FFB25B"}

# Icon per tool in the step timeline.
ICON = {"search_graph": "search", "who_owns": "approval", "design_constraints": "gavel", "blast_radius": "crisis_alert",
        "find_experts": "groups", "kep_details": "description", "decision_trail": "account_tree",
        "single_points_of_failure": "person_alert", "person_profile": "person", "how_connected": "conversion_path",
        "run_cypher": "terminal", "write_review_plan": "edit_document", "recall_memory": "psychology",
        "recent_changes": "update", "critical_code": "local_fire_department", "decision_to_approvers": "route",
        "best_reviewers": "rate_review"}

PHASE = {"thinking": "Model is choosing the next step", "tools": "Querying the graph", "writing": "Writing the answer"}


def brutal():
    return st.session_state.get("brutal", True)


def ink():
    """Border and shadow colour: near-black on light themes, warm off-white on dark ones."""
    return "#F2EFE6" if st.context.theme.type == "dark" else "#111111"


def tile(key, fill=None, height="content", width="stretch"):
    """A bento tile: a bordered container. Neo-brutalist mode adds a thick border, a hard shadow and the fill."""
    return st.container(border=True, key=f"tile-{fill or 'plain'}-{key}", height=height, width=width)


def section(label, icon):
    """A section heading with a rule above it, so the parts of a page read as separate blocks."""
    slug = "".join(c if c.isalnum() else "-" for c in label.lower())
    with st.container(key=f"section-{slug}"):
        st.markdown(f":material/{icon}: {label}")


def paper():
    """The page background the graph sits on (edge labels get a matching backdrop)."""
    if st.context.theme.type == "dark":
        return "#0C0E16"
    return "#FFFBEF" if brutal() else "#FFFFFF"


# ---------------- stylesheets ----------------

BASE_CSS = """<style>
/* top navigation: centred and a little larger */
[data-testid="stTopNav"] { flex: 1 1 auto !important; justify-content: center !important; }
[data-testid="stTopNavLink"] { font-size: 1.05rem !important; padding: 0.45rem 0.95rem !important; }
[data-testid="stTopNavLink"] [data-testid="stIconMaterial"] { font-size: 1.25rem !important; }
/* chat: the person on the right, the agent on the left */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] { text-align: left; }
/* a disabled chat input (no API key yet) measures its height once, before its row has its width, and never again */
[data-testid="stChatInput"] textarea:disabled { height: auto !important; max-height: 2.6rem !important; overflow: hidden; }
/* wide tables in answers scroll inside their own box instead of widening the page */
[data-testid="stMarkdownContainer"] table { display: block; max-width: 100%; overflow-x: auto; }
/* room to breathe: a readable column in the middle of wide screens, generous margins, roomier tiles */
[data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] { max-width: 1120px; margin: 0 auto; }
[data-testid="stMainBlockContainer"] { padding-top: 3rem; padding-bottom: 9rem; }  /* clear of the chat bar */
[data-testid="stBottomBlockContainer"] { padding-top: 0.75rem; padding-bottom: 0.75rem; }
div[class*="st-key-tile-"] { padding: 1.4rem 1.5rem !important; }
/* the per-kind counts on the Data page read one step below the totals */
.st-key-data-kinds [data-testid="stMetricValue"] { font-size: 1.6rem !important; }
/* columns wrap onto their own rows when there is no room for them side by side */
[data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
[data-testid="stColumn"] { min-width: min(100%, 17rem); }
/* sections: a rule and a small heading above each part of a page */
div[class*="st-key-section-"] { border-top: 1px solid rgba(120,120,150,0.35); padding-top: 0.85rem; margin-top: 1.6rem; }
div[class*="st-key-section-"] p { font-size: 0.78rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.8; }
/* starter cards: a wrapping grid, about three across on wide screens and one on phones */
.st-key-starters > div { flex: 1 1 15rem !important; min-width: 15rem; }
/* chats in the sidebar: one line each, left aligned */
.st-key-chats [data-testid="stButton"] button { justify-content: flex-start; text-align: left; min-height: 2.1rem; }
.st-key-chats [data-testid="stButton"] button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-key-chats [data-testid="stButton"] button > div { min-width: 0; }
/* console panel on the right, under the top bar. "auto" (nobody has toggled it yet) is open on wide screens and
   closed on narrow ones; "open" and "closed" are the visitor's choice. The page keeps clear of an open panel. */
:root { --console-w: clamp(320px, 30vw, 440px); }
[data-testid="stLayoutWrapper"]:has(> .st-key-console-open), [data-testid="stLayoutWrapper"]:has(> .st-key-console-auto),
[data-testid="stLayoutWrapper"]:has(> .st-key-console-closed), [data-testid="stLayoutWrapper"]:has(> .st-key-topbar) {
  position: absolute !important; height: 0 !important; margin: 0 !important; }  /* out of the page flow */
.st-key-console-open, .st-key-console-auto { position: fixed !important; top: 3.5rem; right: 0; bottom: 0;
  width: var(--console-w); z-index: 100; padding: 1rem 1rem 0.75rem; display: flex; flex-direction: column; }
.st-key-console-closed { display: none !important; }
[data-testid="stMain"]:has(.st-key-console-open), [data-testid="stMain"]:has(.st-key-console-auto) {
  padding-right: var(--console-w); box-sizing: border-box; }  /* padding, not margin: the page keeps its width */
body:has(.st-key-console-auto) .st-key-topbar .st-key-console-show { display: none; }  /* auto, wide: offer to hide */
@media (max-width: 1100px) {
  .st-key-console-auto { display: none !important; }
  [data-testid="stMain"]:has(.st-key-console-auto) { padding-right: 0; }
  body:has(.st-key-console-auto) .st-key-topbar .st-key-console-show { display: block; }  /* auto, narrow: offer to show */
  body:has(.st-key-console-auto) .st-key-topbar .st-key-console-hide { display: none; }
  .st-key-console-open { width: min(92vw, 440px); box-shadow: -12px 0 32px rgba(0,0,0,0.25); }  /* over the page */
  [data-testid="stMain"]:has(.st-key-console-open) { padding-right: 0; }
  .st-key-topbar [data-testid="stWidgetLabel"] p { display: none; }  /* switch only; its tooltip says what it is */
}
/* top bar controls: the style switch and the console button, left of Streamlit's own toolbar */
.st-key-topbar { position: fixed !important; top: 0.55rem; z-index: 999991; width: auto !important; }
.st-key-topbar [data-testid="stButton"] button { box-shadow: none !important; min-height: 2.1rem; }
/* console: a compact monospace log that fills the panel and scrolls */
.st-key-console-log, .st-key-console-log p, .st-key-console-log summary { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 0.78rem; }
[data-testid="stLayoutWrapper"]:has(> .st-key-console-log) { flex: 1 1 0 !important; min-height: 0; height: auto !important; }
div.st-key-console-log.st-key-console-log { height: 100% !important; max-height: none !important; overflow-y: auto; }
.st-key-console-log > * { flex-shrink: 0 !important; }  /* lines keep their height; the log scrolls instead */
.st-key-console-log p { margin: 0; line-height: 1.45; }
.st-key-console-log [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }  /* undo Streamlit's -1rem (p has no margin here) */
.st-key-console-log strong { display: inline-block; min-width: 4.6em; }
.st-key-console-log [data-testid="stExpander"] details { border: 0 !important; box-shadow: none !important; }
.st-key-console-log [data-testid="stExpander"] summary { padding: 0.05rem 0; min-height: 0; }
.st-key-console-log [data-testid="stExpanderDetails"] { padding: 0.25rem 0 0.5rem 1.4rem; }
</style>"""


# Modernist tile tints (neo-brutalist style off): the same six colours, washed out, on the logo's indigo.
TINT = {"yellow": "#FFF7D1", "pink": "#FDE8F2", "cyan": "#E2F5FA", "lime": "#ECF8DA", "violet": "#ECE9FE", "orange": "#FFEEDB"}


def modern_css():
    """The modernist look: soft tinted tiles with rounded corners and a light shadow, the logo's indigo for
    the active page and controls, its yellow as the highlight. Works on the light and the dark theme."""
    dark = st.context.theme.type == "dark"
    tints = "\n".join(f'div[class*="st-key-tile-{name}-"] {{ background: {"rgba(99,102,241,0.10)" if dark else hex_} !important; }}'
                      for name, hex_ in TINT.items())
    page = "" if dark else ('[data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stBottom"] > div '
                            '{ background: #F4F5FA !important; }')
    surface, line = ("#141826", "#2C3247") if dark else ("#FFFFFF", "#DCDEEC")
    accent = "#A5B4FC" if dark else "#4F46E5"
    return f"""<style>
{page}
h1 {{ font-weight: 700 !important; letter-spacing: -0.025em; }}
h1::after {{ content: ""; display: block; width: 2.2rem; height: 4px; margin-top: 0.55rem; border-radius: 2px;
  background: linear-gradient(90deg, #4F46E5, #FFDE59); }}

/* tiles: white cards (tinted when they carry a colour), rounded, a soft indigo-tinged shadow */
div[class*="st-key-tile-"] {{ background: {surface}; border: 1px solid {line} !important; border-radius: 16px !important;
  box-shadow: 0 1px 2px rgba(17,24,39,0.04), 0 10px 28px rgba(79,70,229,0.07) !important; }}
{tints}
div[class*="st-key-tile-"]:not([class*="st-key-tile-plain-"]) {{ border-color: transparent !important; }}

/* navigation: the open page is an indigo pill */
[data-testid="stTopNavLink"] {{ border-radius: 999px !important; font-weight: 500; }}
[data-testid="stTopNavLink"][aria-current="page"] {{ background: #4F46E5 !important; }}
[data-testid="stTopNavLink"][aria-current="page"] * {{ color: #FFFFFF !important; }}

/* controls: rounded, indigo on hover */
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a {{
  border-radius: 10px !important; font-weight: 500; transition: border-color .12s ease, color .12s ease, box-shadow .12s ease; }}
[data-testid="stButton"] button[kind="secondary"]:hover, [data-testid="stLinkButton"] a:hover {{
  border-color: #4F46E5 !important; color: #4F46E5 !important; box-shadow: 0 2px 10px rgba(79,70,229,0.15); }}
[data-testid="stChatInput"] > div {{ border-radius: 14px !important; box-shadow: 0 4px 18px rgba(79,70,229,0.08); }}

/* sections: an indigo heading over a fine rule */
div[class*="st-key-section-"] {{ border-top: 1px solid {line}; }}
div[class*="st-key-section-"] p {{ color: {accent} !important; opacity: 1; }}

/* chat: every message is its own card; yours is tinted indigo */
[data-testid="stChatMessage"] {{ border-radius: 16px !important; background: {surface}; border: 1px solid {line};
  box-shadow: 0 1px 2px rgba(17,24,39,0.04); padding: 1rem 1.1rem !important; margin-bottom: 0.9rem; }}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {{ background: {"rgba(99,102,241,0.14)" if dark else "#EEF0FF"} !important; }}

/* sidebar chats: the open one gets an indigo marker */
.st-key-chats [data-testid="stButton"] button[kind="secondary"] {{ border-color: transparent !important;
  background: {"rgba(99,102,241,0.16)" if dark else "#EEF0FF"} !important; box-shadow: inset 3px 0 0 #4F46E5; }}
.st-key-console-open, .st-key-console-auto {{ background: {surface}; border-left: 1px solid {line};
  box-shadow: -8px 0 24px rgba(79,70,229,0.06); }}
</style>"""


def apply_style():
    """Always: the base stylesheet (navigation, chat sides, spacing, console). Then one of two looks, switched in the
    sidebar: neo-brutalist, or modernist. Both target our own keys, Streamlit's data-testid attributes and the badge
    class, never generated class names."""
    st.html(BASE_CSS)
    if not brutal():
        st.html(modern_css())
        return
    k = ink()
    light = st.context.theme.type != "dark"
    fills = "\n".join(f'div[class*="st-key-tile-{name}-"] {{ background: {hex_} !important; }}' for name, hex_ in FILL.items())
    paper = ('[data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stBottom"] > div '
             '{ background: #FFFBEF !important; }\n'
             '[data-testid="stSidebar"] { background: #FFF2C7 !important; }') if light else ""
    st.html(f"""<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap');
{paper}
h1, h2, h3, h4, h5, .stMarkdownBadge, [data-testid="stTopNavLink"], [data-testid="stMetricValue"],
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a {{
  font-family: 'Space Grotesk', 'Inter', sans-serif !important; }}
h1 {{ font-weight: 700 !important; letter-spacing: -0.03em; }}

/* bento tiles */
div[class*="st-key-tile-"] {{ border: 3px solid {k} !important; border-radius: 0 !important;
  box-shadow: 6px 6px 0 {k} !important; }}
{fills}
div[class*="st-key-tile-"]:not([class*="st-key-tile-plain-"]) * {{ color: #111111 !important; }}
div[class*="st-key-tile-"]:not([class*="st-key-tile-plain-"]) button {{ background: #FFFFFF !important; }}
div[class*="st-key-tile-"]:not([class*="st-key-tile-plain-"]) a:not([data-testid^="stBaseLinkButton"]) {{
  text-decoration: underline !important; text-underline-offset: 2px; }}

/* controls: square, outlined, hard shadow that collapses when pressed */
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a {{
  border: 2px solid {k} !important; border-radius: 0 !important; box-shadow: 3px 3px 0 {k} !important;
  font-weight: 600 !important; transition: transform .06s ease, box-shadow .06s ease; }}
[data-testid="stButton"] button:hover, [data-testid="stDownloadButton"] button:hover, [data-testid="stLinkButton"] a:hover {{
  transform: translate(-1px, -1px); box-shadow: 4px 4px 0 {k} !important; }}
[data-testid="stButton"] button:active, [data-testid="stDownloadButton"] button:active, [data-testid="stLinkButton"] a:active {{
  transform: translate(3px, 3px); box-shadow: 0 0 0 {k} !important; }}
[data-testid="stTextInputRootElement"], [data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stChatInput"] > div {{ border: 2px solid {k} !important; border-radius: 0 !important; box-shadow: 3px 3px 0 {k}; }}
[data-testid="stButtonGroup"] button {{ border: 2px solid {k} !important; border-radius: 0 !important; font-weight: 600; }}

/* chat, badges, navigation, data */
[data-testid="stChatMessage"] {{ border: 2px solid {k}; border-radius: 0 !important; box-shadow: 4px 4px 0 {k}; }}
.stMarkdownBadge {{ border: 2px solid {k} !important; border-radius: 0 !important; font-weight: 600 !important; }}
[data-testid="stTopNavLink"] {{ border-radius: 0 !important; font-weight: 600; }}
[data-testid="stTopNavLink"][aria-current="page"] {{ background: #FFDE59 !important; border: 2px solid {k}; box-shadow: 3px 3px 0 {k}; }}
[data-testid="stTopNavLink"][aria-current="page"] * {{ color: #111111 !important; }}
[data-testid="stDataFrame"] {{ border: 2px solid {k}; }}
[data-testid="stCode"] pre {{ border: 2px solid {k} !important; border-radius: 0 !important; }}
[data-testid="stSidebar"] {{ border-right: 3px solid {k} !important; }}
[data-testid="stSidebar"] [data-testid="stExpander"] details,
div[class*="st-key-tile-"] [data-testid="stExpander"] details,
[class*="st-key-exp-"] details {{ border: 2px solid {k} !important; border-radius: 0 !important; }}
[class*="st-key-exp-"] details {{ box-shadow: 4px 4px 0 {k}; }}
.st-key-chats [data-testid="stButton"] button {{ box-shadow: none !important; transform: none !important; }}
.st-key-chats [data-testid="stButton"] button[kind="tertiary"] {{ border-color: transparent !important; }}
.st-key-console-open, .st-key-console-auto {{ background: {"#FFFBEF" if light else "#0C0E16"}; border-left: 3px solid {k}; }}
div[class*="st-key-section-"] {{ border-top: 3px solid {k}; }}
div[class*="st-key-section-"] p {{ opacity: 1; font-family: 'Space Grotesk', 'Inter', sans-serif !important; }}
</style>""")


# ---------------- graphs ----------------

def _q(s):
    return json.dumps(str(s), ensure_ascii=False)


def _look():
    """Graphviz node and edge attributes for the active style."""
    if brutal():
        k = ink()
        return (BOLD, 'shape=box style="filled" fontname="Helvetica-Bold" fontsize=10 color="#111111" penwidth=2 margin="0.14,0.07"',
                f'fontname="Helvetica-Bold" fontsize=8 color="{k}" fontcolor="{k}" penwidth=1.6 arrowsize=0.8')
    return (SOFT, 'shape=box style="rounded,filled" fontname="Helvetica" fontsize=10 color="#64748b" penwidth=0.6',
            'fontname="Helvetica" fontsize=8 color="#94a3b8" fontcolor="#94a3b8" arrowsize=0.6')


def dot(graph, rankdir="LR", layout="dot", edge_labels=True):
    colors, node_attrs, edge_attrs = _look()
    lines = [f'digraph G {{ layout={layout}; rankdir={rankdir}; bgcolor="transparent"; pad=0.2; nodesep=0.3; ranksep=0.6;',
             'overlap=false; sep="+10"; splines=true;' if layout != "dot" else "",
             f'node [{node_attrs} fontcolor="#0f172a"];', f'edge [{edge_attrs}];']
    for n in graph["nodes"]:
        title = n.get("title")
        label = n["label"] + (f"\n{title[:30]}{'…' if len(title) > 30 else ''}" if title else "")
        attrs = f'label={_q(label)} fillcolor="{colors.get(n["kind"], "#e2e8f0")}" fontcolor="{FONT.get(n["kind"], "#0f172a")}"'
        attrs += f' tooltip={_q(n["kind_label"] + ": " + (title or n["key"]))}'
        if n.get("url"):
            attrs += f' URL={_q(n["url"])} target="_blank"'
        lines.append(f'  {_q(n["id"])} [{attrs}];')
    lines += [f'  {_q(e["src"])} -> {_q(e["dst"])}' + (f' [label={_q(e["rel"])}];' if edge_labels else ";") for e in graph["edges"]]
    return "\n".join(lines + ["}"])


def legend(graph):
    colors, node_attrs, _ = _look()
    kinds = {n["kind"]: n["kind_label"] for n in graph["nodes"]}
    items = " ".join(f'"{k}" [label={_q(v)} fillcolor="{colors.get(k, "#e2e8f0")}" fontcolor="{FONT.get(k, "#0f172a")}"];'
                     for k, v in kinds.items())
    rows = [list(kinds)[i:i + 4] for i in range(0, len(kinds), 4)]  # wrap into rows of four so narrow tiles fit
    ranks = " ".join("{rank=same; " + " ".join(f'"{k}"' for k in row) + "}" for row in rows)
    stack = " ".join(f'"{a[0]}" -> "{b[0]}" [style=invis];' for a, b in zip(rows, rows[1:]))
    return (f'digraph L {{ rankdir=TB; bgcolor="transparent"; pad=0; nodesep=0.12; ranksep=0.08; '
            f'node [{node_attrs} fontsize=9 height=0.22]; {items} {ranks} {stack} }}')


def graph_view(graph, key=None, caption=None, rankdir="LR", layout="dot", edge_labels=True):
    """Legend + graph + what is shown. With a `key` the graph is interactive (zoom, pan, drag; click a node for
    its details below); without one it is a static Graphviz drawing whose nodes link to their source pages."""
    if not graph["edges"]:
        return
    st.graphviz_chart(legend(graph))
    more = f"Showing {graph['shown_nodes']} of {graph['total_nodes']} nodes. " if graph["total_nodes"] > graph["shown_nodes"] else ""
    if key is None:
        st.graphviz_chart(dot(graph, rankdir, layout, edge_labels), width="content", alt="Graph of the nodes and relationships used")
        st.caption(more + (caption or "Click a node to open its source page."))
        return
    colors = BOLD if brutal() else SOFT
    picked = ui_graph.show(graph, key, colors=colors, fonts=FONT, brutal=brutal(), ink=ink(), background=paper(),
                           layout_name="radial" if layout == "neato" else "tree", edge_labels=edge_labels)
    if more or caption:
        st.caption(more + (caption or ""))
    chosen = next((n for n in graph["nodes"] if n["id"] == picked), None)
    if chosen:
        node_panel(chosen, key)


@st.cache_data(ttl=600, max_entries=500, show_spinner="Reading this node's details from the graph…")
def _card(kind, key, user):
    return T.node_card(kind, key, user)


def node_panel(n, key):
    """The clicked node's profile, from the pipeline, shown in the app."""
    card = _card(n["kind"], n["key"], st.session_state.get("user"))
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.badge(n["kind_label"], color="primary")
        st.markdown(f"**{n['label']}**" + (f" · {n['title']}" if n.get("title") else ""))
        if n.get("url"):
            st.link_button("Open source", n["url"], icon=":material/open_in_new:", type="tertiary")
    st.markdown(card["result"])
    for t in card["tables"]:  # a profile reads better as a wrapping list than as a table with clipped cells
        st.markdown("\n".join(f"- **{r['About']}:** {r['Value']}" for r in t["rows"]))
    n_q = len(card["queries"])
    st.caption(f"{n_q} Cypher quer{'y' if n_q == 1 else 'ies'} · {card['graph_ms']:g} ms · in the console")
    work(f"details for {n['kind_label']} {n['label']} (clicked in the graph)", card["queries"], card["result"], card["ms"])


def graph_title(graph):
    return f"Graph behind this answer · {graph['shown_nodes']} nodes, {len(graph['edges'])} relationships"


# ---------------- step records ----------------

def _link_width(rows, col, pattern):
    """Pixel width for a link column, from its longest shown text (the full URL would make it far too wide)."""
    shown = [m.group(1) for r in rows if (m := re.match(pattern, str(r.get(col) or "")))]
    return int(min(380, 28 + 7.6 * max(map(len, shown), default=8)))


def tables(items, key):
    for i, t in enumerate(items):
        st.caption(f"**{t['title']}** · {len(t['rows'])}")
        config = {col: st.column_config.LinkColumn(col, display_text=pattern, width=_link_width(t["rows"], col, pattern))
                  for col, pattern in t["links"].items()}
        st.dataframe(t["rows"], hide_index=True, column_config=config, key=f"{key}-t{i}",
                     height=min(36 + 35 * len(t["rows"]), 320))


def step_body(ev, key):
    st.markdown(ev["result"])
    if ev.get("file"):
        st.download_button(f"Download {ev['file']['name']}", ev["file"]["content"], file_name=ev["file"]["name"],
                           mime="text/markdown", icon=":material/download:", key=f"{key}-dl", on_click="ignore")
    meta = [f"model chose this after {ev['think_ms'] / 1000:.1f}s"] if ev.get("think_ms") else []
    n = len(ev["queries"])
    st.caption(" · ".join(meta + [f"tool ran in {ev['ms']} ms",
                                  f"its {n} Cypher quer{'y is' if n == 1 else 'ies are'} in the console"]))
    tables(ev["tables"], key)


def step_label(ev):
    n = len(ev["queries"])
    return f":material/{ICON.get(ev['tool'], 'bolt')}: {ev['title']} · {n} quer{'y' if n == 1 else 'ies'} · {ev['graph_ms']:g} ms"


# ---------------- a chat turn ----------------

def numbers(d):
    """The done event's totals as a stack of badges."""
    model = ", ".join(d["served"]) or d["requested"]
    st.badge(model + (" · fallback" if d["fallback"] else ""), icon=":material/smart_toy:", color="gray",
             help=f"Requested {d['requested']}" if d["fallback"] else "Model that wrote this answer")
    st.badge(f"LLM {d['llm_ms'] / 1000:.1f}s · {d['llm_calls']} calls", icon=":material/hourglass_top:", color="orange")
    st.badge(f"Graph {d['graph_ms']:g} ms · {d['queries']} queries", icon=":material/bolt:", color="green",
             help="Time spent in FalkorDB")
    if d["tokens_in"] is not None:
        st.badge(f"{d['tokens_in'] + d['tokens_out']:,} tokens", icon=":material/token:", color="blue",
                 help=f"{d['tokens_in']:,} in, {d['tokens_out']:,} out")
    if d["cost_estimate"] is not None:
        st.badge(f"≈ ${d['cost_estimate']:.4f}", icon=":material/payments:", color="violet",
                 help="Estimated from OpenRouter's published prices")
    st.badge(f"Remembered · {d['memory_linked']} links", icon=":material/memory:", color="primary",
             help="This turn is saved in the graph and linked to the nodes it used")
    if d.get("unverified"):
        st.badge(f"Not from the graph: {', '.join(d['unverified'][:4])}" + (" …" if len(d["unverified"]) > 4 else ""),
                 icon=":material/report:", color="red",
                 help="The answer names these, but no tool returned them this turn. Treat them as unverified.")


def turn_footer(d, turn, open_graph, on_follow_up=None):
    """After an answer: the graph (a framed tile on the current turn, a collapsed expander on older ones),
    a row of numbers, and on the latest turn a row of follow-up questions. No tiles inside tiles."""
    g = d["graph"]
    if g["edges"] and open_graph:
        with tile(f"graph-{turn}"):
            st.markdown(f"**:material/hub: {graph_title(g)}**")
            graph_view(g, key=f"graph-{turn}")
    elif g["edges"]:
        with st.expander(graph_title(g), icon=":material/hub:", key=f"exp-graph-{turn}"):
            graph_view(g)
    with st.container(horizontal=True, gap="small"):
        numbers(d)
    if on_follow_up and d["follow_ups"]:
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            st.caption("Ask next")
            for i, f in enumerate(d["follow_ups"]):
                st.button(f["label"], key=f"follow-{turn}-{i}", on_click=on_follow_up, args=(f["question"],),
                          help=f["question"], icon=":material/subdirectory_arrow_right:")


def recalled_turn(ev):
    """An answer reopened from graph memory: the text and where it came from (its steps were not stored)."""
    st.markdown(ev["text"])
    st.caption(f":material/memory: From memory · asked {ev['when']} · linked to {ev['linked']} graph "
               f"node{'s' if ev['linked'] != 1 else ''}. Its steps were not stored; ask again to see them.")


def render_turn(events, turn, live=False, latest=False, on_follow_up=None):
    """Draw one assistant turn from its events (a live generator or a stored list). Returns the events seen."""
    if not live and events and events[0]["type"] == "recalled":
        recalled_turn(events[0])
        return events
    seen, steps, text = [], {}, ""
    status = st.status(f":shimmer[{PHASE['thinking']}]" if live else "Worked", type="compact",
                       expanded=live, state="running" if live else "complete")
    answer, after = st.empty(), st.container()
    for ev in events:
        seen.append(ev)
        kind = ev["type"]
        if kind == "phase" and live:  # update() collapses unless expanded is passed explicitly
            status.update(label=f":shimmer[{PHASE[ev['phase']]}]", expanded=True, state="running")
        elif kind == "step_start":
            with status:
                steps[ev["id"]] = st.status(f":material/{ICON.get(ev['tool'], 'bolt')}: {ev['title']}", type="step",
                                            state="running", expanded=False)
        elif kind == "step_end":
            box = steps.get(ev["id"])
            if box is None:
                with status:
                    box = steps[ev["id"]] = st.status(ev["title"], type="step")
            box.update(label=step_label(ev), state="error" if ev["status"] == "error" else "complete")
            with box:
                step_body(ev, key=f"t{turn}-{ev['id']}")
        elif kind == "token" and live:
            text += ev["text"]
            answer.markdown(text)
        elif kind == "text_reset" and live:
            text = ""
            answer.empty()
        elif kind == "answer":
            answer.markdown(ev["text"])
        elif kind == "error":
            status.update(label=ev["title"], state="error", expanded=False)
            with after:
                st.error(ev["message"], title=ev["title"], icon=":material/error:")
                with st.expander("Raw error"):
                    st.code(ev["raw"], language=None, wrap_lines=True)
        elif kind == "done":
            status.update(label=f"Worked for {ev['total_ms'] / 1000:.0f}s · {ev['steps']} graph "
                                f"step{'s' if ev['steps'] != 1 else ''}", state="complete", expanded=False)
            with after:
                turn_footer(ev, turn, open_graph=latest or live, on_follow_up=on_follow_up)
    return seen


# ---------------- console: what ran in the background ----------------

TAG = {"you": "blue", "model": "violet", "tool": "orange", "cypher": "green", "memory": "blue", "done": "gray",
       "error": "red", "page": "gray"}


def _show_console(show):
    st.session_state.console_open = show


def topbar():
    """Controls at the right of the top bar: the style switch, and the button that shows or hides the console.
    On Streamlit Community Cloud they sit left of its own Fork and GitHub buttons."""
    right = "9rem" if ".streamlit.app" in (st.context.url or "") else "3.25rem"
    # the page links centre in the top bar's remaining space, so they never run under these controls
    st.html(f"""<style>.st-key-topbar {{ right: {right}; }}
[data-testid="stTopNav"] {{ padding-right: calc({right} + 15rem) !important; }}
@media (max-width: 1100px) {{
  [data-testid="stTopNav"] {{ padding-right: calc({right} + 8.5rem) !important; }}
  [data-testid="stTopNavLink"] {{ padding: 0.4rem 0.55rem !important; font-size: 0.95rem !important; }}
}}</style>""")
    shown = st.session_state.get("console_open")  # None until the visitor chooses: open on wide screens
    with st.container(key="topbar", horizontal=True, vertical_alignment="center", gap="small", width="content"):
        st.toggle("Neo-brutalist", value=True, key="brutal",
                  help="Thick borders, hard shadows, flat colours. Off: the modernist look. The layout stays the same.")
        if shown is not False:
            st.button("Console", icon=":material/right_panel_close:", key="console-hide", type="tertiary",
                      on_click=_show_console, args=(False,), help="Hide the console")
        if shown is not True:
            st.button("Console", icon=":material/right_panel_open:", key="console-show", type="tertiary",
                      on_click=_show_console, args=(True,), help="Show the console")


def console():
    """The console panel on the right: made once per run by app.py, before the page, so every page can write to it
    (also while a turn is still running). Returns the log to write lines into."""
    if st.session_state.get("_console") is None:
        mode = {None: "auto", True: "open", False: "closed"}[st.session_state.get("console_open")]
        with st.container(key=f"console-{mode}", gap="small"):
            st.markdown("**:material/terminal: Console**")
            st.caption("What runs in the background, as it happens: model calls, tool calls and every Cypher query. "
                       "Open a query to see its parameters and response.")
            st.session_state["_console"] = st.container(height=400, autoscroll=True, key="console-log", gap=None)
        st.session_state["_console_lines"] = 0
    return st.session_state["_console"]


def clock(ms):
    """A log timestamp as a code span, or nothing when the work has no time of its own."""
    return f"`{datetime.fromtimestamp(ms / 1000).strftime('%H:%M:%S.%f')[:-3]}` " if ms else ""


def line(at, tag, text):
    with console():
        st.markdown(f"{clock(at)}:{TAG[tag]}[**{tag}**] {text}")
    st.session_state["_console_lines"] += 1


def _json(value):
    return json.dumps(value, indent=1, default=str, ensure_ascii=False)


def query_line(qy):
    """One Cypher query: a single line that opens to the query, its parameters and its response."""
    first = qy["cypher"].splitlines()[0]
    result, rows = qy.get("result", []), qy["rows"]
    label = (f"{clock(qy.get('at'))}:{TAG['cypher']}[**cypher**] `{first[:44]}{'…' if len(first) > 44 else ''}` "
             f"· {rows} row{'s' if rows != 1 else ''} · {qy['ms']:g} ms" + (" · your memory graph" if qy.get("graph") == "memory" else ""))
    with console(), st.expander(label):
        st.code(qy["cypher"], language="cypher", wrap_lines=True)
        if qy["params"]:
            st.caption("Parameters")
            params = _json(qy["params"])
            st.code(params, language="json", wrap_lines=True, height="content" if len(params) < 600 else 200)
        st.caption(f"Response · {rows} row{'s' if rows != 1 else ''}" + (f", first {len(result)} shown" if len(result) < rows else ""))
        if result:
            st.code(_json(result), language="json", wrap_lines=True, height="content" if len(result) <= 3 else 260)
    st.session_state["_console_lines"] += 1


def work(label, queries, result=None, ms=None, tag="tool"):
    """A piece of work that is not an agent turn (an Explore view, a node's details, a page load): what it was,
    the Cypher it ran, and what came out."""
    line(queries[0]["at"] if queries else None, tag, label)
    for qy in queries:
        query_line(qy)
    if result is not None:
        line(queries[-1]["at"] if queries else None, tag, f"✓ {result}" + (f" · {ms} ms" if ms is not None else ""))


def call(tool, args):
    """A tool call as the model (or the Explore page) made it: name and arguments."""
    return f"`{tool}` `{json.dumps(args, ensure_ascii=False, default=str)}`"


def log_event(ev):
    """One pipeline event as console lines. Events that only drive the chat view (phases, tokens) are skipped."""
    kind, at = ev["type"], ev.get("at")
    for qy in ev.get("queries", []) if kind in ("resumed", "memory", "recalled", "step_end") else []:
        query_line(qy)
    if kind == "question":
        line(at, "you", ev["text"])
    elif kind == "resumed":
        line(at, "memory", f"restored {ev['turns']} earlier turn{'s' if ev['turns'] != 1 else ''} of this chat from the "
                           "graph, so the model sees them")
    elif kind == "model":
        tokens = f" · {ev['tokens_in']:,} → {ev['tokens_out']:,} tokens" if ev["tokens_in"] is not None else ""
        cost = f" · ≈ ${ev['cost']:.4f}" if ev["cost"] is not None else ""
        then = "asks for " + ", ".join(f"`{t}`" for t in ev["next"]) if ev["next"] else "writes the answer"
        line(at, "model", f"{ev['model'] or 'model'} · {ev['ms'] / 1000:.1f}s{tokens}{cost} → {then}")
    elif kind == "step_start":
        line(at, "tool", f"▶ {call(ev['tool'], ev['args'])}")
    elif kind == "step_end":
        mark = "✗" if ev["status"] == "error" else "✓"
        line(at, "tool", f"{mark} `{ev['tool']}` → {ev['result']} · {ev['ms']} ms")
    elif kind == "memory":
        line(at, "memory", f"saved this turn in your memory graph, pointing at {ev['linked']} "
                           f"Kubernetes node{'s' if ev['linked'] != 1 else ''}")
    elif kind == "recalled":
        line(at, "memory", f"reopened from the graph · asked {ev['when']}")
    elif kind == "error":
        line(at, "error", f"{ev['title']}: {ev['message']}")
    elif kind == "done":
        if ev.get("unverified"):
            line(at, "error", f"the answer names {', '.join(ev['unverified'])}, which no tool returned this turn")
        line(at, "done", f"{ev['total_ms'] / 1000:.1f}s in total · model {ev['llm_ms'] / 1000:.1f}s over {ev['llm_calls']} "
                         f"call{'s' if ev['llm_calls'] != 1 else ''} · FalkorDB {ev['graph_ms']:g} ms over {ev['queries']} "
                         f"quer{'y' if ev['queries'] == 1 else 'ies'}")


def logged(events):
    """Pass events through unchanged, writing each to the console as it arrives (for a live turn)."""
    for ev in events:
        log_event(ev)
        yield ev


def console_end():
    """Called once at the end of every run: says so when nothing ran in the background."""
    console()
    if not st.session_state["_console_lines"]:
        line(None, "page", "Nothing has run yet. Model calls, tool calls and every Cypher query appear here as they happen.")
