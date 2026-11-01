# honcho — persistent, self-correcting memory for MCP agents

[![test](https://github.com/alfar1zi/honcho-pi/actions/workflows/test.yml/badge.svg)](https://github.com/alfar1zi/honcho-pi/actions/workflows/test.yml)

Memory that an agent **queries and updates**, not a Markdown file that grows and is never read.

| Layer | File | Role |
|---|---|---|
| Storage | `memory_db.py` | SQLite + FTS5, quarantine-based recovery, decay |
| **Runtime** | `honcho_mcp.py` | **MCP server** — the layer that makes memory actually get used |
| Import | `migrate_md.py` | One-shot import of legacy Markdown memory |
| Reclaim | `reclaim.py` | Move memory files into SQLite, then archive them |
| Budget | `budget.py` | Measure what actually consumes the context window |
| Guard | `guard.py` | Compact oversized memory files on a schedule |

Without `honcho_mcp.py` the database is inert. A storage layer answers *where*
memories live; it does not make an agent use them. The MCP server closes that
gap — the agent gets `recall`, `remember` and `reinforce` as real tools in every
session.

## Works with any MCP client

The server speaks plain MCP over stdio (and Streamable HTTP), so it is not tied
to one harness: Claude Code, Codex, OpenCode, Cursor, omp/pi, and anything else
that can launch a stdio server.

It serves **both protocol eras** — `server/discover` for the 2026-07-28 revision
(per-request `_meta`) and the classic `initialize` handshake for everything
older. A server supporting only one era is invisible to half the ecosystem.

Data lives in `~/.honcho` by default. Set `HONCHO_HOME` to put it anywhere.

---

## What makes it "learning" and not just storage

1. **Recall before work.** The agent searches memory with keywords from the task.
2. **Honest typing.** `FACT` only when verified; `INFERENCE`/`ASSUMPTION`
   otherwise. A guess must not enter the store wearing a fact's weight.
3. **Reinforcement.** After reality tests a memory, `success`/`failure`/
   `correction` moves its confidence.
4. **Decay.** Unverified records drift toward a 0.15 floor on a 30-day
   half-life. Stale claims lose to verified ones instead of competing forever.
5. **Ranking by relevance first.** bm25 weighted 0.85, confidence 0.15. A
   well-reinforced but irrelevant memory cannot bulldoze the real hit.

---

## Install

```bash
git clone https://github.com/alfar1zi/honcho-pi
cd honcho-pi
./install.sh
```

`install.sh` detects the harness and writes the right config shape:

| Harness | Config file | Format |
|---|---|---|
| omp / pi | `$PI_CODING_AGENT_DIR/mcp.json` | JSON |
| Claude Code | `~/.claude.json` | JSON |
| Codex | `~/.codex/config.toml` | TOML |
| OpenCode | `~/.config/opencode/opencode.json` | JSON |
| Cursor | `~/.cursor/mcp.json` | JSON |
| anything else | printed snippet | copy by hand |

Force a target with `--harness omp`, or install files only with `--dir DIR`.

Manual registration:

```json
{
  "mcpServers": {
    "mem": {
      "type": "stdio",
      "command": "python3",
      "args": ["<home>/.honcho/honcho_mcp.py"],
      "env": { "HONCHO_HOME": "<home>/.honcho" }
    }
  }
}
```

Then import memory that already exists as Markdown:

```bash
python3 honcho/migrate_md.py <file.md> --db ~/.honcho/memory.db --project myproject
python3 honcho/reclaim.py --all --dry-run    # preview what moves
python3 honcho/reclaim.py --all              # import, verify, archive
```

Restart the client. The agent gains `recall`, `remember`, `reinforce`, `forget`
and `stats`.

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

**Isolation is structural.** Each bearer token hashes to its own directory:

```
/srv/honcho/users/<sha256(token)[:16]>/memory.db
```

Two tokens never share a database — verified by writing as one and recalling as
the other.

**Auth is not optional when public.** The server refuses to bind a non-loopback
address with an empty `HONCHO_TOKENS`.

Client config from any machine:

```json
{
  "mcpServers": {
    "mem": {
      "type": "http",
      "url": "https://honcho.example.com/",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

`GET /health` returns `{"ok": true, ...}` for load balancers. Terminate TLS at
nginx or Caddy; the server speaks plain HTTP on purpose. Do not expose the port
directly.

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

**The database is never deleted.** A corrupt file is quarantined to
`memory.db.corrupt.<timestamp>` (**preserved**, not unlinked), then the newest
backup that passes `integrity_check` is restored. Only if no backup is usable
does a fresh database start at the same path — the damaged file still exists
for forensics.

**FTS cannot desync.** `records_fts` is maintained by `AFTER INSERT/UPDATE/
DELETE` triggers on `records`, so no writer can leave the index stale.
`rebuild_fts()` repairs databases created before those triggers existed.

**Backups rotate** (10 max, mode 0600) and are written through the SQLite
backup API, not `cp`, so a snapshot is consistent even while the database is in
use.

**No secrets.** `remember` refuses content matching API-key, bearer-token or
private-key shapes. An agent that stores a secret has written it into a file it
will later quote back into a prompt.

---

## Tests

Runs on Python 3.10–3.13 via GitHub Actions on every push and pull request.

```bash
python3 test_honcho.py -v
```

Sixteen tests, no pytest, no network, no fixtures. Each corresponds to a defect
that reached usage:

```
storage     ranking with uniform confidence, hostile FTS queries, dedup,
            index sync under direct SQL, reinforce clamping, decay exemptions
recovery    corrupted database quarantined and restored with no rows lost
mcp         handshake, tool cycle, secret refusal, cross-process persistence,
            both protocol eras, harness-neutral default path
migration   importing the same Markdown twice inserts zero rows
```

---

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HONCHO_HOME` | `~/.honcho` | Data root (all harnesses) |
| `HONCHO_DB` | `<HONCHO_HOME>/memory.db` | Database path |
| `HONCHO_BACKUP_DIR` | `<HONCHO_HOME>/backups` | Backup rotation |
| `HONCHO_TRANSPORT` | `stdio` | `stdio` or `http` |
| `HONCHO_HOST` / `HONCHO_PORT` | `127.0.0.1` / `8765` | HTTP bind |
| `HONCHO_TOKENS` | empty | Comma-separated bearer tokens |
| `HONCHO_MAX_RECALL` | `8` | Default recall limit |
| `HONCHO_EXTRA_TOOLS` | empty | Directories with `local_*.py` / `adapter_*.py` |
| `PI_CODING_AGENT_DIR` | unset | omp/pi agent dir, honoured for its per-profile data |
| `OMP_MEMORY_DB` / `OMP_MEMORY_BACKUP_DIR` | unset | Legacy names, still accepted |

---

## Optional tools

The five tools above work everywhere. Anything inherently harness-specific —
a session-transcript reader, for example — belongs outside the package, because
the formats it depends on are undocumented and change without notice.

```bash
export HONCHO_EXTRA_TOOLS="$HOME/.honcho/local"
```

Each module there named `local_*.py` or `adapter_*.py` may expose:

```python
TOOLS = [{"name": "handoff", "description": "...", "inputSchema": {}}]

def call(args, user=""):
    return "text returned to the model"
```

The tool merges into the server at startup. A module that raises is reported on
stderr and skipped; a name that collides with a core tool is ignored. Modules
without those prefixes are never imported, so helper files in the same
directory are not executed.

---

## Context budget

A memory system that injects too much is worse than none. Measured on a real
profile before this work:

| Source | Tokens | Share |
|---|---|---|
| `memories/*.md` (397 KB) | 99,431 | 56% |
| 207 MCP tool schemas | 63,600 | 36% |
| template + append | 16,038 | 9% |
| **Total** | **179,069** | **140% of 128K** |

At 140% the provider truncates silently: instructions vanish, the model
invents, and nothing in the transcript explains why.

**Fix 1 — memory belongs in SQLite.** `reclaim.py` imports every
`memories/*.md`, verifies the record count, then archives the file and leaves a
pointer stub. Re-running is safe: content hashes prevent duplicates.

```
140% -> 65% of the window    (94k tokens recovered)
```

**Fix 2 — measure, don't guess.** `budget.py` reports per-component tokens and
probes each stdio MCP server for its real tool count. In that profile a single
server exposing 207 tools cost more than every memory file combined.

**Fix 3 — bound the growth.** `guard.py compact --threshold-bytes 20000`
archives anything oversized, delegating to `reclaim.py` so the
import-verify-archive guarantees still apply.

---

## Known limits

- **No cross-model handoff in the published build.** A model taking over a
  session sees the message history but not a structured brief. Every harness
  stores transcripts in a different, undocumented format; shipping an adapter
  that breaks silently is worse than shipping none. Use the extension hook
  above when the format is one you control.
- **`guard.py` and `reclaim.py` are omp/pi specific.** They read `memories/*.md`,
  where those harnesses keep lessons. Others never write those files, so both
  tools find nothing to do.
- **Not tested against every MCP client.** The protocol is verified against the
  specification and exercised locally over stdio and HTTP in both the 2025-11-25
  and 2026-07-28 revisions; real-world client behaviour is not covered.
- **No schema migration path.** The schema is stable today; a future change
  would need a migration that does not exist yet.

---

## License

MIT.
