import { useEffect } from 'react'
import { useGraphStore } from '../store.js'
import { useKeyPress } from '@xyflow/react'

export function KeyboardHandler() {
  const deletePressed = useKeyPress(['Delete', 'Backspace'])
  const { nodes, edges, setNodes, setEdges, selectedId, persist } = useGraphStore()

  useEffect(() => {
    if (!deletePressed) return
    const sel = nodes.filter((n) => n.selected).map((n) => n.id)
    const selEdges = edges.filter((e) => e.selected).map((e) => e.id)
    const targets = new Set(sel)
    // also remove edges connected to removed nodes
    edges.forEach((e) => {
      if (targets.has(e.source) || targets.has(e.target)) selEdges.push(e.id)
    })
    if (!sel.length && !selEdges.length) return
    setNodes(nodes.filter((n) => !targets.has(n.id)))
    setEdges(edges.filter((e) => !selEdges.includes(e.id)))
    persist()
  }, [deletePressed, nodes, edges, setNodes, setEdges, selectedId, persist])

  return null
}
