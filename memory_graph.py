"""Conversation memory: one FalkorDB graph per user, kept apart from the Kubernetes graph.

The Kubernetes graph holds only public project data, so nothing anyone asked can be read from it, not even by
the Cypher the model writes. Each name typed in the sidebar gets a graph of its own, named after a hash of the
name (a list of graphs does not show who uses the app):

    (:User {name})-[:HAD]->(:Session {id})-[:HAS]->(:Turn {question, answer, at, refs, unverified})
    (:Turn)-[:NEXT]->(:Turn)                              the timeline: each turn points to the user's next one
    (:Turn)-[:MENTIONS]->(:Ref {id, kind, key, title})    every node the turn's tools returned, by its key in the
                                                          Kubernetes graph

`unverified` lists names the answer gave that no tool returned (flagged "Not from the graph" when shown), so a
recalled answer never passes them off as facts.

A Ref is a pointer, not a copy: `kind` and `key` identify the node in the Kubernetes graph (queries.node()
builds its link), so memory is untouched when the Kubernetes data is reloaded and nothing has to be re-linked.
"""
import hashlib
import re
from datetime import datetime
from functools import cache

from redis.exceptions import ResponseError

from db import GRAPH, exists, mq
from queries import node, when


def graph_name(user):
    """The memory graph that belongs to a name."""
    return f"{GRAPH}_memory_{hashlib.sha256(user.strip().encode()).hexdigest()[:16]}"


@cache
def _indexed(name):
    for index in ("CREATE INDEX FOR (s:Session) ON (s.id)", "CREATE INDEX FOR (r:Ref) ON (r.id)",
                  "CREATE INDEX FOR (t:Turn) ON (t.at)"):
        try:
            mq(name, index)
        except ResponseError:
            pass  # already there
    return name


def remember(user, session, question, answer, mentions, at=None, unverified=()):
    """Store one exchange in the user's memory graph: after their previous turn on the timeline, pointing at
    every node it used (`mentions` are queries.node() refs), with the names it gave that no tool returned.
    Returns how many nodes the turn points at."""
    g = _indexed(graph_name(user))
    turn = mq(g, """
        MERGE (u:User {name: $user})
        MERGE (s:Session {id: $session}) MERGE (u)-[:HAD]->(s)
        CREATE (t:Turn {question: $question, answer: $answer, at: coalesce($at, timestamp()), refs: $ids,
                        unverified: $unverified})
        MERGE (s)-[:HAS]->(t)
        RETURN id(t) AS turn""",
              {"user": user, "session": session, "question": question, "answer": answer[:4000], "at": at,
               "ids": list(dict.fromkeys(m["id"] for m in mentions)), "unverified": list(unverified)})[0]["turn"]
    mq(g, """
        MATCH (t:Turn) WHERE id(t) = $turn
        MATCH (p:Turn) WHERE p <> t AND p.at <= t.at AND NOT (p)-[:NEXT]->()
        WITH t, p ORDER BY p.at DESC LIMIT 1
        MERGE (p)-[:NEXT]->(t)""", {"turn": turn})
    if not mentions:
        return 0
    return mq(g, """
        MATCH (t:Turn) WHERE id(t) = $turn
        UNWIND $refs AS r
        MERGE (ref:Ref {id: r.id}) SET ref.kind = r.kind, ref.key = r.key, ref.title = coalesce(r.title, ref.title)
        MERGE (t)-[:MENTIONS]->(ref)
        RETURN count(DISTINCT ref) AS linked""",
              {"turn": turn, "refs": [{"id": m["id"], "kind": m["kind"], "key": m["key"], "title": m.get("title")}
                                      for m in mentions]})[0]["linked"]


