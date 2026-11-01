# honcho — persistent, self-correcting memory for omp agents

Memory that an agent **queries and updates**, not a Markdown file that grows and is never read.

This repository has two layers:

| Layer | File | Role |
|---|---|---|
| Storage | `memory_db.py` | SQLite + FTS5, quarantine-based recovery, decay |
| **Runtime** | `honcho_mcp.py` | **MCP server** — the layer that makes memory actually get used |
| Import | `migrate_md.py` | One-shot import of legacy `learned.md` files |

Without `honcho_mcp.py` the database is inert. That was the gap in v1: `memory_provider.py` defined a `prefetch()` API that nothing ever called, so memories were written and never surfaced. The MCP server closes it — the agent gets `recall`/`remember`/`reinforce` as real tools in every session.

---

## What makes it "learning" and not just storage

1. **Recall before work.** The agent searches memory with keywords from the task.
2. **Honest typing.** `FACT` only when verified; `INFERENCE`/`ASSUMPTION` otherwise. Prevents a guess from hardening into doctrine.
3. **Reinforcement.** After reality tests a memory, `success`/`failure`/`correction` moves its confidence.
4. **Decay.** Unverified records drift toward a 0.15 floor on a 30-day half-life. Stale claims lose to verified ones instead of competing forever.
5. **Ranking by relevance first.** bm25 weighted 0.85, confidence 0.15. A well-reinforced but irrelevant memory cannot bulldoze the real hit.

---

## Single user (stdio)

```bash
# 1. Put the files where omp can find them
mkdir -p ~/.omp/profiles/<profile>/agent/honcho
cp *.py ~/.omp/profiles/<profile>/agent/honcho/
chmod +x ~/.omp/profiles/<profile>/agent/honcho/*.py
```

Register the server in `~/.omp/profiles/<profile>/agent/mcp.json`:

```json
{
  "mcpServers": {
    "mem": {
      "type": "stdio",
      "command": "python3",
      "args": ["<home>/.omp/profiles/<profile>/agent/honcho/honcho_mcp.py"],
      "env": {
        "HONCHO_HOME": "<home>/.omp/profiles/<profile>/agent/honcho"
      }
    }
  }
}
```

```bash
# 2. Import memory that already exists as Markdown
python3 honcho/honcho_mcp.py --help >/dev/null 2>&1 || true
python3 honcho/migrate_md.py \
  ~/.omp/profiles/<profile>/agent/memories/*/learned.md \
  --db ~/.omp/profiles/<profile>/agent/honcho/memory.db --project myproject

# 3. Reclaim the Markdown so it stops occupying the context window
python3 honcho/reclaim.py --all --dry-run   # preview
python3 honcho/reclaim.py --all             # import, verify, archive
```

Restart omp. The agent now sees `mcp__mem_recall`, `mcp__mem_remember`,
`mcp__mem_reinforce`, `mcp__mem_forget` and `mcp__mem_stats`.

---

## Self-hosted, many users (HTTP)

One process, one token per user, one database per user.

```bash
HONCHO_HOME=/srv/honcho \
HONCHO_TRANSPORT=http \
HONCHO_HOST=0.0.0.0 \
HONCHO_PORT=8765 \
HONCHO_TOKENS="alice=$(openssl rand -hex 24),bob=$(openssl rand -hex 24)" \
python3 honcho_mcp.py
```

**Isolation.** Each bearer token hashes to its own directory:

```
/srv/honcho/users/<sha256(token)[:16]>/memory.db
```

Two tokens never share a database — verified by test: alice writes, bob recalls nothing.

**Auth is not optional when public.** The server refuses to bind a non-loopback address with an empty `HONCHO_TOKENS`.

**Client config** (any machine):

```json
{
  "mcpServers": {
    "mem": {
      "type": "http",
      "url": "https://honcho.example.com/",
      "headers": { "Authorization": "Bearer alice=<token>" }
    }
  }
}
```

`GET /health` returns `{"ok": true, ...}` for load balancers.

**Put it behind TLS.** The server speaks plain HTTP; terminate TLS at nginx/Caddy and never expose port 8765 directly.

---

## Tools

| Tool | Purpose | Key arguments |
|---|---|---|
| `recall` | Search memory before acting | `query`, `limit`, `type`, `project`, `verified_only`, `min_confidence` |
| `remember` | Persist something durable | `content`, `type`, `evidence`, `verified`, `confidence`, `tags`, `project` |
| `reinforce` | Calibrate after reality | `id`, `signal` (`success`/`failure`/`correction`/`decay`) |
| `forget` | Delete a provably wrong record | `id` |
| `stats` | Counts by type | — |

Record types: `FACT`, `ASSUMPTION`, `INFERENCE`, `DECISION`, `RULE`, `CORRECTION`.

