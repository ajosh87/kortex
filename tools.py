"""Agent tools: thin wrappers around queries.py.

Every tool returns two things (LangChain "content_and_artifact"):
  - content:  JSON the model reads
  - artifact: a step record for people: what was done, the result in one line, titled tables of what was
              found (with GitHub links), the exact Cypher it ran (rows, ms, params), and the typed nodes and
              edges it touched (for the graph picture and for memory)

The same tools also power the Explore page directly, without an LLM (see ACTIONS and explore()).
"""
import functools
import json
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from langchain_core.tools import tool

import memory_graph as MG
import queries as Q
from db import TRACE, ro
from queries import node

OUT = Path(__file__).parent / "out"
MAX_GRAPH_NODES = 30  # graph pictures are capped; the cap is reported, never silent

# What each tool is doing, in words, from its arguments. Shown while the step runs and afterwards.
TITLES = {
    "search_graph": "Searching the graph for “{text}”",
    "who_owns": "Finding who can approve `{path}`",
    "design_constraints": "Finding design decisions (KEPs) behind `{path}`",
    "blast_radius": "Following every import chain into `{path}`",
    "find_experts": "Finding people who recently changed `{path}`",
    "kep_details": "Reading KEP-{number}",
    "decision_trail": "Following every decision linked to KEP-{number}",
    "single_points_of_failure": "Checking approver coverage in {sig}",
    "person_profile": "Looking up @{handle}",
    "how_connected": "Finding how {a_key} connects to {b_key}",
    "run_cypher": "Running a custom read-only Cypher query",
    "write_review_plan": "Writing a review plan for `{path}`",
    "recent_changes": "Finding what changed in `{path}` since {since}",
    "critical_code": "Ranking the most depended-on code under `{scope}` (PageRank, betweenness)",
    "decision_to_approvers": "Tracing KEP-{number} to the code it changes and who must approve it",
    "best_reviewers": "Finding who can approve `{path}` and changed it recently",
    "recall_memory": "Recalling earlier conversations about “{about}”",
}
DEFAULTS = {"about": "anything", "since": "30 days ago", "scope": "pkg"}


def _kep_num(s):
    return str(s).upper().replace("KEP-", "").replace("KEP", "").lstrip("#").strip()


def _arg(k, v):
    v = str(v).strip()
    if k == "number":
        return _kep_num(v)
    if k in ("handle", "a_key", "b_key"):
        return v.lstrip("@")
    return v or DEFAULTS.get(k, "")


def describe(name, args):
    """One line saying what a tool call does, e.g. 'Finding who can approve `pkg/kubelet`'."""
    try:
        return TITLES[name].format(**{**DEFAULTS, **{k: _arg(k, v) for k, v in args.items()}})
    except (KeyError, IndexError):
        return f"Calling unknown tool `{name}`"


# ---------------- step records ----------------

def table(title, rows, links=None):
    """A titled table for people. `links` maps a column to the node kind its values are; those cells hold
    the node's URL and the UI shows the key extracted by Q.LINK_TEXT."""
    return {"title": title, "rows": rows, "links": {col: Q.LINK_TEXT[kind] for col, kind in (links or {}).items()}}


def link(kind, key):
    return Q.url(kind, key) or key


def step(data, result, edges=(), nodes=(), tables=(), file=None):
    """What a tool body returns: data for the model plus the human-facing parts of the step record.
    edges are (node, relationship, node) triples; nodes are extra refs (known, but not drawn)."""
    return {"data": data, "result": result, "edges": list(edges), "nodes": list(nodes), "tables": list(tables), "file": file}


MODEL_CHARS = 12000  # how much of a tool's data the model reads


def for_model(summary, data, limit=MODEL_CHARS):
    """What the model reads from a tool: the one-line summary (its counts cover everything found) and the data.
    If the data is too long, lists are cut to their first items, and the note says so: never a JSON string cut
    mid-way, which leaves the model counting a partial list."""
    def cut(value, keep):
        if isinstance(value, list):
            return value[:keep]
        return {k: v[:keep] if isinstance(v, list) else v for k, v in value.items()} if isinstance(value, dict) else value
    text = json.dumps({"summary": summary, "data": data}, default=str)
    for keep in (60, 30, 15, 8, 4):
        if len(text) <= limit:
            break
        text = json.dumps({"summary": summary, "note": f"lists cut to their first {keep} items; the summary counts all of them",
                           "data": cut(data, keep)}, default=str)
    return text if len(text) <= limit else json.dumps({"summary": summary, "note": "data too long to include"})


def record(fn, *args, **kwargs):
    """Run a tool body, capturing its own Cypher and timing. Returns (content for the model, step record)."""
    token, t = TRACE.set([]), time.time()
    try:
        out, error = fn(*args, **kwargs), None
    except Exception as e:  # report to the model and to the timeline instead of crashing the turn
        out, error = step({"error": str(e)}, f"Failed: {e}"), str(e)
    finally:
        trace = TRACE.get()
        TRACE.reset(token)
    refs = {n["id"]: n for a, _, b in out["edges"] for n in (a, b)} | {n["id"]: n for n in out["nodes"]}
    artifact = {
        "result": out["result"], "error": error, "ms": round((time.time() - t) * 1000),
        "queries": trace, "graph_ms": round(sum(x["ms"] for x in trace), 1),
        "nodes": list(refs.values()), "edges": [{"src": a["id"], "rel": r, "dst": b["id"]} for a, r, b in out["edges"]],
        "tables": [x for x in out["tables"] if x["rows"]], "file": out["file"],
    }
    artifact["found"] = sum(len(x["rows"]) for x in artifact["tables"])
    return for_model(out["result"], out["data"]), artifact


