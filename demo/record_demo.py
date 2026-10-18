# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "edge-tts>=7.0", "imageio-ffmpeg>=0.5", "mutagen>=1.47"]
# ///
"""Record Kortex's demo video with an AI voice-over, in one command. Free: Playwright drives and records the
browser, edge-tts speaks the narration (Microsoft's online neural voices, no key), ffmpeg (bundled by
imageio-ffmpeg) joins sound and picture.

    uv run streamlit run app.py --server.port 8502     # terminal 1: the local app (answers with the key in .env)
    uv run demo/record_demo.py                         # terminal 2: writes demo/out/kortex-demo.mp4 (+ .srt)

    uv run demo/record_demo.py --voice en-GB-RyanNeural --url http://localhost:8502

How sound and picture stay in sync: every scene's narration is synthesised first, so its length is known. The
browser then plays the scenes in order: it gets each scene ready (for example, waits for an answer to finish),
notes the moment the scene starts, performs its actions, and holds until that scene's narration would end.
ffmpeg then lays each narration clip at its scene's start time. Waiting for the model never pushes the voice out
of step: the voice simply starts when the scene does.
"""
import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path

import edge_tts
import imageio_ffmpeg
from mutagen.mp3 import MP3
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "out"
SIZE = {"width": 1440, "height": 900}
GAP = 0.8  # seconds of quiet after each scene's narration

# A visible cursor: headless recordings show none, so viewers could not follow the clicks.
CURSOR = """
window.addEventListener('DOMContentLoaded', () => {
  // A normal arrow pointer (headless browsers draw none), tip exactly at the mouse position, and a soft ring on clicks.
  const arrow = document.createElement('div');
  arrow.innerHTML = '<svg width="22" height="30" viewBox="0 0 22 30" xmlns="http://www.w3.org/2000/svg">'
    + '<path d="M2 2 L2 24 L7.5 18.8 L11.4 27.4 L15 25.8 L11.2 17.4 L18.6 17.4 Z" fill="#fff" stroke="#111" '
    + 'stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(arrow.style, {position: 'fixed', left: '-40px', top: '-40px', width: '22px', height: '30px',
    pointerEvents: 'none', zIndex: 2147483647, transform: 'translate(-2px,-2px)',
    filter: 'drop-shadow(0 1px 1.5px rgba(0,0,0,0.35))'});
  document.body.appendChild(arrow);
  document.addEventListener('mousemove', (e) => { arrow.style.left = e.clientX + 'px'; arrow.style.top = e.clientY + 'px'; }, true);
  document.addEventListener('mousedown', (e) => {
    const ring = document.createElement('div');
    Object.assign(ring.style, {position: 'fixed', left: e.clientX + 'px', top: e.clientY + 'px', width: '28px', height: '28px',
      borderRadius: '50%', border: '2px solid rgba(79,70,229,0.7)', pointerEvents: 'none', zIndex: 2147483646,
      transform: 'translate(-50%,-50%) scale(0.4)', opacity: '1', transition: 'transform .35s ease-out, opacity .35s ease-out'});
    document.body.appendChild(ring);
    requestAnimationFrame(() => { ring.style.transform = 'translate(-50%,-50%) scale(1.3)'; ring.style.opacity = '0'; });
    setTimeout(() => ring.remove(), 400);
  }, true);
});
"""


# ---------------- moving around the app like a person ----------------

def pause(seconds):
    time.sleep(seconds)


def point(page, locator):
    """Glide the cursor to an element's centre."""
    box = locator.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
    pause(0.3)


def click(page, locator):
    locator.scroll_into_view_if_needed()
    point(page, locator)
    page.mouse.down()
    page.mouse.up()
    pause(0.6)


def scroll(page, pixels, steps=12):
    """Scroll the page smoothly, with the cursor over the conversation column."""
    page.mouse.move(SIZE["width"] * 0.42, SIZE["height"] * 0.55, steps=10)
    for _ in range(steps):
        page.mouse.wheel(0, pixels / steps)
        pause(0.06)
    pause(0.4)


