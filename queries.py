"""Every Cypher query the product depends on, one function each.

Each function returns plain rows (list of dicts). The agent's tools are thin wrappers around these.
"""
import difflib
import re
from datetime import datetime
from functools import cache

from db import q

GITHUB = "https://github.com/kubernetes"
KIND_LABEL = {"Person": "Person", "Dir": "Folder", "KEP": "KEP", "SIG": "SIG", "Team": "Team",
              "FeatureGate": "Feature gate", "PR": "Pull request", "Component": "Binary", "Subproject": "Subproject",
              "Turn": "Question", "Doc": "Document"}

# For table cells that hold a node URL: the regex that pulls the readable key back out of it.
LINK_TEXT = {"Person": r"https://github\.com/(.*)", "Dir": r".*/tree/[^/]+/(.*)", "KEP": r".*/issues/(\d+)",
             "PR": r".*/pull/(\d+)", "SIG": r".*/tree/[^/]+/(.*)", "Doc": r".*/blob/[^/]+/(.*)"}


@cache
def source_ref(repo="kubernetes"):
    """The commit of kubernetes/<repo> the graph was loaded from, so links show exactly what was loaded."""
    rows = q(f"MATCH (m:Meta {{id: 'load'}}) RETURN m.{repo}_commit AS c")
    return (rows[0]["c"] if rows else None) or "master"


def url(kind, key):
    """Public page for a node, or None when there is no stable one. Code links are pinned to the loaded commit."""
    return {
        "Person": f"https://github.com/{key}",
        "Dir": f"{GITHUB}/kubernetes/tree/{source_ref()}/{'' if key == '/' else key}".rstrip("/"),
        "KEP": f"{GITHUB}/enhancements/issues/{key}",  # KEP numbers are their tracking-issue numbers
        "SIG": f"{GITHUB}/community/tree/master/{key}",
        "Team": f"{GITHUB}/kubernetes/blob/{source_ref()}/OWNERS_ALIASES",
        "PR": f"{GITHUB}/kubernetes/pull/{key}",
        "FeatureGate": "https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/",
        "Doc": f"{GITHUB}/community/blob/{source_ref('community')}/{key}",
    }.get(kind)


# The identifying property of a node, by kind (KEPs have both a path and a number; the number is the key).
NODE_KEY = ("CASE WHEN n:Dir THEN n.path WHEN n:Doc THEN n.path WHEN n:KEP THEN n.number WHEN n:Person THEN n.handle "
            "WHEN n:PR THEN toString(n.number) ELSE n.name END")


def node(kind, key, label=None, title=None):
    """A typed reference to a graph node, as the UI and memory use it."""
    key = str(key)
    return {"id": f"{kind}:{key}", "kind": kind, "kind_label": KIND_LABEL.get(kind, kind), "key": key,
            "label": label or {"KEP": f"KEP-{key}", "Person": f"@{key}", "PR": f"#{key}"}.get(kind, key),
            "title": title, "url": url(kind, key)}

# Folder chain from a path up to the root, stopping at an OWNERS file that sets no_parent_owners.
# Ownership in Kubernetes is inherited down the folder tree, so this is a variable-depth walk.
_OWNER_CHAIN = """
MATCH p = (d:Dir {path: $path})-[:IN*0..]->(a:Dir)
WHERE none(n IN nodes(p)[0..size(nodes(p)) - 1] WHERE coalesce(n.no_parent_owners, false))
"""


def nearest_dir(path):
    """Map any file or folder path to the deepest folder we know about."""
    path = path.strip().strip("/")
    while path and not q("MATCH (d:Dir {path: $p}) RETURN d.path", {"p": path}):
        path = path.rsplit("/", 1)[0] if "/" in path else ""
    return path or "/"


def owners(path, role="APPROVER"):
    """Who must approve (or review) changes to this path, and which OWNERS file grants it."""
    return q(_OWNER_CHAIN + f"""
        MATCH (a)-[:{role}]->(o)
        OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
        RETURN a.path AS granted_by, length(p) AS levels_up, labels(o)[0] AS kind,
               coalesce(o.handle, o.name) AS owner, collect(m.handle) AS team_members
        ORDER BY levels_up, owner""", {"path": path})


