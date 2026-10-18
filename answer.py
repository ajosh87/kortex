"""Finishing the answer: drop stray citation markers (also while the text streams), link every node the
tools returned to its GitHub page, and suggest follow-up questions."""
import re

from tools import all_tools

TOOL_NAMES = "|".join([t.name for t in all_tools()] + ["recall_memory"])


def is_marker(chunk):
    """Citation markers some open models add despite the prompt: 【...】 and [tool_name]."""
    return chunk.startswith("【") or bool(re.fullmatch(rf"\[(?:{TOOL_NAMES})\]", chunk))


def clean(text):
    text = re.sub("[‐‑]", "-", text)  # models write non-breaking hyphens in handles and paths (@ahg‑g)
    # no images: an image URL is fetched the moment the answer renders, so text planted in the data could use one
    # to send what is on screen to another server
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\s*【[^】\n]*(?:】|$)", "", text, flags=re.M)  # closed, or left open until the end of the line
    return re.sub(rf"\s*\[(?:{TOOL_NAMES})\]", "", text)


def stream_cleaner():
    """clean() for text that arrives in pieces: a possible marker is held back until it is complete."""
    held = [""]

    def feed(piece, final=False):
        buf, out = held[0] + piece, ""
        while buf:
            starts = [i for i in (buf.find("【"), buf.find("[")) if i >= 0]
            if not starts:
                out, buf = out + buf, ""
                break
            i = min(starts)
            out, buf = out + buf[:i], buf[i:]
            j = buf.find("】" if buf[0] == "【" else "]")
            if j < 0:  # not complete yet: keep holding, unless the stream ended or it is clearly not a marker
                if final or len(buf) > 80:
                    out, buf = out + (buf if buf[0] == "[" else ""), ""
                break
            chunk, buf = buf[:j + 1], buf[j + 1:]
            out += "" if is_marker(chunk) else chunk
        held[0] = buf
        return out

    return feed


def linkify(text, refs):
    """Turn @handle, KEP-123, #12345 and `folder/path` into GitHub links, but only for nodes the tools
    actually returned this turn (anything else stays plain text, so invented names are not dressed up).
    Code is left alone: links do not render inside code, so only a code span that is exactly a known
    folder or handle becomes a link, and fenced code blocks are never touched."""
    known = {(n["kind"], n["key"].lower()): n for n in refs if n.get("url")}

    def sub(kind, label):
        def rep(m):
            n = known.get((kind, m.group(1).lower()))
            return f"[{label(m.group(1))}]({n['url']})" if n else m.group(0)
        return rep

    def prose(t):
        t = re.sub(r"(?<![\w\[/-])KEP[\s\-‑–]?(\d{1,5})\b", sub("KEP", lambda k: f"KEP-{k}"), t)
        t = re.sub(r"(?<![\w\[/@])@([A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])", sub("Person", lambda h: f"@{h}"), t)
        return re.sub(r"(?<![\w\[&])#(\d{4,7})\b", sub("PR", lambda n: f"#{n}"), t)

    def code(value):  # `pkg/kubelet` -> folder link; `klueska` (a handle written as code) -> @klueska link
        if n := known.get(("Dir", value.lower())) or known.get(("Doc", value.lower())):
            return f"[`{value}`]({n['url']})"
        if n := known.get(("Person", value.lower().lstrip("@"))):
            return f"[@{value.lstrip('@')}]({n['url']})"
        return f"`{value}`"

    out = []
    for i, block in enumerate(re.split(r"(```.*?```)", text, flags=re.S)):
        if i % 2:  # fenced code block
            out.append(block)
            continue
        for j, part in enumerate(re.split(r"(`[^`\n]+`)", block)):
            out.append(code(part[1:-1]) if j % 2 else prose(part))
    return "".join(out)


def unverified(text, refs):
    """Handles, KEPs and PR numbers the answer names that no tool returned this turn (outside code blocks):
    possibly invented, or remembered from training data. Shown with the answer so readers can tell."""
    known = {(n["kind"], n["key"].lower()) for n in refs}
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    found = ([("Person", h, f"@{h}") for h in re.findall(r"(?<![\w\[/@])@([A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])", prose)]
             + [("KEP", k, f"KEP-{k}") for k in re.findall(r"(?<![\w\[/-])KEP[\s\-–]?(\d{1,5})\b", prose)]
             + [("PR", n, f"#{n}") for n in re.findall(r"(?<![\w\[&])#(\d{4,7})\b", prose)])
    return list(dict.fromkeys(label for kind, key, label in found if (kind, key.lower()) not in known))


# Views worth suggesting next, by what a step was about: (argument, tool, full question, short label)
NEXT = [
    ("path", "best_reviewers", "Who should review a change to {}?", "Who should review?"),
    ("path", "blast_radius", "What could break if I change {}?", "What could break?"),
    ("path", "design_constraints", "Which design decisions constrain {}?", "Which decisions constrain it?"),
    ("path", "recent_changes", "What changed recently in {}?", "What changed recently?"),
    ("path", "who_owns", "Who can approve changes to {}?", "Who can approve?"),
    ("number", "decision_to_approvers", "Who must approve the code behind KEP-{}?", "Who approves its code?"),
    ("number", "decision_trail", "Which decisions are linked to KEP-{}?", "Linked decisions?"),
    ("handle", "person_profile", "What does @{} own?", "What do they own?"),
]


def short_subject(arg, value):
    """The last part of what a question is about: 'pkg/kubelet/eviction' -> 'eviction', '2837' -> 'KEP-2837'."""
    return {"path": value.rstrip("/").split("/")[-1], "number": f"KEP-{value}", "handle": f"@{value}"}[arg]


def follow_ups(steps, limit=3):
    """Next questions: views of the same folder, KEP or person that this turn did not use yet.
    Each has the full question (what gets asked) and a short label (what the button shows)."""
    def subject(args, arg):
        return str(args.get(arg, "")).strip().upper().replace("KEP-", "") if arg == "number" else str(args.get(arg, "")).strip().lstrip("@")

    used = {(s["tool"], arg, subject(s["args"], arg)) for s in steps for arg in ("path", "number", "handle") if arg in s["args"]}
    asked = [(arg, subject(s["args"], arg)) for s in steps for arg in ("path", "number", "handle") if arg in s["args"]]
    out = [{"question": q.format(value), "label": f"{label} · {short_subject(arg, value)}"}
           for arg, value in asked for a, tool, q, label in NEXT if a == arg and (tool, arg, value) not in used]
    return list({f["question"]: f for f in out}.values())[:limit]