def graph_tool(fn):
    """Turn a function returning step(...) into a LangChain tool whose artifact is the step record."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return record(fn, *args, **kwargs)
    run.body = fn
    return tool(response_format="content_and_artifact")(run)


def combine_graph(steps, limit=MAX_GRAPH_NODES):
    """Merge steps' edges into one picture, capped at `limit` nodes in the order the tools ran."""
    nodes = {n["id"]: n for s in steps for n in s["nodes"]}
    shown, edges, seen = {}, [], set()
    for e in (e for s in steps for e in s["edges"]):
        key = (e["src"], e["rel"], e["dst"])
        if key in seen or e["src"] not in nodes or e["dst"] not in nodes:
            continue
        new = [i for i in (e["src"], e["dst"]) if i not in shown]
        if len(shown) + len(new) > limit:
            continue
        seen.add(key)
        edges.append(e)
        shown |= {i: nodes[i] for i in new}
    linked = {i for e in (e for s in steps for e in s["edges"]) for i in (e["src"], e["dst"])}
    return {"nodes": list(shown.values()), "edges": edges, "total_nodes": len(linked), "shown_nodes": len(shown)}


def _people(row):
    """An OWNERS entry is a person or a team; teams expand to their members."""
    return row["team_members"] or [row["owner"]]


def _nearest(rows):
    level = min((r["levels_up"] for r in rows), default=0)
    return [r for r in rows if r["levels_up"] == level]


def _owner_rows(rows):
    return [{"Person": link("Person", p), "Via": r["owner"] if r["kind"] == "Team" else "named directly",
             "Granted by": link("Dir", r["granted_by"])} for r in rows for p in _people(r)]


# ---------------- tools ----------------

@graph_tool
def search_graph(text: str):
    """Find folders, KEPs (design proposals), feature gates, SIGs (teams) or people by a name fragment.
    Use this first when the user names something vaguely, e.g. 'eviction', 'sidecar', 'scheduler'."""
    rows = Q.search(text, 15)
    refs = [node(r["kind"], r["key"], title=r["label"]) for r in rows]
    top = ", ".join(f"{n['label']} ({n['kind_label']})" for n in refs[:4])
    return step(rows, f"{len(rows)} matches" + (f": {top}" + (" …" if len(rows) > 4 else "") if rows else ""), nodes=refs,
                tables=[table("Matches", [{"Kind": n["kind_label"], "Name": n["label"], "Title": n["title"] if n["title"] != n["label"] else ""}
                                          for n in refs])])


@graph_tool
def who_owns(path: str):
    """Who can approve and who reviews changes to a file or folder, which OWNERS file grants it
    (ownership is inherited down the folder tree), and which SIG owns it with its leads.
    Kubernetes rule: ONE approval from any person in the nearest approvers list is enough; approvers
    higher up the tree (e.g. pkg/, root) can also approve but are a fallback."""
    d = Q.nearest_dir(path)
    approvers, reviewers, sigs = Q.owners(d, "APPROVER"), Q.owners(d, "REVIEWER"), Q.owning_sigs(d)
    leads = [Q.sig_leads(s)[0] for s in {s for r in sigs[:1] for s in r["sigs"]}]
    near = _nearest(approvers)
    fallback = [r for r in approvers if r not in near]
    folder = node("Dir", d)
    edges = []
    for r in near:
        owners_dir = node("Dir", r["granted_by"])
        if r["granted_by"] != d:
            edges.append((folder, f"inherits from ({r['levels_up']} up)", owners_dir))
        owner = node(r["kind"], r["owner"])
        edges.append((owners_dir, "approver", owner))
        edges += [(owner, "member", node("Person", m)) for m in r["team_members"]]
    edges += [(folder, "owned by", node("SIG", s)) for r in sigs[:1] for s in dict.fromkeys(r["sigs"])]
    people = sorted({p for r in near for p in _people(r)})
    where = near[0]["granted_by"] if near else "none"
    up = near[0]["levels_up"] if near else 0
    result = (f"Nearest OWNERS: `{where}` ({up} level{'s' if up != 1 else ''} up) → {len(people)} approvers"
              + (f" · owning SIG: {', '.join(dict.fromkeys(sigs[0]['sigs']))}" if sigs else "")
              + (f" · {len({r['owner'] for r in fallback})} fallback approvers higher up" if fallback else ""))
    return step({"folder": d,
                 "approval_comes_from": f"{where}/OWNERS" + (f" (inherited, {up} level{'s' if up != 1 else ''} above {d})" if up else ""),
                 "nearest_approvers": near, "fallback_approvers_up_the_tree": fallback[:20],
                 "reviewers": reviewers[:15], "owning_sig": sigs[:1], "sig_leads": leads}, result, edges,
                [node("Person", r["owner"]) for r in fallback if r["kind"] == "Person"]
                + [node("Person", p) for r in reviewers for p in _people(r)],
                tables=[table("Can approve (one approval is enough)", _owner_rows(near), {"Person": "Person", "Granted by": "Dir"}),
                        table("Reviewers", _owner_rows(_nearest(reviewers)), {"Person": "Person", "Granted by": "Dir"}),
                        table("Fallback approvers higher up the tree", _owner_rows(fallback), {"Person": "Person", "Granted by": "Dir"})])


