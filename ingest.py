"""Load the Kubernetes "company" into FalkorDB.

Every source is turned into plain rows by a pure function, then written with one UNWIND query.
Only GitHub handles are stored for people: no names, emails or employers.

    uv run ingest.py            # expects repos cloned under ./data (see README)
"""
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

import yaml
from redis.exceptions import ResponseError

import snapshot
from db import q

DATA = Path(__file__).parent / "data"
K8S = DATA / "kubernetes"
KEPS = DATA / "enhancements" / "keps"
SIGS = DATA / "community" / "sigs.yaml"

# Which code directories each binary lives in (feature gates name binaries, not folders).
COMPONENT_DIRS = {
    "kubelet": ["pkg/kubelet", "cmd/kubelet"],
    "kube-scheduler": ["pkg/scheduler", "cmd/kube-scheduler"],
    "kube-apiserver": ["pkg/kubeapiserver", "cmd/kube-apiserver"],
    "kube-controller-manager": ["pkg/controller", "cmd/kube-controller-manager"],
    "kube-proxy": ["pkg/proxy", "cmd/kube-proxy"],
}

GATE_RE = re.compile(r"\b(?:kube)?features\.([A-Z][A-Za-z0-9]+)\b")
IMPORT_RE = re.compile(r'"k8s\.io/kubernetes/([^"]+)"')
PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ([\w-]+)/")
PR_SQUASH_RE = re.compile(r"\(#(\d+)\)$")


# ---------- small pure helpers ----------

def handle(s):
    return str(s).strip().lstrip("@").split()[0].lower() if s else None


PLACEHOLDERS = {"tbd", "tba", "todo", "none", "n/a", "na", "unknown"}


def is_person(h):
    """GitHub handles only: not placeholders like 'tbd' or group names like 'sig-node-leads' / 'kubernetes/x'."""
    return bool(h) and h not in PLACEHOLDERS and "/" not in h and not h.startswith(("sig-", "wg-", "committee-", "ug-"))


def as_list(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def dir_of(path):
    p = str(PurePosixPath(path).parent)
    return "/" if p == "." else p


def parent(path):
    return None if path == "/" else dir_of(path)


def ancestors(path):
    while path:
        yield path
        path = parent(path)


def git_head(repo):
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def chunks(rows, n=2000):
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def write(cypher, rows):
    t = time.time()
    for batch in chunks(rows):
        q(cypher, {"rows": batch})
    print(f"  {len(rows):>6} rows {time.time() - t:6.1f}s  {cypher.split(chr(10))[1].strip()[:60]}")


def read_yaml(path):
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text) or {}
    except ValueError:  # e.g. an impossible date; read every value as plain text instead
        return yaml.load(text, Loader=yaml.BaseLoader) or {}
    except Exception as e:  # genuinely malformed upstream file: skip it
        print(f"  skip {path}: {e.__class__.__name__}")
        return {}


# ---------- parse: OWNERS files ----------

def parse_aliases():
    data = read_yaml(K8S / "OWNERS_ALIASES").get("aliases", {}) or {}
    return {team: [handle(m) for m in as_list(members)] for team, members in data.items()}


def parse_owners(aliases):
    """Rows of (dir, role, owner, kind) plus dir flags. Aliases become Team nodes."""
    owners, flags, labels = [], [], []
    for f in K8S.rglob("OWNERS"):
        d = dir_of(f.relative_to(K8S).as_posix())
        y = read_yaml(f)
        filt = (y.get("filters") or {}).values()
        for role in ("approvers", "reviewers"):
            names = as_list(y.get(role)) + [n for fv in filt for n in as_list((fv or {}).get(role))]
            for n in names:
                if not n:
                    continue
                kind = "Team" if n in aliases else "Person"
                if kind == "Person" and not is_person(handle(n)):
                    continue  # e.g. a GitHub team name used where a handle belongs
                owners.append({"dir": d, "role": role[:-1].upper(), "name": n if kind == "Team" else handle(n), "kind": kind})
        flags.append({"dir": d, "stop": bool((y.get("options") or {}).get("no_parent_owners"))})
        labels += [{"dir": d, "sig": "sig-" + l.split("/", 1)[1]} for l in as_list(y.get("labels")) if str(l).startswith("sig/")]
    return owners, flags, labels


