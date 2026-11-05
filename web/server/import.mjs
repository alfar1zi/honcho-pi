#!/usr/bin/env node
// Builds graph nodes from operator knowledge sources:
//   - honcho memory.db (RULE/FACT/CORRECTION/DECISION records)
//   - managed-skills + user skills (SKILL.md)
//   - skill index (SKILL_INDEX.md)
// Output: data/graph.json
import { readFile, readdir, stat } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT = join(__dirname, '..', 'data', 'graph.json')

const HOME = process.env.HOME || '/home/farizi'
const MEM_DB = process.env.OMP_MEMORY_DB || join(HOME, '.omp/memory/memory.db')
const MANAGED = process.env.OMP_MANAGED_SKILLS || join(HOME, '.omp/agent/managed-skills')
const SKILLS = process.env.OMP_USER_SKILLS || join(HOME, '.omp/agent/skills')
const INDEX = join(MANAGED, 'SKILL_INDEX.md')

const TYPE_COLORS = {
  RULE: '#7c3aed',
  FACT: '#10b981',
  CORRECTION: '#f59e0b',
  DECISION: '#06b6d4',
  INFERENCE: '#94a3b8',
  skill: '#ec4899',
}

function wordSet(text) {
  return new Set(
    String(text)
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, ' ')
      .split(/\s+/)
      .filter((w) => w.length > 4)
  )
}

function jaccard(a, b) {
  let inter = 0
  for (const w of a) if (b.has(w)) inter++
  return inter / (a.size + b.size - inter)
}

async function readMemory() {
  const Database = createRequire(import.meta.url)('better-sqlite3')
  const db = new Database(MEM_DB, { readonly: true })
  const rows = db
    .prepare('SELECT id, type, content, confidence, verified, tags, created_at FROM records ORDER BY updated_at DESC LIMIT 200')
    .all()
  db.close()
  return rows
}

async function readSkills(dir) {
  const out = []
  let entries
  try {
    entries = await readdir(dir, { withFileTypes: true })
  } catch {
    return out
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const skillMd = join(dir, e.name, 'SKILL.md')
    try {
      const s = await stat(skillMd)
      if (!s.isFile()) continue
      const body = await readFile(skillMd, 'utf-8')
      const name = e.name
      const descMatch = body.match(/^description:\s*(.+)$/m)
      out.push({
        name,
        description: descMatch ? descMatch[1].trim() : '',
        body,
        dir,
      })
    } catch {
      // no SKILL.md
    }
  }
  return out
}

function firstLine(text, n = 90) {
  const line = String(text).split('\n').find((l) => l.trim())
  return line ? line.slice(0, n) : ''
}

async function main() {
  const nodes = []
  const edges = []

  // 1. memory records
  let memories = []
  try {
    memories = await readMemory()
  } catch (e) {
    console.warn('[import] memory db unavailable:', e.message)
  }
  for (const m of memories) {
    nodes.push({
      id: `mem-${m.id}`,
      type: 'graphNode',
      position: { x: 0, y: 0 },
      data: {
        label: firstLine(m.content),
        type: m.type,
        meta: `memory · conf ${Number(m.confidence).toFixed(2)}${m.verified ? ' · verified' : ''}`,
        color: TYPE_COLORS[m.type] || '#64748b',
      },
    })
  }

  // 2. skills (managed + user)
  const managed = await readSkills(MANAGED)
  const user = await readSkills(SKILLS)
  const allSkills = [
    ...managed.map((s) => ({ ...s, source: 'managed' })),
    ...user.map((s) => ({ ...s, source: 'user' })),
  ]
  for (const s of allSkills) {
    nodes.push({
      id: `skill-${s.source}-${s.name}`,
      type: 'graphNode',
      position: { x: 0, y: 0 },
      data: {
        label: s.name,
        type: 'skill',
        meta: `${s.source} skill${s.description ? ' · ' + s.description.slice(0, 60) : ''}`,
        color: TYPE_COLORS.skill,
      },
    })
  }

  // 3. edges
  //    a. memory <-> memory by content similarity (jaccard >= 0.22, top 3 per node)
  const memNodes = nodes.filter((n) => n.id.startsWith('mem-'))
  const memSets = memories.map((m) => wordSet(m.content))
  const neighbors = new Map()
  for (let i = 0; i < memSets.length; i++) {
    const scored = []
    for (let j = 0; j < memSets.length; j++) {
      if (i === j) continue
      const sim = jaccard(memSets[i], memSets[j])
      if (sim >= 0.22) scored.push({ j, sim })
    }
    scored.sort((a, b) => b.sim - a.sim)
    neighbors.set(i, scored.slice(0, 3))
  }
  const seenEdge = new Set()
  for (const [i, list] of neighbors) {
    for (const { j } of list) {
      const key = [Math.min(i, j), Math.max(i, j)].join('-')
      if (seenEdge.has(key)) continue
      seenEdge.add(key)
      edges.push({
        id: `e-sim-${key}`,
        source: `mem-${memories[i].id}`,
        target: `mem-${memories[j].id}`,
        label: 'similar',
      })
    }
  }

  //    b. memory <-> skill by tag or keyword overlap with skill description/body head
  for (let i = 0; i < memories.length; i++) {
    const m = memories[i]
    const mSet = wordSet((m.tags || '') + ' ' + m.content.slice(0, 200))
    for (const s of allSkills) {
      const sSet = wordSet(s.name + ' ' + s.description + ' ' + s.body.slice(0, 400))
      const sim = jaccard(mSet, sSet)
      if (sim >= 0.12) {
        const eid = `e-memsim-${m.id}-${s.source}-${s.name}`
        edges.push({
          id: eid,
          source: `mem-${m.id}`,
          target: `skill-${s.source}-${s.name}`,
          label: 'related',
        })
      }
    }
  }

  //    c. skill <-> skill by shared name fragments (e.g. antislop family)
  for (let i = 0; i < allSkills.length; i++) {
    for (let j = i + 1; j < allSkills.length; j++) {
      const a = allSkills[i]
      const b = allSkills[j]
      const family = (x) => x.name.split('-')[0]
      if (family(a) === family(b) && family(a).length > 4) {
        edges.push({
          id: `e-fam-${a.source}-${a.name}-${b.source}-${b.name}`,
          source: `skill-${a.source}-${a.name}`,
          target: `skill-${b.source}-${b.name}`,
          label: 'family',
        })
      }
    }
  }

  // 4. layout: force-ish circle by group, memory cluster center, skills ring outside
  const R_MEM = 420
  const R_SKILL = 780
  const memCount = memNodes.length
  const skillCount = allSkills.length
  let mi = 0
  let si = 0
  for (const n of nodes) {
    if (n.id.startsWith('mem-')) {
      const a = (2 * Math.PI * mi) / Math.max(1, memCount)
      n.position = { x: R_MEM * Math.cos(a) * 1.6, y: R_MEM * Math.sin(a) * 1.1 }
      mi++
    } else {
      const a = (2 * Math.PI * si) / Math.max(1, skillCount)
      n.position = { x: R_SKILL * Math.cos(a), y: R_SKILL * Math.sin(a) }
      si++
    }
  }

  const graph = { nodes, edges }
  const { writeFile, mkdir } = await import('node:fs/promises')
  await mkdir(dirname(OUT), { recursive: true })
  await writeFile(OUT, JSON.stringify(graph, null, 2))
  console.log(`[import] ${nodes.length} nodes, ${edges.length} edges -> ${OUT}`)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
