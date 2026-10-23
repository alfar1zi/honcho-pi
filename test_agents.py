#!/usr/bin/env python3
"""
test_agents.py - tests for the multi-agent setup layer. No pytest, no network,
no third-party packages: every parser here is honcho's own.

    python3 test_agents.py            # run everything
    python3 test_agents.py -v         # verbose per-test output

Each test proves one claim made by agent_registry.py / agent_config.py /
setup.py. Nothing is asserted that is not exercised here.

Every test that touches the filesystem uses a temp dir and a fake HOME, so a
run never reads or writes the real user config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_config as ac  # noqa: E402
import agent_registry as reg  # noqa: E402

RESULTS: list = []


def test(fn):
    def wrapper():
        try:
            fn()
            RESULTS.append((fn.__name__, True, ""))
        except Exception:
            RESULTS.append((fn.__name__, False, traceback.format_exc()))
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------- fixtures

class Tmp:
    """Fresh temp HOME; agent paths resolve inside it unless overridden."""

    def __enter__(self) -> "Tmp":
        self.root = Path(tempfile.mkdtemp(prefix="honcho-agents-"))
        self.env = {"HOME": str(self.root), "PATH": ""}
        self.spec = ac.render_server_spec(command="/usr/bin/python3",
                                          script="/opt/honcho/honcho_mcp.py",
                                          home=str(self.root / "data"))
        return self

    def path(self, *parts: str) -> str:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    def write(self, path: str, text: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        return path

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)


def agent_with_path(agent_id: str, path: str) -> reg.Agent:
    """Copy of a registry agent pointed at an explicit file (no env games)."""
    base = reg.get_agent(agent_id)
    return reg.Agent(**{**base.__dict__, "config_path": path})


# ---------------------------------------------------------------- registry

@test
def test_registry_has_45_agents_and_unique_ids():
    agents = reg.all_agents()
    assert len(agents) == 45, f"expected 45 seeded agents, got {len(agents)}"
    ids = [a.id for a in agents]
    assert len(set(ids)) == len(ids), "duplicate ids"
    assert "orca" in reg.ATTRIBUTION and "MIT" in reg.ATTRIBUTION


@test
def test_resolve_by_id_alias_and_binary():
    assert reg.resolve("claude").id == "claude"
    assert reg.resolve("cbc").id == "codebuddy"          # alias
    assert reg.resolve("kimi-code").id == "kimi"         # alias
    assert reg.resolve("orca").id == "claude-agent-teams"  # binary of another agent
    assert reg.resolve("auggie").id == "aug"             # binary, no alias
    assert reg.resolve("Claude").id == "claude"          # case-insensitive
    assert reg.resolve_or_none("no-such-agent") is None
    try:
        reg.resolve("no-such-agent")
    except KeyError:
        pass
    else:
        raise AssertionError("resolve() must raise for unknown ids")


@test
def test_yaml_goose_is_print_only_but_hermes_is_full():
    """goose's config.yaml carries providers/model; honcho must not rewrite it."""
    assert reg.get_agent("goose").integration_level == "print-only"
    assert reg.get_agent("hermes").integration_level == "full"
    assert reg.get_agent("aider").integration_level == "none"
    assert reg.get_agent("grok").integration_level == "print-only"
    # Only the root key is evidenced for OpenClaw; the layout is inferred.
    assert reg.get_agent("openclaw").integration_level == "print-only"


@test
def test_required_cmds_gate_installation():
    """orca without claude on PATH is not 'installed'; with it, it is."""
    agent = reg.get_agent("claude-agent-teams")
    only_orca = {"orca": "/bin/orca"}
    both = {"orca": "/bin/orca", "claude": "/bin/claude"}
    assert not agent.is_installed(only_orca.get, runtime="linux")
    assert agent.is_installed(both.get, runtime="linux")


@test
def test_alias_detection_and_unsupported_runtimes():
    claude = reg.get_agent("claude")
    assert claude.is_installed({"claude": "/x"}.get, runtime="linux")
    teams = reg.get_agent("claude-agent-teams")
    which = {"orca": "/x", "claude": "/x"}.get
    assert not teams.is_installed(which, runtime="win32")
    assert not teams.is_installed(which, runtime="wsl")
    assert teams.is_installed(which, runtime="linux")

    tmp = tempfile.mkdtemp()
    try:
        for name in ("orca", "claude", "codex"):
            p = os.path.join(tmp, name)
            Path(p).write_text("#!/bin/sh\n")
            os.chmod(p, 0o755)
        found = ac.detect_installed(path=tmp, runtime="linux")
        assert "claude-agent-teams" in found, found
        assert "claude" in found and "codex" in found
        assert "claude-agent-teams" not in ac.detect_installed(
            path=tmp, runtime="wsl")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_printed_only_reason_matches_the_actual_behaviour():
    """A level and its explanation must agree with what install() does.

    Regression: goose reported no reason (and hermes reported goose's reason)
    because the condition was inverted.
    """
    with Tmp() as t:
        for agent in reg.all_agents():
            # Empty seed file in the agent's own format so parsing is never
            # the reason for a result.
            text = {"toml": "", "yaml": "", "jsonc": "{}\n"}.get(
                agent.config_format, "{}\n")
            path = t.path("reason", agent.id, "config")
            t.write(path, text)
            copy = agent_with_path(agent.id, path)
            r = ac.install(copy, environ=t.env, spec=t.spec)
            if copy.printed_only_reason:
                assert r.status == ac.STATUS_SKIPPED, (agent.id, r)
            assert reg.get_agent("goose").printed_only_reason, "goose reason missing"
            assert not reg.get_agent("hermes").printed_only_reason, \
                "hermes is writable, so it needs no printed-only reason"
            assert reg.get_agent("goose").integration_level == "print-only"
            assert reg.get_agent("hermes").integration_level == "full"


@test
def test_integration_levels_are_derived_not_guessed():
    """Every 'full' agent must have a path, a writable style and a shape."""
    for agent in reg.all_agents():
        if agent.integration_level == "full":
            assert agent.config_path not in ("", "unknown"), agent.id
            assert agent.mcp_style in ac.PLANNERS, (agent.id, agent.mcp_style)
            assert agent.server_spec, f"{agent.id} has no server_spec"


# ---------------------------------------------------------------- parsers

