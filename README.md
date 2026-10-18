# Kortex: the Kubernetes org brain

**An agent that understands how the Kubernetes project works as a company, with FalkorDB as its context layer.**

Built for [Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) by WeMakeDevs · powered by
[FalkorDB](https://www.falkordb.com) · made with [Streamlit](https://streamlit.io).

**Live app: https://kortex-falkordb.streamlit.app** (bring your own OpenRouter, Anthropic, Groq or OpenAI key for the chat;
Explore, Memory and Data work without one).

Kubernetes has thousands of contributors, 24 departments (SIGs), formal design decisions (KEPs),
code ownership rules (OWNERS files) and a steady stream of merged work. Knowing *who must approve a change,
which decisions constrain it, what else could break, and who actually knows the code* means piecing
together four sources by hand. This agent answers from one graph and shows the path behind every answer.

Tracks: **Company Brain** (primary), **Agents That Act on Connected Data**, **Agent Memory and Coordination**.

## What it answers

| Question | Graph work behind it |
|---|---|
| Trace KEP-2837 to the code it changes and the people who must approve it | One walk across every source: KEP → feature gate → code that checks it → nearest inherited OWNERS file → teams → people |
| Who should review a change to `pkg/kubelet/eviction`? | Inherited OWNERS approvers ∩ people who merged changes there recently |
| What is the most critical code in `pkg/kubelet`, and is it thinly owned? | FalkorDB **PageRank** and **betweenness** over Go imports, joined with how many people can approve each package |
| What could break if I change it? | Reverse Go imports followed hop by hop until nothing new is reached, mapped to the binary (kubelet, kube-scheduler) the code ends up in |
| Which design decisions constrain this code? | Folder → feature gates its code checks → KEPs that introduced them → linked KEPs |
| Who would we miss most in sig-node? | Approvers per SIG-owned folder (teams expanded); single-approver folders |
| What changed recently, or since I last asked? | Merged PRs under the folder and updated KEPs behind it, since a date from graph memory |
| What did SIG Node's 2024 annual report say? What is DevStats? | FalkorDB **full-text index** over 224 community documents (SIG and working-group READMEs, charters, annual reports, the DevStats README), each linked to its SIG and to the KEPs and people it mentions |
| What did I ask last time? | Conversations are stored in the graph and linked to every node they used, so recall works by topic across sessions |
| Write a review plan for my change | **Action**: owners, reviewers, KEPs, blast radius and recent contributors in a markdown file you can download |

## Graph data model

```
(:Person {handle})-[:MEMBER_OF]->(:Team {name})                  OWNERS_ALIASES groups
(:Person)-[:CHAIRS|TECH_LEADS]->(:SIG {name, title, mission})     org chart (sigs.yaml)
(:Subproject {name})-[:PART_OF]->(:SIG)    (:Subproject)-[:OWNS]->(:Dir)
(:Dir {path, no_parent_owners})-[:IN]->(:Dir)                     folder tree, root is '/'
(:Dir)-[:APPROVER|REVIEWER]->(:Person|:Team)                      OWNERS files, inherited down the tree
(:Dir)-[:LABELED]->(:SIG)
(:Dir)-[:IMPORTS]->(:Dir)                                         Go package imports
(:Dir)-[:CHECKS_GATE]->(:FeatureGate {name})                      code that checks a feature gate
(:Component {name})-[:LIVES_IN]->(:Dir)                           kubelet, kube-scheduler, ...
(:Person)-[:AUTHORED]->(:PR {number, title, merged_at})-[:TOUCHES]->(:Dir)
(:KEP {number, title, summary, status, stage, milestone, updated, path})-[:OWNED_BY|INVOLVES]->(:SIG)
(:Person)-[:KEP_AUTHOR|KEP_REVIEWER|KEP_APPROVER]->(:KEP)
(:KEP)-[:SEE_ALSO|REPLACES]->(:KEP)
(:KEP)-[:INTRODUCES]->(:FeatureGate)-[:AFFECTS]->(:Component)
(:Doc {path, title, kind, year, group, text})-[:ABOUT]->(:SIG)    community docs, full-text index on title + text
(:Doc)-[:MENTIONS]->(:Person|:KEP)
(:Meta {id: 'load', commits, counts, schema, loaded_at})                                                 provenance
```