def go(name, heading=None):
    """Getting a scene ready: open a page from the top bar and wait until it has drawn (its heading shows), so the
    narration never starts over a blank page. Streamlit keeps a live connection open, so "network idle" never comes."""
    def ready(page):
        click(page, page.locator('[data-testid="stTopNavLink"]', has_text=name).first)
        if heading:
            page.get_by_role("heading", name=heading, exact=True).first.wait_for(timeout=60_000)
        pause(1.5)
    return ready


def search(page, text, view):
    """Explore: find a node, pick its first match, then one of its walks."""
    box = page.get_by_placeholder("e.g. pkg/scheduler")
    click(page, box)
    box.fill("")
    box.type(text, delay=60)
    pause(2.5)
    button = page.locator("[data-testid='stButtonGroup'] button", has_text=view).first
    button.wait_for(timeout=60_000)  # the walks offered for the first match
    click(page, button)
    page.wait_for_selector("text=The walk:", timeout=60_000)
    pause(2.0)


# ---------------- the scenes: (what the voice says, how to get ready unseen, what the viewer sees) ----------------

def nothing(page):
    pass


def landing(page):
    pause(1.5)
    scroll(page, 520, steps=18)
    pause(1.5)
    scroll(page, -520, steps=18)


def ask_trace(page):
    click(page, page.locator(".st-key-starter-0 button"))


def first_query_ready(page):
    page.wait_for_selector('[class*="st-key-log-cypher-"] summary', timeout=480_000)


def open_query(page):
    click(page, page.locator('[class*="st-key-log-cypher-"] summary').first)
    pause(2.0)


def answer_ready(page):
    page.wait_for_selector("text=Worked for", timeout=600_000)
    pause(1.0)


def read_answer(page):
    scroll(page, -2000, steps=6)
    pause(0.8)
    for _ in range(4):
        scroll(page, 260)
        pause(1.6)


def answer_graph(page):
    graph = page.locator('[class*="st-key-tile-plain-graph-"]').first
    graph.scroll_into_view_if_needed()
    pause(1.2)
    box = graph.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.55, steps=25)
    page.mouse.down()
    page.mouse.up()  # big graphs open centred on their busiest node, so the centre is a node
    pause(3.0)
    scroll(page, 380)


def explore_break(page):
    search(page, "pkg/kubelet/metrics", "What could break")
    scroll(page, 420)
    pause(2.5)
    scroll(page, 420)


def explore_critical(page):
    scroll(page, -3000, steps=8)
    search(page, "pkg/kubelet", "Critical code below")
    scroll(page, 360)
    pause(2.0)
    scroll(page, 360)


def explore_docs(page):
    scroll(page, -3000, steps=8)
    search(page, "sig node annual report 2024", "Document")
    scroll(page, 360)
    pause(2.0)
    scroll(page, 360)


def memory(page):
    pause(1.0)
    scroll(page, 300)


def data(page):
    pause(1.0)
    scroll(page, 420)
    pause(2.0)
    scroll(page, 420)


def outro(page):
    new_chat = page.get_by_role("button", name="New chat")
    if new_chat.count():
        click(page, new_chat.first)
    pause(1.5)


