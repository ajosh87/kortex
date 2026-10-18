"""Kortex, the Kubernetes org brain: entry point. Page setup, navigation and the settings every page shares.

    uv run streamlit run app.py
"""
import uuid
from pathlib import Path

import streamlit as st

import agent as A
import memory_graph as MG
import models as M
import snapshot
import ui

HERE = Path(__file__).parent
st.set_page_config(page_title="Kortex · Kubernetes org brain", page_icon=str(HERE / "assets" / "icon.svg"), layout="wide",
                   initial_sidebar_state="expanded")
st.logo(str(HERE / "assets" / "logo.svg"), icon_image=str(HERE / "assets" / "icon.svg"), size="large")

@st.cache_resource(ttl=300, show_spinner=False)
def missing_data():
    """What the database lacks that the bundled snapshot can restore (checked at most every 5 minutes)."""
    return snapshot.missing()


if missing_data():  # a fresh install, or a free cloud instance that restarted empty
    with st.spinner(f"Restoring {' and '.join(missing_data())} from the snapshot (first start only, about a minute)…"):
        snapshot.ensure()
    missing_data.clear()

if "chats" not in st.session_state:
    st.session_state.chats = {}  # session id -> chat messages, for chats opened in this browser session
    st.session_state.session = str(uuid.uuid4())[:8]
st.session_state["_console"] = None  # every run writes a fresh console (ui.console)

page = st.navigation([
    st.Page("app_pages/ask.py", title="Ask", icon=":material/forum:", default=True),
    st.Page("app_pages/explore.py", title="Explore", icon=":material/travel_explore:"),
    st.Page("app_pages/memory.py", title="Memory", icon=":material/psychology:"),
    st.Page("app_pages/data.py", title="Data", icon=":material/database:"),
], position="top")


def open_chat(session):
    """Reopen a remembered chat: from this browser session if it ran here, otherwise from graph memory."""
    if session not in st.session_state.chats:
        with st.spinner("Reopening the chat from memory…"):
            st.session_state.chats[session] = A.transcript(st.session_state.user, session)
    st.session_state.session = session


with st.sidebar:
    user = st.session_state.get("user", "demo")  # the name field sits lower down, but the chat list needs it first
    chats = MG.sessions(user, st.session_state.session, limit=10)
    ui.section("Chats", "forum")
    if not chats:
        st.caption("Your chats appear here once you ask something.")
    with st.container(key="chats", gap=None):
        for c in chats:
            if st.button(c["first_question"], key=f"chat-{c['session']}", width="stretch",
                         type="secondary" if c["current"] else "tertiary",
                         help=f"{c['when']} · {c['turns']} question{'s' if c['turns'] != 1 else ''}"):
                open_chat(c["session"])
                if page.title == "Memory":  # the Memory page shows the open chat; elsewhere, go to the chat
                    st.rerun()
                st.switch_page("app_pages/ask.py")

    ui.section("Settings", "tune")
    st.text_input("Your name", value="demo", key="user", help="Chats and memory are kept per name, across sessions.")
    default_provider, default_model = M.split_spec(M.MODEL)
    providers = M.providers()
    with st.expander("Model", icon=":material/tune:", expanded=M.needs_key(st.session_state.get("api_key"))):
        provider = st.selectbox("Provider", providers, key="provider",
                                index=providers.index(default_provider) if default_provider in providers else 0)
        model = st.text_input("Model id", value=default_model if provider == default_provider else M.PROVIDERS[provider])
        if provider == "bedrock_converse":
            st.session_state.api_key = ""
            st.caption("Bedrock uses your AWS credentials (env vars or ~/.aws).")
        else:
            st.text_input("API key (required)" if M.REQUIRE_USER_KEY else "API key (optional)", type="password", key="api_key",
                          help="Used only in this browser session, never stored." + (
                              "" if M.REQUIRE_USER_KEY else f" Leave empty to use {M.KEY_ENV[provider]} from the server."))
            if M.REQUIRE_USER_KEY:
                st.caption("This app answers with your own key only.")
    st.session_state.spec = f"{provider}:{model.strip()}"

    st.space("medium")
    with ui.tile("credits"):
        st.markdown(":material/emoji_events: Built for [**Graph Hacks**](https://www.wemakedevs.org/hackathons/falkordb) by WeMakeDevs  \n"
                    ":material/hub: Powered by [**FalkorDB**](https://www.falkordb.com)  \n"
                    ":material/bolt: Made with [**Streamlit**](https://streamlit.io)  \n"
                    ":material/smart_toy: Coded with [**Claude Code**](https://claude.com/claude-code), directed and reviewed by the author")
        st.caption("AI disclosure: most of this app's code was written with Claude Code, Anthropic's AI coding assistant. "
                   "Answers in the chat come from the model you pick under Model. "
                   "Data: public Kubernetes repositories. People appear only as GitHub handles.")

ui.apply_style()
ui.console()
page.run()
ui.console_end()
