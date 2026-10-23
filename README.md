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
| **Setup** | `agent_registry.py` + `agent_config.py` + `setup.py` | **Multi-agent install** — one data registry, every MCP client |

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
python3 setup.py detect                 # what is on PATH, and how far honcho can go
python3 setup.py install --agent claude # write the entry (add --dry-run to preview)
```

No dependency, no `pip install`, Python 3.10–3.13.

### 45 agents, one data table

`agent_registry.py` holds every agent as a row — binary, aliases, extra
requirements, runtime exclusions, config path, config format and MCP style —
instead of a branch per agent in an installer. Detection fields are seeded
from [stablyai/orca](https://github.com/stablyai/orca) (MIT) for
binary/detection fields; config fields were researched per agent from vendor
documentation. Adding an agent is a one-line change and every command learns
about it at once: `setup.py` has no agent-specific code.

Each agent gets an integration level:

| Level | Meaning | What `install` does |
|---|---|---|
| `full` | path and shape both verified | merges the entry into the file |
| `print-only` | shape unknown, path unknown, or the vendor CLI owns the file | prints a pasteable snippet, writes nothing |
| `none` | the agent has no MCP support | says so |

```
$ python3 setup.py detect
runtime : linux  (Linux-...-x86_64-with-glibc2.39)
registry: 45 agents  (Seeded from stablyai/orca (MIT) for binary/detection fields)
levels  : full=21, none=1, print-only=23

agent               binary   style            level       note
------------------  -------  ---------------  ----------  -----------------------------------------
claude              claude   json-mcpServers  full
copilot             copilot  json-mcpServers  full
hermes              hermes   yaml             full
claude-agent-teams  orca     unknown          print-only  MCP config location/shape unverified -...
omp                 omp      unknown          print-only  MCP config location/shape unverified -...

5 installed agent(s) detected, 3 writable automatically.
```

The `agent` rows are whatever is on *your* PATH; the counts on the `levels`
line are the registry's and do not depend on what is installed.

`python3 setup.py status` prints a table of all 45: agent, installed?,
level, configured?, config path. `python3 setup.py print --agent ID` prints a
complete minimal file (or CLI command) for any agent, including the ones honcho
deliberately does not write — useful when you want to add the server to a
project config or inspect the shape.

### Shapes are per-agent, not "the JSON one"

Writing the same `{"command", "args", "env"}` block everywhere breaks most
clients. The writers follow each vendor's documented shape. The differences
that actually matter:

| Agent | Where the server goes | Trap |
|---|---|---|
| Claude, Cursor, Gemini, Copilot, Cline, Antigravity, … | `mcpServers.<name>` | Cline disables with `disabled: true`, Antigravity too |
| Codex | `[mcp_servers.<name>]` in TOML | `env` must be the child table `[mcp_servers.<name>.env]` |
| opencode v1, Kilo | `mcp.<name>` | `command` is an **array** (no `args`), env is `environment`, needs `type: "local"` |
| zcode | `mcp.servers.<name>` | two levels deep; the switch is `enable: false` |
| muse | `mcp_servers.<name>` | file must carry `"schema_version": 1` or every command fails |
| amp | the literal dotted key `"amp.mcpServers"` | not a nested `amp` object |
| openclaw | `mcp` root key | layout beyond the root key is inferred, so snippet only |
| hermes | `mcp_servers.<name>` in YAML | — |
| goose | `extensions.<name>` in YAML | fields are `cmd` / `envs`; goose's config also holds providers, so it is **print-only** |
| crush | no file | `crush mcp add …`, printed for you to run |

`opencode` is written in the v1 schema only. If `opencode --version` reports
v2, `install` says so: v2 moved servers to `mcp.servers` and flipped the switch
to `disabled: true`, and silently writing v1 would make the server vanish.

### What install guarantees

- **Merges, never replaces.** Only the server object is added. Your other
  servers, keys, TOML tables and YAML comments stay.
- **Comments survive.** A JSONC file (kilo, opencode `.jsonc`) with `//` or
  `/* */` comments is edited *in its own text* — the entry is spliced in, not
  reserialised, so comments and formatting stay. If the container key is
  missing there is no comment-safe insertion point, so the write is refused
  and a snippet is printed instead.
