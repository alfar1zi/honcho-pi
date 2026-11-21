import { create } from 'zustand'
import { io } from 'socket.io-client'
import { forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide, forceX, forceY } from 'd3-force'

const socket = io({ autoConnect: false })

// Cluster anchors: each record type gravitates to its own sector so the graph
// reads as regions (mindcorpus organises by kind), not one undifferentiated
// blob. Orphans settle on an outer ring instead of drifting through clusters.
const CLUSTER_ORDER = ['FACT', 'DECISION', 'RULE', 'CORRECTION', 'INFERENCE', 'skill', 'note']
const CLUSTER_RADIUS = 340

function clusterAnchor(type) {
  const i = CLUSTER_ORDER.indexOf(type)
  if (i < 0) return { x: 0, y: 0 }
  const angle = (i / CLUSTER_ORDER.length) * Math.PI * 2 - Math.PI / 2
  return { x: Math.cos(angle) * CLUSTER_RADIUS, y: Math.sin(angle) * CLUSTER_RADIUS }
}

// Force layout: repel + link spring + cluster pull. Parameters come from the
// settings panel. Runs once per graph load (or on demand); dragging overrides.
function layoutForce(nodes, edges, forces) {
  if (!nodes.length) return nodes
  const degree = new Map()
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }
  // Seed positions near the cluster anchor so the sim converges into regions.
  const orphanRing = CLUSTER_RADIUS + 260
  const orphans = nodes.filter((n) => !(degree.get(n.id) > 0))
  orphans.forEach((n, i) => {
    const angle = (i / Math.max(1, orphans.length)) * Math.PI * 2
    n.x = Math.cos(angle) * orphanRing
    n.y = Math.sin(angle) * orphanRing
  })
  for (const n of nodes) {
    if (degree.get(n.id) > 0) {
      const a = clusterAnchor(n.data?.type)
      n.x = a.x + (Math.random() - 0.5) * 160
      n.y = a.y + (Math.random() - 0.5) * 160
    }
    if (!n.position) n.position = { x: n.x, y: n.y }
  }
  const f = { repel: -90, linkDistance: 70, centerStrength: 0.3, ...forces }
  const sim = forceSimulation(nodes)
    .force(
      'link',
      forceLink(edges.map((e) => ({ ...e })))
        .id((d) => d.id)
        .distance(f.linkDistance)
        .strength(0.35)
    )
    .force('charge', forceManyBody().strength(f.repel))
    .force('center', forceCenter(0, 0).strength(f.centerStrength * 0.15))
    .force('clusterX', forceX((d) => (degree.get(d.id) > 0 ? clusterAnchor(d.data?.type).x : d.x)).strength(0.12))
    .force('clusterY', forceY((d) => (degree.get(d.id) > 0 ? clusterAnchor(d.data?.type).y : d.y)).strength(0.12))
    .force('collide', forceCollide().radius((d) => nodeRadius(d, degree) + 14))
    .stop()
  sim.tick(300)
  for (const n of nodes) n.position = { x: n.x, y: n.y }
  return nodes
}

// Juggl local-mode pattern: BFS over the undirected edge list, keep the closed
// neighborhood of the root up to `depth` levels (Obsidian "local graph" depth).
export function neighborhood(edges, rootId, depth) {
  const adj = new Map()
  for (const e of edges) {
    if (!adj.has(e.source)) adj.set(e.source, [])
    if (!adj.has(e.target)) adj.set(e.target, [])
    adj.get(e.source).push(e.target)
    adj.get(e.target).push(e.source)
  }
  const seen = new Set([rootId])
  let frontier = [rootId]
  for (let d = 0; d < depth; d++) {
    const next = []
    for (const id of frontier) {
      for (const nb of adj.get(id) || []) {
        if (!seen.has(nb)) {
          seen.add(nb)
          next.push(nb)
        }
      }
    }
    frontier = next
  }
  return seen
}

export function nodeRadius(node, degreeMap) {
  const deg = degreeMap?.get(node.id) || 0
  return 5 + Math.min(9, deg * 1.4)
}

export function parseConfidence(meta) {
  const m = /conf\s+([0-9.]+)/.exec(meta || '')
  return m ? parseFloat(m[1]) : null
}

function toArray(changes) {
  if (!changes) return []
  if (typeof changes.toArray === 'function') return changes.toArray()
  if (typeof changes[Symbol.iterator] === 'function') return [...changes]
  return Array.isArray(changes) ? changes : []
}

function applyNodeChanges(changes, nodes) {
  let next = nodes
  for (const c of toArray(changes)) {
    if (c.type === 'position' && c.position) {
      next = next.map((n) => (n.id === c.id ? { ...n, position: c.position } : n))
    } else if (c.type === 'remove') {
      next = next.filter((n) => n.id !== c.id)
    } else if (c.type === 'select') {
      next = next.map((n) => (n.id === c.id ? { ...n, selected: c.selected } : n))
    } else if (c.type === 'add' && c.node) {
      next = [...next, c.node]
    }
  }
  return next
}

