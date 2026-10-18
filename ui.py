"""Shared rendering for every page. These functions only turn pipeline output (step records, graphs,
done events) into Streamlit elements: no data is computed or reshaped here.

One calm look, built from the logo's colours: soft neutral surfaces, 1 px hairlines, rounded corners on one scale
(8 px controls, 12 px cards and the composer), no hard shadows, the logo's indigo for actions and the open page,
and its yellow only as a small highlight. Light and dark themes share the same tokens.

Like a desktop chat app, the page has three parts: chats on the left (sidebar), the conversation in the middle, and a
console panel on the right, styled like a terminal: a log of what ran (model calls, tool calls, and every Cypher
query with its parameters and response), written as it happens. It is open by default (on narrow screens it starts
minimized); its × minimizes it to a tab on the right edge, and the tab brings it back."""
import json
import re
from datetime import datetime

import streamlit as st

import tools as T
import ui_graph

# ---------------- palette (presentation only) ----------------

# Node colours by kind: muted tints that sit together with the logo's indigo and yellow, dark text on all of them.
SOFT = {"Person": "#BFE3FB", "Dir": "#FFE8A3", "KEP": "#CDD0FB", "SIG": "#C3EED2", "Team": "#BDEDE6",
        "FeatureGate": "#F9CAD7", "PR": "#DDE1EA", "Component": "#FDD9B5", "Subproject": "#E0DAFB", "Turn": "#4F46E5"}
FONT = {"Turn": "#ffffff"}  # dark fills get light text

# Icon per tool in the step timeline.
ICON = {"search_graph": "search", "who_owns": "approval", "design_constraints": "gavel", "blast_radius": "crisis_alert",
        "find_experts": "groups", "kep_details": "description", "decision_trail": "account_tree",
        "single_points_of_failure": "person_alert", "person_profile": "person", "how_connected": "conversion_path",
        "run_cypher": "terminal", "write_review_plan": "edit_document", "recall_memory": "psychology",
        "recent_changes": "update", "critical_code": "local_fire_department", "decision_to_approvers": "route",
        "best_reviewers": "rate_review"}

PHASE = {"thinking": "Model is choosing the next step", "tools": "Querying the graph", "writing": "Writing the answer"}


# Card hues: (light solid, light tint, dark solid). Indigo is the logo's; the others sit next to it.
HUES = {"indigo": ("#4F46E5", "#EEF0FF", "#818CF8"), "violet": ("#7C3AED", "#F3EEFF", "#A78BFA"),
        "sky": ("#0284C7", "#E6F4FE", "#38BDF8"), "teal": ("#0D9488", "#E3F7F4", "#2DD4BF"),
        "amber": ("#D97706", "#FFF4E0", "#FBBF24"), "rose": ("#E11D48", "#FFEBEF", "#FB7185")}

# Console tag colours: a terminal palette, on the console's own dark background in both themes.
TAG = {"you": "#79C0FF", "model": "#D2A8FF", "tool": "#FFA657", "cypher": "#7EE787", "memory": "#56D4DD",
       "done": "#8B949E", "error": "#FF7B72", "page": "#8B949E"}


def tokens():
    """The design's colours for the active theme: neutrals, plus the logo's indigo (accent) and yellow."""
    if st.context.theme.type == "dark":
        return {"page": "#0F1117", "side": "#0B0D13", "surface": "#171A24", "line": "#272C3B", "muted": "#9EA4B8",
                "log": "#B7BCCB", "accent": "#818CF8", "tint": "rgba(99,102,241,0.16)", "tint_line": "rgba(129,140,248,0.38)",
                "hover": "rgba(255,255,255,0.05)"}
    return {"page": "#F7F7FB", "side": "#F1F2F7", "surface": "#FFFFFF", "line": "#DCDFEA", "muted": "#5B6070",
            "log": "#3E4456", "accent": "#4F46E5", "tint": "#EEF0FF", "tint_line": "#D9DCFB", "hover": "rgba(28,32,48,0.05)"}


