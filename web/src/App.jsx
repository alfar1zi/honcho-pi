import { useEffect, useMemo } from 'react'
import { ReactFlow, Background, Controls, MiniMap } from '@xyflow/react'
import { useGraphStore } from './store.js'
import { Toolbar } from './components/Toolbar.jsx'
import { DetailPanel } from './components/DetailPanel.jsx'
import { KeyboardHandler } from './components/KeyboardHandler.jsx'
import { GraphNode } from './components/GraphNode.jsx'

const nodeTypes = { graphNode: GraphNode }

export default function App() {
  const { nodes, edges, loading, onNodesChange, onEdgesChange, onConnect, loadGraph, selectedId, setSelectedId } =
    useGraphStore()

  useEffect(() => {
    loadGraph()
  }, [loadGraph])

  const focusId = useGraphStore((s) => s.hoverId) || selectedId
  const styledEdges = useMemo(() => {
    return edges.map((e) => {
      const active = !!focusId && (e.source === focusId || e.target === focusId)
      return {
        ...e,
        animated: false,
        style: {
          stroke: active ? 'var(--accent)' : 'var(--edge)',
          strokeWidth: active ? 1.6 : 0.7,
          opacity: focusId ? (active ? 1 : 0.08) : 0.4,
        },
      }
    })
  }, [edges, focusId])

  if (loading) {
    return <div className="flex items-center justify-center h-full text-[var(--text-muted)]">loading graph...</div>
  }

  return (
    <div className="relative h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={styledEdges}
        nodeTypes={nodeTypes}
        onNodesChange={(c) => onNodesChange(c)}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={(_, node) => setSelectedId(node.id)}
        onPaneClick={() => setSelectedId(null)}
        onNodeMouseEnter={(_, node) => useGraphStore.getState().setHoverId(node.id)}
        onNodeMouseLeave={() => useGraphStore.getState().setHoverId(null)}
        fitView
        minZoom={0.05}
        maxZoom={2}
      >
        <Background color="#30363d" gap={24} size={1} />
        <Controls position="bottom-right" showInteractive={false} />
        <MiniMap
          position="bottom-left"
          pannable
          zoomable
          nodeColor={(n) => n.data?.color || '#7c3aed'}
          nodeBorderRadius={6}
          maskColor="rgba(13, 17, 23, 0.8)"
          style={{ width: 160, height: 110 }}
        />
      </ReactFlow>
      <Toolbar />
      <DetailPanel />
      <KeyboardHandler />
    </div>
  )
}