@graph_tool
def design_constraints(path: str):
    """Design decisions (KEPs) that constrain a folder: feature gates checked in that code, the KEPs
    that introduced them, their status/stage, and related KEPs. Answers 'why does this code work this way?'"""
    d = Q.nearest_dir(path)
    rows = Q.constraints(d)
    edges = []
    for r in rows:
        k = node("KEP", r["kep"], title=r["title"])
        for g in r["gates"]:
            edges += [(node("Dir", c), "checks gate", node("FeatureGate", g)) for c in r["checked_in"][:2]]
            edges.append((k, "introduced", node("FeatureGate", g)))
        edges += [(k, "owned by", node("SIG", r["owning_sig"]))] if r["owning_sig"] else []
        edges += [(k, "related", node("KEP", rel.split(" ", 1)[0], title=rel.split(" ", 1)[-1])) for rel in r["related"]]
    gates = {g for r in rows for g in r["gates"]}
    return step({"folder": d, "keps": rows}, f"{len(rows)} KEPs via {len(gates)} feature gates checked in this code"
                if rows else "No feature gates from known KEPs are checked here", edges, [node("Dir", d)],
                tables=[table("Design decisions", [{"KEP": link("KEP", r["kep"]), "Title": r["title"], "Status": r["status"],
                                                    "Stage": r["stage"], "Milestone": r["milestone"], "Feature gates": ", ".join(r["gates"]),
                                                    "Owning SIG": r["owning_sig"] or ""} for r in rows], {"KEP": "KEP"})])