@test
def test_jsonc_stripping_respects_strings():
    src = ('{\n  // leading comment\n  "url": "https://example.com/a//b",\n'
           '  "q": "say \\"//no\\"",\n  /* block\n     comment */\n'
           '  "list": [1, 2,],\n}\n')
    data = ac.load_jsonc(src)
    assert data["url"] == "https://example.com/a//b"
    assert data["q"] == 'say "//no"'
    assert data["list"] == [1, 2]
    # Newlines survive, so error line numbers stay meaningful.
    assert ac.strip_jsonc(src).count("\n") == src.count("\n")


@test
def test_toml_parsers_agree_on_codex_shape():
    """Subset parser (3.10 path) must produce what tomllib produces (3.11+)."""
    src = ('model = "gpt-5"  # comment\n\n[mcp_servers.mine]\n'
           'command = "node"\nargs = [\n  "a",\n  "b",\n]\n'
           'enabled = false\n\n[mcp_servers.mine.env]\nTOKEN = "x # not a comment"\n'
           '\n[profiles."weird key"]\nmodel = \'o3\'\n')
    subset = ac.parse_toml_subset(src)
    assert subset["mcp_servers"]["mine"]["args"] == ["a", "b"]
    assert subset["mcp_servers"]["mine"]["env"]["TOKEN"] == "x # not a comment"
    assert subset["mcp_servers"]["mine"]["enabled"] is False
    assert subset["profiles"]["weird key"]["model"] == "o3"
    assert ac.load_toml(src) == subset


@test
def test_toml_subset_rejects_what_it_cannot_represent():
    for bad in ("[mcp_servers.x]\nenv = { A = 1 }\n", "[mcp_servers.x\ncommand=1\n",
                "[mcp_servers.x]\ncommand = \"unterminated\n"):
        try:
            ac.parse_toml_subset(bad)
        except ac.TomlError:
            continue
        raise AssertionError(f"should have refused: {bad!r}")


@test
def test_yaml_subset_round_trips_goose_config():
    src = ("extensions:\n  developer:\n    type: builtin\n    name: developer\n"
           "    envs:\n      A: \"1\"\nGOOSE_PROVIDER: openai\n"
           "GOOSE_MODEL: gpt-5   # trailing comment\n")
    doc = ac.parse_yaml_subset(src)
    assert doc["extensions"]["developer"]["type"] == "builtin"
    assert doc["extensions"]["developer"]["envs"] == {"A": "1"}
    assert doc["GOOSE_MODEL"] == "gpt-5"
    entry = {"type": "stdio", "cmd": "py", "args": ["x"], "envs": {"H": "/d"},
             "enabled": True, "name": "honcho"}
    text = "extensions:\n" + "\n".join(ac.yaml_dump_node({"honcho": entry}, 2)) + "\n"
    assert ac.parse_yaml_subset(text)["extensions"]["honcho"] == entry


@test
def test_yaml_subset_reads_block_sequences():
    """dsh edits a `plugins:` sequence, so sequences are part of the subset."""
    doc = ac.parse_yaml_subset(
        "plugins:\n  - id: a\n    name: x\n    config:\n      k: v\n"
        "  - id: b\nother: 1\n")
    assert doc["plugins"] == [{"id": "a", "name": "x", "config": {"k": "v"}},
                              {"id": "b"}]
    assert doc["other"] == 1, "a key after a sequence must still be read"
    # The dash may be followed by several spaces; the key column is not base+2.
    assert ac.parse_yaml_subset("p:\n  -   id: x\n      name: y\n")["p"] == [
        {"id": "x", "name": "y"}]


@test
def test_yaml_subset_keeps_opaque_values_without_interpreting_them():
    """Real hermes/goose files use block scalars, unindented lists and anchors.

    Refusing the whole file over them made hermes effectively unwritable.
    The bodies are skipped as text, never parsed, so a line inside a block
    scalar that looks like a key cannot corrupt the read.
    """
    doc = ac.parse_yaml_subset(
        "model: claude\n"
        "instructions: |\n"
        "  Be careful.\n"
        "  mcp_servers:\n"        # looks like structure, is text
        "    fake: 1\n"
        "notes: >-\n  folded\n"
        "tools:\n"                # unindented sequence (valid YAML)
        "- read\n"
        "- write\n"
        "base: &b\n  a: 1\n"
        "mcp_servers:\n  other:\n    command: node\n")
    assert isinstance(doc["instructions"], ac._BlockScalar)
    assert doc["tools"] == ["read", "write"]
    assert "fake" not in str(doc), "block-scalar body was parsed as YAML"
    assert doc["mcp_servers"] == {"other": {"command": "node"}}


@test
def test_yaml_subset_refuses_unsupported_constructs():
    # Block scalars and anchors are *representable* (kept as opaque markers);
    # these are genuinely unsupported:
    for bad in ("a: {b: 1}\n", "a:\n\tb: 1\n", "a: 1\na: 2\n",
                "a: !!js process.env.X\n", "a: - b\n"):
        try:
            ac.parse_yaml_subset(bad)
        except ac.YamlError:
            continue
        raise AssertionError(f"should have refused: {bad!r}")


# ---------------------------------------------------------------- JSON writer

@test
def test_json_writer_creates_file_and_is_idempotent():
    with Tmp() as t:
        agent = agent_with_path("claude", t.path(".claude.json"))
        r1 = ac.install(agent, environ=t.env, spec=t.spec)
        assert r1.status == ac.STATUS_OK, r1
        data = json.loads(Path(r1.path).read_text())
        assert data["mcpServers"]["honcho"]["command"] == "/usr/bin/python3"
        assert data["mcpServers"]["honcho"]["args"] == ["/opt/honcho/honcho_mcp.py"]
        before = Path(r1.path).read_bytes()

        r2 = ac.install(agent, environ=t.env, spec=t.spec)
        assert r2.status == ac.STATUS_EXISTS, r2
        assert Path(r1.path).read_bytes() == before, "idempotent run touched the file"
        assert not list(Path(r1.path).parent.glob("*.honcho-backup-*")), \
            "no backup for a no-op"


