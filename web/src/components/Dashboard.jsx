import { useEffect, useState } from 'react'
import { useGraphStore, typeColor } from '../store.js'

// Dashboard answers one question: is the memory store healthy and growing?
// Every number comes from /api/stats (store + graph), nothing invented.
function Bar({ label, value, max, color }) {
  return (
    <div className="flex items-center gap-2 text-[12px]">
      <span className="w-24 shrink-0 text-[var(--text-muted)] truncate">{label}</span>
      <div className="flex-1 h-3 rounded-sm bg-[var(--surface-2)] overflow-hidden">
        <div
          className="h-full rounded-sm"
          style={{ width: `${max ? (value / max) * 100 : 0}%`, background: color }}
        />
      </div>
      <span className="w-10 text-right font-meta text-[11px]">{value}</span>
    </div>
  )
}

export function Dashboard() {
  const theme = useGraphStore((s) => s.theme)
  const setView = useGraphStore((s) => s.setView)
  const setSelectedId = useGraphStore((s) => s.setSelectedId)
  const nodes = useGraphStore((s) => s.nodes)
  const [stats, setStats] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    fetch('/api/stats')
      .then((r) => { if (!r.ok) throw new Error(`stats ${r.status}`); return r.json() })
      .then((body) => { if (alive) setStats(body) })
      .catch((e) => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [])

  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2 text-[13px] text-[var(--text-muted)]">
        <p>Stats unavailable: {error}.</p>
        <p>Start the store server: <code className="font-meta">bash bin/overview.sh --port 4174</code></p>
      </div>
    )
  }
  if (!stats) {
    return <div className="h-full flex items-center justify-center text-[13px] text-[var(--text-muted)]">loading store stats...</div>
  }

  const typeEntries = Object.entries(stats.store)
    .filter(([k, v]) => typeof v === 'number' && v > 0 && k === k.toUpperCase() && k !== 'total')
    .sort((a, b) => b[1] - a[1])
  const typeMax = Math.max(...typeEntries.map(([, v]) => v), 1)
  const actMax = Math.max(...stats.activity.map((a) => a.count), 1)
  const conf = stats.confidence
  const confTotal = conf.low + conf.mid + conf.high || 1
  const g = stats.graph

  const openNode = (id) => {
    if (nodes.some((n) => n.id === id)) {
      setSelectedId(id)
      setView('graph')
    }
  }

  return (
    <div className="relative h-full overflow-y-auto px-6 py-5 pt-16">
      {/* One shared static backdrop instead of per-card blur layers: the
          cards are translucent fills over these glows. */}
      <div
        aria-hidden="true"
        className="fixed inset-0 pointer-events-none"
        style={{
          background:
            'radial-gradient(900px 600px at 75% 15%, color-mix(in srgb, var(--glow-a) 60%, transparent), transparent 72%),' +
            'radial-gradient(1000px 700px at 15% 90%, color-mix(in srgb, var(--glow-b) 55%, transparent), transparent 72%)',
        }}
      />
      <div className="relative max-w-4xl mx-auto space-y-6">
        <div className="glass rounded-2xl px-5 py-3 flex flex-wrap gap-x-8 gap-y-2 font-meta text-[12px] text-[var(--text-muted)]">
          <span><b className="text-[var(--text)] text-[16px]">{stats.store.total}</b> records</span>
          <span><b className="text-[var(--text)] text-[16px]">{stats.store.verified}</b> verified</span>
          <span><b className="text-[var(--text)] text-[16px]">{stats.auto_captured}</b> auto-captured</span>
          <span><b className="text-[var(--text)] text-[16px]">{g.nodes}</b> nodes</span>
          <span><b className="text-[var(--text)] text-[16px]">{g.edges}</b> threads</span>
          <span><b className="text-[var(--text)] text-[16px]">{g.orphans}</b> unlinked</span>
          <span><b className="text-[var(--text)] text-[16px]">{stats.store.reinforcements}</b> reinforcements</span>
        </div>

        <div className="grid md:grid-cols-2 gap-6">
          <section aria-label="Records by type" className="glass rounded-2xl p-4 space-y-1.5">
            <h2 className="text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-2">Records by type</h2>
            {typeEntries.map(([t, v]) => (
              <Bar key={t} label={t} value={v} max={typeMax} color={typeColor(t, theme)} />
            ))}
          </section>

          <section aria-label="Confidence distribution" className="glass rounded-2xl p-4 space-y-1.5">
            <h2 className="text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-2">Confidence</h2>
            <Bar label="< 0.40 (cold)" value={conf.low} max={confTotal} color="var(--correction)" />
            <Bar label="0.40 - 0.69" value={conf.mid} max={confTotal} color="var(--inference)" />
            <Bar label=">= 0.70 (hot)" value={conf.high} max={confTotal} color="var(--fact)" />
            <p className="text-[11px] text-[var(--text-muted)] pt-1">
              Cold records decay toward forgetting; hot ones survive. A store dominated by
              cold rows needs reinforcement, not more capture.
            </p>
          </section>

          <section aria-label="Capture activity" className="glass rounded-2xl p-4 space-y-1.5">
            <h2 className="text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-2">New records per day (last 14 active days)</h2>
            {stats.activity.map((a) => (
              <Bar key={a.day} label={a.day.slice(5)} value={a.count} max={actMax} color="var(--accent)" />
            ))}
          </section>

          <section aria-label="Most connected nodes" className="glass rounded-2xl p-4">
            <h2 className="text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-2">Most connected</h2>
            <div className="space-y-1">
              {g.top_connected.map((n) => (
                <button
                  key={n.id}
                  onClick={() => openNode(n.id)}
                  className="w-full flex items-center gap-2 text-[12px] px-2 py-1 rounded hover:bg-[var(--surface-2)] text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  title={`Open ${n.label} in the graph`}
                >
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: typeColor(n.type, theme) }} />
                  <span className="flex-1 truncate">{n.label}</span>
                  <span className="font-meta text-[11px] text-[var(--text-muted)]">{n.degree}</span>
                </button>
              ))}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}