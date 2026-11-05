import { create } from 'zustand'
import { io } from 'socket.io-client'
import { forceSimulation, forceLink, forceManyBody, forceCenter, forceCollide } from 'd3-force'

const socket = io({ autoConnect: false })

// Obsidian-style force layout: repel + link spring + center pull.
// Runs once per graph load; node dragging still overrides positions.
function layoutForce(nodes, edges) {
  if (!nodes.length) return nodes
  const degree = new Map()
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }
  for (const n of nodes) {
    if (!n.position) n.position = { x: 0, y: 0 }
  }
  const sim = forceSimulation(nodes)
    .force(
      'link',
      forceLink(edges.map((e) => ({ ...e })))
        .id((d) => d.id)
        .distance(70)
        .strength(0.35)
    )
    .force('charge', forceManyBody().strength(-90))
    .force('center', forceCenter(0, 0).strength(0.3))
    .force('collide', forceCollide().radius((d) => nodeRadius(d, degree) + 6))
    .stop()
  sim.tick(300)
  for (const n of nodes) n.position = { x: n.x, y: n.y }
  return nodes
}

export function nodeRadius(node, degreeMap) {
  const deg = degreeMap?.get(node.id) || 0
  return 5 + Math.min(9, deg * 1.4)
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

export const useGraphStore = create((set, get) => ({
  nodes: [],
  edges: [],
  loading: true,
  selectedId: null,
  hoverId: null,
  searchQuery: '',

  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),
  setSelectedId: (id) => set({ selectedId: id }),
  setHoverId: (id) => set({ hoverId: id }),
  setSearchQuery: (q) => set({ searchQuery: q }),

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
