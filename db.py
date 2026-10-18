"""The only database helper: run Cypher against FalkorDB, get rows back as dicts. q() and ro() use the
Kubernetes graph; mq() a user's own memory graph (see memory_graph.py).

Every query is also appended to the current trace (if one is active), so the UI can show
exactly which Cypher produced an answer, when it ran, how long it took and what it returned.
"""
import json
import os
import time
from contextvars import ContextVar
from functools import cache

from falkordb import FalkorDB

GRAPH = os.getenv("FALKORDB_GRAPH", "k8s")
TIMEOUT_MS = int(os.getenv("FALKORDB_TIMEOUT_MS", "10000"))  # FalkorDB's own default is 1 s
TRACE = ContextVar("trace", default=None)  # list of {"cypher", "params", "rows", "ms", "at", "result", "graph"}
RESULT_ROWS = 20  # rows of each response kept in the trace (the count is always complete)


@cache
def client():
    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        username=os.getenv("FALKORDB_USERNAME") or None,
        password=os.getenv("FALKORDB_PASSWORD") or None,
    )


def graph():
    return client().select_graph(GRAPH)


def _run(fn, cypher, params, where="kubernetes"):
    t = time.time()
    res = fn(cypher, params or {}, timeout=TIMEOUT_MS)
    cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
    rows = [dict(zip(cols, row)) for row in res.result_set]
    if (trace := TRACE.get()) is not None and not cypher.lstrip().startswith("MATCH (d:Dir {path: $p}) RETURN"):
        tidy = "\n".join(line.strip() for line in cypher.strip().splitlines() if line.strip())
        trace.append({"cypher": tidy, "params": params or {}, "rows": len(rows), "ms": round((time.time() - t) * 1000, 1),
                      "at": round(t * 1000), "result": json.loads(json.dumps(rows[:RESULT_ROWS], default=str)),
                      "graph": where})
    return rows


def q(cypher, params=None):
    return _run(graph().query, cypher, params)


def ro(cypher, params=None):
    """Read-only query, for Cypher the LLM writes itself. The Kubernetes graph holds no conversation memory,
    so such a query can not read anyone's questions and answers."""
    return _run(graph().ro_query, cypher, params)


def mq(name, cypher, params=None):
    """Query one user's memory graph."""
    return _run(client().select_graph(name).query, cypher, params, where="memory")


def exists(name):
    """Whether a graph exists (memory graphs are created by a user's first question, not by reading them)."""
    return bool(client().connection.exists(name))


def traced(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) and return (its result, the Cypher it ran)."""
    token = TRACE.set([])
    try:
        return fn(*args, **kwargs), TRACE.get()
    finally:
        TRACE.reset(token)