Conversation memory is **not** in this graph. Each user has a memory graph of their own (see "How memory works"):
```
(:User {name})-[:HAD]->(:Session {id})-[:HAS]->(:Turn {question, answer, at, refs})
(:Turn)-[:NEXT]->(:Turn)                               the user's timeline, across chats
(:Turn)-[:MENTIONS]->(:Ref {id, kind, key, title})     what the answer used, by its key in the Kubernetes graph
```

About 10,100 nodes and 33,500 relationships: 586 OWNERS files, ~645 KEPs with their summaries, 24 SIGs,
~1,200 merged PRs, Go imports for the kubelet and scheduler, 224 community documents. The **Data** page shows the exact counts,
the schema as found in the data, and the commits it was loaded from.

Data cleaning at load time: SIG names misspelled in KEP files (e.g. `sig-scheduler`) are mapped to the real
SIG; placeholders (`tbd`) and group names (`sig-node-leads`) are not loaded as people; YAML with invalid
dates is read as plain text instead of being skipped.

## The Cypher and graph algorithms the product depends on

All queries live in [`queries.py`](queries.py), one function each. The key ones:

**Inherited ownership** (variable-depth walk up the folder tree, cut at `no_parent_owners`):
```cypher
MATCH p = (d:Dir {path: $path})-[:IN*0..]->(a:Dir)
WHERE none(n IN nodes(p)[0..size(nodes(p)) - 1] WHERE coalesce(n.no_parent_owners, false))
MATCH (a)-[:APPROVER]->(o)
OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
RETURN a.path AS granted_by, length(p) AS levels_up, coalesce(o.handle, o.name) AS owner, collect(m.handle) AS team_members
ORDER BY levels_up
```

**From a design decision to the people who approve its code** (one query across all sources):
```cypher
MATCH (k:KEP {number: $n})-[:INTRODUCES]->(g:FeatureGate)<-[:CHECKS_GATE]-(d:Dir)
MATCH p = (d)-[:IN*0..]->(a:Dir)
WHERE none(n IN nodes(p)[0..size(nodes(p)) - 1] WHERE coalesce(n.no_parent_owners, false)) AND (a)-[:APPROVER]->()
WITH k, g, d, a.path AS owners_path, length(p) AS up ORDER BY up
WITH k, g, d, collect(owners_path)[0] AS owners_path, min(up) AS up
MATCH (:Dir {path: owners_path})-[:APPROVER]->(o)
OPTIONAL MATCH (m:Person)-[:MEMBER_OF]->(o:Team)
RETURN g.name AS gate, d.path AS code, owners_path, coalesce(o.handle, o.name) AS owner, collect(DISTINCT m.handle) AS members
```

**Critical code** (FalkorDB PageRank over imports, then the nearest approvers of each package):
```cypher
CALL algo.pageRank('Dir', 'IMPORTS') YIELD node, score
WITH node AS d, score WHERE d.path STARTS WITH $scope
WITH d, score ORDER BY score DESC LIMIT 12
// ... nearest OWNERS approvers as above ...
OPTIONAL MATCH (x:Dir)-[:IMPORTS]->(d)
RETURN d.path AS package, score, size(approvers) AS approver_count, count(DISTINCT x) AS importers
```
```cypher
CALL algo.betweenness({nodeLabels: ['Dir'], relationshipTypes: ['IMPORTS'], samplingSize: 1000, samplingSeed: 42})
YIELD node, score
```
Example finding: `pkg/kubelet/metrics` is imported by 27 packages and has a single approver.

**Design decisions that constrain code** (code → feature gate → KEP → linked KEPs):
```cypher
MATCH (d:Dir)-[:IN*0..]->(:Dir {path: $path})
MATCH (d)-[:CHECKS_GATE]->(g:FeatureGate)<-[:INTRODUCES]-(k:KEP)
OPTIONAL MATCH (k)-[:OWNED_BY]->(s:SIG)
OPTIONAL MATCH (k)-[:SEE_ALSO|REPLACES]->(rel:KEP)
RETURN k.number, k.title, k.stage, s.name, collect(DISTINCT g.name), collect(DISTINCT rel.number)
```

