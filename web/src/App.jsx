import { useEffect, useMemo } from 'react'
import { ReactFlow, Background, Controls, MiniMap } from '@xyflow/react'
import { SearchX } from 'lucide-react'
import { useGraphStore, neighborhood, typeColor } from './store.js'
import { Toolbar } from './components/Toolbar.jsx'
import { DetailPanel } from './components/DetailPanel.jsx'
import { KeyboardHandler } from './components/KeyboardHandler.jsx'
import { GraphNode } from './components/GraphNode.jsx'

const nodeTypes = { graphNode: GraphNode }

export default function App() {
  const { edges, loading, onEdgesChange, onConnect, loadGraph, selectedId, setSelectedId, theme } =
    useGraphStore()
  const nodes = useGraphStore((s) => s.nodes)
  const typeFilter = useGraphStore((s) => s.typeFilter)
  const searchQuery = useGraphStore((s) => s.searchQuery)
  const setRf = useGraphStore((s) => s.setRf)
  const setLocalRoot = useGraphStore((s) => s.setLocalRoot)
  const clearFilters = useGraphStore((s) => s.clearFilters)

  useEffect(() => {
    loadGraph()
  }, [loadGraph])

  const q = searchQuery.toLowerCase()
  const localRootId = useGraphStore((s) => s.localRootId)
  const localDepth = useGraphStore((s) => s.localDepth)
  const showOrphans = useGraphStore((s) => s.showOrphans)

  // Juggl local-mode: restrict to the closed neighborhood of the root before
  // the other filters. Obsidian local graph behaves the same way.
  const localIds = useMemo(() => {
    if (!localRootId) return null
    return neighborhood(edges, localRootId, localDepth)
  }, [edges, localRootId, localDepth])

  const degree = useMemo(() => {
    const m = new Map()
    for (const e of edges) {
      m.set(e.source, (m.get(e.source) || 0) + 1)
      m.set(e.target, (m.get(e.target) || 0) + 1)
    }
    return m
  }, [edges])

  const visibleNodes = useMemo(() => {
    let list = nodes
    if (localIds) list = list.filter((n) => localIds.has(n.id))
    if (!showOrphans) list = list.filter((n) => (degree.get(n.id) || 0) > 0)
    if (typeFilter.length || q) {
      list = list.filter((n) => {
        const matchType = !typeFilter.length || typeFilter.includes(n.data.type)
        const matchSearch = !q || (n.data.label || '').toLowerCase().includes(q)
        return matchType && matchSearch
      })
    }
    return list
  }, [nodes, localIds, showOrphans, degree, typeFilter, q])

  // Hide edges whose endpoints are both filtered out, keep styling identical.
  const visibleIds = useMemo(() => new Set(visibleNodes.map((n) => n.id)), [visibleNodes])
  const visibleEdges = useMemo(() => edges.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target)), [edges, visibleIds])

  const focusId = useGraphStore((s) => s.hoverId) || selectedId
  // Threads, not wires: thin low-opacity filaments like the mindcorpus brain
  // render. Edge kind labels ("similar"/"family") are noise at this density;
  // the connection itself is the information. Hover lifts the whole thread.
  const styledEdges = useMemo(() => {
    return visibleEdges.map((e) => {
      const active = !!focusId && (e.source === focusId || e.target === focusId)
      return {
        ...e,
        label: undefined,
        labelStyle: undefined,
        labelBgStyle: undefined,
        animated: false,
        style: {
          stroke: active ? 'var(--accent)' : 'var(--edge)',
          strokeWidth: active ? 1.4 : 0.9,
          opacity: focusId ? (active ? 0.95 : 0.05) : 0.85,
          filter: active
            ? 'drop-shadow(0 0 3px var(--accent))'
            : 'drop-shadow(0 0 2px color-mix(in srgb, var(--edge) 80%, transparent))',
        },
      }
    })
  }, [visibleEdges, focusId])

  if (loading) {
    return <div className="flex items-center justify-center h-full text-[var(--text-muted)]">loading graph...</div>
  }

  return (
    <div className="relative h-full w-full">
      <ReactFlow
        nodes={visibleNodes}
        edges={styledEdges}
        nodeTypes={nodeTypes}
        onNodesChange={useGraphStore.getState().onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={(_, node) => setSelectedId(node.id)}
        onNodeDoubleClick={(_, node) => setLocalRoot(node.id)}
        onPaneClick={() => setSelectedId(null)}
        onNodeMouseEnter={(_, node) => useGraphStore.getState().setHoverId(node.id)}
        onNodeMouseLeave={() => useGraphStore.getState().setHoverId(null)}
        onInit={setRf}
        fitView
        minZoom={0.05}
        maxZoom={2}
      >
        <Background color="transparent" />
        <Controls position="bottom-right" showInteractive={false} />
        <MiniMap
          position="bottom-left"
          pannable
          zoomable
          nodeColor={(n) => typeColor(n.data?.type, theme, n.data?.color)}
          nodeBorderRadius={6}
          maskColor="var(--mask)"
          style={{ width: 160, height: 110, background: 'var(--surface)' }}
        />
      </ReactFlow>
      {visibleNodes.length === 0 && (
        <div
          role="status"
          className="absolute inset-0 flex flex-col items-center justify-center gap-2 pointer-events-none z-10"
        >
          <SearchX size={28} className="text-[var(--text-muted)]" />
          <p className="text-[13px] text-[var(--text-muted)]">
            No nodes match {searchQuery ? `"${searchQuery}"` : 'the current filters'}.
          </p>
          <button
            onClick={clearFilters}
            className="pointer-events-auto text-[12px] px-3 py-1.5 rounded border border-[var(--border)] hover:border-[var(--accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          >
            Clear filters
          </button>
        </div>
      )}
      <Toolbar />
      <DetailPanel />
      <KeyboardHandler />
    </div>
  )
}
