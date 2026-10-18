"""Ask: chat with the agent. Renders agent.ask_stream() events through ui.render_turn(), and logs them to the console."""
import uuid

import streamlit as st

import agent as A
import models as M
import ui

# Starter questions: (icon, title, the graph work it triggers, question, card hue).
STARTERS = [
    (":material/route:", "Trace a decision to code and people",
     "KEP-2837 → its feature gate → the code that checks it → inherited OWNERS → the people who approve.",
     "Trace KEP-2837 (pod-level resources) to the code it changes and the people who must approve that code.", "indigo"),
    (":material/rate_review:", "Who should review an eviction change?",
     "Who can approve the folder (inherited OWNERS), intersected with who merged changes there recently.",
     "I want to change pkg/kubelet/eviction. Who should review it, and which design decisions constrain the change?", "violet"),
    (":material/local_fire_department:", "Riskiest code in the kubelet",
     "FalkorDB PageRank and betweenness over Go imports, then how many people can approve each package.",
     "What is the most critical code in pkg/kubelet, and is any of it thinly owned?", "rose"),
    (":material/crisis_alert:", "What could break?",
     "Follows Go imports backwards, hop by hop until nothing new is reached, to every package and binary that depends on it.",
     "If I change pkg/kubelet/eviction, what else could break?", "amber"),
    (":material/edit_document:", "Write a review plan",
     "Owners, design decisions, impact and recent contributors, combined into a file you can download.",
     "Write a review plan for a change to the soft eviction grace period in pkg/kubelet/eviction.", "teal"),
    (":material/psychology:", "What did I ask, and what changed?",
     "Recalls your earlier chats from graph memory, then lists the PRs and KEPs that changed since.",
     "What did I ask you about the kubelet last time, and what has changed there in the last two weeks?", "sky"),
]


def get_agent(spec, demo):
    """One agent per browser session; rebuilt when name, model or key changes. Keys stay in this session only."""
    user, key = st.session_state.user, st.session_state.get("api_key", "")
    sig = (user, spec, hash(key), demo)
    if st.session_state.get("agent_sig") != sig:
        st.session_state.agent = A.build(user, spec, key or None, demo)
        st.session_state.agent_sig = sig
    return st.session_state.agent


def queue(question):
    st.session_state.pending = question


def new_chat():
    st.session_state.session = str(uuid.uuid4())[:8]


def user_message(text):
    """The person's message, on the right (the agent's answers stay on the left)."""
    with st.container(horizontal_alignment="right"):
        with st.chat_message("user", width="content"):
            st.markdown(text)


def starter(i, locked):
    icon, title, how, question, hue = STARTERS[i]
    with ui.tile(f"starter-{i}", fill=hue, height="stretch"):
        st.markdown(f"#### {icon} {title}")
        st.caption(how)
        st.space("stretch")
        st.button("Ask", key=f"starter-{i}", on_click=queue, args=(question,), icon=":material/arrow_forward:",
                  disabled=locked, help="Add your API key under Model in the sidebar first" if locked else None)


messages = st.session_state.chats.setdefault(st.session_state.session, [])
used = st.session_state.get("demo_used", 0)  # questions asked on the app's demo key in this browser session
locked = M.needs_key(st.session_state.get("api_key"), used)  # this deployment needs the visitor's own key
demo = M.REQUIRE_USER_KEY and not (st.session_state.get("api_key") or "").strip() and not locked
spec = M.MODEL if demo else st.session_state.spec  # the demo key only runs the default model
pending = None if locked else st.session_state.pop("pending", None)  # a starter or follow-up button was clicked
if locked:
    with st.bottom:
        st.caption(":material/key: Add your API key under **Model** in the sidebar to ask"
                   + (f" (the {M.DEMO_QUESTIONS} demo questions of this session are used up)" if used else "")
                   + ". Explore, Memory and Data work without one.")
elif demo:
    with st.bottom:
        st.caption(f":material/key: Demo key: {M.demo_left(used)} of {M.DEMO_QUESTIONS} questions left in this session, "
                   f"answered by `{M.split_spec(M.MODEL)[1]}`. Add your own key under **Model** for more.")
with st.bottom, st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
    prompt = st.chat_input("Add your API key in the sidebar to ask" if locked else
                           "Ask about owners, design decisions, impact or people", submit_mode="disable",
                           disabled=locked) or pending
    if messages or prompt:
        st.button("New chat", icon=":material/add_comment:", on_click=new_chat, key="new-chat",
                  help="Start a new chat. This one stays in the sidebar.")

intro = st.empty()  # one slot, so the starters vanish the moment a question starts (not when the answer ends)
if messages or prompt:
    intro.empty()
else:
    with intro.container():
        st.title("Ask Kortex")
        st.markdown("Who owns code, which design decisions constrain it, what could break and who knows it. "
                    "Every answer is a walk through a FalkorDB graph of the Kubernetes project, and the console "
                    "on the right shows each step as it runs.")
        ui.section("Try a question", "bolt")
        with st.container(horizontal=True, gap="medium", key="starters"):  # a grid that wraps: 3 across on wide screens
            for i in range(len(STARTERS)):
                starter(i, locked)
        st.space("small")
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.caption("No API key? The same graph walks run without a model.")
            if st.button("Explore without a model", icon=":material/travel_explore:", type="tertiary"):
                st.switch_page("app_pages/explore.py")

last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
for i, msg in enumerate(messages):
    if msg["role"] == "user":
        user_message(msg["content"])
        continue
    for ev in msg["events"]:
        ui.log_event(ev)
    with st.chat_message("assistant"):
        latest = i == last and not prompt
        ui.render_turn(msg["events"], turn=i, latest=latest, on_follow_up=queue if latest else None)

if prompt:
    if demo:
        st.session_state.demo_used = used + 1
    messages.append({"role": "user", "content": prompt})
    user_message(prompt)
    with st.chat_message("assistant"):
        try:
            stream = A.ask_stream(get_agent(spec, demo), st.session_state.user, st.session_state.session, prompt,
                                  requested=spec)
        except Exception as e:  # building the model failed (bad provider, missing credentials)
            stream = iter([M.explain_error(e)])
        events = ui.render_turn(ui.logged(stream), turn=len(messages), live=True, on_follow_up=queue)
    messages.append({"role": "assistant", "events": events})
    st.rerun()  # redraw from the stored turn, so the sidebar lists this chat