**Blast radius** (reverse imports, followed to the end of every chain). A breadth-first walk with one query
per hop, repeated until a hop reaches nothing new; there is no hop limit. Each package comes back with its
distance and the folder it imports on its shortest chain, so the graph shows real import chains rather than a
fan of "imports (3 hops)" edges:
```cypher
// $frontier = what the last hop reached, $seen = everything reached so far
MATCH (x:Dir)-[:IMPORTS]->(y:Dir) WHERE y.path IN $frontier AND NOT x.path IN $seen
RETURN x.path AS dependent, min(y.path) AS via
```
In the loaded code, import chains end within 4 hops (`pkg/features`, the most depended-on package, reaches
117 packages: 50 direct, 59 at 2 hops, 6 at 3, 2 at 4) and the whole walk takes about 150 ms. The same walk
follows `SEE_ALSO`/`REPLACES` links between KEPs, in both directions. A single variable-length match
(`-[:SEE_ALSO|REPLACES*1..]-`) would list every possible path and times out on densely linked KEPs; the walk
takes about 50 ms even for KEP-4381, whose links reach 88 KEPs over 10 hops.

**What changed** (merged work under a folder since a date):
```cypher
MATCH (pr:PR)-[:TOUCHES]->(:Dir)-[:IN*0..]->(:Dir {path: $path}) WHERE pr.merged_at >= $since
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(pr)
RETURN DISTINCT pr.number, pr.title, pr.merged_at, p.handle ORDER BY pr.merged_at DESC
```

**Memory recall by topic**, in the user's own memory graph (words matched against earlier questions and the
keys of the Kubernetes nodes each turn points at, so `pkg/kubelet` also finds turns about `pkg/kubelet/eviction`):
```cypher
MATCH (s:Session)-[:HAS]->(t:Turn)
OPTIONAL MATCH (t)-[:MENTIONS]->(r:Ref)
WITH s, t, collect(toLower(r.key)) AS keys
WITH s, t, reduce(hay = toLower(t.question), k IN keys | hay + ' ' + k) AS hay
WHERE all(w IN $words WHERE hay CONTAINS w)
WITH s, t ORDER BY t.at DESC LIMIT $limit
RETURN s.id, t.question, t.answer, t.at, t.refs
```

Without FalkorDB none of these answers exist: ownership inheritance, import chains, decision-to-code links,
centrality and topic memory are all graph work. The **Explore** page runs the same views with no LLM at all.

## The app

Four pages, top navigation. The sidebar holds your name, the model, the style switch and **your chats**
(from graph memory, newest first; click one to reopen it).

| Page | What it is for |
|---|---|
| **Ask** | Chat with the agent: your questions on the right, answers on the left. Starter cards say which graph walk each question triggers. Every answer shows its live step timeline, the interactive graph behind it, cost/speed badges and suggested follow-ups. **New chat** sits next to the input. |
| **Explore** | See exactly what the agent sees: pick a folder, KEP, person or SIG, then one of the agent's walks (each explained in one line). Same tools and Cypher, no model, no API key, answers in milliseconds. |
| **Memory** | What the agent remembers of the open chat, from your memory graph: each question and answer, and a graph of the Kubernetes nodes each one points at. |
| **Data** | Node counts by kind, the schema as found in the data (with relationship counts), and the exact source commits. |

**Layout.** Like a desktop chat app: your chats on the left, the conversation in the middle, and the **console**
on the right, styled like a terminal: its × minimizes it to a tab on the right edge, and the tab brings it back. The
console is a timestamped log of what runs in the background, written while it happens: model calls (model, time,
tokens, cost, which tools it asked for), tool calls with their arguments, every Cypher query (open a line to see
the query, its parameters and its response rows), the memory write, and the turn's totals. On Explore, Memory and
Data it shows the queries behind the page. It is open by default; on narrow screens it starts minimized.

**Interactive graphs.** Graphs are drawn with Cytoscape.js in a small Streamlit custom component (`ui_graph.py`):
drag, Ctrl + scroll or the buttons to zoom, and click a node to see its profile in the app (approvers, owning
SIG, design decisions, importers for a folder; teams, folders and KEPs for a person; and so on), read from the
graph by `tools.node_card()`. A plain scroll keeps scrolling the page.

**Look and feel.** One calm look built from the logo's colours: neutral surfaces with 1 px hairlines, rounded
corners on one scale (8 px controls, 12 px cards and the composer), no hard shadows, the logo's indigo for actions
and the open page, and its yellow as a small highlight under each page title. Starter cards each carry a hue
that sits next to the logo's indigo (a coloured top edge and icon chip). Your questions are soft indigo
bubbles; answers read straight on the page. Light and dark follow the viewer's system setting. Content sits in a
readable column; overview screens use a grid of cards that wraps on narrow screens. The stylesheet is in
`ui.apply_style()` and targets our own keys and Streamlit's `data-testid` attributes.