def owning_sigs(path):
    """Which SIG (team) owns this path: SIG labels on OWNERS files and subprojects, nearest first."""
    return q("""
        MATCH p = (d:Dir {path: $path})-[:IN*0..]->(a:Dir)
        OPTIONAL MATCH (a)-[:LABELED]->(s1:SIG)
        OPTIONAL MATCH (sp:Subproject)-[:OWNS]->(a)
        OPTIONAL MATCH (sp)-[:PART_OF]->(s2:SIG)
        WITH a, length(p) AS levels_up, collect(DISTINCT s1.name) + collect(DISTINCT s2.name) AS found,
             collect(DISTINCT sp.name) AS subprojects
        WITH a, levels_up, subprojects,
             reduce(acc = [], x IN found | CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS sigs  // a SIG named twice
        WHERE size(sigs) > 0
        RETURN a.path AS dir, levels_up, sigs, subprojects ORDER BY levels_up LIMIT 3""", {"path": path})


def sig_leads(sig):
    return q("""
        MATCH (s:SIG {name: $sig})
        OPTIONAL MATCH (c:Person)-[:CHAIRS]->(s)
        OPTIONAL MATCH (t:Person)-[:TECH_LEADS]->(s)
        RETURN s.name AS sig, s.title AS title, s.mission AS mission,
               collect(DISTINCT c.handle) AS chairs, collect(DISTINCT t.handle) AS tech_leads""", {"sig": sig})


def constraints(path):
    """Design decisions that constrain this code: feature gates checked in the folder (or below it)
    -> the KEPs that introduced them -> KEPs those point to."""
    return q("""
        MATCH (d:Dir)-[:IN*0..]->(:Dir {path: $path})
        MATCH (d)-[:CHECKS_GATE]->(g:FeatureGate)<-[:INTRODUCES]-(k:KEP)
        OPTIONAL MATCH (k)-[:OWNED_BY]->(s:SIG)
        OPTIONAL MATCH (k)-[:SEE_ALSO|REPLACES]->(rel:KEP)
        RETURN k.number AS kep, k.title AS title, k.status AS status, k.stage AS stage,
               k.milestone AS milestone, s.name AS owning_sig, collect(DISTINCT g.name) AS gates,
               collect(DISTINCT d.path)[0..5] AS checked_in, collect(DISTINCT rel.number + ' ' + rel.title) AS related
        ORDER BY k.milestone DESC""", {"path": path})


def _walk(cypher, start, key, depth=None):
    """Breadth-first walk, one query per hop, until a hop reaches nothing new (or after `depth` hops).
    `cypher` gets $frontier (reached on the last hop) and $seen (reached so far) and returns one row per newly
    reached node, named by its `key` column. Each row comes back with the hop it was reached on.
    A single variable-length MATCH would enumerate every path, which explodes on densely linked nodes."""
    found, frontier, seen, hops = [], [start], {start}, 0
    while frontier and (depth is None or hops < depth):
        hops += 1
        rows = q(cypher, {"frontier": frontier, "seen": sorted(seen)})
        found += [{**r, "hops": hops} for r in rows]
        frontier = [r[key] for r in rows]
        seen |= set(frontier)
    return found


def blast_radius(path, depth=None):
    """Code that imports this folder, directly or through a chain of imports, followed to the end of every chain
    (or `depth` hops). Each dependent comes with its distance, the folder it imports on its shortest chain (`via`)
    and the binaries it ends up in."""
    rows = _walk("""
        MATCH (x:Dir)-[:IMPORTS]->(y:Dir) WHERE y.path IN $frontier AND NOT x.path IN $seen
        RETURN x.path AS dependent, min(y.path) AS via ORDER BY dependent""", path, "dependent", depth)
    if not rows:
        return []
    bins = {r["dependent"]: r["component"] for r in q("""
        UNWIND $paths AS p MATCH (x:Dir {path: p})
        OPTIONAL MATCH (c:Component)-[:LIVES_IN]->(:Dir)<-[:IN*0..]-(x)
        RETURN p AS dependent, collect(DISTINCT c.name) AS component""", {"paths": [r["dependent"] for r in rows]})}
    return [{**r, "component": bins.get(r["dependent"], [])} for r in rows]