def by_hop(rows, what):
    """How far a walk reached, in words: '27 directly, 14 at 2 hops and 4 at 3 hops; no chain goes further'."""
    counts = Counter(r["hops"] for r in rows)
    parts = [f"{n} {what}" if h == 1 else f"{n} at {h} hops" for h, n in sorted(counts.items())]
    return (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + "; no chain goes further"


@graph_tool
def blast_radius(path: str):
    """Code that depends on a folder through Go imports, directly or through a chain of imports (every chain is
    followed to its end), and which binaries (kubelet, kube-scheduler, ...) that code ends up in. Answers
    'what could break?'"""
    d = Q.nearest_dir(path)
    rows = Q.blast_radius(d)
    edges = [(node("Dir", r["dependent"]), "imports", node("Dir", r["via"])) for r in rows]  # each one's shortest chain
    bins = sorted({c for r in rows for c in r["component"]})
    result = (f"{len(rows)} packages depend on it: {by_hop(rows, 'import it directly')}"
              + (f" · ends up in: {', '.join(bins)}" if bins else "")) if rows else "Nothing imports it"
    result += f" (imports are loaded only for {', '.join(Q.code_scope())})"
    return step({"folder": d, "dependents": rows}, result, edges, [node("Dir", d)],
                tables=[table("Depends on it", [{"Package": link("Dir", r["dependent"]), "Hops": r["hops"],
                                                 "Imports": r["via"], "Binary": ", ".join(r["component"])} for r in rows],
                              {"Package": "Dir"})])


@graph_tool
def find_experts(path: str):
    """People who recently merged pull requests touching a folder or anything below it."""
    d = Q.nearest_dir(path)
    rows = Q.experts(d, 10)
    folder = node("Dir", d)
    edges = []
    for r in rows[:6]:
        pr = node("PR", r["examples"][0])
        edges += [(node("Person", r["person"]), f"authored ({r['prs']} PRs here)", pr), (pr, "touches", folder)]
    return step({"folder": d, "experts": rows}, f"{len(rows)} people merged changes here" + (
        f", most active: @{rows[0]['person']} ({rows[0]['prs']} PRs)" if rows else ""), edges,
        [folder] + [node("Person", r["person"]) for r in rows] + [node("PR", n) for r in rows for n in r["examples"]],
        tables=[table("Recent contributors", [{"Person": link("Person", r["person"]), "Merged PRs": r["prs"],
                                               "Last merged": str(r["last"])[:10], "Example PR": link("PR", r["examples"][0])}
                                              for r in rows], {"Person": "Person", "Example PR": "PR"})])


@graph_tool
def kep_details(number: str):
    """Full record of one KEP (Kubernetes Enhancement Proposal = a design decision): summary, status, stage,
    milestone, owning SIG, authors, approvers, feature gates, affected binaries and the code that checks it."""
    rows = Q.kep(_kep_num(number))
    if not rows:
        return step([], f"KEP-{_kep_num(number)} is not in the graph")
    r = rows[0]
    k = node("KEP", r["kep"], title=r["title"])
    edges = [(k, "owned by", node("SIG", r["owning_sig"]))] if r["owning_sig"] else []
    edges += [(node("Person", a), "authored", k) for a in r["authors"]]
    edges += [(node("Person", a), "approved", k) for a in r["approvers"]]
    edges += [(k, "introduced", node("FeatureGate", g)) for g in r["gates"]]
    edges += [(node("Dir", c), "checks gate", node("FeatureGate", g)) for g in r["gates"][:1] for c in r["code"][:4]]
    fields = [("Summary", r["summary"]), ("Status", r["status"]), ("Stage", r["stage"]), ("Latest milestone", r["milestone"]),
              ("Created", r["created"]), ("Updated", r["updated"]), ("Owning SIG", r["owning_sig"]),
              ("Participating SIGs", ", ".join(r["participating_sigs"])), ("Authors", ", ".join(f"@{a}" for a in r["authors"])),
              ("Approvers", ", ".join(f"@{a}" for a in r["approvers"])), ("Feature gates", ", ".join(r["gates"])),
              ("Binaries", ", ".join(r["components"])), ("Checked in code", ", ".join(r["code"])), ("Source", r["source"])]
    return step(rows, f"KEP-{r['kep']} “{r['title']}” · {r['status']} · {r['stage']} ({r['milestone'] or 'no milestone'})"
                f" · owned by {r['owning_sig'] or 'no known SIG'}", edges,
                tables=[table("Record", [{"Field": f, "Value": v} for f, v in fields if v])])


@graph_tool
def decision_trail(number: str):
    """KEPs linked to a KEP through 'see also' and 'replaces' links, in either direction, followed until nothing
    new is reached. Use it to trace how a decision builds on or supersedes earlier ones."""
    n = _kep_num(number)
    rows = Q.decision_trail(n)
    titles = {r["kep"]: r["title"] for r in rows}
    edges = [(node("KEP", a, title=titles.get(a)), rel.lower().replace("_", " "), node("KEP", b, title=titles.get(b)))
             for a, rel, b in (r["link"] for r in rows)]
    return step(rows, f"{len(rows)} linked decisions: {by_hop(rows, 'directly')}" if rows else "No linked KEPs", edges,
                [node("KEP", n)],
                tables=[table("Linked decisions", [{"KEP": link("KEP", r["kep"]), "Title": r["title"], "Status": r["status"],
                                                    "Hops": r["hops"]} for r in rows], {"KEP": "KEP"})])


@graph_tool
def single_points_of_failure(sig: str):
    """For a SIG (e.g. 'sig-node'): folders with the fewest named approvers, and the people who approve
    the most folders. Answers bus-factor questions like 'who would we miss most?'"""
    sig = sig if sig.startswith("sig-") else f"sig-{sig}"
    thin, busy = Q.single_points_of_failure(sig, 10), Q.busiest_approvers(sig, 10)
    s = node("SIG", sig)
    single = [r for r in thin if r["approver_count"] == 1]
    edges = [(node("Dir", r["dir"]), "only approver", node("Person", r["approvers"][0])) for r in single]
    edges += [(node("Person", b["person"]), f"approves {b['dirs']} folders of", s) for b in busy[:8]]
    result = (f"{len(single)} folder{'s' if len(single) != 1 else ''} with a single approver"
              + (f" · thinnest has {thin[0]['approver_count']}" if thin and not single else "")
              + (f" · busiest approver: @{busy[0]['person']} ({busy[0]['dirs']} folders)" if busy else ""))
    return step({"sig": sig, "folders_with_a_single_approver": [r["dir"] for r in single],
                 "note": "approver counts include every member of approver teams",
                 "thinnest_folders": thin, "busiest_approvers": busy}, result, edges,
                [s] + [node("Person", b["person"]) for b in busy] + [node("Dir", r["dir"]) for r in thin]
                + [node("Person", p) for r in thin for p in r["approvers"]],
                tables=[table("People who approve the most folders", [{"Person": link("Person", b["person"]), "Folders": b["dirs"]}
                                                                      for b in busy], {"Person": "Person"}),
                        table("Folders with the fewest approvers", [{"Folder": link("Dir", r["dir"]), "Approvers": r["approver_count"],
                                                                     "Who": ", ".join(f"@{p}" for p in r["approvers"])} for r in thin],
                              {"Folder": "Dir"})])


@graph_tool
def person_profile(handle: str):
    """Everything a GitHub handle is connected to: teams, SIG leadership, folders they approve, KEPs, PR count."""
    rows = Q.person(handle)
    if not rows:
        return step([], f"@{handle} is not in the graph")
    r = rows[0]
    p = node("Person", r["person"])
    edges = [(p, "member of", node("Team", t)) for t in r["teams"]]
    edges += [(p, "leads", node("SIG", s)) for s in r["leads_sigs"]]
    edges += [(p, "approves", node("Dir", d)) for d in r["approves_dirs"][:6]]
    edges += [(p, "authored", node("KEP", k.split(" ", 1)[0], title=k.split(" ", 1)[-1])) for k in r["authored_keps"][:6]]
    return step(rows, f"@{r['person']}: {len(r['teams'])} teams · leads {len(r['leads_sigs'])} SIGs · approves "
                f"{r['approves_count']} folders · {r['kep_count']} KEPs · {r['merged_prs']} merged PRs", edges,
                [p] + [node("Dir", d) for d in r["approves_dirs"]]
                + [node("KEP", k.split(" ", 1)[0], title=k.split(" ", 1)[-1]) for k in r["authored_keps"]],
                tables=[table("Approves", [{"Folder": link("Dir", d)} for d in r["approves_dirs"]], {"Folder": "Dir"}),
                        table("Authored KEPs", [{"KEP": link("KEP", k.split(" ", 1)[0]), "Title": k.split(" ", 1)[-1]}
                                                for k in r["authored_keps"]], {"KEP": "KEP"}),
                        table("Teams and SIGs", [{"Membership": t} for t in r["teams"]] + [{"Membership": f"leads {s}"} for s in r["leads_sigs"]])])


@graph_tool
def how_connected(a_kind: str, a_key: str, b_kind: str, b_key: str):
    """Shortest chain of relationships between two things. Kinds: Person (key = GitHub handle),
    Dir (key = folder path), KEP (key = number), SIG (key = e.g. sig-node), FeatureGate, Team."""
    a_key = Q.nearest_dir(a_key) if a_kind == "Dir" else a_key.lstrip("@").lower() if a_kind == "Person" else a_key
    b_key = Q.nearest_dir(b_key) if b_kind == "Dir" else b_key.lstrip("@").lower() if b_kind == "Person" else b_key
    a_key, b_key = (_kep_num(a_key) if a_kind == "KEP" else a_key), (_kep_num(b_key) if b_kind == "KEP" else b_key)
    rows = Q.connection(a_kind, a_key, b_kind, b_key)
    if not rows:
        return step([], "No connection within 5 hops", nodes=[node(a_kind, a_key), node(b_kind, b_key)])
    best = rows[0]
    refs = {n["id"]: node(n["kind"], n["key"]) for n in best["nodes"]}
    edges = [(refs[s], rel.lower().replace("_", " "), refs[e]) for s, rel, e in best["rels"]]
    chain = " → ".join(refs[n["id"]]["label"] for n in best["nodes"])
    return step([{"path": [f"{n['kind']}:{n['key']}" for n in r["nodes"]], "relationships": [x[1] for x in r["rels"]]}
                 for r in rows], f"{len(best['rels'])} hops: {chain}", edges,
                tables=[table("Chain", [{"From": refs[s]["label"], "Relationship": rel.lower().replace("_", " "), "To": refs[e]["label"]}
                                        for s, rel, e in best["rels"]])])


@graph_tool
def recent_changes(path: str, since: str = ""):
    """What changed recently in a folder (or anything below it): merged pull requests, and design decisions (KEPs)
    behind that code that were updated. `since` is a date (YYYY-MM-DD); empty means the last 30 days. For
    'what changed since last time', pass the date of the earlier turn from recall_memory. Only covers changes
    up to when the data was loaded."""
    d = Q.nearest_dir(path)
    since = since.strip()[:10] or (date.today() - timedelta(days=30)).isoformat()
    prs, keps = Q.recent_prs(d, since), Q.recent_keps(d, since)
    folder = node("Dir", d)
    edges = [(node("Person", r["author"]), "authored", node("PR", r["pr"], title=r["title"])) for r in prs if r["author"]]
    edges += [(node("PR", r["pr"], title=r["title"]), f"touches ({r['merged']})", folder) for r in prs]
    edges += [(node("KEP", k["kep"], title=k["title"]), f"updated {k['updated']}", folder) for k in keps]
    loaded = Q.stats()["source"].get("loaded_at", "?")
    return step({"folder": d, "since": since, "data_loaded_at": loaded, "merged_prs": prs, "updated_keps": keps},
                f"Since {since}: {len(prs)} merged PRs and {len(keps)} updated KEPs (data as of {loaded})", edges, [folder],
                tables=[table("Merged pull requests", [{"PR": link("PR", r["pr"]), "Title": r["title"], "Merged": r["merged"],
                                                        "Author": link("Person", r["author"]) if r["author"] else ""} for r in prs],
                              {"PR": "PR", "Author": "Person"}),
                        table("Design decisions updated", [{"KEP": link("KEP", k["kep"]), "Title": k["title"], "Updated": k["updated"],
                                                            "Stage": k["stage"]} for k in keps], {"KEP": "KEP"})])


@graph_tool
def critical_code(scope: str = "pkg"):
    """The riskiest code under a folder: packages the most other code depends on (FalkorDB PageRank over Go
    imports) and packages that sit on the most import paths (betweenness centrality), with how many people can
    approve each. Critical code with one or two approvers is the risk. Answers 'what code is most critical?'"""
    scope = Q.nearest_dir(scope) if scope.strip().strip("/") else "pkg"
    scope = "" if scope == "/" else scope  # unknown folder: rank everything
    ranked, choke = Q.critical_code(scope), Q.chokepoints(scope)
    thin = [r for r in ranked if r["approver_count"] <= 2]
    edges = [(node("Dir", r["package"]), f"approved by {r['approver_count']} · {r['importers']} importers", node("Dir", r["owners_path"]))
             for r in ranked if r["owners_path"] != r["package"]]
    edges += [(node("Dir", r["package"]), "only approver" if r["approver_count"] == 1 else "approver", node("Person", p))
              for r in thin for p in r["approvers"]]
    result = (f"{len(ranked)} most depended-on packages under `{scope}`; {len(thin)} of them have two or fewer approvers"
              + (f", e.g. `{thin[0]['package']}`: {thin[0]['importers']} importers, {thin[0]['approver_count']} approver"
                 f"{'s' if thin[0]['approver_count'] != 1 else ''}" if thin else ""))
    return step({"scope": scope, "most_depended_on": ranked, "chokepoints": choke,
                 "note": "PageRank and betweenness are computed by FalkorDB over the IMPORTS relationships of the loaded code"},
                result, edges, [node("Dir", r["package"]) for r in ranked + choke] + [node("Person", p) for r in ranked for p in r["approvers"]],
                tables=[table("Most depended-on (PageRank)", [{"Package": link("Dir", r["package"]), "PageRank": round(r["score"], 5),
                                                              "Importers": r["importers"], "Approvers": r["approver_count"],
                                                              "Approval from": link("Dir", r["owners_path"])} for r in ranked],
                              {"Package": "Dir", "Approval from": "Dir"}),
                        table("Chokepoints (betweenness)", [{"Package": link("Dir", r["package"]), "Betweenness": r["score"]} for r in choke],
                              {"Package": "Dir"})])


@graph_tool
def decision_to_approvers(number: str):
    """From a design decision to people, in one walk: the feature gates a KEP introduced, the code that checks
    them, the nearest OWNERS file of that code, and who must approve it. Answers 'who must approve code behind
    KEP-123?' or 'trace this decision to the code and the people'."""
    n = _kep_num(number)
    rows = Q.decision_code_owners(n)
    if not rows:
        return step([], f"KEP-{n}: none of its feature gates are checked in the loaded code (a graduated gate is often removed)",
                    nodes=[node("KEP", n)])
    k = node("KEP", n)
    edges = [(k, "introduced", node("FeatureGate", r["gate"])) for r in rows]
    edges += [(node("Dir", r["code"]), "checks gate", node("FeatureGate", r["gate"])) for r in rows]
    edges += [(node("Dir", r["code"]), f"inherits from ({r['up']} up)", node("Dir", r["owners_path"])) for r in rows if r["up"]]
    edges += [(node("Dir", r["owners_path"]), "approver", node(r["kind"], r["owner"])) for r in rows]
    edges += [(node("Team", r["owner"]), "member", node("Person", m)) for r in rows if r["kind"] == "Team" for m in r["members"]]
    people = sorted({p for r in rows for p in (r["members"] or [r["owner"]])})
    code = sorted({r["code"] for r in rows})
    return step(rows, f"KEP-{n} → {len({r['gate'] for r in rows})} feature gates → {len(code)} code folders → "
                      f"{len({r['owners_path'] for r in rows})} OWNERS files → {len(people)} people who can approve", edges,
                [node("Person", p) for p in people],
                tables=[table("Code that checks the decision's gates", [{"Gate": r["gate"], "Code": link("Dir", r["code"]),
                                                                         "Approval from": link("Dir", r["owners_path"])}
                                                                        for r in {(x["gate"], x["code"]): x for x in rows}.values()],
                              {"Code": "Dir", "Approval from": "Dir"}),
                        table("Who can approve that code", [{"Person": link("Person", p),
                                                             "Approves": ", ".join(sorted({r["code"] for r in rows if p in (r["members"] or [r["owner"]])}))}
                                                            for p in people], {"Person": "Person"})])


@graph_tool
def best_reviewers(path: str):
    """The best reviewers for a change to a folder: people who can approve it (nearest OWNERS file) AND merged
    changes there recently come first, then approvers, then recent contributors."""
    d = Q.nearest_dir(path)
    near, experts = _nearest(Q.owners(d, "APPROVER")), {r["person"]: r for r in Q.experts(d, 25)}
    approvers = {p for r in near for p in _people(r)}
    both = sorted(approvers & experts.keys(), key=lambda p: -experts[p]["prs"])
    folder = node("Dir", d)
    edges = [(node("Person", p), f"can approve · {experts[p]['prs']} PRs here", folder) for p in both]
    edges += [(node("Person", p), "can approve", folder) for p in sorted(approvers - experts.keys())[:6]]
    edges += [(node("Person", p), f"{r['prs']} PRs here", folder) for p, r in list(experts.items())[:6] if p not in approvers]
    rows = ([{"Person": link("Person", p), "Can approve": "yes", "Merged PRs here": experts[p]["prs"],
              "Last merged": str(experts[p]["last"])[:10]} for p in both]
            + [{"Person": link("Person", p), "Can approve": "yes", "Merged PRs here": 0, "Last merged": ""}
               for p in sorted(approvers - experts.keys())]
            + [{"Person": link("Person", p), "Can approve": "no", "Merged PRs here": r["prs"], "Last merged": str(r["last"])[:10]}
               for p, r in experts.items() if p not in approvers])
    return step({"folder": d, "can_approve_and_recently_changed": both, "can_approve": sorted(approvers),
                 "recent_contributors": list(experts.values())},
                f"{len(both)} {'person' if len(both) == 1 else 'people'} can approve `{d}` and merged changes there recently"
                + (f": {', '.join('@' + p for p in both[:4])}" if both else f"; {len(approvers)} approver{'s' if len(approvers) != 1 else ''}, "
                     f"{len(experts)} recent contributor{'s' if len(experts) != 1 else ''}"),
                edges, [folder] + [node("Person", p) for p in approvers | experts.keys()],
                tables=[table("Reviewers, best first", rows, {"Person": "Person"})])


@tool(description="Read-only Cypher (FalkorDB) ONLY for questions no other tool covers. Approver and reviewer lists "
                  "contain Teams as well as People: expand (:Person)-[:MEMBER_OF]->(:Team) before counting people, and "
                  "remember ownership is inherited from parent folders. Conversation memory is not in this graph "
                  "(each user has a separate one): use recall_memory. Graph schema:\n" + Q.SCHEMA,
      response_format="content_and_artifact")
def run_cypher(cypher: str):
    def body():
        rows = ro(cypher)[:50]
        return step(rows, f"{len(rows)} rows", tables=[table("Rows", [{k: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v
                                                                       for k, v in r.items()} for r in rows[:20]])])
    content, artifact = record(body)
    if artifact["error"]:
        artifact["result"] = f"Cypher error: {artifact['error']}"
        artifact["queries"] = artifact["queries"] or [{"cypher": cypher, "params": {}, "rows": 0, "ms": 0, "at": round(time.time() * 1000), "result": []}]
        return f"Cypher error: {artifact['error']}", artifact
    return content, artifact


@graph_tool
def write_review_plan(path: str, change_summary: str):
    """ACTION: write a review plan for a proposed change to a file or folder: required approvers, suggested
    reviewers, KEPs to read first, what could break, and people to ask. Saves a markdown file and returns it."""
    d = Q.nearest_dir(path)
    appr, revs, keps = Q.owners(d, "APPROVER"), Q.owners(d, "REVIEWER"), Q.constraints(d)
    deps, people, sigs = Q.blast_radius(d), Q.experts(d, 5), Q.owning_sigs(d)
    approvers = sorted({p for r in _nearest(appr) for p in _people(r)})
    reviewers = sorted({p for r in _nearest(revs) for p in _people(r)})
    sig = sigs[0]["sigs"][0] if sigs else "unknown"
    md = "\n".join([
        f"# Review plan: `{d}`", "", f"_Generated {date.today()} from the Kubernetes org graph._", "",
        f"**Change:** {change_summary}", "", f"**Owning SIG:** {sig}", "",
        "## Approval (one approver from this list is enough)", *[f"- @{a}" for a in approvers[:12]], "",
        "## Suggested reviewers", *[f"- @{r}" for r in reviewers[:6]], "",
        "## Read these design decisions first", *([f"- KEP-{k['kep']} {k['title']} ({k['stage']}, {k['milestone']})" for k in keps[:8]] or ["- none found"]), "",
        "## Could break (imports this code)", *([f"- `{x['dependent']}` ({'direct' if x['hops'] == 1 else str(x['hops']) + ' hops'})" for x in deps[:10]]
          or ["- nothing imports it"]), "",
        "## People who changed this recently", *([f"- @{p['person']}: {p['prs']} PRs, last {str(p['last'])[:10]}" for p in people] or ["- none in history window"]),
    ])
    OUT.mkdir(exist_ok=True)
    f = OUT / f"review-plan-{d.replace('/', '_')}.md"
    f.write_text(md, encoding="utf-8")
    folder = node("Dir", d)
    edges = ([(folder, "approver", node("Person", a)) for a in approvers[:6]]
             + [(node("KEP", k["kep"], title=k["title"]), "constrains", folder) for k in keps[:6]]
             + [(node("Dir", x["dependent"]), "imports", node("Dir", x["via"])) for x in deps[:4]])
    return step(f"Saved {f.relative_to(OUT.parent)}\n\n{md}",
                f"Saved `{f.relative_to(OUT.parent).as_posix()}`: {len(approvers)} approvers, {len(keps)} KEPs, "
                f"{len(deps)} dependents, {len(people)} recent contributors", edges,
                [node("Person", a) for a in approvers] + [node("Person", r) for r in reviewers]
                + [node("Person", p["person"]) for p in people] + [node("KEP", k["kep"], title=k["title"]) for k in keps]
                + [node("Dir", x["dependent"]) for x in deps],
                file={"name": f.name, "content": md})


def all_tools():
    return [search_graph, who_owns, best_reviewers, design_constraints, decision_to_approvers, blast_radius, critical_code,
            find_experts, recent_changes, kep_details, decision_trail, single_points_of_failure, person_profile,
            how_connected, run_cypher, write_review_plan]


# ---------------- node profiles: what the detail panel shows when a graph node is clicked ----------------

def _facts(*pairs):
    return [{"About": label, "Value": value} for label, value in pairs if value not in (None, "", [], 0)]


def _join(values, prefix="", limit=12):
    values = list(values)
    return ", ".join(prefix + v for v in values[:limit]) + (f" and {len(values) - limit} more" if len(values) > limit else "")


def _card(kind, key, user=None):
    if kind == "Dir":
        d = Q.nearest_dir(key)
        near, sigs = _nearest(Q.owners(d, "APPROVER")), Q.owning_sigs(d)
        keps, deps = Q.constraints(d), Q.blast_radius(d, 1)
        people = sorted({p for r in near for p in _people(r)})
        return step({}, f"Folder `{d}`: {len(people)} approver{'s' if len(people) != 1 else ''}, {len(keps)} design "
                    f"decision{'s' if len(keps) != 1 else ''} behind it, {len(deps)} package{'s' if len(deps) != 1 else ''} import it",
                    tables=[table("About", _facts(("Approval from", near[0]["granted_by"] + "/OWNERS" if near else ""),
                                                  ("Approvers", _join(people, "@")), ("Owning SIG", _join(sigs[0]["sigs"]) if sigs else ""),
                                                  ("Design decisions", _join((f"KEP-{k['kep']}" for k in keps))),
                                                  ("Imported by", _join(r["dependent"] for r in deps))))])
    if kind == "Person":
        r = (Q.person(key) or [None])[0]
        if not r:
            return step({}, f"@{key} is not in the graph")
        return step({}, f"@{r['person']}: approves {r['approves_count']} folders, authored {r['kep_count']} KEPs, {r['merged_prs']} merged PRs",
                    tables=[table("About", _facts(("Teams", _join(r["teams"])), ("Leads", _join(r["leads_sigs"])),
                                                  ("Approves", _join(r["approves_dirs"])), ("Authored KEPs", _join(r["authored_keps"], limit=6)),
                                                  ("Merged PRs (history window)", r["merged_prs"])))])
    if kind == "KEP":
        r = (Q.kep(_kep_num(key)) or [None])[0]
        if not r:
            return step({}, f"KEP-{key} is not in the graph")
        return step({}, f"KEP-{r['kep']} “{r['title']}” · {r['status']} · {r['stage']}",
                    tables=[table("About", _facts(("Summary", r["summary"]), ("Owning SIG", r["owning_sig"]),
                                                  ("Latest milestone", r["milestone"]), ("Authors", _join(r["authors"], "@")),
                                                  ("Approvers", _join(r["approvers"], "@")), ("Feature gates", _join(r["gates"])),
                                                  ("Checked in code", _join(r["code"]))))])
    if kind == "SIG":
        r = (Q.sig_profile(key) or [None])[0]
        if not r:
            return step({}, f"{key} is not in the graph")
        return step({}, f"{key} ({r['title']}): {r['dirs']} folders, {r['keps']} KEPs",
                    tables=[table("About", _facts(("Mission", r["mission"]), ("Chairs", _join(r["chairs"], "@")),
                                                  ("Tech leads", _join(r["leads"], "@")), ("Subprojects", _join(r["subprojects"]))))])
    if kind == "Team":
        r = (Q.team_profile(key) or [None])[0]
        return step({}, f"Team {key}: {len(r['members'])} members, approver for {len(r['approves'])} folders",
                    tables=[table("About", _facts(("Members", _join(r["members"], "@", 20)), ("Approves", _join(r["approves"])),
                                                  ("Reviews", _join(r["reviews"]))))])
    if kind == "FeatureGate":
        r = (Q.gate_profile(key) or [None])[0]
        keps = [k for k in r["keps"] if k.get("kep")]
        return step({}, f"Feature gate {key}: introduced by {len(keps)} KEPs, checked in {len(r['code'])} folders",
                    tables=[table("About", _facts(("Introduced by", _join(f"KEP-{k['kep']} {k['title']} ({k['stage']})" for k in keps)),
                                                  ("Checked in code", _join(r["code"])), ("Binaries", _join(r["binaries"]))))])
    if kind == "PR":
        r = (Q.pr_profile(key) or [None])[0]
        if not r:
            return step({}, f"PR #{key} is not in the graph")
        return step({}, f"PR #{key} “{r['title']}”",
                    tables=[table("About", _facts(("Author", f"@{r['author']}" if r["author"] else ""), ("Merged", str(r["merged"])[:10]),
                                                  ("Touched folders", _join(r["dirs"]))))])
    if kind == "Component":
        r = (Q.component_profile(key) or [None])[0]
        return step({}, f"Binary {key}: lives in {len(r['dirs'])} folders, affected by {r['gates']} feature gates",
                    tables=[table("About", _facts(("Code", _join(r["dirs"]))))])
    if kind == "Turn":
        session, _, n = key.rpartition(":")
        r = (MG.turn_profile(user, session, n) or [None])[0] if user else None
        if not r:
            return step({}, "This question is no longer in memory")
        return step({}, f"Question asked {r['when']}, linked to {r['linked'] or 0} graph nodes",
                    tables=[table("About", _facts(("Question", r["question"]), ("Answer", r["answer"][:600])))])
    return step({}, f"{kind} {key}")


def node_card(kind, key, user=None):
    """Profile of one node for the in-app detail panel, with the Cypher behind it. Question nodes are read from
    `user`'s memory graph."""
    _, artifact = record(_card, kind, key, user)
    return artifact


# ---------------- Explore: the same graph views, run directly (no LLM) ----------------

# kind -> views offered for it: (tool, label, icon, how to build the tool's arguments from the node key)
# The Explore page's views: (tool, label, icon, the graph walk in plain words, arguments for a node key).
ACTIONS = {
    "Dir": [("who_owns", "Who can approve", ":material/approval:",
             "Walks up the folder tree to the nearest OWNERS file that names approvers (ownership is inherited), "
             "expands teams into people and adds the SIG that owns the code.", lambda k: {"path": k}),
            ("best_reviewers", "Best reviewers", ":material/rate_review:",
             "Intersects two walks: who can approve this folder (inherited OWNERS) and who merged changes in it recently.",
             lambda k: {"path": k}),
            ("design_constraints", "Design decisions", ":material/gavel:",
             "Finds the feature gates this code checks, the KEPs that introduced them and the KEPs those link to.",
             lambda k: {"path": k}),
            ("blast_radius", "What could break", ":material/crisis_alert:",
             "Follows Go imports backwards one hop at a time: the packages that import this folder, then the packages "
             "that import those, until a hop finds nothing new. Then maps them to the binaries they are built into.",
             lambda k: {"path": k}),
            ("critical_code", "Critical code below", ":material/hub:",
             "Ranks the packages under this folder with FalkorDB PageRank and betweenness over Go imports, "
             "and counts who can approve each one.", lambda k: {"scope": k}),
            ("find_experts", "Who knows it", ":material/groups:",
             "People who merged pull requests touching this folder or anything below it.", lambda k: {"path": k}),
            ("recent_changes", "Recent changes", ":material/update:",
             "Pull requests merged here and KEPs behind this code updated in the last 30 days of the loaded history.",
             lambda k: {"path": k})],
    "KEP": [("kep_details", "Record", ":material/description:",
             "The KEP itself: status, stage, milestone, owning SIG, authors, approvers, feature gates and the code that "
             "checks them.", lambda k: {"number": k}),
            ("decision_to_approvers", "Code and approvers", ":material/conversion_path:",
             "One walk from decision to people: KEP → feature gates → code that checks them → inherited OWNERS → approvers.",
             lambda k: {"number": k}),
            ("decision_trail", "Linked decisions", ":material/account_tree:",
             "Follows 'see also' and 'replaces' links between KEPs, in both directions, one hop at a time until a hop "
             "finds nothing new.", lambda k: {"number": k})],
    "Person": [("person_profile", "Profile", ":material/person:",
                "Everything this handle is connected to: teams, SIGs they lead, folders they approve, KEPs and merged PRs.",
                lambda k: {"handle": k})],
    "SIG": [("single_points_of_failure", "Bus factor", ":material/person_alert:",
             "The SIG's folders whose nearest OWNERS file names the fewest people, and who approves the most folders.",
             lambda k: {"sig": k})],
}
_BY_NAME = {t.name: t for t in all_tools()}


def explore_search(text, limit=20):
    """Search limited to things the Explore page has views for, as typed node refs."""
    rows = [r for r in Q.search(text, 60) if r["kind"] in ACTIONS][:limit]
    return [node(r["kind"], r["key"], title=r["label"] if r["label"] != r["key"] else None) for r in rows]


def actions_for(kind):
    return [{"tool": t, "label": label, "icon": icon, "how": how} for t, label, icon, how, _ in ACTIONS.get(kind, [])]


def explore(tool_name, kind, key):
    """Run one graph view for a node, exactly as the agent would, and return its step record + graph."""
    build = next(b for t, _, _, _, b in ACTIONS[kind] if t == tool_name)
    args = build(key)
    _, artifact = record(_BY_NAME[tool_name].func.body, **args)
    rec = {"tool": tool_name, "args": args, "title": describe(tool_name, args), **artifact,
           "status": "error" if artifact["error"] else "complete"}
    return {**rec, "graph": combine_graph([rec])}