# ---------- parse: Go code (imports + feature gate checks) ----------

def parse_code():
    imports, gates = set(), set()
    for f in K8S.rglob("*.go"):
        if f.name.endswith("_test.go"):
            continue
        d = dir_of(f.relative_to(K8S).as_posix())
        text = f.read_text(encoding="utf-8", errors="ignore")
        head = text.split("\nfunc ", 1)[0]
        imports |= {(d, imp) for imp in IMPORT_RE.findall(head) if imp != d}
        gates |= {(d, g) for g in GATE_RE.findall(text)}
    return [{"src": a, "dst": b} for a, b in imports], [{"dir": a, "gate": g} for a, g in gates]


# ---------- parse: merged pull requests from git history ----------

def parse_prs():
    out = subprocess.run(
        ["git", "-C", str(K8S), "log", "--first-parent", "--diff-merges=first-parent",
         "--name-only", "--format=@@%cI|%s%n%b%n@@files"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", check=True,
    ).stdout
    prs = []
    for block in out.split("@@")[1:]:
        if block.startswith("files"):
            if prs:
                prs[-1]["dirs"] = sorted({dir_of(l) for l in block.splitlines()[1:] if l.strip()})
            continue
        first, _, body = block.partition("\n")
        date, _, subject = first.partition("|")
        if m := PR_MERGE_RE.match(subject):
            num, author = int(m[1]), m[2].lower()
            title = body.strip().splitlines()[0] if body.strip() else subject
        elif m := PR_SQUASH_RE.search(subject):
            num, author, title = int(m[1]), None, subject
        else:
            continue
        prs.append({"number": num, "author": author, "title": title[:200], "merged_at": date, "dirs": []})
    return prs


# ---------- parse: SIGs (the org chart) ----------

def k8s_owner_dir(url):
    m = re.search(r"kubernetes/kubernetes/(?:blob/)?master/(.+)/OWNERS$", url or "")
    return m[1] if m else None


def parse_sigs():
    y = read_yaml(SIGS)
    sigs, leads, subs = [], [], []
    for s in y.get("sigs", []):
        sigs.append({"name": s["dir"], "title": s.get("name"), "mission": (s.get("mission_statement") or "").strip()})
        for role in ("chairs", "tech_leads"):
            leads += [{"sig": s["dir"], "role": role.upper(), "person": handle(p.get("github"))}
                      for p in (s.get("leadership") or {}).get(role, []) or []]
        for sp in s.get("subprojects", []) or []:
            dirs = [d for d in map(k8s_owner_dir, sp.get("owners", []) or []) if d]
            subs.append({"sig": s["dir"], "name": sp["name"], "dirs": dirs})
    return sigs, leads, subs


# ---------- community documents: SIG and working-group READMEs, charters, annual reports ----------

DOCS = DATA / "community"
DOC_GLOBS = ["sig-*/README.md", "sig-*/charter.md", "sig-*/annual-report-*.md", "wg-*/README.md", "wg-*/charter.md",
             "wg-*/annual-report-*.md", "sig-contributor-experience/devstats/README.md"]
DOC_CHARS = 12000  # text kept per document (searchable and readable by the agent)
LEAD_LINE = re.compile(r"^\s*[*-]\s*(.+?)\s*\(\*\*\[@([\w-]+)\]\([^)]*\)\*\*\).*$", re.M)  # "* Name (**[@handle](url)**), Employer"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WORD = r"[A-ZÀ-ÖØ-Þ][^\W\d_A-ZÀ-ÖØ-Þ]+"  # a capitalised word, accents included (CamelCase like DevStats does not match)
PROPER = re.compile(rf"(?<![\w@/.-])({WORD}(?:[ -]{WORD}){{0,2}})(?![\w/])")
KEEP = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
        "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "monday",
        "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "pacific", "eastern", "central", "european",
        "europe", "america", "north", "south", "asia", "english", "spanish", "chinese", "japanese", "china", "india",
        "japan", "london", "paris", "amsterdam", "chicago", "atlanta", "salt", "lake", "detroit", "valencia", "shanghai",
        "hong", "kong", "tokyo", "summit", "contributor", "summer"}  # calendar words and places: not people
KEP_REF = re.compile(r"(?:KEP[- #]?|/keps/sig-[\w-]+/|enhancements/issues/)(\d{2,5})\b")
AT_HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9][A-Za-z0-9-]{1,38})\b")