---

## Reliability guarantees

**The database is never deleted.** v1's `connect()` removed the file on any `DatabaseError`, silently destroying data. Now:

1. Try to open.
2. On corruption: move to `memory.db.corrupt.<timestamp>` (**quarantined, preserved**).
3. Restore the newest backup that passes `integrity_check`.
4. Only if no backup is usable, start fresh at the same path — the corrupt file still exists for forensics.

**FTS cannot desync.** `records_fts` is maintained by `AFTER INSERT/UPDATE/DELETE` triggers on `records`, so no writer can leave the index stale. `rebuild_fts()` repairs databases created before triggers existed.

**Backups rotate** (10 max, mode 0600) and are written through the SQLite backup API, not `cp`, so a snapshot is consistent even while the DB is in use.

**No secrets.** `remember` refuses content matching API-key, bearer-token or private-key patterns.

---

## Tests

```bash
# storage: write, search, reinforce, dedup, scoping, decay
python3 - <<'EOF'
import os, sys; sys.path.insert(0, '.')
os.environ['OMP_MEMORY_DB'] = '/tmp/t.db'
import memory_db as m
c = m.init_db(m.connect('/tmp/t.db'), '/tmp/t.db')
rid = m.upsert_record(c, 'FACT', 'probe content', verified=1, confidence=0.9, source='t')
assert m.search(c, 'probe')[0]['id'] == rid
assert m.reinforce(c, rid, 'failure') == 0.8
print('storage OK')
EOF

# MCP protocol over stdio
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | HONCHO_HOME=/tmp/h python3 honcho_mcp.py
```

---

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HONCHO_HOME` | `$PI_CODING_AGENT_DIR/honcho` or `~/.omp/profiles/<profile>/agent/honcho` | Data root |
| `OMP_MEMORY_DB` | `<HONCHO_HOME>/memory.db` | Explicit DB path (single-user) |
| `HONCHO_TRANSPORT` | `stdio` | `stdio` or `http` |
| `HONCHO_HOST` / `HONCHO_PORT` | `127.0.0.1` / `8765` | HTTP bind |
| `HONCHO_TOKENS` | empty | Comma-separated bearer tokens |
| `HONCHO_MAX_RECALL` | `8` | Default recall limit |
| `OMP_MEMORY_BUSY_TIMEOUT_MS` | `30000` | SQLite lock wait |
| `OMP_MEMORY_MAX_BACKUPS` | `10` | Backup rotation depth |

---


## Context budget: the silent killer

A memory system that injects too much is worse than none. Measured on a real
profile before this work:

| Source | Tokens | Share |
|---|---|---|
| `memories/*.md` (397 KB) | 99,431 | 56% |
| 207 MCP `rea` tool schemas | 63,600 | 36% |
| Template + APPEND | 16,038 | 9% |
| **Total** | **179,069** | **140% of 128K** |

At 140% the provider truncates silently: instructions vanish, the model
hallucinates, and nothing in the transcript explains why.

### Fix 1 — memory belongs in SQLite, not Markdown

`reclaim.py` imports every `memories/*.md` into honcho, verifies the record
count, *then* archives the file and leaves a stub pointer.

```
140% -> 65% of the context window   (94k tokens recovered)
```

```bash
python3 reclaim.py --all --dry-run   # see what would move
python3 reclaim.py --all             # import, verify, archive
```

Re-running is safe: content hashes prevent duplicates and already-imported
files are detected.

### Fix 2 — measure, don't guess

```bash
python3 budget.py          # per-component tokens + verdict
python3 budget.py --json   # machine-readable
```

It probes each stdio MCP server for its real tool count and estimates schema
cost at ~300 tokens/tool. A server with 200 tools costs more than every
memory file combined.

### Fix 3 — stop the growth at the source

omp hardcodes `MAX_LEARNED_LESSONS = 100` at up to 2000 chars each, so a single
`learned.md` can reach 200 KB. That is not configurable, so guard it:

```bash
python3 guard.py compact                  # archive anything > 20 KB
python3 guard.py compact --threshold-bytes 10000
```

Schedule it:

```cron
0 3 * * * HONCHO_HOME=$HOME/.honcho python3 $HOME/.honcho/guard.py compact >> /tmp/honcho-guard.log 2>&1
```

`guard.py` is omp/pi specific: it compacts `memories/*.md`, which only those
harnesses write. Under anything else it finds nothing and exits.

### When to prune tools instead

If `budget.py` reports tools above ~50% of the window, the answer is fewer MCP
servers, not smaller memories. Disable a server in `mcp.json` with
`"enabled": false` rather than deleting its config.

---


## License

MIT. See the repository for the full text.
