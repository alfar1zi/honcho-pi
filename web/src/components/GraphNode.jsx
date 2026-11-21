import { memo } from 'react'
import { Handle, Position, useViewportStore } from '@xyflow/react'
import { useGraphStore, parseConfidence, typeColor } from '../store.js'

// Graph node: circle sized by degree, colored by record type. Confidence
// drives opacity; hover/selection dims everything not connected. Degree and
// adjacency come from the store's shared indexes (O(1) per node), so a hover
// flip no longer rescans the edge list in every node. memo keeps untouched
// nodes out of React's render path entirely.
function GraphNodeInner({ id, data, selected }) {
  const hoverId = useGraphStore((s) => s.hoverId)
  const selectedId = useGraphStore((s) => s.selectedId)
  const setHoverId = useGraphStore((s) => s.setHoverId)
  const searchQuery = useGraphStore((s) => s.searchQuery)
  const typeFilter = useGraphStore((s) => s.typeFilter)
  const theme = useGraphStore((s) => s.theme)
  const setSelectedId = useGraphStore((s) => s.setSelectedId)
  const deg = useGraphStore((s) => s.indexes.degree.get(id) || 0)
  const focusId = hoverId || selectedId
  const isConnected = useGraphStore((s) =>
    !!focusId && (s.indexes.adjacency.get(focusId)?.has(id) || false))
  // Boolean selector: re-render only when crossing the label threshold,
  // not on every pan/zoom frame (useViewport has no selector support).
  const zoomedIn = useViewportStore((s) => s.transform[2] > 0.75)
  const dimmed = !!focusId && id !== focusId && !isConnected
  const r = 5 + Math.min(9, deg * 1.4)

  const conf = parseConfidence(data.meta)
  const q = searchQuery.toLowerCase()
  const matchesSearch = !q || (data.label || '').toLowerCase().includes(q)
  const matchesType = !typeFilter.length || typeFilter.includes(data.type)
  const faded = !matchesSearch || !matchesType

  const baseOpacity = conf !== null ? 0.45 + 0.55 * conf : 1
  const opacity = dimmed ? 0.15 : faded ? 0.18 : baseOpacity
  const color = typeColor(data.type, theme, data.color)
  const focused = selected || hoverId === id

  return (
    <button
      type="button"
      className="group relative flex items-center justify-center transition-opacity rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)]"
      style={{ opacity }}
      aria-label={`${data.type || 'node'}: ${data.label || id}${conf !== null ? `, confidence ${conf.toFixed(2)}` : ''}`}
      aria-pressed={selected}
      onClick={() => setSelectedId(id)}
      onMouseEnter={() => setHoverId(id)}
      onMouseLeave={() => setHoverId(null)}
      onFocus={() => setHoverId(id)}
      onBlur={() => setHoverId(null)}
    >
      <Handle type="target" position={Position.Top} className="!w-px !h-px !border-0 !opacity-0" />
      {data.image ? (
        <img
          src={data.image}
          alt=""
          draggable={false}
          className={`rounded-full object-cover transition-all ${selected ? 'ring-2 ring-offset-2 ring-offset-[var(--bg)]' : ''}`}
          style={{
            width: Math.max(28, r * 2.4),
            height: Math.max(28, r * 2.4),
            border: `2px solid ${color}`,
            boxShadow: focused
              ? `0 0 10px 1px color-mix(in srgb, ${color} 70%, transparent)`
              : 'none',
            ...(selected ? { '--tw-ring-color': color } : {}),
          }}
        />
      ) : (
        <div
          className={`rounded-full transition-all ${selected ? 'ring-2 ring-offset-2 ring-offset-[var(--bg)]' : ''}`}
          style={{
            width: r * 2,
            height: r * 2,
            background: color,
            boxShadow: focused
              ? `0 0 10px 1px color-mix(in srgb, ${color} 70%, transparent)`
              : `0 0 6px 0 color-mix(in srgb, ${color} 35%, transparent)`,
            ...(selected ? { '--tw-ring-color': color } : {}),
          }}
        />
      )}
      {((zoomedIn && deg >= 4) || focusId === id || isConnected || selected || (q && matchesSearch)) && (
        <div
          className="node-label absolute left-full ml-2 whitespace-nowrap text-[11px] pointer-events-none select-none max-w-[140px] truncate"
          style={{ color: 'var(--text)', opacity: focusId && !dimmed ? 1 : 0.8 }}
          title={data.label}
        >
          <Highlight text={data.label} query={matchesSearch ? searchQuery : ''} />
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!w-px !h-px !border-0 !opacity-0" />
    </button>
  )
}

export const GraphNode = memo(GraphNodeInner)

export function Highlight({ text, query }) {
  if (!query) return text
  const idx = text.toLowerCase().indexOf(query.toLowerCase())
  if (idx < 0) return text
  return (
    <>
      {text.slice(0, idx)}
      <mark>{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  )
}