## Transparency: what you see for every answer

The UI only renders what the pipeline emits (`agent.ask_stream()`); nothing is computed in the UI.

| On screen | Comes from |
|---|---|
| **Live step timeline**: one step per tool call, appearing while the agent works ("Finding who can approve `pkg/kubelet/eviction`"). Each step opens to its result, the tables it found (with links) and how long the model took to choose it | Each tool returns a step record (LangChain `content_and_artifact`) built by `tools.record()` |
| **Console**: model calls, tool calls, every Cypher query with its parameters, row count, time and response, the memory write | The events themselves (`question`, `model`, `step_start`/`step_end`, `memory`, `done`), each stamped with its time; `db.py` records each query's response rows |
| **The answer**, streamed as it is written. Every `@handle`, `KEP-123`, `#PR` and folder links to its source, but only if a tool actually returned it, so invented names stay plain text | `answer.linkify()` |
| **Graph behind this answer**: the nodes and relationships the tools touched, coloured by kind; click a node for its profile | `tools.combine_graph()` (capped at 30 nodes, with the cap stated); profiles from `tools.node_card()` |
| **Badges**: model that actually answered (and whether a fallback kicked in), LLM time vs graph time, tokens, estimated cost, how many graph nodes the turn was linked to in memory | The `done` event |
| **Follow-up questions** | `answer.follow_ups()`: views of the same folder or KEP the turn did not use yet |

Code links point at the exact commit that was loaded, so they show what the graph saw. A typical answer:
~20 s of LLM time, ~40 ms of graph time over 2 to 6 queries, about $0.002.

## How memory works

Memory is a FalkorDB graph **per user**, separate from the Kubernetes graph (`memory_graph.py`). The graph is
named after a hash of the name typed in the sidebar (`k8s_memory_<hash>`), so even a list of graphs does not
show who uses the app. Inside it:

- each question is a `Turn` in a `Session` (one chat), chained to the user's previous turn with `NEXT`: a timeline
  across all their chats;
- each turn `MENTIONS` a `Ref` for every folder, KEP, person or SIG its answer used. A `Ref` holds the node's
  kind and key, a pointer into the Kubernetes graph rather than a copy, so links and node details are resolved
  there at read time.

What this gives:
- **Nothing personal is in the shared graph.** The Kubernetes graph holds only public project data, so no
  query on it, including the Cypher the model writes, can see anyone's questions.
- **Memory still drives retrieval.** `recall_memory` finds earlier turns by topic (a turn about
  `pkg/kubelet/eviction` matches "kubelet"); "what changed since I last asked" takes the date of that turn and
  asks the Kubernetes graph for changes since then; continuing an older chat (or one from before a restart)
  reads its turns back first, and the console says so.
- **Reloads do not touch memory.** `ingest.py` replaces the Kubernetes graph whole; refs keep pointing at the same
  keys, so nothing has to be re-linked.

Memory is separated by the name you type, with no login: anyone who types the same name sees that name's chats.

FalkorDB runs with append-only persistence (`--appendonly yes` in the Docker command below): every write,
including each remembered turn, goes to the append-only file and is flushed to disk every second, so a crash
loses at most the last second instead of everything since the last snapshot.

## Architecture

```
data/ (git clones) ──ingest.py──▶ FalkorDB ◀── queries.py ◀── tools.py ◀── agent.py (ask_stream) ◀── app.py + app_pages/
                                     ▲                                          │
                                     └──────────── conversation memory ─────────┘
```

Functional style throughout: pure parse functions return rows, one `q()` helper runs Cypher, tools are
plain decorated functions, one script per page. No classes.