def experts(path, limit=10):
    """People who recently merged changes in this folder or below it."""
    return q("""
        MATCH (pe:Person)-[:AUTHORED]->(pr:PR)-[:TOUCHES]->(:Dir)-[:IN*0..]->(:Dir {path: $path})
        WITH pe, count(DISTINCT pr) AS prs, max(pr.merged_at) AS last, collect(DISTINCT pr.number)[0..3] AS examples
        RETURN pe.handle AS person, prs, last, examples ORDER BY prs DESC, last DESC LIMIT $limit""",
             {"path": path, "limit": limit})


def kep(number):
    """One design decision with its people, teams, feature gates, code locations and neighbours."""
    return q("""
        MATCH (k:KEP {number: $n})
        OPTIONAL MATCH (k)-[:OWNED_BY]->(s:SIG)
        OPTIONAL MATCH (k)-[:INVOLVES]->(ps:SIG)
        OPTIONAL MATCH (a:Person)-[:KEP_AUTHOR]->(k)
        OPTIONAL MATCH (ap:Person)-[:KEP_APPROVER]->(k)
        OPTIONAL MATCH (k)-[:INTRODUCES]->(g:FeatureGate)
        OPTIONAL MATCH (g)<-[:CHECKS_GATE]-(d:Dir)
        OPTIONAL MATCH (g)-[:AFFECTS]->(c:Component)
        RETURN k.number AS kep, k.title AS title, k.status AS status, k.stage AS stage, k.milestone AS milestone,
               k.created AS created, k.updated AS updated, k.path AS source, k.summary AS summary, s.name AS owning_sig,
               collect(DISTINCT ps.name) AS participating_sigs, collect(DISTINCT a.handle) AS authors,
               collect(DISTINCT ap.handle) AS approvers, collect(DISTINCT g.name) AS gates,
               collect(DISTINCT c.name) AS components, collect(DISTINCT d.path)[0..10] AS code""", {"n": str(number)})


def decision_trail(number, depth=None):
    """KEPs linked to a KEP through SEE_ALSO / REPLACES links, in either direction, followed until nothing new is
    reached (or `depth` hops). Each comes with its distance and the link that reached it ([from, type, to])."""
    return _walk("""
        MATCH (k:KEP)-[r:SEE_ALSO|REPLACES]-(o:KEP) WHERE k.number IN $frontier AND NOT o.number IN $seen
        WITH o, collect([startNode(r).number, type(r), endNode(r).number])[0] AS link
        RETURN o.number AS kep, o.title AS title, o.status AS status, link ORDER BY kep""", str(number), "kep", depth)


def recent_prs(path, since, limit=15):
    """Pull requests merged on or after `since` (YYYY-MM-DD) that touched this folder or anything below it."""
    return q("""
        MATCH (pr:PR)-[:TOUCHES]->(:Dir)-[:IN*0..]->(:Dir {path: $path}) WHERE pr.merged_at >= $since
        OPTIONAL MATCH (p:Person)-[:AUTHORED]->(pr)
        RETURN DISTINCT pr.number AS pr, pr.title AS title, substring(pr.merged_at, 0, 10) AS merged, p.handle AS author
        ORDER BY merged DESC LIMIT $limit""", {"path": path, "since": since, "limit": limit})


def recent_keps(path, since, limit=10):
    """Design decisions behind this code (via the feature gates it checks) whose KEP was updated on or after `since`."""
    return q("""
        MATCH (d:Dir)-[:IN*0..]->(:Dir {path: $path})
        MATCH (d)-[:CHECKS_GATE]->(:FeatureGate)<-[:INTRODUCES]-(k:KEP) WHERE k.updated >= $since
        RETURN DISTINCT k.number AS kep, k.title AS title, k.updated AS updated, k.stage AS stage, k.status AS status
        ORDER BY updated DESC LIMIT $limit""", {"path": path, "since": since, "limit": limit})


# The approvers of the nearest OWNERS file above `d` (inheritance stops at no_parent_owners), for use inside other
# queries; {carry} lists the variables to keep. Teams come back as `o`, with their members as `m`.
_NEAREST_APPROVERS = """
MATCH p = (d)-[:IN*0..]->(a:Dir)
WHERE none(n IN nodes(p)[0..size(nodes(p)) - 1] WHERE coalesce(n.no_parent_owners, false)) AND (a)-[:APPROVER]->()
WITH {carry}, a.path AS owners_path, length(p) AS up ORDER BY up
WITH {carry}, collect(owners_path)[0] AS owners_path, min(up) AS up
MATCH (:Dir {{path: owners_path}})-[:APPROVER]->(o)
OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
"""