@test
def test_json_writer_preserves_existing_user_content_and_jsonc_comments():
    with Tmp() as t:
        path = t.path(".config", "kilo", "kilo.jsonc")
        t.write(path, '{\n  // user theme\n  "theme": "dark",\n'
                      '  "mcp": {\n    "mine": {"type": "local", '
                      '"command": ["node", "s.js"]},\n  },\n}\n')
        agent = agent_with_path("kilo", path)
        r = ac.install(agent, environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        data = ac.load_jsonc(Path(path).read_text())
        assert data["theme"] == "dark", "user key lost"
        assert data["mcp"]["mine"]["command"] == ["node", "s.js"], "user server lost"
        honcho = data["mcp"]["honcho"]
        assert honcho["type"] == "local"
        assert honcho["command"] == ["/usr/bin/python3", "/opt/honcho/honcho_mcp.py"]
        assert "args" not in honcho, "opencode/kilo fold args into command"
        assert honcho["environment"]["HONCHO_HOME"] == str(t.root / "data")
        assert honcho["enabled"] is True


@test
def test_json_variants_use_their_documented_root_keys_and_switches():
    cases = {
        "amp": (["amp.mcpServers", "honcho"], {"command": "/usr/bin/python3"}),
        "muse": (["mcp_servers", "honcho"], {"transport": "stdio", "enabled": True}),
        "zcode": (["mcp", "servers", "honcho"], {"enable": True}),
        "antigravity": (["mcpServers", "honcho"], {"command": "/usr/bin/python3"}),
        "cline": (["mcpServers", "honcho"], {"disabled": False, "autoApprove": []}),
        "claude": (["mcpServers", "honcho"], {"command": "/usr/bin/python3"}),
    }
    with Tmp() as t:
        for aid, (keys, expected) in cases.items():
            base = reg.get_agent(aid)
            path = t.path("case", aid + ".json")
            agent = agent_with_path(aid, path)
            r = ac.install(agent, environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_OK, (aid, r)
            data = ac.load_jsonc(Path(path).read_text())
            node: Any = data
            for key in keys:
                node = node[key]
            for key, value in expected.items():
                assert node.get(key) == value, (aid, key, node.get(key))
            if aid == "muse":
                assert data["schema_version"] == 1
            if aid == "amp":
                assert "amp" not in data, "amp root key is a literal dotted string"


@test
def test_zcode_legacy_flat_entries_are_lifted_not_lost():
    with Tmp() as t:
        path = t.path("z.json")
        t.write(path, '{"mcp": {"old": {"command": "foo", "args": ["a"]},'
                      ' "enabled": true}}\n')
        r = ac.install(agent_with_path("zcode", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        data = json.loads(Path(path).read_text())
        assert data["mcp"]["servers"]["old"] == {"command": "foo", "args": ["a"]}
        assert data["mcp"]["enabled"] is True, "non-server mcp key must stay put"
        assert "honcho" in data["mcp"]["servers"]


@test
def test_conflicting_entry_is_refused_not_overwritten():
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, json.dumps({"mcpServers": {"honcho": {"command": "other"}}}))
        before = Path(path).read_bytes()
        r = ac.install(agent_with_path("claude", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_CONFLICT, r
        assert Path(path).read_bytes() == before


# ---------------------------------------------------------------- TOML writer

@test
def test_toml_writer_merges_keeps_tables_and_is_idempotent():
    with Tmp() as t:
        path = t.path("codex", "config.toml")
        t.write(path, 'model = "gpt-5"\napproval_policy = "on-request"\n\n'
                      '[mcp_servers.mine]\ncommand = "node"\nargs = ["a"]\n\n'
                      '[profiles.fast]\nmodel = "o3"\n')
        agent = agent_with_path("codex", path)
        r = ac.install(agent, environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        data = ac.load_toml(Path(path).read_text())
        assert data["model"] == "gpt-5"
        assert data["profiles"]["fast"]["model"] == "o3"
        assert data["mcp_servers"]["mine"]["command"] == "node"
        honcho = data["mcp_servers"]["honcho"]
        assert honcho["command"] == "/usr/bin/python3"
        assert honcho["enabled"] is True
        assert honcho["env"]["HONCHO_HOME"] == str(t.root / "data")

        text = Path(path).read_text()
        assert "[mcp_servers.honcho.env]" in text, "codex wants a child table for env"

        before = Path(path).read_bytes()
        r2 = ac.install(agent, environ=t.env, spec=t.spec)
        assert r2.status == ac.STATUS_EXISTS, r2
        assert Path(path).read_bytes() == before


@test
def test_toml_writer_replaces_its_own_stale_block_without_touching_neighbours():
    with Tmp() as t:
        path = t.path("config.toml")
        t.write(path, '[mcp_servers.zed]\ncommand = "z"\n\n'
                      '[mcp_servers.honcho]\ncommand = "/old/python"\n\n'
                      '[mcp_servers.alpha]\ncommand = "a"\n')
        r = ac.install(agent_with_path("codex", path), environ=t.env, spec=t.spec)
        # A different entry under our name is a conflict, not a silent replace.
        assert r.status == ac.STATUS_CONFLICT, r
        data = ac.load_toml(Path(path).read_text())
        assert data["mcp_servers"]["honcho"]["command"] == "/old/python"
        assert set(data["mcp_servers"]) == {"zed", "honcho", "alpha"}


# ---------------------------------------------------------------- YAML writer

@test
def test_yaml_writer_inserts_into_hermes_and_keeps_everything_else():
    with Tmp() as t:
        path = t.path(".hermes", "config.yaml")
        original = ('# hermes config\nmodel: claude\nmcp_servers:\n'
                    '  other:\n    command: node\n    args: ["o.js"]\n'
                    'logging:\n  level: info   # keep me\n')
        t.write(path, original)
        agent = agent_with_path("hermes", path)
        r = ac.install(agent, environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        text = Path(path).read_text()
        assert "# hermes config" in text and "# keep me" in text, "comments lost"
        doc = ac.parse_yaml_subset(text)
        assert doc["mcp_servers"]["other"]["command"] == "node"
        assert doc["mcp_servers"]["honcho"]["command"] == "/usr/bin/python3"
        assert doc["logging"]["level"] == "info"
        before = Path(path).read_bytes()
        assert ac.install(agent, environ=t.env, spec=t.spec).status == ac.STATUS_EXISTS
        assert Path(path).read_bytes() == before


@test
def test_goose_is_never_written_and_yields_a_snippet_instead():
    with Tmp() as t:
        path = t.path(".config", "goose", "config.yaml")
        t.write(path, "extensions: {}\n")
        before = Path(path).read_bytes()
        r = ac.install(agent_with_path("goose", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_SKIPPED, r
        assert Path(path).read_bytes() == before
        assert "cmd:" in r.snippet and "envs:" in r.snippet and "extensions:" in r.snippet


# ---------------------------------------------------------------- safety

@test
def test_real_user_config_shapes_survive_a_full_install_round_trip():
    """The destructive-safe test: a busy user file, install, everything stays."""
    with Tmp() as t:
        claude = t.path(".claude.json")
        user_doc = {
            "numStartups": 108,
            "projects": {"/work": {"allowedTools": ["Bash(ls)"]}},
            "mcpServers": {
                "aurora": {"command": "node", "args": ["aurora.js"],
                           "env": {"TOKEN": "abc"}},
                "files": {"command": "uvx", "args": ["server-filesystem"]},
            },
        }
        t.write(claude, json.dumps(user_doc, indent=2))
        r = ac.install(agent_with_path("claude", claude), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        after = json.loads(Path(claude).read_text())
        assert after["numStartups"] == 108
        assert after["projects"] == user_doc["projects"]
        assert after["mcpServers"]["aurora"] == user_doc["mcpServers"]["aurora"]
        assert after["mcpServers"]["files"] == user_doc["mcpServers"]["files"]
        assert "honcho" in after["mcpServers"]
        assert r.backup and Path(r.backup).exists(), "backup missing"
        assert json.loads(Path(r.backup).read_text()) == user_doc, \
            "backup must be the original bytes"


@test
def test_unparsable_file_is_refused_and_left_byte_identical():
    bad = {
        "claude": ("{.claude.json", '{"mcpServers": {"a": }}\n'),
        "codex": ("config.toml", "[mcp_servers.x\ncommand = 1\n"),
        "hermes": ("config.yaml", "mcp_servers:\n\tbad: tab\n"),
    }
    with Tmp() as t:
        for aid, (name, text) in bad.items():
            path = t.path("bad", aid, name)
            t.write(path, text)
            r = ac.install(agent_with_path(aid, path), environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_ERROR, (aid, r)
            assert "untouched" in r.message, (aid, r.message)
            assert Path(path).read_text() == text, f"{aid}: file was modified"
            assert not list(Path(path).parent.glob("*.honcho-backup-*"))


@test
def test_symlinked_config_is_followed_not_replaced():
    """Dotfile managers (stow/chezmoi) symlink configs into a repo.

    os.replace() on the link path would swap the symlink for a regular file
    and detach the dotfile. The write must go through to the target.
    """
    with Tmp() as t:
        repo = Path(t.root) / "dotfiles" / ".claude.json"
        repo.parent.mkdir(parents=True)
        repo.write_text('{"mcpServers": {"keep": {"command": "k"}}}\n')
        link = Path(t.root) / ".claude.json"
        link.symlink_to(repo)
        r = ac.install(agent_with_path("claude", str(link)), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        assert link.is_symlink(), "the symlink was replaced by a regular file"
        assert os.path.realpath(link) == os.path.realpath(repo)
        data = json.loads(repo.read_text())
        assert data["mcpServers"]["keep"] == {"command": "k"}
        assert "honcho" in data["mcpServers"], "target file was not updated"
        # A dangling link is refused: creating its target would be a guess.
        dangling = Path(t.root) / "dangling.json"
        dangling.symlink_to(Path(t.root) / "missing" / "x.json")
        r2 = ac.install(agent_with_path("claude", str(dangling)), environ=t.env,
                        spec=t.spec)
        assert r2.status == ac.STATUS_ERROR, r2
        assert dangling.is_symlink() and not (Path(t.root) / "missing").exists()


@test
def test_jsonc_comments_survive_the_write():
    """A commented file must keep its comments; reserialising would drop them."""
    with Tmp() as t:
        path = t.path(".config", "kilo", "kilo.jsonc")
        t.write(path,
                '{\n'
                '  // why this project uses a local proxy\n'
                '  "theme": "dark",\n'
                '  /* mcp servers, reviewed 2026-10 */\n'
                '  "mcp": {\n'
                "    // the user's own server\n"
                '    "serverA": { "type": "local", "command": ["node", "a.js"] }\n'
                '  }\n'
                '}\n')
        before = Path(path).read_text()
        r = ac.install(agent_with_path("kilo", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        after = Path(path).read_text()
        for comment in ("// why this project uses a local proxy",
                        "/* mcp servers, reviewed 2026-10 */",
                        "// the user's own server"):
            assert comment in after, f"comment lost: {comment}"
        assert after.startswith('{\n  // why'), "pre-existing formatting shifted"
        data = ac.load_jsonc(after)
        assert data["theme"] == "dark"
        assert data["mcp"]["serverA"]["command"] == ["node", "a.js"]
        assert data["mcp"]["honcho"]["type"] == "local"
        assert data["mcp"]["honcho"]["command"][0] == "/usr/bin/python3"
        assert len(after) > len(before)
        # Idempotent on the commented path too: second run changes nothing.
        second = ac.install(agent_with_path("kilo", path), environ=t.env, spec=t.spec)
        assert second.status == ac.STATUS_EXISTS, second
        assert Path(path).read_text() == after


@test
def test_existing_but_unparseable_files_are_refused_never_overwritten():
    """Whitespace-only counts as existing content honcho will not discard."""
    cases = {
        "claude": ("config.json", " \n\n  \n"),
        "codex": ("config.toml", " \n\n  \n"),
        "hermes": ("config.yaml", "\n  \n"),
        "kilo": ("kilo.jsonc", "\n\n"),
    }
    with Tmp() as t:
        for aid, (name, payload) in cases.items():
            path = t.path("blank", aid, name)
            Path(path).write_text(payload)
            r = ac.install(agent_with_path(aid, path), environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_ERROR, (aid, r)
            assert Path(path).read_text() == payload, f"{aid}: blank file overwritten"
            assert "empty" in r.message, (aid, r.message)
        # A file that does not exist at all is still created.
        fresh = t.path("fresh", "config.json")
        r = ac.install(agent_with_path("claude", fresh), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK and Path(fresh).exists()


@test
def test_dry_run_reports_the_change_without_writing():
    with Tmp() as t:
        path = t.path(".cursor", "mcp.json")
        t.write(path, '{"mcpServers": {"mine": {"command": "x"}}}\n')
        before = Path(path).read_bytes()
        r = ac.install(agent_with_path("cursor", path), dry_run=True,
                       environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_DRY_RUN, r
        assert Path(path).read_bytes() == before
        assert '"honcho"' in r.diff and '"mine"' in r.diff
        # Dry run on a file that does not exist yet must not create it.
        missing = str(Path(t.root) / "new" / "mcp.json")
        r2 = ac.install(agent_with_path("cursor", missing), dry_run=True,
                        environ=t.env, spec=t.spec)
        assert r2.status == ac.STATUS_DRY_RUN
        assert not Path(missing).exists()


@test
def test_atomic_write_preserves_mode_and_leaves_no_temp_files():
    with Tmp() as t:
        path = t.path("perm", "config.json")
        t.write(path, '{"a": 1}\n')
        os.chmod(path, 0o640)
        r = ac.install(agent_with_path("claude", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o640)
        assert oct(os.stat(r.backup).st_mode & 0o777) == oct(0o600)
        leftovers = [p for p in Path(path).parent.iterdir() if p.name.startswith(".honcho-")]
        assert not leftovers, leftovers


@test
def test_env_overrides_are_honoured():
    with Tmp() as t:
        custom = str(Path(t.root) / "elsewhere" / ".claude.json")
        env = dict(t.env, CLAUDE_CONFIG_DIR=str(Path(t.root) / "elsewhere"))
        r = ac.install(reg.get_agent("claude"), environ=env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        assert r.path == custom, (r.path, custom)
        assert "honcho" in json.loads(Path(custom).read_text())["mcpServers"]

        codex_home = str(Path(t.root) / "cx")
        r2 = ac.install(reg.get_agent("codex"), environ=dict(t.env, CODEX_HOME=codex_home),
                        spec=t.spec)
        assert r2.path == os.path.join(codex_home, "config.toml"), r2.path

        cline_file = t.path("custom", "cline.json")
        r3 = ac.install(reg.get_agent("cline"),
                        environ=dict(t.env, CLINE_MCP_SETTINGS_PATH=cline_file),
                        spec=t.spec)
        assert r3.path == cline_file, r3.path

        dsh = ac.install(reg.get_agent("dsh"), environ=t.env, spec=t.spec)
        assert dsh.status == ac.STATUS_SKIPPED, "DSH_HOME unset -> cannot resolve"


@test
def test_unknown_and_cli_add_agents_never_write():
    with Tmp() as t:
        for aid in ("grok", "pi", "crush", "aider", "openclaw"):
            agent = reg.get_agent(aid)
            r = ac.install(agent, environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_SKIPPED, (aid, r)
            assert r.snippet, f"{aid}: skipped without a snippet"
        assert "mcp add" in reg.get_agent("crush").cli_add
        assert "mcp add" in ac.install(reg.get_agent("crush"), environ=t.env,
                                       spec=t.spec).command


# ---------------------------------------------------------------- review findings

@test
def test_dsh_writer_works_or_refuses_and_never_crashes():
    """dsh was 'full' with a crashing writer (str + list) and zero tests."""
    with Tmp() as t:
        cases = {
            "fresh file": None,
            "header only": "plugins:\n",
            "existing plugin": "plugins:\n  - id: other\n    name: other\n",
            "nested item": "# keep\nmodel: x\nplugins:\n  - id: p\n"
                           "    name: n\n    config:\n      a: 1\n",
            "mixed list": "plugins:\n  - just-a-string\n",
        }
        for label, text in cases.items():
            path = t.path("dsh", label.replace(" ", "_") + ".yml")
            if text is not None:
                Path(path).write_text(text)
            agent = agent_with_path("dsh", path)
            r = ac.install(agent, environ=t.env, spec=t.spec)
            assert r.status in (ac.STATUS_OK, ac.STATUS_ERROR), (label, r)
            if label == "mixed list":
                assert r.status == ac.STATUS_ERROR, r
                assert Path(path).read_text() == text, "mixed list was modified"
                continue
            assert r.status == ac.STATUS_OK, (label, r)
            doc = ac.parse_yaml_subset(Path(path).read_text())
            ids = [item["id"] for item in doc["plugins"]]
            assert "mcp-honcho" in ids, (label, doc)
            if text and "id: other" in text:
                assert "other" in ids, "existing plugin entry lost"
            second = ac.install(agent, environ=t.env, spec=t.spec)
            assert second.status == ac.STATUS_EXISTS, (label, second)


@test
def test_name_option_registers_and_checks_that_name():
    """A --name that writers ignore makes 'already configured' a lie."""
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, '{"mcpServers": {}}\n')
        agent = agent_with_path("claude", path)
        spec = ac.render_server_spec(name="honcho-dev", command="/usr/bin/python3",
                                     script="/s.py", home="/d")
        r = ac.install(agent, environ=t.env, spec=spec)
        assert r.status == ac.STATUS_OK, r
        data = json.loads(Path(path).read_text())
        assert "honcho-dev" in data["mcpServers"], data
        assert "honcho" not in data["mcpServers"], "constant name written"
        assert ac.is_configured(agent, t.env, name="honcho-dev") is True
        assert ac.is_configured(agent, t.env, name="honcho") is False, \
            "the file is readable; that name simply is not in it"
        # The default name may still be added beside it.
        r2 = ac.install(agent, environ=t.env, spec=t.spec)
        assert r2.status == ac.STATUS_OK, r2
        assert set(json.loads(Path(path).read_text())["mcpServers"]) == {
            "honcho-dev", "honcho"}
        # TOML and YAML honour it too.
        codex = agent_with_path("codex", t.path("codex", "config.toml"))
        assert ac.install(codex, environ=t.env, spec=spec).status == ac.STATUS_OK
        assert "honcho-dev" in ac.load_toml(Path(codex.config_path).read_text())[
            "mcp_servers"]
        hermes = agent_with_path("hermes", t.path("hermes", "config.yaml"))
        assert ac.install(hermes, environ=t.env, spec=spec).status == ac.STATUS_OK
        assert "honcho-dev" in ac.parse_yaml_subset(
            Path(hermes.config_path).read_text())["mcp_servers"]


@test
def test_verify_is_load_bearing():
    """_verify re-parses the merged text; without it a planner bug ships."""
    with Tmp() as t:
        agent = agent_with_path("claude", t.path(".claude.json"))
        entry = ac.render_json_entry(t.spec, agent.server_spec)
        broken = '{"mcpServers": {}}'   # entry is not in here
        try:
            ac._verify(agent, broken, entry)
        except ac.Refusal:
            pass
        else:
            raise AssertionError("_verify accepted text without the entry")
        # And a planner that drops the entry must be reported, not written.
        original = ac.PLANNERS["json-mcpServers"]
        ac.PLANNERS["json-mcpServers"] = lambda a, p, e, o, n=ac.SERVER_NAME: '{"x": 1}\n'
        try:
            r = ac.install(agent, environ=t.env, spec=t.spec)
        finally:
            ac.PLANNERS["json-mcpServers"] = original
        assert r.status == ac.STATUS_ERROR, r
        assert not Path(agent.config_path).exists(), "unverified text was written"


@test
def test_is_configured_distinguishes_no_from_unreadable():
    with Tmp() as t:
        agent = agent_with_path("claude", t.path(".claude.json"))
        assert ac.is_configured(agent, t.env) is False, \
            "no file is readable absence, not an unknown"
        unresolved = reg.Agent(**{**reg.get_agent("dsh").__dict__,
                                  "config_path": "$NOPE_DIR/x.yml"})
        assert ac.is_configured(unresolved, t.env) is None, \
            "an unresolvable location is unknown, shown as '?'"
        ac.install(agent, environ=t.env, spec=t.spec)
        assert ac.is_configured(agent, t.env) is True
        other = agent_with_path("cursor", t.path("cursor.json"))
        Path(other.config_path).write_text('{"mcpServers": {}}\n')
        assert ac.is_configured(other, t.env) is False, "clean file, not configured"
        broken = agent_with_path("kimi", t.path("broken.json"))
        Path(broken.config_path).write_text('{"mcpServers": ')
        assert ac.is_configured(broken, t.env) is None, \
            "an unreadable file is not 'not configured'"


@test
def test_concurrent_writers_do_not_lose_updates():
    """Two processes editing one file: the lock must serialise them."""
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, '{"mcpServers": {}}\n')
        script = (
            "import sys, time; sys.path.insert(0, %r)\n"
            "import agent_config as ac, agent_registry as reg\n"
            "a = reg.Agent(**{**reg.get_agent('claude').__dict__, "
            "'config_path': %r})\n"
            "spec = ac.render_server_spec(name=sys.argv[1], command='/usr/bin/python3', "
            "script='/s.py', home='/d')\n"
            "print(ac.install(a, spec=spec, environ={'HOME': %r, 'PATH': ''}).status)\n"
        ) % (str(HERE), path, str(t.root))
        procs = [subprocess.Popen([sys.executable, "-c", script, f"srv{i}"],
                                  stdout=subprocess.PIPE, text=True)
                 for i in range(4)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert outs == ["ok"] * 4, outs
        servers = json.loads(Path(path).read_text())["mcpServers"]
        assert set(servers) == {f"srv{i}" for i in range(4)}, servers
        # Nothing is left behind: the lock directory is removed on release.
        assert not os.path.exists(path + ".lock"), \
            "lock directory was left behind"
        leftovers = [n for n in os.listdir(Path(path).parent)
                     if not n.startswith(Path(path).name)]
        assert leftovers == [], f"unexpected files next to the config: {leftovers}"


@test
def test_no_lock_left_behind_after_success_failure_or_dry_run():
    """A lock that persists is litter in the user's config directory."""
    with Tmp() as t:
        path = t.path(".claude.json")
        agent = agent_with_path("claude", path)
        r = ac.install(agent, environ=t.env, spec=t.spec)          # create
        assert r.status == ac.STATUS_OK, r
        assert not os.path.exists(path + ".lock")
        ac.install(agent, environ=t.env, spec=t.spec)              # exists
        assert not os.path.exists(path + ".lock")
        Path(path).write_text('{"mcpServers": ')                   # parse error
        assert ac.install(agent, environ=t.env, spec=t.spec).status == ac.STATUS_ERROR
        assert not os.path.exists(path + ".lock")
        ac.install(agent, environ=t.env, spec=t.spec, dry_run=True)
        assert not os.path.exists(path + ".lock")
        assert not [n for n in os.listdir(Path(path).parent) if n.endswith(".lock")]


@test
def test_backups_are_capped_per_config():
    """Unbounded backups flood the config directory (64 writes -> 64 files)."""
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, '{"mcpServers": {}}\n')
        agent = agent_with_path("claude", path)
        for i in range(15):
            spec = ac.render_server_spec(name=f"s{i}", command="/usr/bin/python3",
                                         script="/s.py", home="/d")
            assert ac.install(agent, environ=t.env, spec=spec).status == ac.STATUS_OK
        backups = [n for n in os.listdir(Path(path).parent) if ".honcho-backup-" in n]
        assert len(backups) <= ac.MAX_BACKUPS, len(backups)
        assert len(backups) == ac.MAX_BACKUPS, len(backups)
        # Pruning keeps the newest by name (timestamps sort chronologically)
        # and removes the oldest: the oldest surviving backup must be a late
        # one, not the very first file state.
        kept = sorted(backups)
        assert len(kept) == ac.MAX_BACKUPS
        oldest_doc = json.loads((Path(path).parent / kept[0]).read_text())
        # By run 5 the backups already contain more than just the seed entry.
        assert len(oldest_doc["mcpServers"]) >= ac.MAX_BACKUPS - len(kept) + 4, oldest_doc


@test
def test_read_only_config_is_not_overwritten():
    if os.name == "nt" or os.geteuid() == 0:
        return
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, '{"mcpServers": {}}\n')
        os.chmod(path, 0o400)
        try:
            r = ac.install(agent_with_path("claude", path), environ=t.env,
                           spec=t.spec)
            assert r.status == ac.STATUS_SKIPPED, r
            assert "not writable" in r.message
            assert Path(path).read_text() == '{"mcpServers": {}}\n'
        finally:
            os.chmod(path, 0o600)


@test
def test_detection_requires_an_executable_file():
    with Tmp() as t:
        bindir = Path(t.root) / "bin"
        bindir.mkdir()
        (bindir / "claude").mkdir()                       # directory
        (bindir / "codex").write_text("#!/bin/sh\n")      # not executable
        real = bindir / "gemini"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        upper = bindir / "COPILOT"
        upper.write_text("#!/bin/sh\n")
        upper.chmod(0o755)
        found = ac.detect_installed(path=str(bindir), runtime="linux",
                                    environ={"PATH": str(bindir)})
        assert found == ["gemini"], found


@test
def test_utf8_bom_config_is_understood_and_preserved():
    with Tmp() as t:
        path = t.path(".claude.json")
        Path(path).write_bytes(b'\xef\xbb\xbf{"mcpServers": {}}\n')
        r = ac.install(agent_with_path("claude", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        raw = Path(path).read_bytes()
        assert raw.startswith(b'\xef\xbb\xbf'), "BOM was dropped"
        assert "honcho" in json.loads(raw.decode("utf-8-sig"))["mcpServers"]


@test
def test_unexpected_exception_is_reported_per_agent():
    """One broken agent must not abort the others in --all."""
    with Tmp() as t:
        original = ac.PLANNERS["toml-table"]

        def boom(*a, **k):
            raise RuntimeError("simulated parser bug")

        ac.PLANNERS["toml-table"] = boom
        try:
            bad = ac.install(agent_with_path("codex", t.path("codex", "config.toml")),
                             environ=t.env, spec=t.spec)
            good = ac.install(agent_with_path("claude", t.path(".claude.json")),
                              environ=t.env, spec=t.spec)
        finally:
            ac.PLANNERS["toml-table"] = original
        assert bad.status == ac.STATUS_ERROR, bad
        assert "simulated parser bug" in bad.message, bad.message
        assert good.status == ac.STATUS_OK, good


@test
def test_hermes_handles_common_real_world_yaml():
    """Block scalars, unindented lists and next-line `[]` must not block hermes."""
    with Tmp() as t:
        path = t.path(".hermes", "config.yaml")
        t.write(path,
                "plugins:\n  enabled:\n    []\n"
                "instructions: |\n  Be careful.\n  mcp_servers:\n    fake: 1\n"
                "tools:\n- read\n- write\n"
                "mcp_servers:\n  other:\n    command: node\n")
        r = ac.install(agent_with_path("hermes", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        text = Path(path).read_text()
        assert text.startswith("plugins:\n  enabled:\n    []"), "header rewritten"
        assert "  fake: 1\n" in text, "block scalar body altered"
        doc = ac.parse_yaml_subset(text)
        assert doc["mcp_servers"]["honcho"]["command"] == "/usr/bin/python3"
        assert doc["mcp_servers"]["other"] == {"command": "node"}


@test
def test_jsonc_splice_keeps_layout_valid_and_creates_missing_container():
    with Tmp() as t:
        cases = {
            "trailing comma + comment": '{\n  // c\n  "mcp": {\n'
                                        '    "mine": {"t": 1},\n  },\n}\n',
            "no container": '{\n  // top\n  "theme": "dark"\n}\n',
            "one-line container": '{\n  "mcp": {"a": {"t": 1}}  // x\n}\n',
        }
        for label, text in cases.items():
            path = t.path("kilo", label.replace(" ", "_") + ".jsonc")
            Path(path).write_text(text)
            r = ac.install(agent_with_path("kilo", path), environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_OK, (label, r)
            after = Path(path).read_text()
            for comment in ("// c", "// top", "// x"):
                if comment in text:
                    assert comment in after, (label, "comment lost")
            data = ac.load_jsonc(after)          # must still be valid
            assert "honcho" in data["mcp"], (label, data)
            if "mine" in text:
                assert data["mcp"]["mine"] == {"t": 1}
                assert '    "honcho": {' in after, (label, after)
            second = ac.install(agent_with_path("kilo", path), environ=t.env,
                                spec=t.spec)
            assert second.status == ac.STATUS_EXISTS, (label, second)


@test
def test_backups_are_unique_and_never_shared():
    with Tmp() as t:
        path = t.path(".claude.json")
        t.write(path, '{"mcpServers": {}}\n')
        agent = agent_with_path("claude", path)
        spec_a = ac.render_server_spec(name="a", command="/usr/bin/python3",
                                       script="/s.py", home="/d")
        spec_b = ac.render_server_spec(name="b", command="/usr/bin/python3",
                                       script="/s.py", home="/d")
        r1 = ac.install(agent, environ=t.env, spec=spec_a)
        r2 = ac.install(agent, environ=t.env, spec=spec_b)
        assert r1.status == r2.status == ac.STATUS_OK
        assert r1.backup != r2.backup, "two changes must not share one backup name"
        backups = list(Path(path).parent.glob("*.honcho-backup-*"))
        assert len(backups) == 2, backups


@test
def test_yaml_values_containing_colon_space_are_not_truncated():
    with Tmp() as t:
        path = t.path(".hermes", "config.yaml")
        r = ac.install(agent_with_path("hermes", path), environ=t.env, spec=t.spec)
        assert r.status == ac.STATUS_OK, r
        doc = ac.parse_yaml_subset(Path(path).read_text())
        args = doc["mcp_servers"]["honcho"]["args"]
        assert args == ["/opt/honcho/honcho_mcp.py"], args


@test
def test_yaml_writer_quotes_values_that_need_it():
    """A value with ':' or '#' unquoted would parse back as something else."""
    with Tmp() as t:
        spec = ac.render_server_spec(command="/usr/bin/python3",
                                      script="/s.py", home="/d")
        spec["env"]["NOTE"] = "a: b"
        spec["env"]["URL"] = "http://x/#frag"
        path = t.path(".hermes", "config.yaml")
        r = ac.install(agent_with_path("hermes", path), environ=t.env, spec=spec)
        assert r.status == ac.STATUS_OK, r
        text = Path(path).read_text()
        doc = ac.parse_yaml_subset(text)
        env = doc["mcp_servers"]["honcho"]["env"]
        assert env["NOTE"] == "a: b", (env, text)
        assert env["URL"] == "http://x/#frag", env
        assert ac.is_configured(agent_with_path("hermes", path), t.env) is True


@test
def test_only_a_clean_unconfigured_file_reports_false():
    """is_configured must never claim 'not configured' for a damaged file."""
    with Tmp() as t:
        damaged = {
            "claude": ("c.json", '{"mcpServers": '),
            "codex": ("c.toml", "[broken\n"),
            "hermes": ("c.yaml", "a: 1\na: 2\n"),
        }
        for aid, (name, text) in damaged.items():
            path = t.path("dmg", aid, name)
            Path(path).write_text(text)
            agent = agent_with_path(aid, path)
            assert ac.is_configured(agent, t.env) is None, aid
            r = ac.install(agent, environ=t.env, spec=t.spec)
            assert r.status == ac.STATUS_ERROR, (aid, r)
            assert r.status not in (ac.STATUS_OK, ac.STATUS_EXISTS), aid


@test
def test_failures_are_never_reported_as_success():
    with Tmp() as t:
        path = t.path(".claude.json")
        Path(path).write_text('{"mcpServers": ')
        agent = agent_with_path("claude", path)
        results = [ac.install(agent, environ=t.env, spec=t.spec) for _ in range(3)]
        for r in results:
            assert not r.ok, r
            assert r.status == ac.STATUS_ERROR, r
        assert Path(path).read_text() == '{"mcpServers": '
        assert not list(Path(path).parent.glob("*.honcho-backup-*"))


@test
def test_detection_case_rules_follow_the_filesystem():
    """Uppercase must match only where the filesystem is case-insensitive.

    os.name and the lookup are patched together: the point is that the
    fallback the Windows branch uses is actually wired in.
    """
    with Tmp() as t:
        bindir = Path(t.root) / "bin"
        bindir.mkdir()
        exe = bindir / "CLAUDE"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        path = str(bindir)
        assert ac.detect_installed(path=path, runtime="linux",
                                   environ={"PATH": path}) == []
        real_name = ac.os.name
        try:
            ac.os.name = "nt"
            assert "claude" in ac.detect_installed(path=path, runtime="win32",
                                                   environ={"PATH": path})
        finally:
            ac.os.name = real_name


@test
def test_parser_does_not_depend_on_input_between_calls():
    """Two parses of the same document must agree (no hidden state)."""
    doc = "a: 1\nb:\n  c: 2\nlist:\n  - x\n"
    first = ac.parse_yaml_subset(doc)
    second = ac.parse_yaml_subset(doc)
    assert first == second == {"a": 1, "b": {"c": 2}, "list": ["x"]}
    toml = '[a]\nb = 1\n'
    assert ac.load_toml(toml) == ac.load_toml(toml)


# ---------------------------------------------------------------- setup.py CLI

def _run_cli(args, env, cwd=HERE):
    proc = subprocess.run([sys.executable, "setup.py", *args], cwd=str(cwd),
                          capture_output=True, text=True, env=env)
    return proc


@test
def test_cli_detect_status_and_print_are_meaningful():
    with Tmp() as t:
        bindir = Path(t.root) / "bin"
        bindir.mkdir()
        for name in ("claude", "codex"):
            exe = bindir / name
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        env = {"HOME": str(t.root), "PATH": str(bindir),
               "PYTHONPATH": str(HERE)}
        detect = _run_cli(["detect"], env)
        assert detect.returncode == 0, detect.stderr
        assert "claude" in detect.stdout and "codex" in detect.stdout
        assert "2 installed agent(s)" in detect.stdout, detect.stdout
        assert "45 agents" in detect.stdout

        status = _run_cli(["status"], env)
        assert status.returncode == 0, status.stderr
        assert "config path" in status.stdout
        assert len([l for l in status.stdout.splitlines()
                    if l.split() and l.split()[0] in {a.id for a in reg.all_agents()}]) == 45

        snippet = _run_cli(["print", "--agent", "claude"], env)
        assert snippet.returncode == 0, snippet.stderr
        assert '"mcpServers"' in snippet.stdout and "honcho_mcp.py" in snippet.stdout

        bad = _run_cli(["print", "--agent", "nope"], env)
        assert bad.returncode == 2, bad.returncode


@test
def test_cli_install_dry_run_and_real_run_against_a_fake_home():
    with Tmp() as t:
        env = {"HOME": str(t.root), "PATH": os.environ.get("PATH", ""),
               "PYTHONPATH": str(HERE)}
        target = Path(t.root) / ".claude.json"
        target.write_text('{"mcpServers": {"keep": {"command": "k"}}}\n')
        before = target.read_bytes()

        dry = _run_cli(["install", "--agent", "claude", "--dry-run"], env)
        assert dry.returncode == 0, dry.stderr
        assert "dry-run" in dry.stdout and "1 would change" in dry.stdout
        assert target.read_bytes() == before, "dry-run modified the file"

        real = _run_cli(["install", "--agent", "claude"], env)
        assert real.returncode == 0, real.stderr
        data = json.loads(target.read_text())
        assert data["mcpServers"]["keep"] == {"command": "k"}
        assert "honcho" in data["mcpServers"]

        again = _run_cli(["install", "--agent", "claude"], env)
        assert again.returncode == 0
        assert "already configured" in again.stdout, again.stdout

        # A failed agent makes the exit code non-zero, but the others still run.
        bad = Path(t.root) / ".codex"
        bad.mkdir()
        (bad / "config.toml").write_text("[broken\n")
        mixed = _run_cli(["install", "--agent", "codex", "--agent", "claude"], env)
        assert mixed.returncode == 1, mixed.stdout
        assert "codex: error" in mixed.stdout, mixed.stdout


# ---------------------------------------------------------------- runner

def collect_tests() -> list:
    """Every @test-decorated function in this module, sorted by name.

    Collected once from the module dict (not re-scanned while importing), so
    the count is stable and calling main() twice does not double-count.
    """
    found = [(name, obj) for name, obj in vars(sys.modules[__name__]).items()
             if name.startswith("test_") and callable(obj)]
    return [obj for _, obj in sorted(found, key=lambda item: item[0])]


def main() -> int:
    verbose = "-v" in sys.argv
    RESULTS.clear()
    for t in collect_tests():
        t()
        if verbose:
            ok, err = RESULTS[-1][1], RESULTS[-1][2]
            print(f"{t.__name__}: {'ok' if ok else 'FAIL'}")
            if not ok:
                print(err)
    failed = [r for r in RESULTS if not r[1]]
    for name, _, err in failed:
        print(f"\nFAIL {name}\n{err}")
    passed = len(RESULTS) - len(failed)
    print(f"\n{passed}/{len(RESULTS)} tests passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