def tile(key, fill=None, height="content", width="stretch"):
    """A card: a bordered container. `fill="accent"` tints it with the logo's indigo (one result per page); a hue
    name from HUES gives it that colour's top edge and icon chip."""
    return st.container(border=True, key=f"tile-{fill or 'plain'}-{key}", height=height, width=width)


def section(label, icon):
    """A section heading with a rule above it, so the parts of a page read as separate blocks."""
    slug = "".join(c if c.isalnum() else "-" for c in label.lower())
    with st.container(key=f"section-{slug}"):
        st.markdown(f":material/{icon}: {label}")


def paper():
    """The surface graphs sit on (edge labels get a matching backdrop)."""
    return tokens()["surface"]


# ---------------- stylesheets ----------------

BASE_CSS = """<style>
/* top navigation: centred and a little larger */
[data-testid="stTopNav"] { flex: 1 1 auto !important; justify-content: center !important; }
[data-testid="stTopNavLink"] { font-size: 1.02rem !important; padding: 0.42rem 0.9rem !important; }
[data-testid="stTopNavLink"] [data-testid="stIconMaterial"] { font-size: 1.2rem !important; }
/* chat: the person on the right, the agent on the left */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] { text-align: left; }
/* a disabled chat input (no API key yet) measures its height once, before its row has its width, and never again */
[data-testid="stChatInput"] textarea:disabled { height: auto !important; max-height: 2.6rem !important; overflow: hidden; }
/* wide tables in answers scroll inside their own box instead of widening the page */
[data-testid="stMarkdownContainer"] table { display: block; max-width: 100%; overflow-x: auto; }
/* room to breathe: a readable column in the middle of wide screens, generous margins, roomier cards */
[data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] { max-width: 1120px; margin: 0 auto; }
[data-testid="stMainBlockContainer"] { padding-top: 3rem; padding-bottom: 9rem; }  /* clear of the chat bar */
[data-testid="stBottomBlockContainer"] { padding-top: 0.75rem; padding-bottom: 0.9rem; }
div[class*="st-key-tile-"] { padding: 1.3rem 1.4rem !important; }
/* the per-kind counts on the Data page read one step below the totals */
.st-key-data-kinds [data-testid="stMetricValue"] { font-size: 1.6rem !important; }
/* columns wrap onto their own rows when there is no room for them side by side */
[data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
[data-testid="stColumn"] { min-width: min(100%, 17rem); }
/* sections: a rule and a small heading above each part of a page */
div[class*="st-key-section-"] { border-top: 1px solid rgba(120,120,150,0.3); padding-top: 0.85rem; margin-top: 1.6rem; }
div[class*="st-key-section-"] p { font-size: 0.8rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
/* starter cards: a wrapping grid, about three across on wide screens and one on phones */
.st-key-starters > div { flex: 1 1 15rem !important; min-width: 15rem; }
/* chats in the sidebar: one line each, left aligned */
.st-key-chats [data-testid="stButton"] button { justify-content: flex-start; text-align: left; min-height: 2.15rem; }
.st-key-chats [data-testid="stButton"] button p { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-key-chats [data-testid="stButton"] button > div { min-width: 0; }
/* console panel on the right, under the top bar. "auto" (nobody has toggled it yet) is open on wide screens and
   closed on narrow ones; "open" and "closed" are the visitor's choice. The page keeps clear of an open panel. */
:root { --console-w: clamp(280px, 24vw, 340px); }
[data-testid="stLayoutWrapper"]:has(> .st-key-console-open), [data-testid="stLayoutWrapper"]:has(> .st-key-console-auto),
[data-testid="stLayoutWrapper"]:has(> .st-key-console-closed) {
  position: absolute !important; height: 0 !important; margin: 0 !important; }  /* out of the page flow */
.st-key-console-open, .st-key-console-auto { position: fixed !important; top: 3.5rem; right: 0; bottom: 0;
  width: var(--console-w); z-index: 100; padding: 0.9rem 0.9rem 0.6rem; display: flex; flex-direction: column; }
.st-key-console-closed { display: none !important; }
[data-testid="stMain"]:has(.st-key-console-open), [data-testid="stMain"]:has(.st-key-console-auto) {
  padding-right: var(--console-w); box-sizing: border-box; }  /* padding, not margin: the page keeps its width */
/* minimized: a tab on the right edge that brings the console back */
[data-testid="stLayoutWrapper"]:has(> .st-key-console-tab) { position: absolute !important; height: 0 !important; margin: 0 !important; }
.st-key-console-tab { position: fixed !important; right: 0; top: 4.5rem; z-index: 100; width: 2.4rem !important; }
.st-key-console-tab [data-testid="stElementContainer"], .st-key-console-tab [data-testid="stButton"],
.st-key-console-tab button { width: 2.4rem !important; min-width: 0 !important; padding-left: 0 !important; padding-right: 0 !important; }
.st-key-console-tab [data-testid="stElementContainer"] { padding: 0 !important; margin: 0 !important; }
.st-key-console-tab button { writing-mode: vertical-rl; border-radius: 10px 0 0 10px !important; padding: 0.8rem 0.4rem !important;
  min-height: 0 !important; background: #0D1117 !important; color: #C9D1D9 !important; border: 1px solid #30363D !important; }
.st-key-console-tab button:hover { color: #FFFFFF !important; border-color: #8B949E !important; }
.st-key-console-tab button [data-testid="stIconMaterial"] { writing-mode: horizontal-tb; }
[data-testid="stMain"]:has(.st-key-console-closed) { padding-right: 2.6rem; box-sizing: border-box; }
body:not(:has(.st-key-console-closed)) .st-key-console-tab { display: none; }  /* open: no tab */
@media (max-width: 1100px) {
  .st-key-console-auto { display: none !important; }  /* auto on a narrow screen: minimized */
  [data-testid="stMain"]:has(.st-key-console-auto) { padding-right: 2.6rem; }
  body:has(.st-key-console-auto) .st-key-console-tab { display: block; }
  .st-key-console-open { width: min(92vw, 380px); box-shadow: -12px 0 32px rgba(0,0,0,0.25); }  /* over the page */
  [data-testid="stMain"]:has(.st-key-console-open) { padding-right: 0; }
  [data-testid="stTopNavLink"] { padding: 0.4rem 0.55rem !important; font-size: 0.95rem !important; }
}
/* console: a small header, then a compact monospace log that fills the panel and scrolls */
.st-key-console-head { padding-bottom: 0.55rem; border-bottom: 1px solid #21262D; }
.st-key-console-head p { font-size: 0.85rem; margin: 0; }
.st-key-console-head [data-testid="stButton"] button { min-height: 0 !important; padding: 0.15rem 0.3rem !important; }
.st-key-console-head [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }  /* no -1rem: title and caption stack */
.st-key-console-head [data-testid="stCaptionContainer"] p { font-size: 0.78rem; }
.st-key-console-empty { text-align: center; padding-top: 2.5rem; }
.st-key-console-empty p { font-family: "Inter", sans-serif !important; }
.st-key-console-log, .st-key-console-log p, .st-key-console-log summary { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 0.83rem; }
[data-testid="stLayoutWrapper"]:has(> .st-key-console-log) { flex: 1 1 0 !important; min-height: 0; height: auto !important; }
div.st-key-console-log.st-key-console-log { height: 100% !important; max-height: none !important; overflow-y: auto; }
.st-key-console-log > * { flex-shrink: 0 !important; }  /* lines keep their height; the log scrolls instead */
.st-key-console-log p { margin: 0; line-height: 1.5; }
.st-key-console-log [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }  /* undo Streamlit's -1rem (p has no margin here) */
.st-key-console-log strong { display: inline-block; min-width: 4.6em; font-weight: 600; }
.st-key-console-log [data-testid="stExpander"] details { border: 0 !important; box-shadow: none !important; background: transparent !important; }
.st-key-console-log [data-testid="stExpander"] summary { padding: 0.05rem 0; min-height: 0; }
.st-key-console-log [data-testid="stExpanderDetails"] { padding: 0.25rem 0 0.5rem 1.4rem; }
</style>"""


