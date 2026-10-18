"""Interactive graph: a small Streamlit custom component (v2) around Cytoscape.js.

Zoom (Ctrl/Cmd + scroll, or the buttons), pan and drag in the browser; a plain scroll keeps scrolling the page.
Clicking a node sends its id back to Python, which shows the node's profile in the app. Only presentation lives
here: nodes and edges come from the pipeline as they are.
"""
import streamlit as st

CYTOSCAPE = "https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.esm.min.mjs"

_HTML = """<div class="kx">
  <div class="kx-frame">
    <div class="kx-graph"></div>
    <div class="kx-loading"><span class="kx-spin"></span>Drawing the graph…</div>
    <div class="kx-tools">
      <button data-zoom="in" title="Zoom in">+</button><button data-zoom="out" title="Zoom out">−</button><button data-zoom="fit" title="Show everything">⤢</button>
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
.kx-tools { position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; }
.kx-tools button { width: 28px; height: 28px; font: 600 15px/1 sans-serif; cursor: pointer; color: #111;
  background: #fff; border: 1.5px solid #111; border-radius: 4px; }
.kx-tools button:hover { background: #FFDE59; }
.kx-hint { font-size: 0.8rem; opacity: 0.7; padding-top: 0.25rem; color: var(--st-text-color); line-height: 1.35; }
"""

_JS = """
const LIB = "%s"
const charts = new WeakMap()

function teardown(parentElement) {
  const current = charts.get(parentElement)
  if (!current) return
  document.removeEventListener("scroll", current.onScroll, true)
  current.cy.destroy()
  charts.delete(parentElement)
}

function draw(parentElement, data, setTriggerValue) {
  const box = parentElement.querySelector(".kx-graph")
  const hint = parentElement.querySelector(".kx-hint")
  box.style.height = data.height + "px"
  hint.textContent = data.hint
  const signature = JSON.stringify([data.elements, data.style, data.layout, data.height])
  const current = charts.get(parentElement)
  if (current && current.signature === signature) {  // same graph: only the selection may have changed
    current.cy.$(":selected").unselect()
    if (data.selected) current.cy.$id(data.selected).select()
    return
  }
  teardown(parentElement)
  const loading = parentElement.querySelector(".kx-loading")
  loading.style.display = "flex"  // until the library is loaded and the layout has run
  import(LIB).then(({ default: cytoscape }) => {
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
    cy.on("dbltap", (evt) => { if (evt.target === cy) cy.fit(undefined, 16) })
    cy.on("tap", "node", (evt) => setTriggerValue("selected", evt.target.id()))
    cy.on("mouseover", "node", (evt) => { box.style.cursor = "pointer"; hint.textContent = evt.target.data("tip") })
    cy.on("mouseout", "node", () => { box.style.cursor = "grab"; hint.textContent = data.hint })
    if (data.selected) cy.$id(data.selected).select()
    charts.set(parentElement, { cy, signature, onScroll })
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


def stylesheet(brutal, ink, background, edge_labels=True, accent="#4F46E5"):
    """Cytoscape styles: soft rounded nodes, or neo-brutalist square nodes with thick outlines."""
    edge = ink if brutal else "#94a3b8"
    return [
        {"selector": "node", "style": {
            "label": "data(label)", "background-color": "data(color)", "color": "data(font)",
            "shape": "rectangle" if brutal else "round-rectangle", "width": "data(w)", "height": "data(h)",
            "border-width": 2.5 if brutal else 1, "border-color": "#111111" if brutal else "#64748b",
            "font-size": 12, "font-weight": 700 if brutal else 500,
            "font-family": "Space Grotesk, Inter, sans-serif" if brutal else "Inter, sans-serif",
            "text-wrap": "wrap", "text-valign": "center", "text-halign": "center"}},
        {"selector": "edge", "style": {
            "curve-style": "bezier", "width": 1.8 if brutal else 1, "line-color": edge,
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


def show(graph, key, *, colors, fonts, brutal, ink, background, layout_name="tree", edge_labels=True):
    """Draw an interactive graph. Returns the id of the node the user last clicked (kept across reruns)."""
    pick_key = f"{key}-pick"

    def picked():
        value = st.session_state[key].selected
        if value:
            st.session_state[pick_key] = value

    height = int(min(560, max(300, 22 * len(graph["nodes"]))))
    _GRAPH(key=key, on_selected_change=picked, data={
        "elements": elements(graph, colors, fonts), "style": stylesheet(brutal, ink, background, edge_labels),
        "layout": layout(layout_name, len(graph["nodes"])), "height": height, "selected": st.session_state.get(pick_key), "min_zoom": 0.8,
        "hint": "Click a node for its details · drag to move · Ctrl + scroll or the buttons to zoom."})
    return st.session_state.get(pick_key)
