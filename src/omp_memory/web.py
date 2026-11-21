"""Interactive graph view server for the memory store.

Serves the built single-page app in web/dist/ and a small JSON API over the
records graph. Pure standard library: http.server plus a hand-rolled
Server-Sent Events fan-out. Graph cosmetics (positions, labels, images) live
in web/data/graph.json via PUT /api/graph. Record content edits write back to
SQLite through POST /api/record; images are copied into web/data/media/ via
POST /api/image so both the operator and agents can attach them.
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

# In a source checkout web/ sits next to src/; in a venv install it does not,
# so OMP_MEMORY_WEB_DIR (or ~/.omp/memory/web) points at the built assets.
_WEB_CANDIDATES = [
    Path(os.environ["OMP_MEMORY_WEB_DIR"]) if os.environ.get("OMP_MEMORY_WEB_DIR") else None,
    Path(__file__).resolve().parent.parent.parent / "web",
    Path.home() / ".omp/memory/web",
]
WEB_DIR = next((p for p in _WEB_CANDIDATES if p and (p / "dist" / "index.html").is_file()),
               next(p for p in _WEB_CANDIDATES if p))
DIST_DIR = WEB_DIR / "dist"
GRAPH_FILE = WEB_DIR / "data" / "graph.json"
MEDIA_DIR = WEB_DIR / "data" / "media"

# Images: raster formats only, no SVG (script vector), 8 MB cap.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_IMAGE_MAX_BYTES = 8 * 1024 * 1024

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


def _refresh_graph() -> dict:
    """Rebuild from the store keeping cosmetics, persist, return. Called after
    a record write so every tab sees fresh content without losing layout."""
    graph = _merge_fresh(_load_graph())
    _save_graph(graph)
    return graph


def _merge_fresh(saved: dict) -> dict:
    """Rebuild from the store, keep operator cosmetics (position/label/image).

    The store owns record truth; graph.json owns presentation. Without this
    merge a content edit in the UI would be reverted by the next rebuild.
    """
    fresh = build_graph()
    old = {n["id"]: n for n in saved.get("nodes", [])}
    for n in fresh["nodes"]:
        prev = old.get(n["id"])
        if not prev:
            continue
        if prev.get("position"):
            n["position"] = prev["position"]
        for key in ("image", "color"):
            if key in prev.get("data", {}):
                n["data"][key] = prev["data"][key]
        if not n["id"].startswith("mem-") and "meta" in prev.get("data", {}):
            n["data"]["meta"] = prev["data"]["meta"]
        # Label override only when the operator explicitly renamed the node
        # (customLabel flag set by the UI); otherwise fresh content wins.
        if prev.get("data", {}).get("customLabel") and prev["data"].get("label"):
            n["data"]["label"] = prev["data"]["label"]
            n["data"]["customLabel"] = True
    # Operator-added nodes (notes) have no store record; keep them as-is.
    fresh_ids = {n["id"] for n in fresh["nodes"]}
    for n in saved.get("nodes", []):
        if n["id"] not in fresh_ids:
            fresh["nodes"].append(n)
    fresh_edge_ids = {e["id"] for e in fresh["edges"]}
    for e in saved.get("edges", []):
        if e["id"] not in fresh_edge_ids:
            fresh["edges"].append(e)
    return fresh


def _record_payload(r: dict) -> dict:
    return {
        "id": r["id"],
        "type": r["type"],
        "content": r["content"],
        "evidence": r["evidence"],
        "confidence": r["confidence"],
        "verified": bool(r["verified"]),
        "tags": r["tags"],
        "source": r["source"],
        "project_id": r["project_id"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _update_record(record_id: str, patch: dict) -> dict:
    """Edit one record in place. A content change goes through the store's
    correction primitive (db.reinforce): same id, FTS trigger sync, evidence
    or verified required, length capped, confidence surprise + supersede
    history applied. A raw UPDATE here would bypass those invariants."""
    conn = db.connect()
    try:
        r = db.get_record(conn, record_id)
        if not r:
            raise KeyError(record_id)
        content = patch.get("content", r["content"])
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must not be empty")
        evidence = patch.get("evidence", r["evidence"])
        verified = 1 if patch.get("verified", r["verified"]) else 0
        if content.strip() != r["content"].strip():
            try:
                db.reinforce(conn, record_id, "correction",
                             content=content, evidence=str(evidence),
                             verified=verified, source="web-ui")
            except ValueError as e:
                # reinforce words it as "correction rewrite..."; the UI
                # caller just edited content, so translate the wording.
                raise ValueError(str(e).replace("correction rewrite", "content edit"))
        else:
            conn.execute(
                "UPDATE records SET evidence=?, verified=?, updated_at=?"
                " WHERE id=?",
                (str(evidence), verified, db.now_iso(), record_id),
            )
            conn.commit()
        return _record_payload(db.get_record(conn, record_id))
    finally:
        conn.close()


def _store_image(payload: dict) -> dict:
    """Copy a local file or decode a data URL into MEDIA_DIR. Agents call this
    with {"path": "/abs/file.png"}; the browser sends {"dataUrl": "data:..."}.
    Returns the served URL. Raster only, size-capped, content sniffed."""
    import base64
    import hashlib

    if payload.get("path"):
        src = Path(payload["path"]).expanduser()
        if not src.is_file():
            raise FileNotFoundError(str(src))
        ext = src.suffix.lower()
        if ext not in _IMAGE_EXTS:
            raise ValueError(f"unsupported image type: {ext}")
        data = src.read_bytes()
    elif payload.get("dataUrl"):
        m = re.match(r"data:image/(png|jpeg|webp|gif);base64,(.+)$",
                     payload["dataUrl"], re.S)
        if not m:
            raise ValueError("dataUrl must be a base64 raster image")
        ext = ".jpg" if m.group(1) == "jpeg" else f".{m.group(1)}"
        data = base64.b64decode(m.group(2))
    else:
        raise ValueError("provide path or dataUrl")
    if len(data) > _IMAGE_MAX_BYTES:
        raise ValueError("image exceeds 8 MB")
    if not _sniff_image(data, ext):
        raise ValueError("file content does not match an image signature")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(data).hexdigest()[:16] + ext
    (MEDIA_DIR / name).write_bytes(data)
    return {"url": f"/media/{name}"}


def _sniff_image(data: bytes, ext: str) -> bool:
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _dashboard_stats() -> dict:
    """Real numbers for the dashboard: store stats + graph health + activity."""
    conn = db.connect()
    try:
        s = db.stats(conn)
        rows = conn.execute(
            "SELECT confidence, created_at, source FROM records"
        ).fetchall()
    finally:
        conn.close()
    buckets = {"low": 0, "mid": 0, "high": 0}
    per_day: dict = {}
    auto = 0
    for r in rows:
        c = float(r["confidence"])
        buckets["low" if c < 0.4 else "mid" if c < 0.7 else "high"] += 1
        day = (r["created_at"] or "")[:10]
        if day:
            per_day[day] = per_day.get(day, 0) + 1
        if "auto-capture" in (r["source"] or ""):
            auto += 1
    graph = _load_graph()
    degree: dict = {}
    for e in graph["edges"]:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    nodes = graph["nodes"]
    orphans = sum(1 for n in nodes if n["id"] not in degree)
    top = sorted(nodes, key=lambda n: -degree.get(n["id"], 0))[:8]
    days = sorted(per_day)[-14:]
    return {
        "store": s,
        "confidence": buckets,
        "auto_captured": auto,
        "graph": {
            "nodes": len(nodes),
            "edges": len(graph["edges"]),
            "orphans": orphans,
            "top_connected": [
                {"id": n["id"], "label": n["data"].get("label", n["id"]),
                 "type": n["data"].get("type", ""), "degree": degree.get(n["id"], 0)}
                for n in top
            ],
        },
        "activity": [{"day": d, "count": per_day[d]} for d in days],
    }


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
        if path == "/api/stats":
            self._json(_dashboard_stats())
            return
        if path.startswith("/api/record/"):
            conn = db.connect()
            try:
                r = db.get_record(conn, path[len("/api/record/"):])
            finally:
                conn.close()
            if not r:
                self._json({"error": "not found"}, 404)
                return
            self._json(_record_payload(r))
            return
        if path.startswith("/media/"):
            self._media(path[len("/media/"):])
            return
        if path == "/events":
            self._sse()
            return
        self._static(path)

    def _media(self, name):
        # Content-addressed filenames only; no traversal, no dotfiles.
        if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|jpeg|webp|gif)", name):
            self.send_error(404)
            return
        target = MEDIA_DIR / name
        if not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

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

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > _IMAGE_MAX_BYTES + 4 * 1024 * 1024:
            raise ValueError("payload too large")
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/record", "/api/image"):
            self.send_error(404)
            return
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError) as e:
            self._json({"error": str(e)}, 400)
            return
        if path == "/api/image":
            try:
                self._json(_store_image(payload))
            except (OSError, ValueError) as e:
                self._json({"error": str(e)}, 400)
            return
        record_id = payload.get("id", "")
        if not record_id:
            self._json({"error": "id required"}, 400)
            return
        try:
            updated = _update_record(record_id, payload)
        except KeyError:
            self._json({"error": "not found"}, 404)
            return
        except ValueError as e:
            self._json({"error": str(e)}, 400)
            return
        # Refresh so every open tab sees the new content; cosmetics survive.
        graph = _refresh_graph()
        _broadcast("graph:updated", graph)
        self._json(updated)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="omp-memory overview")
    ap.add_argument("--port", type=int, default=4173)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    if not (DIST_DIR / "index.html").exists():
        import sys
        print(f"[overview] dist missing: {DIST_DIR}", file=sys.stderr)
        checkout = Path(__file__).resolve().parent.parent.parent
        if (checkout / "web").is_dir():
            print("[overview] run: bash bin/web-deploy.sh", file=sys.stderr)
        else:
            print("[overview] run bin/web-deploy.sh from a repo checkout "
                  "(or set OMP_MEMORY_WEB_DIR)", file=sys.stderr)
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