def lead_names():
    """Name -> GitHub handle for everyone named in sigs.yaml leadership and in README leadership lists. Used only to
    replace those names with handles in document text; the names themselves are never stored."""
    y = read_yaml(SIGS)
    names = {}
    for group in ("sigs", "workinggroups", "committees", "usergroups"):
        for g in y.get(group, []) or []:
            for people in (g.get("leadership") or {}).values():
                for p in people or []:
                    if p.get("name") and handle(p.get("github")):
                        names[p["name"].strip()] = handle(p["github"])
    for path in DOCS.glob("*/README.md"):
        names.update({n.strip(): h.lower() for n, h in LEAD_LINE.findall(path.read_text(encoding="utf-8", errors="ignore"))})
    return names


def scrub(text, names, lowercase_words):
    """Document text with people reduced to GitHub handles: leadership lines keep only the handle (no name, no
    employer), known names become @handle, e-mail addresses go, and any run of capitalised words with a word that
    never appears in lower case in the documents (names, employers, places) becomes [name]. A lone capitalised word
    that starts a sentence stays ("Additionally"), unless it is a known person's first name. Errs towards removing
    too much."""
    text = LEAD_LINE.sub(lambda m: f"- @{m.group(2).lower()}", text)
    text = EMAIL.sub("[email]", text)
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, f"@{names[name]}")
    first_names = {n.split()[0].lower() for n in names}

    def proper(m):
        words = [w.lower() for w in re.split(r"[ -]", m.group(1))]
        if all(w in lowercase_words or w in KEEP or w.removesuffix("s") in KEEP for w in words):
            return m.group(0)  # ordinary words, capitalised ("Project Health", "March")
        starts_sentence = re.search(r"(^|[.!?:#*|>(\[-])\s*$", m.string[max(0, m.start() - 4):m.start()]) or m.start() == 0
        if len(words) == 1 and starts_sentence and words[0] not in first_names:
            return m.group(0)
        return "[name]"
    return PROPER.sub(proper, text)


def parse_docs():
    """One row per community document: path, title, kind, year, group folder, scrubbed text, and the GitHub handles
    and KEP numbers it mentions."""
    paths = sorted({p for g in DOC_GLOBS for p in DOCS.glob(g)})
    raw = {p: p.read_text(encoding="utf-8", errors="ignore") for p in paths}
    vocabulary = [*raw.values(), *(f.read_text(encoding="utf-8", errors="ignore") for f in KEPS.glob("*/*/README.md"))]
    prose = (re.sub(r"@[\w-]+|https?://\S+|\]\([^)]*\)", " ", t) for t in vocabulary)  # handles and links hold names
    lowercase_words = {w for t in prose for w in re.findall(r"\b[^\W\d_]+\b", t) if w.islower()}
    names = lead_names()
    rows = []
    for p, t in raw.items():
        rel = p.relative_to(DOCS).as_posix()
        clean = scrub(t, names, lowercase_words)
        title = next((line.lstrip("# ").strip() for line in clean.splitlines() if line.startswith("# ")), rel)
        year = re.search(r"annual-report-(\d{4})", rel)
        kind = ("Annual report" if year else "Charter" if rel.endswith("charter.md")
                else "DevStats README" if "devstats" in rel else "README")
        rows.append({"path": rel, "title": title, "kind": kind, "year": int(year.group(1)) if year else None,
                     "group": rel.split("/")[0], "text": clean[:DOC_CHARS],
                     "handles": sorted({h.lower() for h in AT_HANDLE.findall(clean) if is_person(h.lower())}),
                     "keps": sorted(set(KEP_REF.findall(t)))})
    return rows


# KEP files sometimes misspell SIG names; map the known variants, drop anything that is not a SIG.
SIG_ALIASES = {"sig-scheduler": "sig-scheduling", "sig-arch": "sig-architecture", "sig-apimachinery": "sig-api-machinery",
               "sig-machinery": "sig-api-machinery", "sig-contribex": "sig-contributor-experience"}


def canonical_sig(name, known):
    n = str(name or "").strip().lower()
    n = SIG_ALIASES.get(n, n)
    return n if n in known else None


# ---------- parse: KEPs (design decisions) ----------

