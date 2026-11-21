import { useMemo } from 'react'
import { Handle, Position, useViewport } from '@xyflow/react'
import { useGraphStore, parseConfidence, typeColor } from '../store.js'

// Obsidian graph node: a circle sized by degree, colored by record type.
// Confidence drives opacity; hover or selection dims everything not connected.
// Keyboard: focusable button; Tab moves between nodes, Enter selects.
export function GraphNode({ id, data, selected }) {
  const hoverId = useGraphStore((s) => s.hoverId)
  const selectedId = useGraphStore((s) => s.selectedId)
  const setHoverId = useGraphStore((s) => s.setHoverId)
  const edges = useGraphStore((s) => s.edges)
  const searchQuery = useGraphStore((s) => s.searchQuery)
  const typeFilter = useGraphStore((s) => s.typeFilter)
  const theme = useGraphStore((s) => s.theme)
  const setSelectedId = useGraphStore((s) => s.setSelectedId)
  // Label count tracks screen space, not degree: below this zoom only the
  // focus neighborhood and search hits keep labels; hubs join when zoomed in.
  const { zoom } = useViewport()

  const focusId = hoverId || selectedId
  const connected = useMemo(() => {
    const set = new Set()
    if (focusId) {
      for (const e of edges) {
        if (e.source === focusId) set.add(e.target)
        if (e.target === focusId) set.add(e.source)
      }
    }
    return set
  }, [edges, focusId])
  const dimmed = !!focusId && id !== focusId && !connected.has(id)

  const deg = edges.reduce((n, e) => n + (e.source === id || e.target === id ? 1 : 0), 0)
  const r = 5 + Math.min(9, deg * 1.4)

  const conf = parseConfidence(data.meta)
  const q = searchQuery.toLowerCase()
  const matchesSearch = !q || (data.label || '').toLowerCase().includes(q)
  const matchesType = !typeFilter.length || typeFilter.includes(data.type)
  const faded = !matchesSearch || !matchesType

  const baseOpacity = conf !== null ? 0.45 + 0.55 * conf : 1
  const opacity = dimmed ? 0.15 : faded ? 0.18 : baseOpacity
  const color = typeColor(data.type, theme, data.color)

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
            boxShadow: selected || hoverId === id
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
            boxShadow: selected || hoverId === id
              ? `0 0 10px 1px color-mix(in srgb, ${color} 70%, transparent)`
              : `0 0 6px 0 color-mix(in srgb, ${color} 35%, transparent)`,
            ...(selected ? { '--tw-ring-color': color } : {}),
          }}
        />
      )}
      {((zoom > 0.75 && deg >= 4) || focusId === id || connected.has(id) || selected || (q && matchesSearch)) && (
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
