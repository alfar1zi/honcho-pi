"""Interactive graph view server for the memory store.

Serves the built single-page app in web/dist/ and a small JSON API over the
records graph. Pure standard library: http.server plus a hand-rolled
Server-Sent Events fan-out. The frontend edits node positions and labels in
web/data/graph.json through PUT /api/graph; record nodes are read-only here
(the CLI and MCP server own writes to the store).
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"
GRAPH_FILE = WEB_DIR / "data" / "graph.json"

sse_clients: list = []
sse_lock = threading.Lock()

# Layout: records ring inward, skills ring outward.
RING_RECORD = 420
RING_SKILL = 780


def build_graph() -> dict:
    """Read the store and rebuild the default graph (records + skills)."""
    conn = db.connect()
    try:
        rows = db.list_records(conn, limit=1000)
    finally:
        conn.close()

    nodes = []
    for r in rows:
        first = next((l for l in r["content"].splitlines() if l.strip()), "")
        nodes.append(
            {
                "id": f"mem-{r['id']}",
                "type": "graphNode",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": first[:90],
                    "type": r["type"],
                    "meta": f"memory · conf {float(r['confidence']):.2f}"
                    + (" · verified" if r["verified"] else ""),
                    "color": _TYPE_COLORS.get(r["type"], "#64748b"),
                },
            }
        )

    skill_nodes = _skill_nodes()
    nodes.extend(skill_nodes)

    edges = _similarity_edges(rows)
    edges.extend(_skill_edges(rows, skill_nodes))

    _ring_layout(nodes)
    return {"nodes": nodes, "edges": edges}


_TYPE_COLORS = {
    "RULE": "#7c3aed",
    "FACT": "#10b981",
    "CORRECTION": "#f59e0b",
    "DECISION": "#06b6d4",
    "INFERENCE": "#94a3b8",
    "skill": "#ec4899",
}


def _skill_nodes() -> list:
    home = Path.home()
    dirs = [
        home / ".omp/agent/managed-skills",
        home / ".omp/agent/skills",
    ]
    nodes = []
    for root in dirs:
        if not root.is_dir():
            continue
        source = "managed" if "managed" in str(root) else "user"
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                body = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            desc = ""
            m = re.search(r"^description:\s*(.+)$", body, re.M)
            if m:
                desc = m.group(1).strip()
            nodes.append(
                {
                    "id": f"skill-{source}-{child.name}",
                    "type": "graphNode",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "label": child.name,
                        "type": "skill",
                        "meta": f"{source} skill" + (f" · {desc[:60]}" if desc else ""),
                        "color": _TYPE_COLORS["skill"],
                    },
                }
            )
    return nodes


def _tokens(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 4}


def _tokens(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 4}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _tfidf_vectors(docs: list[str]) -> list[dict]:
    """TF-IDF weight vectors, one dict per doc. Term weight = tf * idf."""
    n = len(docs)
    df: dict[str, int] = {}
    tokenized = []
    for doc in docs:
        toks = [w for w in re.split(r"[^a-z0-9]+", doc.lower()) if len(w) > 4]
        tokenized.append(toks)
        for w in set(toks):
            df[w] = df.get(w, 0) + 1
    vecs = []
    for toks in tokenized:
        tf: dict[str, int] = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        vecs.append(
            {
                w: (1 + math.log(c)) * math.log((n + 1) / (df[w] + 1)) + 1e-9
                for w, c in tf.items()
            }
        )
    return vecs


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[w] * b[w] for w in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _similarity_edges(rows: list) -> list:
    vecs = _tfidf_vectors([r["content"] for r in rows])
    edges = []
    seen = set()
    for i, vec in enumerate(vecs):
        scored = []
        for j, other in enumerate(vecs):
            if i == j:
                continue
            sim = _cosine(vec, other)
            if sim >= 0.22:
                scored.append((j, sim))
        scored.sort(key=lambda p: -p[1])
        for j, _ in scored[:3]:
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "id": f"e-sim-{key[0]}-{key[1]}",
                    "source": f"mem-{rows[i]['id']}",
                    "target": f"mem-{rows[j]['id']}",
                    "label": "similar",
                }
            )
    return edges


def _skill_edges(rows: list, skill_nodes: list) -> list:
    edges = []
    for r in rows:
        r_tokens = _tokens(r["content"][:200])
        if not r_tokens:
            continue
        for s in skill_nodes:
            s_tokens = _tokens(
                s["data"]["label"] + " " + s["data"].get("meta", "")
            )
            if _jaccard(r_tokens, s_tokens) >= 0.12:
                edges.append(
                    {
                        "id": f"e-memsim-{r['id']}-{s['id']}",
                        "source": f"mem-{r['id']}",
                        "target": s["id"],
                        "label": "related",
                    }
                )
    return edges


def _ring_layout(nodes: list) -> None:
    records = [n for n in nodes if n["id"].startswith("mem-")]
    skills = [n for n in nodes if n["id"].startswith("skill-")]
    for i, n in enumerate(records):
        a = 2 * math.pi * i / max(1, len(records))
        n["position"] = {
            "x": RING_RECORD * math.cos(a) * 1.6,
            "y": RING_RECORD * math.sin(a) * 1.1,
        }
    for i, n in enumerate(skills):
        a = 2 * math.pi * i / max(1, len(skills))
        n["position"] = {"x": RING_SKILL * math.cos(a), "y": RING_SKILL * math.sin(a)}


def _load_graph() -> dict:
    if GRAPH_FILE.is_file():
        try:
            return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return build_graph()


def _save_graph(graph: dict) -> None:
    GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_FILE.write_text(json.dumps(graph, indent=2), encoding="utf-8")


def _broadcast(event: str, data) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with sse_lock:
        dead = []
        for res in sse_clients:
            try:
                res.write(payload)
            except OSError:
                dead.append(res)
        for res in dead:
            sse_clients.remove(res)


class Handler(BaseHTTPRequestHandler):
    server_version = "omp-memory-overview/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/graph":
            self._json(_load_graph())
            return
        if path == "/events":
            self._sse()
            return
        self._static(path)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        with sse_lock:
            sse_clients.append(self.wfile)
        try:
            while True:
                # Blocked writes are fine; a dropped client raises and cleans up.
                threading.Event().wait(3600)
        except (OSError, ConnectionError):
            pass
        finally:
            with sse_lock:
                if self.wfile in sse_clients:
                    sse_clients.remove(self.wfile)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        target = (DIST_DIR / path.lstrip("/")).resolve()
        if not target.is_file() or not str(target).startswith(str(DIST_DIR)):
            # SPA fallback
            target = DIST_DIR / "index.html"
        if not target.is_file():
            self.send_error(404, "run npm run build in web/")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/graph":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            graph = json.loads(raw)
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        _save_graph(graph)
        _broadcast("graph:updated", graph)
        self._json({"ok": True})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="omp-memory overview")
    ap.add_argument("--port", type=int, default=4173)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    if not DIST_DIR.is_file() and not (DIST_DIR / "index.html").exists():
        print(f"[overview] dist missing: {DIST_DIR}", file=__import__("sys").stderr)
        print("[overview] run: npm --prefix web run build", file=__import__("sys").stderr)
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[overview] {url}")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