def critical_code(scope, limit=12):
    """Packages under `scope` that the most code depends on (PageRank over Go imports), with how many people can
    approve each one and how many packages import it directly. Critical and thinly owned means risk."""
    return q("""
        CALL algo.pageRank('Dir', 'IMPORTS') YIELD node, score
        WITH node AS d, score WHERE d.path STARTS WITH $scope
        WITH d, score ORDER BY score DESC LIMIT $limit""" + _NEAREST_APPROVERS.format(carry="d, score") + """
        WITH d, score, owners_path, up, collect(DISTINCT coalesce(m.handle, o.handle)) AS approvers
        OPTIONAL MATCH (x:Dir)-[:IMPORTS]->(d)
        RETURN d.path AS package, score, owners_path, up, size(approvers) AS approver_count, approvers,
               count(DISTINCT x) AS importers
        ORDER BY score DESC""", {"scope": scope, "limit": limit})


def chokepoints(scope, limit=8):
    """Packages under `scope` that sit on the most import paths between other packages (betweenness centrality)."""
    return q("""
        CALL algo.betweenness({nodeLabels: ['Dir'], relationshipTypes: ['IMPORTS'], samplingSize: 1000, samplingSeed: 42})
        YIELD node, score
        WITH node AS d, score WHERE d.path STARTS WITH $scope AND score > 0
        RETURN d.path AS package, round(score) AS score ORDER BY score DESC LIMIT $limit""", {"scope": scope, "limit": limit})


def decision_code_owners(number):
    """From a design decision to the people who must approve the code it changed, in one walk:
    KEP -> feature gate -> code that checks it -> nearest OWNERS file -> approvers (teams expanded)."""
    return q("""
        MATCH (k:KEP {number: $n})-[:INTRODUCES]->(g:FeatureGate)<-[:CHECKS_GATE]-(d:Dir)""" + _NEAREST_APPROVERS.format(carry="k, g, d") + """
        RETURN g.name AS gate, d.path AS code, owners_path, up, labels(o)[0] AS kind, coalesce(o.handle, o.name) AS owner,
               collect(DISTINCT m.handle) AS members ORDER BY code, owner""", {"n": str(number)})


def single_points_of_failure(sig, limit=10):
    """Folders a SIG owns where very few people can approve, and the people they depend on most."""
    return q(_SPOF, {"sig": sig, "limit": limit})


_SPOF = """
MATCH (s:SIG {name: $sig})<-[:LABELED]-(d:Dir)
MATCH (d)-[:APPROVER]->(o)
OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
WITH d, collect(DISTINCT coalesce(m.handle, o.handle)) AS approvers
WHERE size(approvers) > 0
RETURN d.path AS dir, size(approvers) AS approver_count, approvers
ORDER BY approver_count, dir LIMIT $limit
"""


def busiest_approvers(sig, limit=10):
    """People who are approvers for the most folders of a SIG: losing them hurts most."""
    return q("""
        MATCH (s:SIG {name: $sig})<-[:LABELED]-(d:Dir)-[:APPROVER]->(o)
        OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
        WITH coalesce(m.handle, o.handle) AS person, count(DISTINCT d) AS dirs
        WHERE person IS NOT NULL
        RETURN person, dirs ORDER BY dirs DESC LIMIT $limit""", {"sig": sig, "limit": limit})


def person(handle):
    """Everything one person is connected to: teams, SIG roles, owned folders, KEPs, recent PRs."""
    return q("""
        MATCH (p:Person {handle: $h})
        OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
        WITH p, collect(DISTINCT t.name) AS teams
        OPTIONAL MATCH (p)-[:CHAIRS|TECH_LEADS]->(s:SIG)
        WITH p, teams, collect(DISTINCT s.name) AS leads_sigs
        OPTIONAL MATCH (d:Dir)-[:APPROVER]->(p)
        WITH p, teams, leads_sigs, collect(DISTINCT d.path) AS dirs
        OPTIONAL MATCH (p)-[:KEP_AUTHOR]->(k:KEP)
        WITH p, teams, leads_sigs, dirs, collect(DISTINCT k.number + ' ' + k.title) AS keps
        OPTIONAL MATCH (p)-[:AUTHORED]->(pr:PR)
        RETURN p.handle AS person, teams, leads_sigs, dirs[0..10] AS approves_dirs, size(dirs) AS approves_count,
               keps[0..10] AS authored_keps, size(keps) AS kep_count, count(DISTINCT pr) AS merged_prs""",
             {"h": handle.lower().lstrip("@")})


