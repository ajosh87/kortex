"""The agent: an LLM with graph tools, plus memory that lives in the same FalkorDB graph.

ask_stream() is the pipeline the UI renders. It yields plain dict events, in order, each stamped with `at`
(epoch ms):
  question                the question asked
  resumed                 earlier turns of this chat restored from graph memory (only when the agent had not seen them)
  model                   one finished model call: model, time, tokens, cost, and the tools it asked for next
  step_start / step_end   one per tool call, with the step record (result, tables, Cypher, timings, nodes, edges)
  phase                   what the agent is doing: "thinking" (model), "tools" (graph) or "writing" (answer)
  token / text_reset      the answer as it is written (reset if that text turned out to precede a tool call)
  answer                  the final answer, cleaned and with GitHub links for everything the graph returned
  memory                  the turn saved in the graph: how many nodes it was linked to, and the Cypher that did it
  error                   a failed turn, explained (title, message) plus the raw error
  done                    totals: models used, LLM vs graph time, tokens, estimated cost, the answer's graph,
                          memory saved, follow-up questions, and names in the answer no tool returned
Events that ran Cypher (step_end, resumed, memory) carry it as `queries`.

    uv run agent.py "Who must approve a change to pkg/kubelet/eviction?"
"""
import os
import sys
import time
import uuid

from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

import answer as ANS
import memory_graph as MG
import models as M
import queries as Q
from db import traced
from queries import node
from tools import all_tools, combine_graph, describe, graph_tool, step, table

PROMPT = """You are the "company brain" for the Kubernetes project, treated as a company:
SIGs are departments, OWNERS files say who owns which code, KEPs are design decisions, merged PRs are work history.

Rules:
- Answer only from tool results. Every claim must come from the graph; never invent people, folders or KEPs.
  Do not add background from general knowledge. If the graph lacks a detail, say "not in the graph".
- Approval rule (Kubernetes OWNERS): for each changed file, one /approve from anyone in the approvers of its
  nearest OWNERS file is enough (not all of them); approvers in parent OWNERS files also count unless inheritance
  is cut (no_parent_owners). A change spanning several folders needs an approver for each. Merging also needs a
  /lgtm from a reviewer or approver.
- When asked to write, draft or create a review plan, call write_review_plan once and present its result.
- Pick the tool that matches the question:
  who should review a change -> best_reviewers; trace a design decision to code and approvers -> decision_to_approvers;
  most critical / riskiest code -> critical_code; what changed recently -> recent_changes;
  a SIG's or working group's mission, scope, meetings, annual report, goals, or DevStats -> search_docs, then
  read_doc on the best match (cite it by its path, e.g. `sig-node/annual-report-2024.md`).
- For "what changed since last time": call recall_memory, then recent_changes with `since` set to that turn's date.
  Changes are only known up to when the data was loaded; say so when it matters.
- Cite sources inline: GitHub handles as @handle (people only), folders as `path`, design decisions as KEP-<number>.
  SIGs and teams are plain names (sig-node), never @sig-node. [name] in a document is a removed personal name:
  never guess who it was.
  Never use 【】 or tool-name citation markers.
- When the question is about earlier conversations or "last time", call recall_memory first with a short
  topic such as a folder path or one or two keywords (e.g. "pkg/kubelet" or "eviction").
- If a name is vague, call search_graph to find the exact folder, KEP or SIG before other tools.
- Use the specific tools whenever one fits (ownership, bus factor, KEPs, impact, experts, connections).
  Use run_cypher only for questions none of them covers. If a run_cypher result disagrees with a specific
  tool, trust the specific tool: it encodes the Kubernetes rules (inherited OWNERS, teams expanded to people).
- Never say a number is complete when a tool note says its list was cut; use the counts in the tool's summary.
- Be concise: short sections or bullets, most important answer first.

What the graph covers (say so when an answer depends on it):
{scope}
"""


def memory_tool(user):
    @graph_tool
    def recall_memory(about: str = ""):
        """Earlier questions and answers from this user, across sessions. Pass a folder path, KEP number,
        GitHub handle or SIG to get only exchanges that touched it (folders also match their subfolders)."""
        rows = MG.recall(user, about.strip() or None, 5)
        mentioned = [node(*ref.split(":", 1)) for r in rows for ref in r["refs"] if ":" in ref]  # what those turns used
        return step([{k: v for k, v in r.items() if k != "refs"} | {"answer": r["answer"][:1500]} for r in rows],
                    f"{len(rows)} earlier turns found" if rows else "No earlier turns found",
                    nodes=mentioned,
                    tables=[table("Earlier turns", [{"When": Q.when(r["at"]), "Question": r["question"]} for r in rows])])
    return recall_memory


