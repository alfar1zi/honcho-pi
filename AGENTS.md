# AGENTS.md

## Commands

```bash
python -m pytest          # full suite, ~12s, no network
bash bin/clean-code.sh    # quality gate
bash install.sh --dry-run # preview install
```

## Rules

- Python 3.9+ standard library only. No runtime dependencies.
- Every behaviour change needs a test in `tests/` that fails before the change.
- Tests use `tmp_path`; never touch `~/.omp/memory` from a test.
- `src/omp_memory/db.py` is the only module that opens SQLite. Callers use its API.
- The CLI in `src/omp_memory/cli.py` is the single entry point. `bin/*.sh` are shims.
- Do not add a markdown file to explain something the README already covers.
