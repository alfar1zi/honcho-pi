import { useMemo, useEffect, useRef, useState } from 'react'
import { X, Type, Palette, Tag, Link2, ImagePlus, Pencil, Check, Trash2 } from 'lucide-react'
import { useGraphStore, parseConfidence, typeColor } from '../store.js'

const COLORS = ['#7c3aed', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#6366f1']

// Click a node: view its full record (content, type, confidence, evidence meta,
// connected neighbors) and edit its visual label/meta/color.
export function DetailPanel() {
  const { nodes, edges, selectedId, setNodes, setSelectedId, persist, theme } = useGraphStore()
  const node = nodes.find((n) => n.id === selectedId)
  const closeRef = useRef(null)
  const open = !!node
  const saveRecord = useGraphStore((s) => s.saveRecord)
  const attachImage = useGraphStore((s) => s.attachImage)
  const removeImage = useGraphStore((s) => s.removeImage)
  const fileRef = useRef(null)
  const [record, setRecord] = useState(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [status, setStatus] = useState('')

  const isRecord = !!selectedId && selectedId.startsWith('mem-')

  // Full record content comes from the store, not the 90-char graph label.
  useEffect(() => {
    setRecord(null)
    setEditing(false)
    setStatus('')
    if (!isRecord) return
    let alive = true
    fetch(`/api/record/${selectedId.slice(4)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => { if (alive) setRecord(body) })
      .catch(() => {})
    return () => { alive = false }
  }, [selectedId, isRecord])

  const startEdit = () => {
    setDraft(record?.content || node?.data.label || '')
    setEditing(true)
    setStatus('')
  }

  const commitEdit = async () => {
    try {
      const updated = await saveRecord(selectedId.slice(4), { content: draft })
      setRecord(updated)
      setEditing(false)
      setStatus('saved to store')
    } catch (e) {
      setStatus(`save failed: ${e.message}`)
    }
  }

  const onPickImage = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || !node) return
    try {
      setStatus('uploading image...')
      await attachImage(node.id, file)
      setStatus('image attached')
    } catch (err) {
      setStatus(`image failed: ${err.message}`)
    }
  }

  // Panel behaves like a non-modal dialog: Escape closes, focus lands on close.
  useEffect(() => {
    if (open) closeRef.current?.focus()
  }, [selectedId])
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key === 'Escape') setSelectedId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setSelectedId])

  const neighbors = useMemo(() => {
    if (!node) return []
    const ids = new Set()
    for (const e of edges) {
      if (e.source === node.id) ids.add(e.target)
      if (e.target === node.id) ids.add(e.source)
    }
    return nodes
      .filter((n) => ids.has(n.id))
      .map((n) => ({ id: n.id, label: n.data.label, color: typeColor(n.data.type, theme, n.data.color) }))
  }, [edges, nodes, node, theme])

  if (!node) return null

  const conf = parseConfidence(node.data.meta)

  const color = typeColor(node.data.type, theme, node.data.color)

  const update = (patch) => {
    setNodes(nodes.map((n) => (n.id === node.id ? { ...n, data: { ...n.data, ...patch } } : n)))
    persist()
  }

  return (
    <div
      role="dialog"
      aria-label={`Record details: ${node.data.label || node.id}`}
      className="absolute top-16 right-4 w-80 max-h-[calc(100%-5rem)] bg-[var(--surface)] border border-[var(--border)] rounded-lg shadow-2xl z-20 overflow-hidden flex flex-col"
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border)] shrink-0">
        <span className="text-[12px] font-semibold text-[var(--text-muted)]">Record details</span>
        <button
          ref={closeRef}
          onClick={() => setSelectedId(null)}
          aria-label="Close details"
          className="p-1 rounded hover:bg-[var(--surface-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        >
          <X size={14} />
        </button>
      </div>
      <div className="p-3 space-y-3 overflow-y-auto">
        <div className="flex items-center gap-2">
          <span
            className="text-[10px] font-semibold px-1.5 py-0.5 rounded"
            style={{ background: `${color}22`, color }}
          >
            {node.data.type || 'page'}
          </span>
          {conf !== null && (
            <span className="text-[10px] text-[var(--text-muted)]">
              confidence {conf.toFixed(2)}
            </span>
          )}
          {node.data.meta?.includes('verified') && (
            <span className="text-[10px] text-emerald-400" style={theme === 'light' ? { color: '#047857' } : undefined}>
              verified
            </span>
          )}
        </div>

        {node.data.image && (
          <div className="relative">
            <img
              src={node.data.image}
              alt={node.data.label || 'attached image'}
              className="w-full rounded border border-[var(--border)] max-h-48 object-cover"
            />
            <button
              onClick={() => removeImage(node.id)}
              aria-label="Remove image"
              className="absolute top-1 right-1 p-1 rounded bg-[var(--surface)] border border-[var(--border)] hover:border-[var(--accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            >
              <Trash2 size={12} />
            </button>
          </div>
        )}

        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] uppercase tracking-wide text-[var(--text-muted)]">Content</span>
            <span className="flex items-center gap-1">
              {isRecord && !editing && (
                <button
                  data-testid="edit-record"
                  onClick={startEdit}
                  aria-label="Edit record content"
                  className="p-1 rounded hover:bg-[var(--surface-2)] text-[var(--text-muted)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  title="Edit content (writes to the memory store)"
                >
                  <Pencil size={12} />
                </button>
              )}
              {editing && (
                <button
                  data-testid="save-record"
                  onClick={commitEdit}
                  aria-label="Save record content"
                  className="p-1 rounded hover:bg-[var(--surface-2)] text-[var(--fact)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  title="Save to store"
                >
                  <Check size={12} />
                </button>
              )}
              <button
                data-testid="attach-image"
                onClick={() => fileRef.current?.click()}
                aria-label="Attach image"
                className="p-1 rounded hover:bg-[var(--surface-2)] text-[var(--text-muted)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                title="Attach image to this node"
              >
                <ImagePlus size={12} />
              </button>
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                className="hidden"
                onChange={onPickImage}
                aria-hidden="true"
                tabIndex={-1}
              />
            </span>
          </div>
          {editing ? (
            <textarea
              data-testid="record-editor"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={8}
              className="w-full text-[12px] leading-relaxed bg-[var(--surface-2)] border border-[var(--accent)] rounded p-2 whitespace-pre-wrap font-mono outline-none resize-y"
            />
          ) : (
            <div className="text-[12px] leading-relaxed bg-[var(--surface-2)] border border-[var(--border)] rounded p-2 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono">
              {record?.content || node.data.label || '(no content)'}
            </div>
          )}
          {status && (
            <div role="status" className="text-[10px] mt-1 text-[var(--text-muted)]">{status}</div>
          )}
        </div>

        {record?.evidence && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] mb-1">Evidence</div>
            <div className="text-[11px] text-[var(--text-muted)] whitespace-pre-wrap">{record.evidence}</div>
          </div>
        )}

        {node.data.meta && (
          <div>
            <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] mb-1">Provenance</div>
            <div className="text-[11px] text-[var(--text-muted)]">{node.data.meta}</div>
          </div>
        )}

        {neighbors.length > 0 && (
          <div>
            <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-[var(--text-muted)] mb-1">
              <Link2 size={10} /> Connected ({neighbors.length})
            </div>
            <div className="flex flex-wrap gap-1">
              {neighbors.slice(0, 8).map((n) => (
                <button
                  key={n.id}
                  onClick={() => setSelectedId(n.id)}
                  className="text-[10px] px-1.5 py-0.5 rounded border border-[var(--border)] hover:border-[var(--accent)] truncate max-w-[140px] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  style={{ color: n.color }}
                  title={n.label}
                >
                  {n.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <div>
          <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] mb-1">
            <Type size={12} /> Graph label
          </label>
          <input
            data-testid="label-input"
            value={node.data.label || ''}
            onChange={(e) => update({ label: e.target.value, customLabel: true })}
            className="w-full bg-[var(--surface-2)] border border-[var(--border)] rounded px-2 py-1.5 text-[13px] outline-none focus:border-[var(--accent)] focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
          />
        </div>

        {!isRecord && (
          <>
            <div>
              <label className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)] mb-1">
                <Tag size={12} /> Meta
              </label>
              <input
                data-testid="meta-input"
                value={node.data.meta || ''}
                onChange={(e) => update({ meta: e.target.value })}
                placeholder="optional note"
                className="w-full bg-[var(--surface-2)] border border-[var(--border)] rounded px-2 py-1.5 text-[13px] outline-none focus:border-[var(--accent)] focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
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
                    aria-label={`Set color ${c}`}
                    aria-pressed={node.data.color === c}
                    className={`w-6 h-6 rounded-full border-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${
                      node.data.color === c ? 'border-white' : 'border-transparent'
                    }`}
                    style={{ background: c }}
                  />
                ))}
              </div>
            </div>
          </>
        )}

        <div className="text-[10px] text-[var(--text-muted)] font-mono truncate" title={node.id}>
          {node.id}
        </div>
      </div>
    </div>
  )
}
