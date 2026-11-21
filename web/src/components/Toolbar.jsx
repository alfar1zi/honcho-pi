import { useMemo, useState } from 'react'
import { Plus, Save, Search, Network, Moon, Sun, RefreshCw, Maximize, Settings2, Pin, PinOff, EyeOff, Eye, ZoomIn, LayoutDashboard } from 'lucide-react'
import { useGraphStore, typeColor } from '../store.js'

const CHIP_ORDER = ['FACT', 'DECISION', 'RULE', 'CORRECTION', 'INFERENCE', 'skill']

// Obsidian graph settings panel: Filters (orphans) + Forces sliders.
function SettingsPanel() {
  const showOrphans = useGraphStore((s) => s.showOrphans)
  const toggleOrphans = useGraphStore((s) => s.toggleOrphans)
  const forces = useGraphStore((s) => s.forces)
  const setForces = useGraphStore((s) => s.setForces)
  const relayout = useGraphStore((s) => s.relayout)
  const localRootId = useGraphStore((s) => s.localRootId)
  const localDepth = useGraphStore((s) => s.localDepth)
  const setLocalDepth = useGraphStore((s) => s.setLocalDepth)
  const nodes = useGraphStore((s) => s.nodes)
  const root = nodes.find((n) => n.id === localRootId)

  return (
    <div
      role="dialog"
      aria-label="Graph settings"
      className="absolute top-16 right-4 w-72 glass-strong rounded-2xl z-20 p-3 pointer-events-auto space-y-4"
    >
      <div className="flex items-center justify-between">
        <span className="text-[12px] font-semibold text-[var(--text-muted)]">Graph settings</span>
        <button onClick={relayout} className="text-[11px] px-2 py-0.5 rounded border border-[var(--border)] hover:border-[var(--accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]">
          Apply forces
        </button>
      </div>

      <section className="space-y-1.5" aria-label="Forces">
        <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Forces</div>
        {[
          ['Repel force', 'repel', -600, -20, 10],
          ['Link distance', 'linkDistance', 30, 300, 1],
          ['Center force', 'centerStrength', 0, 1, 0.05],
        ].map(([label, key, min, max, step]) => (
          <label key={key} className="flex items-center gap-2 text-[12px]">
            <span className="w-24 text-[var(--text-muted)]">{label}</span>
            <input
              type="range"
              min={min} max={max} step={step}
              value={forces[key]}
              onChange={(e) => setForces({ [key]: Number(e.target.value) })}
              aria-label={label}
              className="flex-1 accent-[var(--accent)]"
            />
            <span className="w-10 text-right font-mono text-[11px] text-[var(--text-muted)]">
              {forces[key]}
            </span>
          </label>
        ))}
      </section>

      <section className="space-y-1.5" aria-label="Filters">
        <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Filters</div>
        <label className="flex items-center gap-2 text-[12px] cursor-pointer">
          <input type="checkbox" checked={showOrphans} onChange={toggleOrphans} className="accent-[var(--accent)]" />
          Show orphans
          <span className="text-[10px] text-[var(--text-muted)]">(nodes without links)</span>
        </label>
      </section>

      {localRootId && (
        <section className="space-y-1" aria-label="Local graph">
          <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Local graph</div>
          <div className="text-[11px] text-[var(--text-muted)] truncate" title={root?.data.label}>
            Root: {root?.data.label || localRootId}
          </div>
          <label className="flex items-center gap-2 text-[12px]">
            <span className="w-14 text-[var(--text-muted)]">Depth</span>
            <input
              type="range" min={1} max={5} step={1}
              value={localDepth}
              onChange={(e) => setLocalDepth(Number(e.target.value))}
              aria-label="Local graph depth"
              className="flex-1 accent-[var(--accent)]"
            />
            <span className="w-4 text-right font-mono text-[11px] text-[var(--text-muted)]">{localDepth}</span>
          </label>
          <button
            onClick={exitLocal}
            className="text-[11px] px-2 py-1 rounded border border-[var(--border)] hover:border-[var(--accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          >
            Back to global graph
          </button>
        </section>
      )}
    </div>
  )
}

