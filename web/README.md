# design-extractor-web

Interactive graph view (Obsidian-style) for design-extractor references.

## Quick start

```bash
npm install
npm run dev        # dev server on :5173 + backend on :4001
```

Open http://localhost:5173. Drag nodes, edit labels/colors in the right panel,
connect nodes by dragging from a bottom handle to a top handle, press Delete to
remove. Every change persists to `data/graph.json` and broadcasts live to other
tabs.

## Production / skill integration

```bash
npm run build      # produces dist/
node ~/.omp/agent/skills/design-extractor/scripts/overview.mjs
```

`overview.mjs` is a zero-dependency server (node:http + SSE) that serves `dist/`,
exposes `/api/graph` (GET/PUT) and `/events` (SSE), and opens the browser.

## Data

`data/graph.json`:

```json
{
  "nodes": [{ "id": "x", "type": "graphNode", "position": {"x":0,"y":0},
              "data": {"label":"X","type":"page","meta":"note","color":"#7c3aed"} }],
  "edges": [{ "id": "e1", "source": "x", "target": "y" }]
}
```

Nodes = captured pages, tokens, components. Edges = relationships (component
uses token, page contains component, navigation link).

## Stack

- React 19 + Vite 6
- @xyflow/react 13 (React Flow canvas)
- zustand 5 (state)
- lucide-react (icons)
- tailwindcss 4
- dev backend: express + socket.io + chokidar
- skill server: node:http + SSE (no deps)