function applyEdgeChanges(changes, edges) {
  let next = edges
  for (const c of toArray(changes)) {
    if (c.type === 'remove') {
      next = next.filter((e) => e.id !== c.id)
    } else if (c.type === 'select') {
      next = next.map((e) => (e.id === c.id ? { ...e, selected: c.selected } : e))
    } else if (c.type === 'add' && c.edge) {
      next = [...next, c.edge]
    }
  }
  return next
}

// Per-theme type colors, sourced from the CSS variables in index.css so the
// palette lives in one place. Light theme uses darker variants (WCAG AA on
// white). Unknown types fall back to node.data.color.
const TYPE_VARS = {
  FACT: '--fact',
  DECISION: '--decision',
  RULE: '--rule',
  CORRECTION: '--correction',
  INFERENCE: '--inference',
  skill: '--skill',
  note: '--note',
}

// Resolve the palette once per theme; typeColor is on the hot path (every
// node render + minimap), so getComputedStyle per call is wasteful.
const paletteCache = new Map()

export function typeColor(type, theme, fallback) {
  const v = TYPE_VARS[type]
  if (!v) return fallback || '#9b7bff'
  let palette = paletteCache.get(theme)
  if (!palette && typeof document !== 'undefined') {
    const cs = getComputedStyle(document.documentElement)
    palette = {}
    for (const [t, varName] of Object.entries(TYPE_VARS)) {
      palette[t] = cs.getPropertyValue(varName).trim()
    }
    paletteCache.set(theme, palette)
  }
  return palette?.[type] || fallback || '#9b7bff'
}

export const useGraphStore = create((set, get) => ({
  nodes: [],
  edges: [],
  loading: true,
  selectedId: null,
  hoverId: null,
  searchQuery: '',
  typeFilter: [],
  theme: 'dark',
  localRootId: null,
  localDepth: 2,
  // Orphans are 187 of 314 nodes; showing them by default squeezes the
  // connected region (where the threads live) into the middle of the frame.
  // Toggle them on from the settings panel when auditing unlinked records.
  showOrphans: false,
  forces: { repel: -90, linkDistance: 70, centerStrength: 0.3 },
  rf: null,

  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),
  setSelectedId: (id) => set({ selectedId: id }),
  setHoverId: (id) => set({ hoverId: id }),
  setSearchQuery: (q) => set({ searchQuery: q }),
  toggleType: (type) =>
    set((s) => ({
      typeFilter: s.typeFilter.includes(type)
        ? s.typeFilter.filter((t) => t !== type)
        : [...s.typeFilter, type],
    })),
  clearFilters: () => set({ typeFilter: [], searchQuery: '' }),
  toggleTheme: () => {
    const next = get().theme === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    set({ theme: next })
  },
  setRf: (rf) => set({ rf }),
  // Juggl LocalMode: switch root + depth, graph collapses to the closed
  // neighborhood; null root returns to the global view.
  setLocalRoot: (id) => set({ localRootId: id, selectedId: id }),
  setLocalDepth: (depth) => set({ localDepth: depth }),
  exitLocal: () => set({ localRootId: null }),
  toggleOrphans: () => set((s) => ({ showOrphans: !s.showOrphans })),
  setForces: (patch) => set((s) => ({ forces: { ...s.forces, ...patch } })),

  fitView: () => get().rf?.fitView({ padding: 0.2, duration: 400 }),

  relayout: () => {
    const { nodes, edges, forces } = get()
    set({ nodes: layoutForce(nodes.map((n) => ({ ...n })), edges, forces) })
    setTimeout(() => get().fitView(), 50)
  },

  onNodesChange: (changes) => {
    const list = toArray(changes)
    set({ nodes: applyNodeChanges(list, get().nodes) })
    if (list.some((c) => c.type === 'remove')) get().persist()
  },
  onEdgesChange: (changes) => {
    const list = toArray(changes)
    set({ edges: applyEdgeChanges(list, get().edges) })
    if (list.some((c) => c.type === 'remove')) get().persist()
  },
  onConnect: (connection) => {
    set({ edges: [...get().edges, { ...connection, id: `e-${Date.now()}` }] })
    get().persist()
  },

  onNodeDragStop: () => get().persist(),

  loadGraph: async () => {
    set({ loading: true })
    try {
      const res = await fetch('/api/graph')
      const graph = await res.json()
      set({
        nodes: layoutForce(graph.nodes || [], graph.edges || []),
        edges: graph.edges || [],
        loading: false,
      })
    } catch {
      set({ loading: false })
    }
    socket.connect()
  },

  persist: async () => {
    const { nodes, edges } = get()
    const plainNodes = nodes.map((n) => ({ ...n, selected: undefined, dragging: undefined }))
    const plainEdges = edges.map((e) => ({ ...e, selected: undefined }))
    const graph = { nodes: plainNodes, edges: plainEdges }
    if (socket.connected) {
      socket.emit('graph:update', graph)
    } else {
      await fetch('/api/graph', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(graph),
      })
    }
  },
}))

socket.on('graph:updated', (graph) => {
  useGraphStore.setState({ nodes: graph.nodes || [], edges: graph.edges || [] })
})

// SSE fallback for the no-dep overview server
const es = new EventSource('/events')
es.addEventListener('graph:updated', (ev) => {
  const graph = JSON.parse(ev.data)
  useGraphStore.setState({ nodes: graph.nodes || [], edges: graph.edges || [] })
})