- **Symlinks are followed, not replaced.** Dotfile managers (stow, chezmoi,
  Nix home-manager) link configs into a repo. honcho resolves the link and
  writes to the target; the link stays a link. A dangling link is refused.
- **Only a missing file is created.** A file that exists but is empty,
  whitespace-only or unparseable is refused and left byte-for-byte untouched.
  honcho never "repairs" a file it cannot read.
- **Backup before writing.** `<file>.honcho-backup-<timestamp>`, mode `0600`,
  containing the original bytes. At most 10 are kept per config; older ones
  are pruned so the directory cannot fill up.
- **Atomic.** Temp file in the same directory, `fsync`, original permissions,
  `os.replace`. A crash leaves the old file or the new one.
- **Idempotent.** An identical entry reports `already configured` and the file
  is not touched. A *different* entry under the same name is a conflict, not
  overwritten: that entry is yours until you remove it. `--name` registers
  under a different name instead, and `status`/`detect` look for that name.
- **One writer at a time.** Each write takes an exclusive advisory lock, held
  on a lock *directory* `<file>.lock/` (a plain lock file cannot be deleted
  safely: unlinking it lets a waiter and a newcomer both hold "the lock").
  The directory is removed on release, so nothing is left behind. The file is
  re-read under the lock before writing; if it changed in the meantime the
  write is refused rather than clobbering the newer content.
- **Read-only configs are left alone.** Write permission is checked under the
  lock, immediately before the write, so a file revoked mid-run is not
  rewritten through its directory.
- **One agent cannot break the rest.** An unexpected error is caught per
  agent and reported as that agent's failure, so `install --all` completes.
- **Dry run touches nothing.** `--dry-run` computes and verifies the merged
  document, then prints a diff and stops.
- **Env overrides honoured.** `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  `COPILOT_HOME`, `HERMES_HOME`, `DSH_HOME`, `GEMINI_CLI_HOME`,
  `CLINE_MCP_SETTINGS_PATH`, `XDG_CONFIG_HOME`.

Parsing uses honcho's own parsers: JSON with comments (comment stripping and
the splice both respect string literals and escapes), `tomllib` on 3.11+ with a
subset parser on 3.10, and a YAML block-mapping/block-sequence parser for the
goose/hermes/dsh/vibe subset. YAML outside that subset — anchors, block
scalars, flow mappings — is refused rather than rewritten.

### Which agents are written, and which only get a snippet

Verified location and shape, so `install` writes the file (21):

```
claude  qoder  gemini  antigravity  amp  kiro  aug  cline  cursor  droid
kimi  qwen-code  copilot  devin          (mcpServers / amp.mcpServers)
codex                                    (TOML [mcp_servers.<name>])
opencode  kilo                           (mcp.<name>, command array)
zcode  muse                              (mcp.servers / mcp_servers)
hermes                                   (YAML mcp_servers, text-merged)
dsh                                      (YAML plugins: sequence, id mcp-honcho)
```

`install` prints a snippet and writes nothing (23):

| Reason | Agents |
|---|---|
| MCP location unverified — sources conflict or no documented user path | `grok`, `pi`, `continue`, `codebuddy`, `qoder-cn`, `opencode2`, `openclaude`, `autohand`, `ante`, `trae`, `mimo-code`, `omp`, `prime-agent`, `freebuff`, `codebuff`, `command-code`, `mistral-vibe`, `rovo`, `jcode`, `claude-agent-teams` |
| Root key evidenced, server-object layout inferred | `openclaw` |
| Config is owned by the vendor CLI (also holds providers/model) | `goose` |
| No stable config file; a CLI subcommand adds the server | `crush` (`crush mcp add honcho …`) |

`aider` has no MCP support at all.

Two cases are worth stating plainly:

- **`grok` is unverified, not missing.** `docs.x.ai` describes a TOML file and
  `agentsync` a JSON one, referring to different CLIs. Ignoring that would mean
  writing into a file the agent never reads.
- **`claude-agent-teams` is not a separate agent.** It is the
  `orca claude-teams` launch mode, detected through the `orca` binary plus a
  required `claude`.

### How the safety claims are checked

Destructive bugs in an installer are not caught by reading the code, so each
guarantee above has a test that fails when the behaviour is removed, verified
by mutation (delete the symlink resolution, the comment splice, or the
empty-file refusal, and the matching test goes red). The explicit scenarios:

| Scenario | Expected, and tested |
|---|---|
| config is a symlink into a dotfiles repo | link stays a link, repo target updated |
| JSONC with `//` and `/* */` | every comment still present after install |
| 5 bytes of whitespace | refused, file byte-identical |
| valid config holding other servers | every other server and key intact, backup = original bytes |
| garbage text | refused, file byte-identical |
| leftover temp files after a write | none; mode preserved |
| `--dry-run` | no file created or modified |
| conflict with an existing `honcho` entry | refused, file unchanged |