def kep_id(path):
    """'/keps/sig-node/753-sidecar-containers' -> '753'"""
    m = re.search(r"/(\d+)-[^/]*/?$", str(path or ""))
    return m[1] if m else None


def kep_summary(folder, limit=900):
    """The '## Summary' section of a KEP's README, without template comments."""
    f = folder / "README.md"
    if not f.exists():
        return ""
    text = f.read_text(encoding="utf-8", errors="ignore")
    body = text.split("\n## Summary", 1)[1].split("\n## ", 1)[0] if "\n## Summary" in text else ""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return " ".join(body.split())[:limit]


def parse_keps():
    keps, people, links, gates = [], [], [], []
    for f in KEPS.glob("*/*/kep.yaml"):
        y = read_yaml(f)
        num = str(y.get("kep-number") or kep_id(f.parent.as_posix()) or "")
        if not num:
            continue
        keps.append({
            "number": num, "title": y.get("title"), "status": y.get("status"), "stage": y.get("stage"),
            "milestone": str(y.get("latest-milestone") or ""), "created": str(y.get("creation-date") or ""),
            "updated": str(y.get("last-updated") or ""), "owning_sig": y.get("owning-sig"),
            "sigs": [s for s in as_list(y.get("participating-sigs")) if s],
            "path": f.parent.relative_to(KEPS.parent).as_posix(), "summary": kep_summary(f.parent),
        })
        for role in ("authors", "reviewers", "approvers"):
            people += [{"kep": num, "role": role[:-1].upper(), "person": handle(p)} for p in as_list(y.get(role)) if is_person(handle(p))]
        for rel, key in (("SEE_ALSO", "see-also"), ("REPLACES", "replaces")):
            links += [{"src": num, "dst": kep_id(t), "rel": rel} for t in as_list(y.get(key)) if kep_id(t)]
        for g in as_list(y.get("feature-gates")):
            if isinstance(g, dict) and g.get("name"):
                gates.append({"kep": num, "gate": g["name"], "components": [c for c in as_list(g.get("components")) if c]})
    return keps, people, links, gates


# ---------- write ----------

SCHEMA = [
    "CREATE INDEX FOR (d:Dir) ON (d.path)",
    "CREATE INDEX FOR (p:Person) ON (p.handle)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (s:SIG) ON (s.name)",
    "CREATE INDEX FOR (k:KEP) ON (k.number)",
    "CREATE INDEX FOR (g:FeatureGate) ON (g.name)",
    "CREATE INDEX FOR (r:PR) ON (r.number)",
    "CREATE INDEX FOR (c:Component) ON (c.name)",
    "CREATE INDEX FOR (d:Doc) ON (d.path)",
]


def load_dirs(paths):
    rows = sorted({a for p in paths for a in ancestors(p)})
    write("""UNWIND $rows AS p
        MERGE (:Dir {path: p})""", rows)
    write("""UNWIND $rows AS r
        MATCH (c:Dir {path: r.child}) MATCH (p:Dir {path: r.parent}) MERGE (c)-[:IN]->(p)""",
          [{"child": p, "parent": parent(p)} for p in rows if parent(p)])


def load_org(sigs, leads, subs):
    """Org chart: SIGs (departments), their chairs and tech leads, and the subprojects they own."""
    write("""UNWIND $rows AS r
        MERGE (s:SIG {name: r.name}) SET s.title = r.title, s.mission = r.mission""", sigs)
    write("""UNWIND $rows AS r
        MATCH (s:SIG {name: r.sig}) MERGE (p:Person {handle: r.person})
        FOREACH (_ IN CASE WHEN r.role = 'CHAIRS' THEN [1] ELSE [] END | MERGE (p)-[:CHAIRS]->(s))
        FOREACH (_ IN CASE WHEN r.role = 'TECH_LEADS' THEN [1] ELSE [] END | MERGE (p)-[:TECH_LEADS]->(s))""", leads)
    write("""UNWIND $rows AS r
        MATCH (s:SIG {name: r.sig}) MERGE (sp:Subproject {name: r.name}) MERGE (sp)-[:PART_OF]->(s)
        WITH sp, r UNWIND r.dirs AS d MATCH (dir:Dir {path: d}) MERGE (sp)-[:OWNS]->(dir)""", subs)