def design_css():
    """The look: neutral surfaces with hairlines, rounded corners on one scale, the logo's indigo for actions and the
    open page, its yellow once per page under the title. Targets our own keys and Streamlit's data-testid attributes."""
    t = tokens()
    return f"""<style>
[data-testid="stAppViewContainer"], [data-testid="stHeader"], [data-testid="stBottom"] > div {{ background: {t["page"]} !important; }}
[data-testid="stSidebar"] {{ background: {t["side"]} !important; border-right: 1px solid {t["line"]} !important; }}
h1 {{ font-weight: 700 !important; letter-spacing: -0.025em; }}
h1::after {{ content: ""; display: block; width: 2rem; height: 3px; margin-top: 0.55rem; border-radius: 2px;
  background: linear-gradient(90deg, #4F46E5, #FFDE59); }}
h4 {{ font-weight: 600 !important; letter-spacing: -0.01em; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {t["muted"]} !important; opacity: 1 !important; }}

/* cards: a surface with a hairline and 12 px corners, flat at rest; one indigo-tinted result card per page */
div[class*="st-key-tile-"] {{ background: {t["surface"]} !important; border: 1px solid {t["line"]} !important;
  border-radius: 12px !important; box-shadow: none !important; }}
div[class*="st-key-tile-accent-"] {{ background: {t["tint"]} !important; border-color: {t["tint_line"]} !important; }}
.st-key-starters div[class*="st-key-tile-"] {{ transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease; }}
.st-key-starters div[class*="st-key-tile-"]:hover {{ border-color: {t["tint_line"]} !important; transform: translateY(-1px);
  box-shadow: 0 1px 2px rgba(16,24,40,0.04), 0 8px 24px rgba(79,70,229,0.10) !important; }}
.st-key-starters h4 span[role="img"] {{ color: {t["accent"]} !important; background: {t["tint"]}; border-radius: 8px;
  padding: 0.3rem; margin-right: 0.45rem; font-size: 1.1rem !important; vertical-align: -0.2rem !important; }}

/* navigation: the open page is a soft indigo pill */
[data-testid="stTopNavLink"] {{ border-radius: 999px !important; font-weight: 500; }}
[data-testid="stTopNavLink"][aria-current="page"] {{ background: {t["tint"]} !important; }}
[data-testid="stTopNavLink"][aria-current="page"] * {{ color: {t["accent"]} !important; }}

/* controls: 8 px corners; indigo on hover */
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a {{
  border-radius: 8px !important; font-weight: 500; transition: border-color .12s ease, color .12s ease, background .12s ease; }}
[data-testid="stButton"] button[kind="secondary"]:hover, [data-testid="stLinkButton"] a:hover {{
  border-color: {t["accent"]} !important; color: {t["accent"]} !important; }}
[data-testid="stTextInputRootElement"], [data-testid="stSelectbox"] [data-baseweb="select"] > div {{ border-radius: 8px !important; }}
[data-testid="stExpander"] details {{ border-radius: 12px !important; border-color: {t["line"]} !important; }}
[data-testid="stCode"] pre {{ border-radius: 8px !important; }}
.stMarkdownBadge {{ border-radius: 999px !important; }}

/* the composer: 12 px corners, a faint lift, indigo when focused */
[data-testid="stChatInput"] > div {{ border-radius: 12px !important; border: 1px solid {t["line"]} !important;
  background: {t["surface"]} !important; box-shadow: 0 1px 2px rgba(16,24,40,0.04), 0 6px 20px rgba(79,70,229,0.06); }}
[data-testid="stChatInput"]:focus-within > div {{ border-color: {t["accent"]} !important; }}

/* sections: a fine rule, a muted label with an indigo icon */
div[class*="st-key-section-"] {{ border-top: 1px solid {t["line"]}; }}
div[class*="st-key-section-"] p {{ color: {t["muted"]} !important; }}
div[class*="st-key-section-"] span[role="img"] {{ color: {t["accent"]} !important; }}

/* chat: your message is a soft indigo bubble; the answer reads straight on the page */
[data-testid="stChatMessage"] {{ background: transparent !important; border: 0 !important; padding: 0.4rem 0 !important;
  margin-bottom: 1rem; }}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {{ background: {t["tint"]} !important;
  border: 1px solid {t["tint_line"]} !important; border-radius: 12px !important; padding: 0.75rem 1rem !important; }}

/* sidebar chats: quiet rows, the open one tinted indigo */
.st-key-chats [data-testid="stButton"] button {{ border: 0 !important; border-radius: 8px !important; background: transparent !important; }}
.st-key-chats [data-testid="stButton"] button:hover {{ background: {t["hover"]} !important; color: inherit !important; }}
.st-key-chats [data-testid="stButton"] button[kind="secondary"] {{ background: {t["tint"]} !important; }}
.st-key-chats [data-testid="stButton"] button[kind="secondary"] p {{ color: {t["accent"]} !important; font-weight: 600; }}

/* separations: the top bar and each answer get a hairline under them */
[data-testid="stHeader"] {{ border-bottom: 1px solid {t["line"]}; }}
[data-testid="stChatMessage"]:not(:has([data-testid="stChatMessageAvatarUser"])) {{ border-bottom: 1px solid {t["line"]} !important;
  border-radius: 0 !important; padding-bottom: 1.4rem !important; margin-bottom: 1.4rem; }}

{hue_css()}
{terminal_css()}
</style>"""