def connection(a_kind, a_key, b_kind, b_key):
    """Shortest chain of relationships between two things, e.g. a person and a KEP."""
    key = {"Person": "handle", "Dir": "path", "KEP": "number", "SIG": "name", "FeatureGate": "name", "Team": "name"}
    # FalkorDB's shortestPath is directed-only, so search outward one hop at a time (iterative deepening).
    # Folder-tree edges (:IN) are left out: every folder reaches every other one through the root.
    for hops in range(1, 6):
        rows = q(f"""
            MATCH (a:{a_kind} {{{key[a_kind]}: $a}}) MATCH (b:{b_kind} {{{key[b_kind]}: $b}})
            MATCH p = (a)-[:{_LINKS}*1..{hops}]-(b)
            WITH p ORDER BY length(p) LIMIT 3
            RETURN [n IN nodes(p) | {{id: id(n), kind: labels(n)[0], key: toString(coalesce(n.handle, n.number, n.path, n.name, ''))}}] AS nodes,
                   [r IN relationships(p) | [id(startNode(r)), type(r), id(endNode(r))]] AS rels""", {"a": a_key, "b": b_key})
        if rows:
            return rows
    return []


_LINKS = ("AUTHORED|TOUCHES|APPROVER|REVIEWER|MEMBER_OF|KEP_AUTHOR|KEP_REVIEWER|KEP_APPROVER|INTRODUCES"
          "|CHECKS_GATE|OWNED_BY|INVOLVES|CHAIRS|TECH_LEADS|SEE_ALSO|REPLACES|IMPORTS|LABELED")


def search(text, limit=10):
    """Find folders, KEPs, feature gates, SIGs or people by a name fragment. '@handle' finds the person, and
    'KEP-2837', 'KEP 2837' or '2837' finds the KEP by number."""
    t = text.strip().lower().lstrip("@")
    if text.strip().startswith("@"):  # a handle: people only, the exact one first, then those starting with it
        return q("""
            MATCH (p:Person) WHERE p.handle CONTAINS $t
            RETURN 'Person' AS kind, p.handle AS key, p.handle AS label
            ORDER BY p.handle <> $t, NOT p.handle STARTS WITH $t, size(p.handle) LIMIT $limit""", {"t": t, "limit": limit})
    number = re.fullmatch(r"(?:kep)?[\s#-]*(\d{1,5})", t)
    return q("""
        CALL {
          MATCH (k:KEP) WHERE k.number = $n
          RETURN 'KEP' AS kind, k.number AS key, k.title AS label
          UNION
          MATCH (k:KEP) WHERE toLower(k.title) CONTAINS $t
          RETURN 'KEP' AS kind, k.number AS key, k.title AS label
          UNION
          MATCH (g:FeatureGate) WHERE toLower(g.name) CONTAINS $t
          RETURN 'FeatureGate' AS kind, g.name AS key, g.name AS label
          UNION
          MATCH (d:Dir) WHERE toLower(d.path) CONTAINS $t AND (d)-[:APPROVER|IMPORTS|CHECKS_GATE]-()
          RETURN 'Dir' AS kind, d.path AS key, d.path AS label
          UNION
          MATCH (s:SIG) WHERE toLower(s.name) CONTAINS $t OR toLower(coalesce(s.title, '')) CONTAINS $t
          RETURN 'SIG' AS kind, s.name AS key, s.title AS label
          UNION
          MATCH (p:Person) WHERE p.handle CONTAINS $t
          RETURN 'Person' AS kind, p.handle AS key, p.handle AS label
        }
        RETURN kind, key, label LIMIT $limit""", {"t": t, "n": number.group(1) if number else None, "limit": limit})