def load_ownership(aliases, owners, flags, dir_labels):
    """Who owns which code: OWNERS files (approvers, reviewers, SIG labels) and OWNERS_ALIASES teams."""
    write("""UNWIND $rows AS r
        MERGE (t:Team {name: r.team}) WITH t, r UNWIND r.members AS m
        MERGE (p:Person {handle: m}) MERGE (p)-[:MEMBER_OF]->(t)""",
          [{"team": t, "members": [m for m in ms if m]} for t, ms in aliases.items()])
    write("""UNWIND $rows AS r
        MATCH (d:Dir {path: r.dir}) SET d.no_parent_owners = r.stop""", flags)
    for kind, key in (("Person", "handle"), ("Team", "name")):
        for role in ("APPROVER", "REVIEWER"):
            write(f"""UNWIND $rows AS r
                MATCH (d:Dir {{path: r.dir}}) MERGE (o:{kind} {{{key}: r.name}}) MERGE (d)-[:{role}]->(o)""",
                  [r for r in owners if r["kind"] == kind and r["role"] == role])
    write("""UNWIND $rows AS r
        MATCH (d:Dir {path: r.dir}) MATCH (s:SIG {name: r.sig}) MERGE (d)-[:LABELED]->(s)""", dir_labels)


def load_code(imports, code_gates):
    """Code structure: Go imports between packages, feature gates the code checks, binaries."""
    write("""UNWIND $rows AS r
        MATCH (a:Dir {path: r.src}) MATCH (b:Dir {path: r.dst}) MERGE (a)-[:IMPORTS]->(b)""", imports)
    write("""UNWIND $rows AS r
        MATCH (d:Dir {path: r.dir}) MERGE (g:FeatureGate {name: r.gate}) MERGE (d)-[:CHECKS_GATE]->(g)""", code_gates)
    write("""UNWIND $rows AS r
        MERGE (c:Component {name: r.name}) WITH c, r UNWIND r.dirs AS d
        MATCH (dir:Dir {path: d}) MERGE (c)-[:LIVES_IN]->(dir)""",
          [{"name": n, "dirs": ds} for n, ds in COMPONENT_DIRS.items()])


def load_history(prs):
    """Work history: merged pull requests, who authored them, which folders they touched."""
    write("""UNWIND $rows AS r
        MERGE (pr:PR {number: r.number}) SET pr.title = r.title, pr.merged_at = r.merged_at
        FOREACH (a IN CASE WHEN r.author IS NULL THEN [] ELSE [r.author] END |
            MERGE (p:Person {handle: a}) MERGE (p)-[:AUTHORED]->(pr))
        WITH pr, r UNWIND r.dirs AS d MATCH (dir:Dir {path: d}) MERGE (pr)-[:TOUCHES]->(dir)""", prs)


def load_decisions(keps, kep_people, kep_links, kep_gates):
    """Design decisions: KEPs, their SIGs and people, links between them, and the feature gates they introduce."""
    write("""UNWIND $rows AS r
        MERGE (k:KEP {number: r.number})
        SET k.title = r.title, k.status = r.status, k.stage = r.stage, k.milestone = r.milestone,
            k.created = r.created, k.updated = r.updated, k.path = r.path, k.summary = r.summary
        WITH k, r OPTIONAL MATCH (s:SIG {name: r.owning_sig})
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | MERGE (k)-[:OWNED_BY]->(s))
        WITH k, r UNWIND r.sigs AS ps MATCH (s2:SIG {name: ps}) MERGE (k)-[:INVOLVES]->(s2)""", keps)
    for role in ("AUTHOR", "REVIEWER", "APPROVER"):
        write(f"""UNWIND $rows AS r
            MATCH (k:KEP {{number: r.kep}}) MERGE (p:Person {{handle: r.person}}) MERGE (p)-[:KEP_{role}]->(k)""",
              [r for r in kep_people if r["role"] == role])
    for rel in ("SEE_ALSO", "REPLACES"):
        write(f"""UNWIND $rows AS r
            MATCH (a:KEP {{number: r.src}}) MATCH (b:KEP {{number: r.dst}}) MERGE (a)-[:{rel}]->(b)""",
              [r for r in kep_links if r["rel"] == rel])
    write("""UNWIND $rows AS r
        MATCH (k:KEP {number: r.kep}) MERGE (g:FeatureGate {name: r.gate}) MERGE (k)-[:INTRODUCES]->(g)
        WITH g, r UNWIND r.components AS c MERGE (comp:Component {name: c}) MERGE (g)-[:AFFECTS]->(comp)""", kep_gates)


