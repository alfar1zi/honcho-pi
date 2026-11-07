import { useEffect } from 'react'
import { useGraphStore } from '../store.js'
import { useKeyPress, useReactFlow } from '@xyflow/react'

// Obsidian graph navigation: arrow keys pan (Shift = faster), +/- zoom.
// Delete/Backspace removes selected nodes and edges.
export function KeyboardHandler() {
  const deletePressed = useKeyPress(['Delete', 'Backspace'])
  const rf = useReactFlow()
  const { nodes, edges, setNodes, setEdges, persist } = useGraphStore()

  useEffect(() => {
    if (!deletePressed) return
    const sel = nodes.filter((n) => n.selected).map((n) => n.id)
    const selEdges = edges.filter((e) => e.selected).map((e) => e.id)
    const targets = new Set(sel)
    edges.forEach((e) => {
      if (targets.has(e.source) || targets.has(e.target)) selEdges.push(e.id)
    })
    if (!sel.length && !selEdges.length) return
    setNodes(nodes.filter((n) => !targets.has(n.id)))
    setEdges(edges.filter((e) => !selEdges.includes(e.id)))
    persist()
  }, [deletePressed, nodes, edges, setNodes, setEdges, persist])

  useEffect(() => {
    const onKey = (e) => {
      if (!rf) return
      const target = e.target
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return
      const speed = e.shiftKey ? 120 : 40
      switch (e.key) {
        case 'ArrowLeft':
          rf.setViewport((v) => ({ ...v, x: v.x + speed }))
          break
        case 'ArrowRight':
          rf.setViewport((v) => ({ ...v, x: v.x - speed }))
          break
        case 'ArrowUp':
          rf.setViewport((v) => ({ ...v, y: v.y + speed }))
          break
        case 'ArrowDown':
          rf.setViewport((v) => ({ ...v, y: v.y - speed }))
          break
        case '+' :
        case '=':
          rf.zoomIn({ duration: 150 })
          break
        case '-':
          rf.zoomOut({ duration: 150 })
          break
        default:
          return
      }
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rf])

  return null
}
