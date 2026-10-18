"""A snapshot of the Kubernetes graph and the demo user's chats, shipped in the repo, so the app can rebuild its
data on any FalkorDB: a fresh local install, or a free cloud instance that restarted empty (the Free tier keeps
no data across restarts).

    ingest.py writes it (export); the app and the keep-alive job call ensure(), which restores only what is missing.

    uv run snapshot.py            restore anything missing, print what was done
"""
import gzip
import json
import re
import threading
from collections import defaultdict
from pathlib import Path

from redis.exceptions import ResponseError

import memory_graph as MG
from db import GRAPH, TIMEOUT_MS, client, exists
from queries import node

FOLDER = Path(__file__).parent / "snapshot"
KUBERNETES = FOLDER / "kubernetes.json.gz"
DEMO = FOLDER / "demo-memory.json.gz"
DEMO_USER = "demo"
BATCH = 1000
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # labels and types are written into Cypher text, so only plain names


def _write(path, data):
    path.parent.mkdir(exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _run(graph, cypher, params=None):
    return client().select_graph(graph).query(cypher, params or {}, timeout=TIMEOUT_MS * 6).result_set


def _chunks(rows):
    return (rows[i:i + BATCH] for i in range(0, len(rows), BATCH))


def _all(cypher):
    """Every row of a query, read in pages: FalkorDB caps a single result set (RESULTSET_SIZE, 10,000 by default)."""
    rows, page = [], 5000
    while chunk := _run(GRAPH, f"{cypher} SKIP {len(rows)} LIMIT {page}"):
        rows += chunk
        if len(chunk) < page:
            break
    return rows


def export():
    """Write the Kubernetes graph (nodes, relationships, indexes) and the demo user's chats to snapshot/."""
    nodes = [{"id": i, "label": label, "props": props}
             for i, label, props in _all("MATCH (n) RETURN id(n), labels(n)[0], properties(n) ORDER BY id(n)")]
    edges = [{"a": a, "type": t, "b": b}
             for a, t, b in _all("MATCH (a)-[r]->(b) RETURN id(a), type(r), id(b) ORDER BY id(r)")]
    indexes = [{"label": r[0], "properties": r[1]} for r in _run(GRAPH, "CALL db.indexes() YIELD label, properties, entitytype "
                                                                       "WHERE entitytype = 'NODE' RETURN label, properties")]
    _write(KUBERNETES, {"nodes": nodes, "edges": edges, "indexes": indexes})
    turns = sorted(({"session": s["session"], "question": t["question"], "answer": t["answer"], "at": t["at"],
                     "refs": [{"kind": r["kind"], "key": r["key"], "title": r.get("title")} for r in t["refs"]]}
                    for s in MG.sessions(DEMO_USER, limit=10000) for t in MG.session_turns(DEMO_USER, s["session"])),
                   key=lambda t: t["at"])
    _write(DEMO, turns)
    return {"nodes": len(nodes), "relationships": len(edges), "indexes": len(indexes), "demo turns": len(turns)}


def restore(graph=GRAPH):
    """Rebuild the Kubernetes graph from the snapshot. It is built under a temporary name and copied into place
    at the end, so nobody ever reads a half-built graph."""
    data = _read(KUBERNETES)
    temp = f"{graph}_restoring"
    if exists(temp):
        client().select_graph(temp).delete()
    label = {n["id"]: n["label"] for n in data["nodes"]}
    by_label = defaultdict(list)
    for n in data["nodes"]:
        by_label[n["label"]].append({"sid": n["id"], "props": n["props"]})
    by_type = defaultdict(list)
    for e in data["edges"]:
        by_type[(label[e["a"]], e["type"], label[e["b"]])].append({"a": e["a"], "b": e["b"]})
    assert all(NAME.match(x) for x in list(by_label) + [t for _, t, _ in by_type]), "unexpected label or type in snapshot"

    for lab, rows in by_label.items():  # nodes, each with a temporary snapshot id to connect them by
        _run(temp, f"CREATE INDEX FOR (n:{lab}) ON (n._sid)")
        for chunk in _chunks(rows):
            _run(temp, f"UNWIND $rows AS r CREATE (n:{lab}) SET n = r.props, n._sid = r.sid", {"rows": chunk})
    for (la, rel, lb), rows in by_type.items():
        for chunk in _chunks(rows):
            _run(temp, f"UNWIND $rows AS r MATCH (a:{la} {{_sid: r.a}}) MATCH (b:{lb} {{_sid: r.b}}) CREATE (a)-[:{rel}]->(b)",
                 {"rows": chunk})
    for lab in by_label:
        _run(temp, f"MATCH (n:{lab}) SET n._sid = NULL")
        _run(temp, f"DROP INDEX ON :{lab}(_sid)")
    for ix in data["indexes"]:
        if NAME.match(ix["label"]) and all(NAME.match(p) for p in ix["properties"]):
            try:
                _run(temp, f"CREATE INDEX FOR (n:{ix['label']}) ON ({', '.join('n.' + p for p in ix['properties'])})")
            except ResponseError:
                pass  # already there
    if exists(graph):
        client().select_graph(graph).delete()
    client().connection.execute_command("GRAPH.COPY", temp, graph)
    client().select_graph(temp).delete()
    return {"nodes": len(data["nodes"]), "relationships": len(data["edges"])}


def restore_demo():
    """Give the demo user back their chats from the snapshot (only when they have none)."""
    turns = _read(DEMO)
    for t in turns:
        MG.remember(DEMO_USER, t["session"], t["question"], t["answer"],
                    [node(r["kind"], r["key"], title=r.get("title")) for r in t["refs"]], at=t["at"])
    return len(turns)


def missing():
    """What the snapshot could restore because the database lacks it: the Kubernetes graph (no graph at all, as on a
    fresh install or a free cloud instance that restarted; an empty one is left alone, since ingest.py empties it
    while reloading) and the demo user's chats (none at all)."""
    gone = []
    if KUBERNETES.exists() and not exists(GRAPH):
        gone.append("the Kubernetes graph")
    if DEMO.exists() and not MG.sessions(DEMO_USER, limit=1):
        gone.append("the demo chats")
    return gone


_lock = threading.Lock()  # the app's sessions share one process: only one of them restores


def ensure():
    """Restore whatever is missing. Returns what was restored (nothing, on a normal start)."""
    with _lock:
        gone, done = missing(), {}
        if "the Kubernetes graph" in gone:
            done["kubernetes"] = restore()
        if "the demo chats" in gone:
            done["demo turns"] = restore_demo()
        return done


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    print("restored:", ensure() or "nothing, everything is in place")