def recall(user, about=None, limit=5):
    """Earlier exchanges of this user, newest first. With `about`, keep only exchanges whose question or
    mentioned nodes (folder paths, KEP numbers, handles, SIGs) contain every word of it, e.g.
    'pkg/kubelet' also finds turns about pkg/kubelet/eviction, and 'kubelet eviction' works too."""
    g = graph_name(user)
    if not exists(g):
        return []
    words = [w for w in re.split(r"[\s,;]+", (about or "").lower().replace("kep-", "")) if w]
    rows = mq(g, """
        MATCH (s:Session)-[:HAS]->(t:Turn)
        OPTIONAL MATCH (t)-[:MENTIONS]->(r:Ref)
        WITH s, t, collect(toLower(r.key)) AS keys
        WITH s, t, reduce(hay = toLower(t.question), k IN keys | hay + ' ' + k) AS hay
        WHERE all(w IN $words WHERE hay CONTAINS w)
        WITH s, t ORDER BY t.at DESC LIMIT $limit
        RETURN s.id AS session, t.question AS question, t.answer AS answer, t.at AS at, t.refs AS refs,
               t.unverified AS unverified""", {"words": words, "limit": limit})
    # Narrow to the turns first, then read their text: projecting t.answer straight after the reduce() crashed
    # FalkorDB (signal 11) in testing on falkordb/falkordb:latest, Redis 8.10.2.
    return [_turn_row(r) for r in rows]


def recall_linked(user, ids, limit=10):
    """Earlier exchanges of this user that point at any of these Kubernetes graph nodes (ids such as
    'Dir:pkg/kubelet'), the ones pointing at the most of them first, each with the ids it matched (`hits`)."""
    g = graph_name(user)
    if not ids or not exists(g):
        return []
    rows = mq(g, """
        MATCH (s:Session)-[:HAS]->(t:Turn)-[:MENTIONS]->(r:Ref) WHERE r.id IN $ids
        WITH s, t, collect(r.id) AS hits
        WITH s, t, hits ORDER BY size(hits) DESC, t.at DESC LIMIT $limit
        RETURN s.id AS session, t.question AS question, t.answer AS answer, t.at AS at, t.refs AS refs,
               t.unverified AS unverified, hits""", {"ids": list(ids), "limit": limit})
    return [_turn_row(r) for r in rows]


def _turn_row(r):
    return {**r, "refs": r["refs"] or [], "unverified": r.get("unverified") or [],
            "date": datetime.fromtimestamp(r["at"] / 1000).strftime("%Y-%m-%d")}


def sessions(user, current=None, limit=8):
    """This user's chats, newest first, with their first question."""
    g = graph_name(user)
    if not exists(g):
        return []
    rows = mq(g, """
        MATCH (s:Session)-[:HAS]->(t:Turn)
        WITH s, t ORDER BY t.at
        WITH s, collect(t.question) AS questions, min(t.at) AS started, count(t) AS turns
        RETURN s.id AS session, started, turns, questions[0] AS first_question
        ORDER BY started DESC LIMIT $limit""", {"limit": limit})
    return [{**r, "when": when(r["started"]), "current": r["session"] == current} for r in rows]


def session_turns(user, session):
    """One chat: each turn with its question, answer, time and the nodes it points at."""
    g = graph_name(user)
    if not exists(g):
        return []
    rows = mq(g, """
        MATCH (:Session {id: $session})-[:HAS]->(t:Turn)
        OPTIONAL MATCH (t)-[:MENTIONS]->(r:Ref)
        WITH t, collect(CASE WHEN r IS NULL THEN NULL ELSE {kind: r.kind, key: r.key, title: r.title} END) AS refs
        RETURN t.question AS question, t.answer AS answer, t.at AS at, t.unverified AS unverified, refs
        ORDER BY at""", {"session": session})
    return [{**r, "when": when(r["at"]), "unverified": r["unverified"] or [],
             "refs": [node(x["kind"], x["key"], title=x.get("title")) for x in r["refs"] if x]} for r in rows]


def turn_profile(user, session, n):
    """The n-th question (1-based) of a chat."""
    g = graph_name(user)
    if not exists(g):
        return []
    rows = mq(g, """
        MATCH (:Session {id: $s})-[:HAS]->(t:Turn)
        WITH t ORDER BY t.at
        WITH collect(t) AS turns
        WITH turns[$i] AS t WHERE t IS NOT NULL
        RETURN t.question AS question, t.answer AS answer, t.at AS at, size(t.refs) AS linked""",
              {"s": session, "i": int(n) - 1})
    return [{**r, "when": when(r["at"])} for r in rows]
