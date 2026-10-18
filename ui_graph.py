"""Interactive graph: a small Streamlit custom component (v2) around Cytoscape.js.

Zoom (Ctrl/Cmd + scroll, or the buttons), pan and drag in the browser; a plain scroll keeps scrolling the page.
The layout menu redraws the same graph another way (force, tree, circle, rings, grid), and the full-screen button
opens it over the whole window (Esc closes it). Clicking a node sends its id back to Python, which shows the
node's profile in the app. Only presentation lives here: nodes and edges come from the pipeline as they are.
"""
import streamlit as st

CYTOSCAPE = "https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.esm.min.mjs"

_HTML = """<div class="kx">
  <div class="kx-frame">
    <div class="kx-graph"></div>
    <div class="kx-loading"><span class="kx-spin"></span>Drawing the graph…</div>
    <div class="kx-tools">
      <select class="kx-layout" title="Draw the same graph another way"></select>
      <button data-zoom="in" title="Zoom in">+</button><button data-zoom="out" title="Zoom out">−</button><button data-zoom="fit" title="Show everything">⤢</button>
      <button class="kx-full-btn" title="Full screen">⛶</button>
    </div>
  </div>
  <div class="kx-hint"></div>
</div>"""

_CSS = """
.kx { width: 100%; font-family: var(--st-font, sans-serif); }
.kx-frame { position: relative; }
.kx-graph { width: 100%; }
.kx-loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 0.6rem;
  font-size: 0.85rem; color: var(--st-text-color); opacity: 0.75; pointer-events: none; }
.kx-spin { width: 18px; height: 18px; border-radius: 50%; border: 2.5px solid rgba(79,70,229,0.25);
  border-top-color: #4F46E5; animation: kx-spin 0.8s linear infinite; }
@keyframes kx-spin { to { transform: rotate(360deg); } }
.kx-tools { position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; z-index: 2; }
.kx-tools button, .kx-tools select { height: 28px; font: 500 14px/1 Inter, sans-serif; cursor: pointer;
  color: var(--kx-text); background: var(--kx-surface); border: 1px solid var(--kx-line); border-radius: 8px; }
.kx-tools button { width: 28px; padding: 0; }
.kx-tools select { padding: 0 6px; font-size: 12px; }
.kx-tools button:hover, .kx-tools select:hover { background: var(--kx-tint); }
.kx-full { position: fixed; left: 0; top: 0; width: 100vw; height: 100vh; z-index: 1000000; background: var(--kx-bg); padding: 14px 18px; box-sizing: border-box;
  box-shadow: 0 0 0 100vmax rgba(0,0,0,0.45); }
.kx-full .kx-graph { height: calc(100vh - 64px) !important; }
.kx-hint { font-size: 0.8rem; opacity: 0.7; padding-top: 0.25rem; color: var(--st-text-color); line-height: 1.35; }
"""