def build(user, spec=M.MODEL, api_key=None):
    return create_agent(M.make_model(spec, api_key), all_tools() + [memory_tool(user)],
                        system_prompt=PROMPT.format(scope=Q.data_scope()), checkpointer=InMemorySaver())


def transcript(user, session):
    """A remembered chat as chat messages, to reopen it in the chat window. Each answer comes back as a
    `recalled` event (the full step records were not stored); the Cypher that read it is on the first one."""
    turns, queries = traced(MG.session_turns, user, session)
    return [m for i, t in enumerate(turns) for m in (
        {"role": "user", "content": t["question"]},
        {"role": "assistant", "events": [{"type": "recalled", "at": t["at"], "when": t["when"], "text": t["answer"],
                                          "linked": len(t["refs"]), "queries": queries if i == 0 else []}]})]


def earlier_turns(agent, user, session):
    """Turns of this chat the agent has not seen (it was rebuilt or the app restarted), as model messages."""
    if agent.get_state({"configurable": {"thread_id": session}}).values.get("messages"):
        return [], []
    turns, queries = traced(MG.session_turns, user, session)
    return [m for t in turns for m in ({"role": "user", "content": t["question"]},
                                       {"role": "assistant", "content": t["answer"]})], queries


def memory(user, session):
    """One remembered session for the Memory page: its turns and a graph of what each turn was linked to."""
    turns = MG.session_turns(user, session)
    nodes, edges = {}, []
    for i, t in enumerate(turns, 1):
        turn = node("Turn", f"{session}:{i}", label=f"Q{i}", title=t["question"])
        nodes[turn["id"]] = turn
        for ref in t["refs"]:
            nodes[ref["id"]] = ref
            edges.append({"src": turn["id"], "rel": "mentions", "dst": ref["id"]})
    graph = combine_graph([{"nodes": list(nodes.values()), "edges": edges}], limit=45)
    return {"turns": [{**t, "n": i, "linked": len(t["refs"])} for i, t in enumerate(turns, 1)], "graph": graph}


# ---------------- one turn, as events ----------------

# The step record of a tool call that returned no artifact (e.g. the model asked for a tool that does not exist).
NO_ARTIFACT = {"ms": 0, "queries": [], "graph_ms": 0, "nodes": [], "edges": [], "tables": [], "found": 0, "file": None}


def model_text(chunk, meta):
    """Answer text in a streamed chunk; empty for chunks that belong to a tool call."""
    if meta.get("langgraph_node") != "model" or getattr(chunk, "tool_call_chunks", None):
        return ""
    return chunk.text if isinstance(chunk.text, str) else str(chunk.content or "")


def model_call(m, ms, requested):
    """Accounting for one finished model call: time, model that served it, tokens, estimated cost."""
    u = getattr(m, "usage_metadata", None) or {}
    served = M.unrepeat(m.response_metadata.get("model_name"))
    return {"ms": ms, "model": served, "tokens_in": u.get("input_tokens"), "tokens_out": u.get("output_tokens"),
            "cost": M.estimate_cost(requested, served, u.get("input_tokens"), u.get("output_tokens"))}


def step_start(call, think_ms):
    return {"id": call["id"], "tool": call["name"], "args": call["args"], "title": describe(call["name"], call["args"]),
            "think_ms": think_ms}


def step_end(m, started):
    s = started.get(m.tool_call_id) or step_start({"id": m.tool_call_id, "name": m.name, "args": {}}, 0)
    art = m.artifact or {**NO_ARTIFACT, "result": str(m.content)[:300], "error": str(m.content)[:300]}
    failed = bool(art.get("error")) or getattr(m, "status", "") == "error"
    return {**s, **art, "status": "error" if failed else "complete"}


def totals(calls, steps, requested):
    """The numbers behind a turn, for the done event."""
    name = M.split_spec(requested)[1]
    models = list(dict.fromkeys(c["model"] for c in calls if c["model"]))
    tokens = bool(calls) and all(c["tokens_in"] is not None for c in calls)
    priced = bool(calls) and all(c["cost"] is not None for c in calls)
    return {"requested": name, "served": models, "fallback": any(m != name for m in models),
            "llm_ms": sum(c["ms"] for c in calls), "llm_calls": len(calls),
            "graph_ms": round(sum(s["graph_ms"] for s in steps), 1), "queries": sum(len(s["queries"]) for s in steps),
            "steps": len(steps),
            "tokens_in": sum(c["tokens_in"] for c in calls) if tokens else None,
            "tokens_out": sum(c["tokens_out"] or 0 for c in calls) if tokens else None,
            "cost_estimate": round(sum(c["cost"] for c in calls), 6) if priced else None}