def topic_nodes(about, per_node=40):
    """What a recall topic means in the Kubernetes graph, as node ids ('Dir:pkg/kubelet/eviction'), the keys memory
    turns point at: the nodes the topic names (name search, plus the folder for a path), and the code, KEPs,
    feature gates, documents and PRs connected to them: one hop, or two through a feature gate (a KEP and the code
    that checks its gates). People, teams and SIGs are not followed: they connect to almost everything. At most
    `per_node` connected nodes each. Returns (named, connected)."""
    about = about.strip()
    named = {f"{r['kind']}:{r['key']}" for r in search(about, 10)} if about else set()
    if "/" in about:
        named.add(f"Dir:{nearest_dir(about)}")
    if not named:
        return [], []
    seed = NODE_KEY.replace("n:", "s:").replace("n.", "s.")
    rows = q(f"""
        MATCH (s) WHERE NOT s:Meta AND (labels(s)[0] + ':' + toString({seed})) IN $ids
        OPTIONAL MATCH (s)--(n) WHERE n:Dir OR n:KEP OR n:FeatureGate OR n:Doc OR n:PR
        WITH s, collect(DISTINCT labels(n)[0] + ':' + toString({NODE_KEY}))[0..$per] AS near
        OPTIONAL MATCH (s)-[:INTRODUCES|CHECKS_GATE]->(:FeatureGate)<-[:INTRODUCES|CHECKS_GATE]-(n)
        WHERE n <> s AND (n:Dir OR n:KEP)
        WITH near, collect(DISTINCT labels(n)[0] + ':' + toString({NODE_KEY}))[0..$per] AS via_gate
        RETURN near + via_gate AS connected""", {"ids": list(named), "per": per_node})
    connected = {i for r in rows for i in r["connected"]} - named
    return sorted(named), sorted(connected)


@cache
def _names():
    """Every person, SIG and folder, by kind: what a misspelt search is compared against."""
    return q("""
        MATCH (p:Person) RETURN 'Person' AS kind, p.handle AS key
        UNION MATCH (s:SIG) RETURN 'SIG' AS kind, s.name AS key
        UNION MATCH (d:Dir) RETURN 'Dir' AS kind, d.path AS key""")


def close_names(text, limit=5):
    """Names in the graph closest to one that matches nothing ('thokin' -> thockin), best first."""
    kinds = {r["key"]: r["kind"] for r in _names() if r["kind"] == "Person" or not text.strip().startswith("@")}
    keys = difflib.get_close_matches(text.strip().lower().lstrip("@"), list(kinds), n=limit, cutoff=0.75)
    return [{"kind": kinds[k], "key": k} for k in keys]


# ---------- community documents (SIG READMEs, charters, annual reports) ----------