_JS = """
const LIB = "%s"
const charts = new WeakMap()
const pending = new WeakMap()  // the latest draw requested for each element

function teardown(parentElement) {
  pending.delete(parentElement)
  const current = charts.get(parentElement)
  if (!current) return
  document.removeEventListener("scroll", current.onScroll, true)
  document.removeEventListener("keydown", current.onKey, true)
  parentElement.querySelector(".kx").classList.remove("kx-full")
  current.cy.destroy()
  charts.delete(parentElement)
}

function draw(parentElement, data, setTriggerValue) {
  const root = parentElement.querySelector(".kx")
  const box = parentElement.querySelector(".kx-graph")
  const hint = parentElement.querySelector(".kx-hint")
  hint.textContent = data.hint
  for (const [k, v] of Object.entries(data.chrome)) root.style.setProperty("--kx-" + k, v)
  const signature = JSON.stringify([data.elements, data.style, data.layout, data.height])
  const current = charts.get(parentElement)
  if (current && current.signature === signature) {  // same graph: only the selection may have changed
    current.cy.$(":selected").unselect()
    if (data.selected) current.cy.$id(data.selected).select()
    return
  }
  teardown(parentElement)
  box.style.height = data.height + "px"
  const token = {}  // a newer draw (or unmount) before the library loads makes this one stale
  pending.set(parentElement, token)
  const loading = parentElement.querySelector(".kx-loading")
  loading.style.display = "flex"  // until the library is loaded and the layout has run
  import(LIB).then(({ default: cytoscape }) => {
    if (pending.get(parentElement) !== token) return
    const cy = cytoscape({
      container: box, elements: data.elements, style: data.style, layout: data.layout,
      minZoom: 0.15, maxZoom: 3, boxSelectionEnabled: false,
      userZoomingEnabled: false,  // a plain scroll wheel scrolls the page; zoom with Ctrl/Cmd + scroll or the buttons
    })
    // Cytoscape caches where its canvas is on screen and only refreshes that on window scrolls; Streamlit scrolls
    // an inner element, so refresh it on any scroll (once per frame) or clicks land on the wrong node.
    let queued = false
    const onScroll = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; cy.resize() }) } }
    document.addEventListener("scroll", onScroll, { capture: true, passive: true })
    box.onmouseenter = () => cy.resize()

    const zoomBy = (factor, at) => cy.zoom({ level: cy.zoom() * factor, renderedPosition: at || { x: box.clientWidth / 2, y: box.clientHeight / 2 } })
    box.onwheel = (e) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      const r = box.getBoundingClientRect()
      zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, { x: e.clientX - r.left, y: e.clientY - r.top })
    }
    parentElement.querySelectorAll(".kx-tools button").forEach((b) => {
      b.onclick = () => b.dataset.zoom === "fit" ? cy.fit(undefined, 16) : zoomBy(b.dataset.zoom === "in" ? 1.25 : 0.8)
    })
    if (cy.zoom() < data.min_zoom && cy.nodes().length > 15) {  // a big graph too small to read when it all fits: start on its busiest node
      const hub = cy.nodes().max((n) => n.degree()).ele
      cy.zoom(data.min_zoom)
      cy.center(hub)
    } else if (cy.zoom() < data.min_zoom) {  // a small graph: make the frame taller so all of it fits at a readable size
      const bb = cy.elements().boundingBox()
      box.style.height = Math.round(Math.min(560, Math.max(data.height, bb.h * data.min_zoom + 40))) + "px"
      cy.resize()
      cy.fit(undefined, 16)
    }
    const menu = parentElement.querySelector(".kx-layout")  // the same graph, drawn another way
    menu.innerHTML = Object.keys(data.layouts).map((name) => `<option>${name}</option>`).join("")
    const fit = () => { cy.fit(undefined, 24); if (cy.zoom() > 1.4) { cy.zoom(1.4); cy.center() } }  // small graphs stay readable, not huge
    menu.onchange = () => { cy.layout(data.layouts[menu.value] || data.layout).run(); fit() }
    const fullBtn = parentElement.querySelector(".kx-full-btn")
    const setFull = (on) => {
      root.classList.toggle("kx-full", on)
      fullBtn.textContent = on ? "✕" : "⛶"
      fullBtn.title = on ? "Close full screen (Esc)" : "Full screen"
      requestAnimationFrame(() => { cy.resize(); fit() })
    }
    fullBtn.onclick = () => setFull(!root.classList.contains("kx-full"))
    const onKey = (e) => { if (e.key === "Escape" && root.classList.contains("kx-full")) setFull(false) }
    document.addEventListener("keydown", onKey, true)
    cy.on("dbltap", (evt) => { if (evt.target === cy) cy.fit(undefined, 16) })
    cy.on("tap", "node", (evt) => setTriggerValue("selected", evt.target.id()))
    cy.on("mouseover", "node", (evt) => { box.style.cursor = "pointer"; hint.textContent = evt.target.data("tip") })
    cy.on("mouseout", "node", () => { box.style.cursor = "grab"; hint.textContent = data.hint })
    if (data.selected) cy.$id(data.selected).select()
    charts.set(parentElement, { cy, signature, onScroll, onKey })
    loading.style.display = "none"
  }).catch(() => { loading.style.display = "none"; hint.textContent = "The interactive graph could not load (the graph library needs internet access)." })
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  if (!data || !parentElement.querySelector(".kx-graph")) return
  draw(parentElement, data, setTriggerValue)
  return () => teardown(parentElement)
}
""" % CYTOSCAPE

_GRAPH = st.components.v2.component("kortex_graph", html=_HTML, css=_CSS, js=_JS)


def _size(label):
    lines = label.split("\n")
    return {"w": 20 + 7.6 * max(len(x) for x in lines), "h": 14 + 16 * len(lines)}


def elements(graph, colors, fonts):
    """Cytoscape elements from a pipeline graph {nodes, edges} plus the node colours for the active style."""
    nodes = []
    for n in graph["nodes"]:
        title = n.get("title")
        name = n["label"]
        if n["kind"] == "Dir" and len(name) > 24:  # long folder paths: the last two parts (full path on hover and in details)
            name = "…/" + "/".join(name.split("/")[-2:])
        label = name + (f"\n{title[:28]}{'…' if len(title) > 28 else ''}" if title else "")
        nodes.append({"data": {"id": n["id"], "label": label, "color": colors.get(n["kind"], "#e2e8f0"),
                               "font": fonts.get(n["kind"], "#0f172a"), "tip": f"{n['kind_label']}: {title or n['key']} (click for details)",
                               **_size(label)}})
    edges = [{"data": {"id": f"e{i}", "source": e["src"], "target": e["dst"], "label": e["rel"]}} for i, e in enumerate(graph["edges"])]
    return nodes + edges


