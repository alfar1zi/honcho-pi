import { create } from 'zustand'
import { io } from 'socket.io-client'
import { forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide } from 'd3-force'

const socket = io({ autoConnect: false })

// Obsidian-style force layout: repel + link spring + center pull.
// Parameters come from the settings panel (Obsidian "Forces" section).
// Runs once per graph load (or on demand); node dragging still overrides positions.
function layoutForce(nodes, edges, forces) {
  if (!nodes.length) return nodes
  const degree = new Map()
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }
  for (const n of nodes) {
    if (!n.position) n.position = { x: 0, y: 0 }
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
    .force('center', forceCenter(0, 0).strength(f.centerStrength))
    .force('collide', forceCollide().radius((d) => nodeRadius(d, degree) + 6))
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

// Per-theme type colors. The server palette only guarantees contrast on dark;
// light theme uses darker variants (WCAG AA on white/ blended badge, verified
// with check_contrast). Unknown types fall back to node.data.color.
export const TYPE_STYLES = {
  dark: {
    FACT: '#34d399',
    DECISION: '#22d3ee',
    RULE: '#a78bfa',
    CORRECTION: '#fbbf24',
    INFERENCE: '#cbd5e1',
    skill: '#f472b6',
  },
  light: {
    FACT: '#047857',
    DECISION: '#155e75',
    RULE: '#7c3aed',
    CORRECTION: '#92400e',
    INFERENCE: '#475569',
    skill: '#be185d',
  },
}

export function typeColor(type, theme, fallback) {
  return TYPE_STYLES[theme]?.[type] || fallback || '#7c3aed'
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
  showOrphans: true,
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