| File | Role |
|---|---|
| `db.py` | `q()` / `ro()` on the Kubernetes graph, `mq()` on a memory graph: run Cypher, return dicts, record each query (graph, time, rows, response) into the current trace; `traced()` |
| `ingest.py` | Parse OWNERS, OWNERS_ALIASES, sigs.yaml, kep.yaml (+ summaries), Go imports, git history; one loader per part of the graph; record provenance |
| `queries.py` | Every Cypher query and graph algorithm call on the Kubernetes graph, typed node references with their source URLs, what the data covers, stats |
| `memory_graph.py` | Conversation memory: one graph per user (remember, recall, chats, turns) |
| `tools.py` | Agent tools returning step records, step titles, the review-plan action, the Explore views |
| `agent.py` | The prompt, the memory tool and `ask_stream()`: one turn as a stream of events |
| `models.py` | Which LLM answers: `provider:model` specs, OpenRouter fallbacks, cost estimate, errors in plain words |
| `answer.py` | Finishing the answer: strip stray citation markers, links, follow-up questions |
| `app.py` | Page setup, logo, top navigation, sidebar (name, model, API key, style, chats, credits) |
| `app_pages/` | `ask.py`, `explore.py`, `memory.py`, `data.py`: one script per page |
| `ui.py` | Shared rendering: step timeline, graphs, node details, tables with link columns, badges, the console |
| `ui_graph.py` | The interactive graph: a Streamlit custom component (v2) around Cytoscape.js |
| `snapshot.py`, `snapshot/` | The Kubernetes graph and the demo chats as compressed JSON, written by `ingest.py`; restored when missing |
| `.github/workflows/keepalive.yml` | Every 12 hours: keep the FalkorDB Cloud instance in use, restore it if it lost its data |
| `.streamlit/config.toml` | Light and dark theme; switch in the app's Settings menu |

## Setup (local)

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker run -d --name falkordb -p 127.0.0.1:6379:6379 -p 127.0.0.1:3000:3000 -e REDIS_ARGS="--appendonly yes" -v falkordb_data:/var/lib/falkordb/data falkordb/falkordb:latest
cp .env.example .env    # pick a model and add its API key (Explore, Memory and Data need none)
uv run streamlit run app.py
```

That is all: on its first start the app finds no graph and loads the bundled snapshot (`snapshot/`, the
Kubernetes graph plus the demo user's chats) in about half a minute. The snapshot is only ever loaded *into*
FalkorDB: every answer, walk and algorithm runs as Cypher on FalkorDB, and the app does not start without it.

### Rebuilding the graph from the source repositories

To load fresh data instead of the snapshot, clone the public repositories (sparse, a few minutes; needs git):

```bash
mkdir data && cd data
git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/community.git
git -C community sparse-checkout set --no-cone /sigs.yaml '/sig-*/README.md' '/sig-*/charter.md' '/sig-*/annual-report-*.md' \
  '/wg-*/README.md' '/wg-*/charter.md' '/wg-*/annual-report-*.md' /sig-contributor-experience/devstats/README.md
git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/enhancements.git
git -C enhancements sparse-checkout set keps
git clone --filter=blob:none --no-checkout --shallow-since=2025-04-01 https://github.com/kubernetes/kubernetes.git
git -C kubernetes sparse-checkout set --no-cone OWNERS OWNERS_ALIASES /pkg/kubelet/ /pkg/scheduler/ /cmd/kubelet/
git -C kubernetes checkout
cd ..
```

On Windows: run `git config --global core.longpaths true` first (some KEP folder names are long), and on
Git Bash prefix the `sparse-checkout` commands with `MSYS_NO_PATHCONV=1`.

Then load them (about 30 seconds). This also rewrites the snapshot:

```bash
uv run ingest.py
```

Re-run it after `git pull` in `data/` to refresh the graph; memory graphs are not touched.
Explore the raw graph in the FalkorDB Browser at http://localhost:3000.

### Choosing the LLM

The model is a `provider:model` string, set with `MODEL` in `.env` or in the app sidebar.
Judges can paste their own key in the sidebar; it stays in that browser session only.

| Provider | Example `MODEL` | Key |
|---|---|---|
| OpenRouter (default) | `openrouter:openai/gpt-oss-120b` | `OPENROUTER_API_KEY` |
| Anthropic (Claude) | `anthropic:claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| Groq | `groq:openai/gpt-oss-120b` | `GROQ_API_KEY` |
| OpenAI | `openai:gpt-5-mini` | `OPENAI_API_KEY` |
| AWS Bedrock | `bedrock_converse:us.amazon.nova-pro-v1:0` | AWS credential chain + `AWS_REGION` |

Any model with tool calling works. On OpenRouter, `MODEL_FALLBACKS` lists backup models tried when the main
one fails or is rate-limited. The Explore page needs no model at all.

## Demo video

The demo video is generated, not hand-edited. With the app running locally (`uv run streamlit run app.py --server.port 8502`):

```bash
uv run demo/record_demo.py
```