def _fulltext(text):
    """A safe full-text query from free text: plain words only, all required, the last one also as a prefix
    ('annual rep' finds 'annual report'). Empty when there are no usable words."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1][:8]
    return " ".join(words[:-1] + [words[-1] + "*" if len(words[-1]) > 2 else words[-1]]) if words else ""


def snippet(text, query, width=260):
    """The part of a document around the first query word it contains."""
    low = text.lower()
    at = min((i for i in (low.find(w) for w in re.findall(r"[a-z0-9]{3,}", query.lower())) if i >= 0), default=0)
    start = max(0, at - width // 3)
    return ("…" if start else "") + " ".join(text[start:start + width].split()) + "…"


def search_docs(text, limit=8):
    """Community documents matching the words (FalkorDB full-text index on title and text, best match first)."""
    query = _fulltext(text)
    if not query:
        return []
    rows = q("""
        CALL db.idx.fulltext.queryNodes('Doc', $query) YIELD node, score
        WITH node, score ORDER BY score DESC LIMIT $limit
        OPTIONAL MATCH (node)-[:ABOUT]->(s:SIG)
        RETURN node.path AS path, node.title AS title, node.kind AS kind, node.year AS year, node.group AS group,
               s.name AS sig, node.text AS text, score
        ORDER BY score DESC""", {"query": query, "limit": limit})
    return [{**{k: v for k, v in r.items() if k != "text"}, "snippet": snippet(r["text"] or "", text)} for r in rows]


def doc(path):
    """One community document: its text and what it links to in the graph."""
    return q("""
        MATCH (d:Doc {path: $p})
        OPTIONAL MATCH (d)-[:ABOUT]->(s:SIG) WITH d, collect(s.name) AS sigs
        OPTIONAL MATCH (d)-[:MENTIONS]->(p:Person) WITH d, sigs, collect(p.handle) AS people
        OPTIONAL MATCH (d)-[:MENTIONS]->(k:KEP)
        RETURN d.path AS path, d.title AS title, d.kind AS kind, d.year AS year, d.group AS group, d.text AS text,
               sigs, people, collect({kep: k.number, title: k.title}) AS keps""", {"p": path.strip().lstrip("/")})


# ---------- agent memory: conversations are part of the graph ----------

def when(ms):
    """A time for people, with the server's zone as an offset ('Oct 17, 10:53 UTC' on Streamlit Community Cloud,
    'Oct 17, 16:23 UTC+05:30' in India)."""
    if not ms:
        return ""
    d = datetime.fromtimestamp(ms / 1000).astimezone()
    mins = int(d.utcoffset().total_seconds() // 60)
    return f"{d:%b %d, %H:%M} UTC" + (f"{'+' if mins > 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}" if mins else "")


@cache
def code_scope():
    """The top-level folders whose Go imports were loaded (impact and centrality only see these)."""
    return [r["top"] for r in q("""
        MATCH (d:Dir)-[:IMPORTS]->() WITH split(d.path, '/') AS p
        RETURN DISTINCT p[0] + '/' + p[1] AS top ORDER BY top""")]


def data_scope():
    """What the graph covers, in words, for the agent's prompt."""
    prs = q("MATCH (pr:PR) RETURN min(pr.merged_at) AS first, max(pr.merged_at) AS last, count(pr) AS n")[0]
    loaded = (q("MATCH (m:Meta {id: 'load'}) RETURN m.loaded_at AS at") or [{"at": None}])[0]["at"]
    return (f"- Go imports and feature-gate checks are loaded only for {', '.join(code_scope())}. Impact, centrality and "
            "gate-to-code results cover only that code; code elsewhere in Kubernetes may also depend on it.\n"
            f"- Merged pull requests: {prs['n']} from {str(prs['first'])[:10]} to {str(prs['last'])[:10]}. "
            "'Experts', 'recent changes' and PR counts only see that window.\n"
            "- OWNERS files, SIGs and KEPs are loaded for the whole project.\n"
            f"- Community documents: {q('MATCH (d:Doc) RETURN count(d) AS n')[0]['n']} from kubernetes/community (SIG and "
            "working-group READMEs, charters, annual reports, and the DevStats README). People in them appear only "
            "as GitHub handles; other names were replaced by [name]. Nothing else from that repo is loaded (no meeting "
            "notes, no DevStats numbers).\n"
            f"- Loaded {loaded}. Nothing after that is known.")


def stats():
    """What the graph holds and where it came from, as recorded by ingest.py (counts are taken at load time)."""
    rows = q("MATCH (m:Meta {id: 'load'}) RETURN properties(m) AS m")
    m = rows[0]["m"] if rows else {}
    counts = [{"kind": k, "kind_label": KIND_LABEL.get(k, k), "n": n}
              for k, n in zip(m.get("kinds", []), m.get("kind_counts", []))]
    source = {k: v for k, v in m.items() if k not in ("id", "kinds", "kind_counts", "relationships") and not k.startswith("schema_")}
    repos = {"kubernetes": "kubernetes", "enhancements": "enhancements", "community": "community"}
    commits = [{"repo": f"kubernetes/{r}", "commit": m[f"{r}_commit"][:10], "url": f"{GITHUB}/{r}/commit/{m[f'{r}_commit']}"}
               for r in repos if m.get(f"{r}_commit")]
    schema = list(zip(m.get("schema_src", []), m.get("schema_rel", []), m.get("schema_dst", []), m.get("schema_n", [])))
    schema_graph = {"nodes": [node(k, k, label=KIND_LABEL.get(k, k)) for k in dict.fromkeys(x for a, _, b, _ in schema for x in (a, b))],
                    "edges": [{"src": f"{a}:{a}", "rel": f"{rel} · {n:,}", "dst": f"{b}:{b}"} for a, rel, b, n in schema]}
    schema_graph |= {"total_nodes": len(schema_graph["nodes"]), "shown_nodes": len(schema_graph["nodes"])}
    return {"counts": counts, "nodes": sum(m.get("kind_counts", [])), "relationships": m.get("relationships", 0),
            "source": source, "commits": commits,
            "schema": schema_graph}


# ---------- node profiles (the detail panel shown when a graph node is clicked) ----------