`install.sh` is still there for copying the runtime files to another directory
(`./install.sh --dir DIR`); it is no longer how config gets registered.

Then import memory that already exists as Markdown:

```bash
python3 migrate_md.py <file.md> --db ~/.honcho/memory.db --project myproject
python3 reclaim.py --all --dry-run    # preview what moves
python3 reclaim.py --all              # import, verify, archive
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
Both suites run on every version; no pytest, no network, no third-party
packages. Everything that touches a config file uses a temp `HOME`, so a test
run never reads or writes the real one.

```bash
python3 test_honcho.py -v    # memory storage + MCP protocol   (16)
python3 test_agents.py -v    # multi-agent registry + writers   (54)
```

`test_honcho.py` — each test corresponds to a defect that reached usage:

```
storage     ranking with uniform confidence, hostile FTS queries, dedup,
            index sync under direct SQL, reinforce clamping, decay exemptions
recovery    corrupted database quarantined and restored with no rows lost
mcp         handshake, tool cycle, secret refusal, cross-process persistence,
            both protocol eras, harness-neutral default path
migration   importing the same Markdown twice inserts zero rows
```

`test_agents.py` — each test proves one claim made above:

```
registry    45 agents, unique ids, resolve by id/alias/binary, required_cmds,
            unsupported runtimes (win32/wsl), levels derived not guessed,
            printed-only reasons agree with what install() does
parsers     JSONC stripped without touching string literals; TOML subset
            parser vs tomllib on the same input; YAML subset round-trip
            including block sequences, nested plugin items, block scalars,
            unindented lists and next-line `[]`; each parser refuses
            constructs it cannot represent
writers     one test per shape family (mcpServers, opencode/kilo array
            command, amp dotted key, muse schema_version, zcode nesting +
            legacy lift, codex TOML child env table, hermes YAML)
safety      user servers/keys/tables/comments preserved; dsh plugins written
            or refused, never crashing; --name honoured by every writer;
            _verify is load-bearing; is_configured distinguishes "no" from
            "unreadable"; 4 parallel processes keep every edit; no lock left
            behind after success, failure or dry run; backups capped at 10
            over 15 writes; read-only files untouched; BOM preserved;
            symlink followed,
            not replaced (and a dangling link refused); JSONC comments
            survive the write; whitespace-only and unparsable files refused
            and byte-identical; backup equals the original bytes; dry run
            writes nothing; mode preserved, no temp files left behind;
            conflicting entry refused; env overrides honoured
cli         detect/status/print/install --dry-run/install against a fake HOME
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