def load_docs(docs):
    """Community documents (READMEs, charters, annual reports) with full-text search, linked to their SIG and to
    the people and KEPs they mention (only ones already in the graph)."""
    try:
        q("CALL db.idx.fulltext.createNodeIndex('Doc', 'title', 'text')")
    except ResponseError:
        pass  # index already exists
    write("""UNWIND $rows AS r
        MERGE (d:Doc {path: r.path}) SET d.title = r.title, d.kind = r.kind, d.year = r.year, d.group = r.group, d.text = r.text
        WITH d, r OPTIONAL MATCH (s:SIG {name: r.group})
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | MERGE (d)-[:ABOUT]->(s))""", docs)
    write("""UNWIND $rows AS r
        MATCH (d:Doc {path: r.path}) UNWIND r.handles AS h MATCH (p:Person {handle: h}) MERGE (d)-[:MENTIONS]->(p)""", docs)
    write("""UNWIND $rows AS r
        MATCH (d:Doc {path: r.path}) UNWIND r.keps AS n MATCH (k:KEP {number: n}) MERGE (d)-[:MENTIONS]->(k)""", docs)


def record_provenance(prs):
    """What was loaded, from which commits and history window, with counts and the schema (shown in the app)."""
    counts = q("MATCH (n) WHERE NOT n:Meta RETURN labels(n)[0] AS kind, count(*) AS n ORDER BY n DESC")
    rels = q("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
    schema = q("""MATCH (a)-[r]->(b)
                  RETURN labels(a)[0] AS src, type(r) AS rel, labels(b)[0] AS dst, count(*) AS n ORDER BY n DESC""")
    q("MERGE (m:Meta {id: 'load'}) SET m += $props", {"props": {
        "kinds": [r["kind"] for r in counts], "kind_counts": [r["n"] for r in counts], "relationships": rels,
        **{f"schema_{k}": [r[k] for r in schema] for k in ("src", "rel", "dst", "n")},
        "loaded_at": time.strftime("%Y-%m-%d %H:%M"),
        "prs_since": min((p["merged_at"][:10] for p in prs), default=""),
        **{f"{name}_commit": git_head(DATA / name) for name in ("kubernetes", "enhancements", "community")},
    }})


def main():
    q("MATCH (n) DETACH DELETE n")  # conversation memory lives in per-user graphs (memory_graph.py), not here
    for s in SCHEMA:
        try:
            q(s)
        except ResponseError:
            continue  # index already exists

    print("parsing ...")
    aliases = parse_aliases()
    owners, flags, dir_labels = parse_owners(aliases)
    imports, code_gates = parse_code()
    prs = parse_prs()
    sigs, leads, subs = parse_sigs()
    keps, kep_people, kep_links, kep_gates = parse_keps()
    docs = parse_docs()
    known = {s["name"] for s in sigs}
    keps = [{**k, "owning_sig": canonical_sig(k["owning_sig"], known),
             "sigs": sorted({c for c in (canonical_sig(x, known) for x in k["sigs"]) if c})} for k in keps]
    dir_labels = [r for r in ({**r, "sig": canonical_sig(r["sig"], known)} for r in dir_labels) if r["sig"]]

    print("writing ...")
    load_dirs([r["dir"] for r in flags] + [r["src"] for r in imports] + [r["dst"] for r in imports]
              + [d for p in prs for d in p["dirs"]] + [d for s in subs for d in s["dirs"]]
              + [d for ds in COMPONENT_DIRS.values() for d in ds])
    load_org(sigs, leads, subs)
    load_ownership(aliases, owners, flags, dir_labels)
    load_code(imports, code_gates)
    load_history(prs)
    load_decisions(keps, kep_people, kep_links, kep_gates)
    load_docs(docs)
    record_provenance(prs)
    print("  snapshot:", snapshot.export())  # what the app restores from if the database ever loses the graph
    print("done:", q("MATCH (n) RETURN count(n) AS nodes")[0], q("MATCH ()-[r]->() RETURN count(r) AS edges")[0])


if __name__ == "__main__":
    main()
