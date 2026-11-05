import { X, Type, Palette, Tag } from 'lucide-react'
import { useGraphStore } from '../store.js'

const COLORS = ['#7c3aed', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#6366f1']

export function DetailPanel() {
  const { nodes, selectedId, setNodes, setSelectedId, persist } = useGraphStore()
  const node = nodes.find((n) => n.id === selectedId)
  if (!node) return null

  const update = (patch) => {
    setNodes(nodes.map((n) => n.id === node.id ? { ...n, data: { ...n.data, ...patch } } : n))
    persist()
  }

  return (
    <div className="absolute top-16 right-4 w-72 bg-[var(--surface)] border border-[var(--border)] rounded-lg shadow-2xl z-20 overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border)]">
        <span className="text-[12px] font-semibold text-[var(--text-muted)]">Edit node</span>
        <button onClick={() => setSelectedId(null)} className="p-1 rounded hover:bg-[var(--surface-2)]">
          <X size={14} />
        </button>
      </div>
      <div className="p-3 space-y-3">
        <div>
          <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] mb-1">
            <Type size={12} /> Label
          </label>
          <input
            data-testid="label-input"
            value={node.data.label || ''}
            onChange={(e) => update({ label: e.target.value })}
            className="w-full bg-[var(--surface-2)] border border-[var(--border)] rounded px-2 py-1.5 text-[13px] outline-none focus:border-[var(--accent)]"
          />
        </div>
        <div>
          <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] mb-1">
            <Tag size={12} /> Meta
          </label>
          <input
            data-testid="meta-input"
            value={node.data.meta || ''}
            onChange={(e) => update({ meta: e.target.value })}
            placeholder="optional note"
            className="w-full bg-[var(--surface-2)] border border-[var(--border)] rounded px-2 py-1.5 text-[13px] outline-none focus:border-[var(--accent)]"
          />
        </div>
        <div>
          <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] mb-1">
            <Palette size={12} /> Color
          </label>
          <div className="flex gap-1.5">
            {COLORS.map((c) => (
              <button
                key={c}
                onClick={() => update({ color: c })}
                className={`w-6 h-6 rounded-full border-2 ${node.data.color === c ? 'border-white' : 'border-transparent'}`}
                style={{ background: c }}
              />
            ))}
          </div>
        </div>
        <div className="text-[10px] text-[var(--text-muted)] font-mono">
          {node.id} · type: {node.data.type || 'page'}
        </div>
      </div>
    </div>
  )
}
