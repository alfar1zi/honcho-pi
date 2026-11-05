import { Handle, Position } from '@xyflow/react'
import { useGraphStore } from '../store.js'

// Obsidian graph node: a circle sized by degree, label beside it.
// Hover or selection dims everything not directly connected.
export function GraphNode({ id, data, selected }) {
  const hoverId = useGraphStore((s) => s.hoverId)
  const selectedId = useGraphStore((s) => s.selectedId)
  const setHoverId = useGraphStore((s) => s.setHoverId)
  const nodes = useGraphStore((s) => s.nodes)
  const edges = useGraphStore((s) => s.edges)
  const searchQuery = useGraphStore((s) => s.searchQuery)

  const focusId = hoverId || selectedId
  const connected = new Set()
  if (focusId) {
    for (const e of edges) {
      if (e.source === focusId) connected.add(e.target)
      if (e.target === focusId) connected.add(e.source)
    }
  }
  const dimmed = !!focusId && id !== focusId && !connected.has(id)

  const deg = edges.reduce((n, e) => n + (e.source === id || e.target === id ? 1 : 0), 0)
  const r = 5 + Math.min(9, deg * 1.4)

  const matchesSearch =
    !searchQuery || (data.label || '').toLowerCase().includes(searchQuery.toLowerCase())
  const faded = !matchesSearch

  return (
    <div
      className="group relative flex items-center justify-center transition-opacity"
      style={{ opacity: dimmed ? 0.15 : faded ? 0.25 : 1 }}
      onMouseEnter={() => setHoverId(id)}
      onMouseLeave={() => setHoverId(null)}
    >
      <Handle type="target" position={Position.Top} className="!w-px !h-px !border-0 !opacity-0" />
      <div
        className={`rounded-full transition-all ${selected ? 'ring-2 ring-offset-2 ring-offset-[var(--bg)]' : ''}`}
        style={{
          width: r * 2,
          height: r * 2,
          background: data.color || '#7c3aed',
          boxShadow: selected || hoverId === id ? `0 0 12px 2px ${data.color || '#7c3aed'}` : 'none',
          ...(selected ? { '--tw-ring-color': data.color || '#7c3aed' } : {}),
        }}
      />
      <div
        className="absolute left-full ml-2 whitespace-nowrap text-[11px] pointer-events-none select-none transition-opacity"
        style={{ color: 'var(--text)', opacity: focusId && !dimmed ? 1 : 0.75 }}
      >
        {data.label}
      </div>
      <Handle type="source" position={Position.Bottom} className="!w-px !h-px !border-0 !opacity-0" />
    </div>
  )
}