SCENES = [
    ("This is Kortex: an org brain for the Kubernetes project, built on FalkorDB. Kubernetes runs like a company. "
     "Special interest groups own the code, owners files decide who may approve a change, design proposals called "
     "KEPs record the decisions, and thousands of pull requests show who actually does the work. Kortex loads all of "
     "it into one graph, and answers questions that need several of those sources at once.", nothing, landing),
    ("Let's trace a design decision to the people who must approve it. KEP twenty-eight thirty-seven added "
     "pod-level resources. The agent follows the KEP to its feature gate, to every folder of code that checks that "
     "gate, up the folder tree to the nearest owners file, and expands teams into people. One graph walk, across "
     "four sources.", nothing, ask_trace),
    ("On the right, the console shows everything the agent does, as it happens: each model call, each tool, and "
     "every Cypher query. Open one to see its parameters and the exact rows FalkorDB returned.",
     first_query_ready, open_query),
    ("In the answer, every person, design decision and folder links to its source, but only when the graph "
     "returned it. If the model names something no tool found, Kortex flags it as not from the graph.",
     answer_ready, read_answer),
    ("Below is the graph behind the answer. Click any node to read its profile straight from the graph: who "
     "approves it, which decisions shaped it, and what depends on it.", nothing, answer_graph),
    ("The Explore page runs the same graph walks with no model at all. What could break if we change the kubelet "
     "metrics package? Kortex follows Go imports backwards, one hop at a time, until a hop finds nothing new. "
     "Forty-five packages depend on it, twenty-seven of them directly, and every chain ends within three hops.",
     go("Explore", "Explore"), explore_break),
    ("Critical code ranks packages with FalkorDB's PageRank and betweenness algorithms over the import graph, then "
     "counts how many people can approve each one. Code that much of the project depends on, with a single "
     "approver, is a real risk, and the graph shows exactly where it is.", nothing, explore_critical),
    ("Explore also searches the community's own documents with FalkorDB's full-text index: SIG charters, READMEs, "
     "annual reports and the DevStats guide. Here is SIG Node's annual report for 2024, linked to the design "
     "decisions and the people it mentions. People appear only as GitHub handles.", nothing, explore_docs),
    ("Kortex remembers. Every question and answer is stored in FalkorDB, in a separate graph for each user, linked "
     "to the folders, people and decisions it used. The agent recalls earlier chats by topic, and the shared "
     "Kubernetes graph never holds anyone's questions.", go("Memory", "Memory"), memory),
    ("The Data page shows what the graph holds: about ten thousand nodes and thirty-three thousand relationships, "
     "the schema as found in the data, and the exact commits it was loaded from. People appear only as public "
     "GitHub handles.", go("Data", "What the graph holds"), data),
    ("Kortex is open source and live on Streamlit, backed by FalkorDB Cloud. Bring your own model key, or explore "
     "without one. It is entered in all three tracks: agents that act on connected data, agent memory, and "
     "company brain. Thanks for watching.", go("Ask"), outro),
]


# ---------------- voice, recording, and the final cut ----------------

def speak(lines, voice, rate):
    """One mp3 per scene. Returns their paths and lengths in seconds."""
    async def run():
        for i, text in enumerate(lines):
            await edge_tts.Communicate(text, voice, rate=rate).save(str(OUT / f"voice-{i:02d}.mp3"))
    asyncio.run(run())
    clips = [OUT / f"voice-{i:02d}.mp3" for i in range(len(lines))]
    return clips, [MP3(c).info.length for c in clips]


def record(url, lengths):
    """Play every scene in the browser and record it. Returns the video file, each scene's start, and each
    unseen wait (getting a scene ready, e.g. while the model writes), all in seconds from the video's start."""
    starts, waits = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=SIZE, device_scale_factor=1, color_scheme="light",
                                      record_video_dir=str(OUT), record_video_size=SIZE)
        context.add_init_script(CURSOR)
        page = context.new_page()
        t0 = time.monotonic()  # the video starts with the page
        try:
            page.goto(url)
            page.wait_for_selector(".st-key-starters", timeout=120_000)
            pause(3)
            for i, ((_, prepare, show), length) in enumerate(zip(SCENES, lengths)):
                ready = time.monotonic() - t0
                prepare(page)
                start = time.monotonic() - t0
                starts.append(start)
                waits.append((ready, start))
                print(f"scene {i + 1}/{len(SCENES)} at {start:6.1f}s ({length:4.1f}s of voice, {start - ready:4.1f}s getting ready)")
                show(page)
                pause(max(0.0, t0 + start + length + GAP - time.monotonic()))
            pause(1.5)
        finally:
            video = Path(page.video.path())
            context.close()  # writes the video
            browser.close()
    return video, starts, waits


