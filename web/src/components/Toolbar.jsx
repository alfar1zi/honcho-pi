import { Plus, Save, Search, Network, Eye } from 'lucide-react'
import { useGraphStore } from '../store.js'

export function Toolbar() {
  const { nodes, edges, persist, setNodes, searchQuery, setSearchQuery } = useGraphStore()

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
    <div className="absolute top-4 left-4 right-4 flex items-center gap-2 z-10 pointer-events-none">
      <div className="flex items-center gap-1.5 bg-[var(--surface)] border border-[var(--border)] rounded-lg px-2.5 py-1.5 pointer-events-auto">
        <Network size={15} className="text-[var(--accent)]" />
        <span className="text-[13px] font-semibold">honcho-pi</span>
        <span className="text-[11px] text-[var(--text-muted)] ml-1">
          {nodes.length} nodes · {edges.length} edges
        </span>
      </div>
      <div className="flex items-center gap-1.5 bg-[var(--surface)] border border-[var(--border)] rounded-lg px-2 py-1.5 pointer-events-auto">
        <Search size={14} className="text-[var(--text-muted)]" />
        <input
          data-testid="search-input"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="filter nodes..."
          className="bg-transparent outline-none text-[12px] w-44 placeholder:text-[var(--text-muted)]"
        />
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-1 bg-[var(--surface)] border border-[var(--border)] rounded-lg p-1 pointer-events-auto">
        <button data-testid="add-node" onClick={addNode} className="p-1.5 rounded hover:bg-[var(--surface-2)]" title="Add node">
          <Plus size={16} />
        </button>
        <button onClick={persist} className="p-1.5 rounded hover:bg-[var(--surface-2)]" title="Save">
          <Save size={16} />
        </button>
      </div>
    </div>
  )
}