export function Toolbar() {
  const { nodes, edges, persist, setNodes, searchQuery, setSearchQuery } = useGraphStore()
  const typeFilter = useGraphStore((s) => s.typeFilter)
  const toggleType = useGraphStore((s) => s.toggleType)
  const theme = useGraphStore((s) => s.theme)
  const toggleTheme = useGraphStore((s) => s.toggleTheme)
  const relayout = useGraphStore((s) => s.relayout)
  const fitView = useGraphStore((s) => s.fitView)
  const view = useGraphStore((s) => s.view)
  const setView = useGraphStore((s) => s.setView)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const typeCounts = useMemo(() => {
    const counts = new Map()
    for (const n of nodes) {
      const t = n.data.type || 'note'
      counts.set(t, (counts.get(t) || 0) + 1)
    }
    const ordered = CHIP_ORDER.filter((t) => counts.has(t)).map((t) => [t, counts.get(t)])
    const rest = [...counts.entries()].filter(([t]) => !CHIP_ORDER.includes(t))
    return [...ordered, ...rest]
  }, [nodes])

  const addNode = () => {
    const id = `n-${Date.now()}`
    setNodes([
      ...nodes,
      {
        id,
        type: 'graphNode',
        position: { x: 200 + Math.random() * 200, y: 200 + Math.random() * 200 },
        data: { label: 'New node', type: 'note', color: '#7c3aed' },
      },
    ])
    setTimeout(persist, 0)
  }

  return (
    <div className="absolute top-4 left-4 right-4 flex items-center gap-2 z-10 pointer-events-none flex-wrap">
      <div className="flex items-center gap-1.5 glass rounded-xl px-2.5 py-1.5 pointer-events-auto">
        <Network size={15} className="text-[var(--accent)]" />
        <span className="text-[13px] font-semibold">honcho-pi</span>
        <span className="text-[11px] text-[var(--text-muted)] ml-1 font-meta">
          {nodes.length} nodes · {edges.length} edges
        </span>
      </div>
      <div className="flex items-center gap-1.5 glass rounded-xl px-2 py-1.5 pointer-events-auto">
        <Search size={14} className="text-[var(--text-muted)]" />
        <input
          data-testid="search-input"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="filter nodes..."
          aria-label="Filter nodes by label"
          className="bg-transparent outline-none text-[12px] w-44 placeholder:text-[var(--text-muted)] focus-visible:ring-1 focus-visible:ring-[var(--accent)] rounded"
        />
      </div>
      <div
        className="flex items-center gap-1 glass rounded-xl px-1.5 py-1.5 pointer-events-auto"
        role="group"
        aria-label="Filter by record type"
      >
        {typeCounts.map(([type, count]) => {
          const active = !typeFilter.length || typeFilter.includes(type)
          const color = typeColor(type, theme, nodes.find((n) => n.data.type === type)?.data.color)
          return (
            <button
              key={type}
              data-testid={`filter-${type}`}
              onClick={() => toggleType(type)}
              aria-pressed={typeFilter.includes(type)}
              aria-label={`Filter ${type}, ${count} nodes`}
              className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border transition-opacity focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
              style={{
                borderColor: active ? color : 'var(--border)',
                color: active ? color : 'var(--text-muted)',
                opacity: active ? 1 : undefined,
              }}
              title={`${type}: ${count} nodes`}
            >
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
              {type} <span className="text-[var(--text-muted)]">{count}</span>
            </button>
          )
        })}
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-1 glass rounded-xl p-1 pointer-events-auto">
        <button
          data-testid="toggle-dashboard"
          onClick={() => setView(view === 'dashboard' ? 'graph' : 'dashboard')}
          aria-label={view === 'dashboard' ? 'Back to graph' : 'Open dashboard'}
          aria-pressed={view === 'dashboard'}
          className={`p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${view === 'dashboard' ? 'text-[var(--accent)]' : ''}`}
          title={view === 'dashboard' ? 'Back to graph' : 'Dashboard: store health'}
        >
          {view === 'dashboard' ? <Network size={16} /> : <LayoutDashboard size={16} />}
        </button>
        <button data-testid="add-node" onClick={addNode} aria-label="Add node" className="p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]" title="Add node">
          <Plus size={16} />
        </button>
        <button onClick={relayout} aria-label="Re-run force layout" className="p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]" title="Re-run force layout">
          <RefreshCw size={16} />
        </button>
        <button onClick={fitView} aria-label="Zoom to fit" className="p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]" title="Zoom to fit">
          <Maximize size={16} />
        </button>
        <button onClick={persist} aria-label="Save graph" className="p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]" title="Save">
          <Save size={16} />
        </button>
        <button onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} className="p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]" title="Toggle theme">
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
        </button>
        <button
          onClick={() => setSettingsOpen(!settingsOpen)}
          aria-label="Graph settings"
          aria-expanded={settingsOpen}
          className={`p-1.5 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${settingsOpen ? 'text-[var(--accent)]' : ''}`}
          title="Graph settings"
        >
          <Settings2 size={16} />
        </button>
      </div>
      {settingsOpen && <SettingsPanel />}
    </div>
  )
}