def hue_css():
    """Each hue: a coloured top edge on its cards, its icon chip, and a matching border on hover."""
    dark = st.context.theme.type == "dark"
    rules = []
    for name, (solid, tint, solid_dark) in HUES.items():
        c, bg = (solid_dark, f"{solid_dark}29") if dark else (solid, tint)
        rules.append(f'''div[class*="st-key-tile-{name}-"] {{ border-top: 3px solid {c} !important; }}
div[class*="st-key-tile-{name}-"]:hover {{ border-color: {c} !important; }}
div[class*="st-key-tile-{name}-"] h4 span[role="img"] {{ color: {c} !important; background: {bg} !important; }}''')
    return "\n".join(rules)


def terminal_css():
    """The console looks like a terminal in both themes: a dark panel, light monospace text, coloured tags,
    dark code blocks with a terminal syntax palette."""
    tags = "\n".join(f'[class*="st-key-log-{tag}-"] p > strong:first-of-type {{ color: {c} !important; }}' for tag, c in TAG.items())
    return f'''.st-key-console-open, .st-key-console-auto {{ background: #0D1117 !important; border-left: 1px solid #30363D; }}
.st-key-console-head p {{ color: #E6EDF3 !important; }}
.st-key-console-head [data-testid="stCaptionContainer"] p, .st-key-console-empty [data-testid="stCaptionContainer"] p,
.st-key-console-log [data-testid="stCaptionContainer"] p {{ color: #8B949E !important; }}
.st-key-console-head [data-testid="stButton"] button {{ color: #8B949E !important; background: transparent !important; border: 0 !important; }}
.st-key-console-head [data-testid="stButton"] button:hover {{ color: #E6EDF3 !important; background: rgba(255,255,255,0.08) !important; }}
.st-key-console-log {{ scrollbar-color: #30363D transparent; }}
.st-key-console-log [data-testid="stMarkdownContainer"], .st-key-console-log summary,
.st-key-console-log summary * {{ color: #C9D1D9 !important; }}
.st-key-console-log p code {{ background: rgba(110,118,129,0.18) !important; color: #A5D6FF !important; }}
.st-key-console-log p > code:first-child {{ background: transparent !important; color: #6E7681 !important; padding: 0; }}  /* the time */
{tags}
.st-key-console-log summary [data-testid="stIconMaterial"] {{ color: #8B949E !important; }}
.st-key-console-log [data-testid="stCode"] pre {{ background: #161B22 !important; border: 1px solid #30363D !important; }}
.st-key-console-log [data-testid="stCode"] code, .st-key-console-log [data-testid="stCode"] code span {{ color: #C9D1D9 !important; }}
.st-key-console-log .token.keyword {{ color: #FF7B72 !important; }}
.st-key-console-log .token.function {{ color: #D2A8FF !important; }}
.st-key-console-log .token.string {{ color: #A5D6FF !important; }}
.st-key-console-log .token.number, .st-key-console-log .token.boolean, .st-key-console-log .token.property {{ color: #79C0FF !important; }}
.st-key-console-log .token.class-name {{ color: #FFA657 !important; }}
.st-key-console-log .token.relationship {{ color: #7EE787 !important; }}
.st-key-console-log .token.variable {{ color: #FFA198 !important; }}
.st-key-console-log .token.punctuation, .st-key-console-log .token.operator {{ color: #8B949E !important; }}'''


