# honcho-pi web

The memory universe: an interactive graph of everything the agent remembers,
plus a dashboard for store health. Served by `omp_memory.web` (stdlib Python).

## Run it

```bash
bash bin/overview.sh --port 4174     # from a repo checkout
omp-memory overview --port 4174      # from an install
```

Open http://localhost:4174. The store is read from `OMP_MEMORY_DB`.

## What you can do

- **Explore**: drag, zoom, hover a node to light up its threads. Double-click
  collapses the view to that node's neighborhood (local graph).
- **Edit a record**: click a memory node, pencil icon, change the content,
  give evidence, save. The edit writes to SQLite as a logged correction.
- **Attach an image**: click a node, image icon, pick a file. The node becomes
  a thumbnail. Agents can do the same via the API.
- **Dashboard**: toolbar icon. Records by type, confidence buckets, capture
  activity, unlinked count, most-connected nodes.

## API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/graph` | GET / PUT | Node positions, labels, images (cosmetics) |
| `/api/record/<id>` | GET | Full record from the store |
| `/api/record` | POST | Edit content/evidence (correction primitive) |
| `/api/image` | POST | `{"path": ...}` or `{"dataUrl": ...}`, returns `/media/...` |
| `/api/stats` | GET | Dashboard numbers |
| `/events` | GET (SSE) | Live graph updates across tabs |

Content edits require evidence or `verified`, cap at 20000 chars, and are
logged as corrections. Images: PNG/JPEG/WebP/GIF only, 8 MB cap.

## Development

```bash
npm install
npm run dev    # vite :5173 + express :4001 (graph cosmetics)
```

Store-backed endpoints proxy to the Python server, so also run
`bash bin/overview.sh --port 4174` in dev.

After UI changes, deploy so the installed server picks them up:

```bash
bash bin/web-deploy.sh    # npm build + copy dist to ~/.omp/memory/web
```

## Stack

React 19, @xyflow/react 13, zustand 5, d3-force, tailwindcss 4, lucide-react.
Prod server: Python stdlib `http.server` + SSE, no runtime deps.