It speaks each scene's narration with an AI voice (edge-tts), drives and records the app in a headless browser
(Playwright), and joins sound and picture with ffmpeg into `demo/out/kortex-demo.mp4`, plus subtitles
(`kortex-demo.srt`). Each scene waits for its narration to finish before the next one starts, and each voice clip
is placed at the moment its scene began, so the two stay in step even when the model takes longer to answer. The
narration and the clicks live together in `SCENES` in `demo/record_demo.py`.

## Deployment

1. Create a free instance on [FalkorDB Cloud](https://app.falkordb.cloud/). Its endpoint (host and port) is on
   the instance's Connectivity tab; the user (`falkordb`) and password on its Instance Details tab. The Free tier
   has no TLS.
2. On [Streamlit Community Cloud](https://share.streamlit.io/), deploy this repository with `app.py` as the main
   file and Python 3.12. Add these secrets:
   ```toml
   MODEL = "openrouter:openai/gpt-oss-120b"
   REQUIRE_USER_KEY = "true"      # visitors answer with their own API key; the deployment never spends yours
   FALKORDB_HOST = "..."
   FALKORDB_PORT = "..."
   FALKORDB_USERNAME = "falkordb"
   FALKORDB_PASSWORD = "..."
   FALKORDB_TLS = "false"
   ```
   On its first start the app loads the snapshot into the instance (about a minute over the network).
3. Add the same five `FALKORDB_*` values as GitHub repository secrets for the keep-alive job
   (`.github/workflows/keepalive.yml`).

**Why the keep-alive job.** A Free FalkorDB Cloud instance that is unused for a day is stopped, and deleted after
7 days, and it keeps no data across restarts. Every 12 hours the job queries the instance, which keeps it in use,
and restores the graph and the demo chats from the snapshot if they are gone. The app does the same check
itself (at most every 5 minutes), so a restarted instance heals on the next visit too. Chats under other names
live only as long as the instance does.

## Security

- **Conversation memory** is in a separate graph per user; the Kubernetes graph holds no personal data.
- **Cypher the LLM writes** (`run_cypher`) is read-only (`GRAPH.RO_QUERY`), has a 10 s timeout, and can only
  reach the Kubernetes graph, so it can not read anyone's chats.
- **Every other query** is fixed Cypher with parameters; labels that vary come from fixed lists, never from model text.
- **Answers** never embed images (an image URL is fetched on render, so text planted in the data could use one
  to leak what is on screen), and names in an answer that no tool returned are flagged as "Not from the graph".
- **API keys** pasted in the sidebar stay in that browser session; `.env` and `.streamlit/secrets.toml` are git-ignored.
  With `REQUIRE_USER_KEY=true` the server's keys are never used for chats, and Bedrock (server AWS credentials)
  is not offered; Explore, Memory and Data work without a key.
- **FalkorDB** has no password locally, so the Docker command binds it to `127.0.0.1` only, with append-only
  persistence on.
- **Memory** is separated by the name typed in the sidebar, with no login (see "How memory works").

## Data and privacy

All data is public, from the Kubernetes GitHub repositories
([kubernetes](https://github.com/kubernetes/kubernetes), [enhancements](https://github.com/kubernetes/enhancements),
[community](https://github.com/kubernetes/community), all Apache-2.0). People are stored **only as GitHub handles**:
names, emails and employers in `sigs.yaml` are dropped, and PR authors come from the merge bot's commit
message rather than commit emails.

Community documents are scrubbed before they are stored: leadership lines keep only the handle, the names of
known leads become their `@handle`, e-mail addresses go, and any other run of capitalised words containing a
word never seen in lower case across the documents and KEPs (personal names, employers, some places) becomes
`[name]`. It errs towards removing too much; a lone first name starting a sentence can remain. The agent is
told never to guess who a `[name]` was.

## AI assistance disclosure

Most of this code was written with [Claude Code](https://claude.com/claude-code) (Anthropic's AI coding
assistant) in a guided session; commits carry a `Co-Authored-By: Claude` line, and the app says so in its sidebar.
The author chose the problem and the data, set the design rules (functional style, a UI that only renders what
the pipeline returns, transparency for every answer, per-user memory, security limits), reviewed the changes and
tested the app. At run time, answers come from the model the user picks (OpenRouter, Anthropic, Groq, OpenAI or Bedrock).
The demo video's voice-over is AI-generated (a Microsoft neural voice through edge-tts).