def messages(update):
    """The finished messages in an 'updates' stream item: {node name: {"messages": [...]}}."""
    return [m for u in (update or {}).values() for m in (u or {}).get("messages", [])]


def tool_call_events(m, started, think_ms):
    """A model call that asked for tools: one step_start per call, then the 'querying the graph' phase."""
    for c in m.tool_calls:
        started[c["id"]] = step_start(c, think_ms)
    return [{"type": "step_start", **started[c["id"]]} for c in m.tool_calls] + [{"type": "phase", "phase": "tools"}]


def ask_stream(agent, user, session, question, requested=M.MODEL):
    for ev in _turn(agent, user, session, question, requested):
        yield {"at": round(time.time() * 1000), **ev}


def _turn(agent, user, session, question, requested):
    t0 = mark = time.time()
    steps, started, calls, text, feed = [], {}, [], "", ANS.stream_cleaner()
    yield {"type": "question", "text": question}
    try:
        history, queries = earlier_turns(agent, user, session)
        if history:
            yield {"type": "resumed", "turns": len(history) // 2, "queries": queries}
        yield {"type": "phase", "phase": "thinking"}
        for mode, data in agent.stream({"messages": history + [{"role": "user", "content": question}]},
                                       {"configurable": {"thread_id": session}, "recursion_limit": 30},
                                       stream_mode=["messages", "updates"]):
            if mode == "messages":  # answer text as it is written
                if piece := feed(model_text(*data)):
                    yield from ([] if text else [{"type": "phase", "phase": "writing"}])
                    text += piece
                    yield {"type": "token", "text": piece}
                continue
            for m in messages(data):
                if m.type == "tool":  # a tool finished: its step record
                    steps.append(step_end(m, started))
                    yield {"type": "step_end", **steps[-1]}
                    continue
                calls.append(model_call(m, round((time.time() - mark) * 1000), requested))  # a model call finished
                mark = time.time()
                yield {"type": "model", **calls[-1], "next": [c["name"] for c in m.tool_calls]}
                if not m.tool_calls:  # the final answer
                    text = m.text if isinstance(m.text, str) else str(m.content)
                    continue
                if text:  # text streamed before a tool call was not the answer
                    text, feed = "", ANS.stream_cleaner()
                    yield {"type": "text_reset"}
                yield from tool_call_events(m, started, calls[-1]["ms"])
            if "tools" in (data or {}):
                mark = time.time()
                yield {"type": "phase", "phase": "thinking"}
        if tail := feed("", final=True):
            yield {"type": "token", "text": tail}
    except Exception as e:
        yield M.explain_error(e)
        return
    refs = [n for s in steps for n in s["nodes"]]
    final = ANS.linkify(ANS.clean(text).strip(), refs)
    yield {"type": "answer", "text": final}
    graph = combine_graph(steps)
    linked, queries = traced(MG.remember, user, session, question, final, graph["nodes"] or refs[:30])
    yield {"type": "memory", "linked": linked, "queries": queries}
    yield {"type": "done", **totals(calls, steps, requested), "total_ms": round((time.time() - t0) * 1000),
           "graph": graph, "memory_linked": linked, "follow_ups": ANS.follow_ups(steps),
           "unverified": ANS.unverified(final, refs)}


def ask(agent, user, session, question, requested=M.MODEL):
    """Run one turn to completion (CLI, tests). Returns the answer plus the full event log."""
    events = list(ask_stream(agent, user, session, question, requested))
    first = lambda kind: next((e for e in events if e["type"] == kind), None)
    return {"answer": (first("answer") or {}).get("text", ""), "error": first("error"), "done": first("done"),
            "events": events, "steps": [e for e in events if e["type"] == "step_end"]}


if __name__ == "__main__":
    user = os.getenv("USER_NAME", "demo")
    agent, session = build(user), str(uuid.uuid4())[:8]
    r = ask(agent, user, session, " ".join(sys.argv[1:]) or "Who must approve a change to pkg/kubelet/eviction?")
    if r["error"]:
        print(f"{r['error']['title']}: {r['error']['message']}\n{r['error']['raw'][:500]}")
        sys.exit(1)
    print(r["answer"], "\n")
    for s in r["steps"]:
        print(f"  [{s['status']}] {s['title']} -> {s['result']} ({len(s['queries'])} queries, {s['graph_ms']} ms)")
    d = r["done"]
    print(f"\n  {', '.join(d['served'])}{' (fallback)' if d['fallback'] else ''} · LLM {d['llm_ms'] / 1000:.1f}s "
          f"({d['llm_calls']} calls) · graph {d['graph_ms']} ms ({d['queries']} queries) · tokens "
          f"{d['tokens_in']}/{d['tokens_out']} · ≈${d['cost_estimate']} · memory linked {d['memory_linked']} nodes")
