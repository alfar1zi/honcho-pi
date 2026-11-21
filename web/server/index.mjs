import express from 'express'
import { createServer } from 'node:http'
import { Server } from 'socket.io'
import chokidar from 'chokidar'
import { readFile, writeFile, readdir, stat, mkdir } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = join(__dirname, '..')
const DATA_DIR = join(ROOT, 'data')
const GRAPH_FILE = join(DATA_DIR, 'graph.json')
const PORT = process.env.PORT || 4001

const app = express()
app.use(express.json({ limit: '10mb' }))
app.use(express.static(join(ROOT, 'dist')))
app.use('/media', express.static(join(DATA_DIR, 'media'), { immutable: true, maxAge: '1y' }))

// The stdlib prod server owns /events; in dev, socket.io carries updates, so
// this stub just keeps EventSource from erroring in the console.
app.get('/events', (_req, res) => {
  res.set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
  res.write(': connected\n\n')
  const keep = setInterval(() => res.write(': keep\n\n'), 30000)
  res.on('close', () => clearInterval(keep))
})

const httpServer = createServer(app)
const io = new Server(httpServer, { cors: { origin: '*' } })

async function ensureDataDir() {
  await mkdir(DATA_DIR, { recursive: true })
}

async function loadGraph() {
  try {
    const raw = await readFile(GRAPH_FILE, 'utf-8')
    return JSON.parse(raw)
  } catch {
    return { nodes: [], edges: [] }
  }
}

async function saveGraph(graph) {
  await ensureDataDir()
  await writeFile(GRAPH_FILE, JSON.stringify(graph, null, 2))
  return graph
}

app.get('/api/graph', async (_req, res) => {
  res.json(await loadGraph())
})

app.put('/api/graph', async (req, res) => {
  const saved = await saveGraph(req.body)
  io.emit('graph:updated', saved)
  res.json({ ok: true, saved })
})

app.get('/api/refs', async (_req, res) => {
  try {
    const refsDir = join(ROOT, 'refs')
    const entries = await readdir(refsDir, { withFileTypes: true })
    const refs = []
    for (const e of entries) {
      if (e.isDirectory()) {
        const s = await stat(join(refsDir, e.name))
        refs.push({ name: e.name, modified: s.mtime.toISOString() })
      }
    }
    res.json(refs)
  } catch {
    res.json([])
  }
})

io.on('connection', (socket) => {
  socket.on('graph:update', async (graph) => {
    const saved = await saveGraph(graph)
    socket.broadcast.emit('graph:updated', saved)
  })
})

// Watch the graph file so external edits propagate too
const watcher = chokidar.watch(GRAPH_FILE, { ignoreInitial: true })
watcher.on('change', async () => {
  io.emit('graph:updated', await loadGraph())
})

await ensureDataDir()
if (!await loadGraph()) await saveGraph({ nodes: [], edges: [] })

httpServer.listen(PORT, () => {
  console.log(`server on http://localhost:${PORT}`)
})