def stylesheet(background, edge_labels=True, accent="#4F46E5"):
    """Cytoscape styles: soft rounded nodes with hairline outlines; the selected node gets an indigo ring."""
    edge = "#9AA3B5"
    return [
        {"selector": "node", "style": {
            "label": "data(label)", "background-color": "data(color)", "color": "data(font)",
            "shape": "round-rectangle", "width": "data(w)", "height": "data(h)",
            "border-width": 1, "border-color": "rgba(28,32,48,0.28)",
            "font-size": 12, "font-weight": 500, "font-family": "Inter, sans-serif",
            "text-wrap": "wrap", "text-valign": "center", "text-halign": "center"}},
        {"selector": "edge", "style": {
            "curve-style": "bezier", "width": 1.2, "line-color": edge,
            "target-arrow-shape": "triangle", "target-arrow-color": edge, "arrow-scale": 0.9,
            "label": "data(label)" if edge_labels else "", "font-size": 9, "color": edge, "text-rotation": "autorotate",
            "text-background-color": background, "text-background-opacity": 0.9, "text-background-padding": 2}},
        {"selector": "node:selected", "style": {"border-width": 5, "border-color": accent}},
    ]


def layout(name, n=30):
    """Radial for memory (one question at the centre of what it touched); force-directed otherwise, which keeps
    both chains (folder → OWNERS → team → people) and fans (25 importers of one package) readable."""
    if name == "radial":
        return {"name": "concentric", "minNodeSpacing": 18, "animate": False, "padding": 12}
    if n <= 15:  # a handful of nodes: a compact layered tree (force layouts stretch short chains into a long line)
        return {"name": "breadthfirst", "directed": True, "spacingFactor": 1.15, "padding": 16, "animate": False,
                "nodeDimensionsIncludeLabels": True}
    return {"name": "cose", "animate": False, "padding": 16, "nodeDimensionsIncludeLabels": True,
            "idealEdgeLength": 120, "nodeRepulsion": 400000, "nodeOverlap": 60, "edgeElasticity": 100,
            "gravity": 0.3, "componentSpacing": 80, "numIter": 2500}


# Other ways to draw the same nodes and edges, offered in the graph's layout menu ("Auto" is the default pick).
LAYOUTS = {
    "Force": {"name": "cose", "animate": False, "padding": 16, "nodeDimensionsIncludeLabels": True, "idealEdgeLength": 120,
              "nodeRepulsion": 400000, "nodeOverlap": 60, "gravity": 0.3, "componentSpacing": 80, "numIter": 2500},
    "Tree": {"name": "breadthfirst", "directed": True, "spacingFactor": 1.15, "padding": 16, "animate": False,
             "nodeDimensionsIncludeLabels": True},
    "Circle": {"name": "circle", "padding": 16, "animate": False, "nodeDimensionsIncludeLabels": True},
    "Rings": {"name": "concentric", "minNodeSpacing": 18, "padding": 16, "animate": False},  # busiest nodes in the middle
    "Grid": {"name": "grid", "padding": 16, "animate": False, "avoidOverlap": True, "nodeDimensionsIncludeLabels": True},
}


def show(graph, key, *, colors, fonts, background, chrome, accent="#4F46E5", layout_name="tree", edge_labels=True):
    """Draw an interactive graph. Returns the id of the node the user last clicked (kept across reruns).
    `chrome` colours the buttons and the full-screen backdrop: {text, surface, line, tint}."""
    pick_key = f"{key}-pick"

    def picked():
        value = st.session_state[key].selected
        if value:
            st.session_state[pick_key] = value

    height = int(min(560, max(300, 22 * len(graph["nodes"]))))
    _GRAPH(key=key, on_selected_change=picked, data={
        "elements": elements(graph, colors, fonts), "style": stylesheet(background, edge_labels, accent),
        "layout": layout(layout_name, len(graph["nodes"])), "height": height, "selected": st.session_state.get(pick_key), "min_zoom": 0.8,
        "layouts": {"Auto": layout(layout_name, len(graph["nodes"])), **LAYOUTS}, "chrome": {**chrome, "bg": background},
        "hint": "Click a node for its details · drag to move · Ctrl + scroll or the buttons to zoom · "
                "the menu draws it another way · ⛶ opens it full screen."})
    return st.session_state.get(pick_key)