def sig_profile(sig):
    return q("""
        MATCH (s:SIG {name: $s})
        OPTIONAL MATCH (c:Person)-[:CHAIRS]->(s) WITH s, collect(DISTINCT c.handle) AS chairs
        OPTIONAL MATCH (t:Person)-[:TECH_LEADS]->(s) WITH s, chairs, collect(DISTINCT t.handle) AS leads
        OPTIONAL MATCH (k:KEP)-[:OWNED_BY]->(s) WITH s, chairs, leads, count(DISTINCT k) AS keps
        OPTIONAL MATCH (d:Dir)-[:LABELED]->(s) WITH s, chairs, leads, keps, count(DISTINCT d) AS dirs
        OPTIONAL MATCH (sp:Subproject)-[:PART_OF]->(s)
        RETURN s.title AS title, s.mission AS mission, chairs, leads, keps, dirs, collect(DISTINCT sp.name) AS subprojects""",
             {"s": sig})


def team_profile(team):
    return q("""
        MATCH (t:Team {name: $t})
        OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t) WITH t, collect(DISTINCT p.handle) AS members
        OPTIONAL MATCH (d:Dir)-[:APPROVER]->(t) WITH t, members, collect(DISTINCT d.path) AS approves
        OPTIONAL MATCH (r:Dir)-[:REVIEWER]->(t)
        RETURN members, approves, collect(DISTINCT r.path) AS reviews""", {"t": team})


def gate_profile(gate):
    return q("""
        MATCH (g:FeatureGate {name: $g})
        OPTIONAL MATCH (k:KEP)-[:INTRODUCES]->(g) WITH g, collect(DISTINCT {kep: k.number, title: k.title, stage: k.stage}) AS keps
        OPTIONAL MATCH (d:Dir)-[:CHECKS_GATE]->(g) WITH g, keps, collect(DISTINCT d.path) AS code
        OPTIONAL MATCH (g)-[:AFFECTS]->(c:Component)
        RETURN keps, code, collect(DISTINCT c.name) AS binaries""", {"g": gate})


def pr_profile(number):
    return q("""
        MATCH (pr:PR {number: $n})
        OPTIONAL MATCH (p:Person)-[:AUTHORED]->(pr)
        OPTIONAL MATCH (pr)-[:TOUCHES]->(d:Dir)
        RETURN pr.title AS title, pr.merged_at AS merged, p.handle AS author, collect(DISTINCT d.path) AS dirs""",
             {"n": int(number)})


def component_profile(name):
    return q("""
        MATCH (c:Component {name: $c})
        OPTIONAL MATCH (c)-[:LIVES_IN]->(d:Dir) WITH c, collect(DISTINCT d.path) AS dirs
        OPTIONAL MATCH (g:FeatureGate)-[:AFFECTS]->(c)
        RETURN dirs, count(DISTINCT g) AS gates""", {"c": name})


SCHEMA = """
(:Person {handle})-[:MEMBER_OF]->(:Team {name})              # OWNERS_ALIASES groups
(:Person)-[:CHAIRS|TECH_LEADS]->(:SIG {name, title, mission})  # org chart
(:Subproject {name})-[:PART_OF]->(:SIG); (:Subproject)-[:OWNS]->(:Dir)
(:Dir {path, no_parent_owners})-[:IN]->(:Dir)                  # folder tree, root is '/'
(:Dir)-[:APPROVER|REVIEWER]->(:Person|:Team)                   # OWNERS files (inherited down the tree)
(:Dir)-[:LABELED]->(:SIG)
(:Dir)-[:IMPORTS]->(:Dir)                                      # Go package imports
(:Dir)-[:CHECKS_GATE]->(:FeatureGate {name})                   # code that checks a feature gate
(:Component {name})-[:LIVES_IN]->(:Dir)                        # kubelet, kube-scheduler, ...
(:Person)-[:AUTHORED]->(:PR {number, title, merged_at})-[:TOUCHES]->(:Dir)
(:KEP {number, title, summary, status, stage, milestone, created, updated, path})-[:OWNED_BY|INVOLVES]->(:SIG)
(:Person)-[:KEP_AUTHOR|KEP_REVIEWER|KEP_APPROVER]->(:KEP)
(:KEP)-[:SEE_ALSO|REPLACES]->(:KEP)
(:KEP)-[:INTRODUCES]->(:FeatureGate)-[:AFFECTS]->(:Component)
(:Doc {path, title, kind, year, group, text})-[:ABOUT]->(:SIG); (:Doc)-[:MENTIONS]->(:Person|:KEP)  # community docs
"""