def cuts(starts, waits, keep=(2.0, 1.5)):
    """Spans to cut from the recording: the page loading before the first scene, and the middle of every long wait
    (the first and last seconds stay, so the viewer sees the work start and finish)."""
    spans = [(0.0, max(0.0, starts[0] - 0.5))]  # the first scene's wait is the page loading: covered here
    spans += [(a + keep[0], b - keep[1]) for a, b in waits[1:] if b - a > sum(keep) + 2]
    merged = []
    for a, b in sorted(x for x in spans if x[1] > x[0]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def shift(t, spans):
    """Where a moment of the recording lands in the cut video."""
    return t - sum(min(b, t) - a for a, b in spans if a < t)


def srt(lines, starts, lengths, path):
    """Subtitles, one cue per scene, for captions on YouTube or similar."""
    def ts(s):
        return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},{int(s * 1000 % 1000):03d}"
    cues = [f"{i + 1}\n{ts(a)} --> {ts(a + n)}\n{text}\n" for i, (text, a, n) in enumerate(zip(lines, starts, lengths))]
    path.write_text("\n".join(cues), encoding="utf-8")


def mux(video, clips, starts, spans, path):
    """The recording without the cut spans, each voice clip placed at its scene's start in the cut video."""
    keep = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in spans) or "0"
    picture = f"[0:v]fps=25,select='not({keep})',setpts=N/25/TB[v]"  # constant frame rate first, so cuts stay exact
    delays = [f"[{i + 1}:a]adelay={int(shift(s, spans) * 1000)}:all=1[a{i}]" for i, s in enumerate(starts)]
    mix = "".join(f"[a{i}]" for i in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0[voice]"
    inputs = ["-i", str(video)] + [x for c in clips for x in ("-i", str(c))]
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *inputs,
                    "-filter_complex", ";".join([picture, *delays, mix]), "-map", "[v]", "-map", "[voice]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", str(path)], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://localhost:8502")
    ap.add_argument("--voice", default="en-US-AndrewNeural", help="any edge-tts voice: `uv run --with edge-tts edge-tts --list-voices`")
    ap.add_argument("--rate", default="+0%", help="speaking speed, e.g. +5%%")
    ap.add_argument("--recut", action="store_true", help="rebuild the final cut from the last recording (no new voice or recording)")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    lines = [text for text, _, _ in SCENES]
    timeline = OUT / "timeline.json"
    if args.recut:
        t = json.loads(timeline.read_text(encoding="utf-8"))
        video, starts, waits, lengths = Path(t["video"]), t["starts"], t["waits"], t["lengths"]
        clips = [OUT / f"voice-{i:02d}.mp3" for i in range(len(lines))]
    else:
        print("voice-over ...")
        clips, lengths = speak(lines, args.voice, args.rate)
        print(f"  {len(clips)} clips, {sum(lengths):.0f}s of narration")
        print("recording the app ...")
        video, starts, waits = record(args.url, lengths)
        timeline.write_text(json.dumps({"video": str(video), "starts": starts, "waits": waits, "lengths": lengths}), encoding="utf-8")
    print("final cut ...")
    spans = cuts(starts, waits)
    final = OUT / "kortex-demo.mp4"
    mux(video, clips, starts, spans, final)
    srt(lines, [shift(s, spans) for s in starts], lengths, OUT / "kortex-demo.srt")
    cut = sum(b - a for a, b in spans)
    print(f"done: {final} ({shift(starts[-1], spans) + lengths[-1] + 2:.0f}s; cut {cut:.0f}s of loading and waiting) "
          f"and {final.with_suffix('.srt')}")


if __name__ == "__main__":
    main()