def apply_style():
    """The base stylesheet (layout: navigation, chat sides, spacing, console panel), then the look."""
    st.html(BASE_CSS)
    st.html(design_css())


# ---------------- graphs ----------------

def _q(s):
    return json.dumps(str(s), ensure_ascii=False)


def _look():
    """Graphviz node and edge attributes: rounded, soft, hairline outlines."""
    return (SOFT, 'shape=box style="rounded,filled" fontname="Helvetica" fontsize=10 color="#9AA3B5" penwidth=0.6',
            'fontname="Helvetica" fontsize=8 color="#9AA3B5" fontcolor="#7A8296" arrowsize=0.6')


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
    picked = ui_graph.show(graph, key, colors=SOFT, fonts=FONT, background=paper(), accent=tokens()["accent"],
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

def _show_console(show):
    st.session_state.console_open = show


def console():
    """The console panel on the right: made once per run by app.py, before the page, so every page can write to it
    (also while a turn is still running). Returns the log to write lines into."""
    if st.session_state.get("_console") is None:
        mode = {None: "auto", True: "open", False: "closed"}[st.session_state.get("console_open")]
        with st.container(key=f"console-{mode}", gap="small"):
            with st.container(key="console-head", horizontal=True, vertical_alignment="top", gap="small"):
                with st.container(gap=None):
                    st.markdown("**:material/terminal: Console**")
                    st.caption("Model calls, tool calls and every Cypher query, as they run.")
                st.button("", icon=":material/close:", key="console-close", type="tertiary", help="Minimize the console",
                          on_click=_show_console, args=(False,))
            st.session_state["_console"] = st.container(height=400, autoscroll=True, key="console-log", gap=None, border=False)
        if mode != "open":  # minimized (or "auto", which is minimized on narrow screens): the tab to bring it back
            with st.container(key="console-tab", width="content"):
                st.button("Console", icon=":material/terminal:", key="console-reopen", help="Show the console",
                          on_click=_show_console, args=(True,))
        st.session_state["_console_lines"] = 0
    return st.session_state["_console"]


def clock(ms):
    """A log timestamp as a code span, or nothing when the work has no time of its own."""
    return f"`{datetime.fromtimestamp(ms / 1000).strftime('%H:%M:%S.%f')[:-3]}` " if ms else ""


def line(at, tag, text):
    """One log line. Its container is named after the tag, which gives the tag its terminal colour."""
    with console(), st.container(key=f"log-{tag}-{st.session_state['_console_lines']}"):
        st.markdown(f"{clock(at)}**{tag}** {text}")
    st.session_state["_console_lines"] += 1


def _json(value):
    return json.dumps(value, indent=1, default=str, ensure_ascii=False)


def query_line(qy):
    """One Cypher query: a single line that opens to the query, its parameters and its response."""
    first = qy["cypher"].splitlines()[0]
    result, rows = qy.get("result", []), qy["rows"]
    label = (f"{clock(qy.get('at'))}**cypher** `{first[:44]}{'…' if len(first) > 44 else ''}` "
             f"· {rows} row{'s' if rows != 1 else ''} · {qy['ms']:g} ms" + (" · your memory graph" if qy.get("graph") == "memory" else ""))
    with console(), st.container(key=f"log-cypher-{st.session_state['_console_lines']}"), st.expander(label):
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
    if not st.session_state["_console_lines"]:
        with console(), st.container(key="console-empty"):
            st.caption("Nothing has run yet. Ask something, or open Explore.")
