#!/usr/bin/env python3
"""
agent_registry.py - one data table describing every coding agent honcho knows.

Nothing here decides *how* to write a config; that is agent_config.py's job.
This module answers three questions only:

    which binary do I look for?      binary, aliases, required_cmds,
                                     unsupported_runtimes
    where does its config live?      config_path (+ env overrides, fallbacks)
    what shape does that file take?  config_format, mcp_style, server_spec

The point of a table instead of per-agent code: adding an agent is a one-line
data change, and every consumer (detect, install, print, status) learns about
it at the same time. A branch-per-agent installer is how the five-target
`install.sh` this replaces went stale.

Provenance
----------
ATTRIBUTION: Seeded from stablyai/orca (MIT) for binary/detection fields.
  src/shared/tui-agent-config.ts (TUI_AGENT_CONFIG_SOURCE; commit 5c3938ce)
  src/shared/agent-hook-types.ts  (AGENT_HOOK_TARGETS)
Config fields (config_path, config_format, mcp_style) do NOT come from Orca;
they were researched per agent from official vendor docs and neighbouring
registries. Where sources disagree the entry is deliberately `unknown`:
writing a half-guessed path into a user's config is worse than printing a
snippet they paste themselves.

The `server_spec` of each shape (root key, command layout, env key, disable
polarity, mandatory fields) follows /tmp/mcp-shapes.md, which was verified
against vendor documentation on 2026-11-01. Three traps it documents, all
encoded below:
  (a) `args` is NOT universal - opencode/kilo fold exe+args into a `command`
      ARRAY and rename env to `environment`;
  (b) the disable switch differs per agent - `enabled` vs `disabled` vs
      `enable`, and some agents have none;
  (c) the env key is `env` / `environment` / `envs`.

field semantics
---------------
aliases, required_cmds, unsupported_runtimes
    Empty tuple means "no value", not "unknown". Aliases are *extra*
    executables only; the `binary` itself is always probed.
required_cmds
    Extra binaries that must also be on PATH for the agent to count as
    installed (the orca launcher needs `claude` beside it).
unsupported_runtimes
    Labels matched against current_runtime(): "win32", "wsl", or
    platform.system().lower() such as "linux"/"darwin".
config_path
    "unknown" -> no verified location, so no automatic write. May begin with
    "$VAR/" (expanded from the environment). The table carries the plain
    user-level path; env overrides live in PATH_OVERRIDES and fallbacks in
    PATH_FALLBACKS.
integration_level (derived, never stored)
    "none"        mcp_style == "none": agent has no MCP support.
    "print-only"  style unknown, or style known but location unknown, or the
                  location exists but honcho will not write that format.
    "full"        location and shape both known and writable.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

ATTRIBUTION = (
    "Seeded from stablyai/orca (MIT) for binary/detection fields"
)

MCP_STYLES = (
    "json-mcpServers",  # JSON, root "mcpServers" (amp: literal "amp.mcpServers")
    "json-mcp",         # JSON, root "mcp" / "mcp.servers" / "mcp_servers"
    "json-servers",     # JSON, root "servers" (VS Code workspace shape)
    "toml-table",       # TOML, [mcp_servers.<name>]
    "yaml",             # goose / hermes / dsh
    "cli-add",          # no stable file; a CLI subcommand adds the server
    "none",             # agent does not support MCP
    "unknown",          # not enough evidence, or sources conflict
)

WRITABLE_STYLES = ("json-mcpServers", "json-mcp", "json-servers", "toml-table")


@dataclass(frozen=True)
class Agent:
    id: str
    binary: str
    aliases: Tuple[str, ...] = ()
    required_cmds: Tuple[str, ...] = ()
    unsupported_runtimes: Tuple[str, ...] = ()
    has_hook: bool = False
    config_path: str = "unknown"
    config_format: str = "unknown"
    mcp_style: str = "unknown"
    note: str = ""
    # Shape details the writers need; see shapes.md.
    server_spec: Dict[str, str] = field(default_factory=dict, compare=False)
    # CLI command template for mcp_style == "cli-add", e.g. "crush mcp add {name} {exe}"
    cli_add: str = ""
    # Extra data not part of the public dict form (seed extras, caveats).
    extra: Dict[str, str] = field(default_factory=dict, compare=False)

    # -- derived ----------------------------------------------------------

    @property
    def integration_level(self) -> str:
        if self.mcp_style == "none":
            return "none"
        if self.mcp_style == "unknown":
            return "print-only"
        if self.config_path == "unknown" or self.framework_owned_config:
            return "print-only"
        return "full"

    @property
    def writable(self) -> bool:
        return self.integration_level == "full"

    @property
    def printed_only_reason(self) -> str:
        """Why install will not write a file for this agent ("" if writable)."""
        if self.mcp_style == "none":
            return "agent does not support MCP"
        if self.mcp_style == "unknown":
            return "MCP config location/shape unverified - see note"
        if self.config_path == "unknown":
            return "config location unverified - see note"
        if self.mcp_style == "cli-add":
            return "no stable config file; use the CLI command shown"
        if self.mcp_style == "yaml" and self.framework_owned_config:
            return ("config owned by the agent's own CLI (providers/model share "
                    "the file) - a snippet is printed instead of an edit")
        return ""

    @property
    def framework_owned_config(self) -> bool:
        """True for YAML configs the vendor CLI rewrites wholesale.

        goose's config.yaml carries providers and the active model; an external
        writer losing a comment would look like a config regression, so honcho
        prints for it and writes only for hermes/dsh, where the MCP block can
        be inserted without touching anything else.
        """
        return self.server_spec.get("yaml_kind") == "goose"

    @property
    def detected_names(self) -> Tuple[str, ...]:
        return (self.binary,) + self.aliases

    def expand_config_path(self, environ: Optional[Dict[str, str]] = None) -> Optional[str]:
        """The table's own config_path with $VARS and ~ expanded, or None."""
        env = os.environ if environ is None else environ
        return _expand(self.config_path, env)

    def config_candidates(self, environ: Optional[Dict[str, str]] = None) -> List[str]:
        """Every path honcho will consider: env override, table path, fallback.

        The first existing file wins; when none exists, the first candidate is
        created. An empty list means the location cannot be resolved here.
        """
        env = os.environ if environ is None else environ
        out: List[str] = []
        override = path_override(self.id, env)
        if override:
            out.append(override)
        own = self.expand_config_path(env)
        if own:
            out.append(own)
        for tmpl in PATH_FALLBACKS.get(self.id, ()):
            resolved = _expand(tmpl, env)
            if resolved:
                out.append(resolved)
        return _dedupe(out)

    def unsupported_here(self, runtime: Optional[str] = None) -> Optional[str]:
        rt = (runtime or current_runtime()).lower()
        return rt if rt in self.unsupported_runtimes else None

    def is_installed(self, which=shutil.which, runtime: Optional[str] = None) -> bool:
        if self.unsupported_here(runtime):
            return False
        if not any(which(name) for name in self.detected_names):
            return False
        return all(which(name) for name in self.required_cmds)

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "binary": self.binary,
            "aliases": list(self.aliases),
            "required_cmds": list(self.required_cmds),
            "unsupported_runtimes": list(self.unsupported_runtimes),
            "has_hook": self.has_hook,
            "config_path": self.config_path,
            "config_format": self.config_format,
            "mcp_style": self.mcp_style,
            "integration_level": self.integration_level,
            "note": self.note,
        }


def _expand(path: str, env: Dict[str, str]) -> Optional[str]:
    if not path or path == "unknown":
        return None
    pattern = r"%(\w+)%" if os.name == "nt" else r"\$(\w+)"
    for var in re.findall(pattern, path):
        value = env.get(var)
        if not value:
            return None
        path = path.replace(("%" + var + "%") if os.name == "nt" else ("$" + var), value)
    if path == "~":
        home = env.get("HOME") or env.get("USERPROFILE")
        return home or None
    if path.startswith("~/"):
        home = env.get("HOME") or env.get("USERPROFILE")
        return (home + path[1:]) if home else None
    return path


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        key = os.path.normpath(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _agent(
    id: str,
    binary: str,
    aliases: Iterable[str] = (),
    required_cmds: Iterable[str] = (),
    unsupported_runtimes: Iterable[str] = (),
    has_hook: bool = False,
    config_path: str = "unknown",
    config_format: str = "unknown",
    mcp_style: str = "unknown",
    note: str = "",
    server_spec: Optional[Dict[str, str]] = None,
    cli_add: str = "",
    extra: Optional[Dict[str, str]] = None,
) -> Agent:
    if mcp_style not in MCP_STYLES:
        raise ValueError(f"{id}: unknown mcp_style {mcp_style!r}")
    return Agent(
        id=id,
        binary=binary,
        aliases=tuple(aliases),
        required_cmds=tuple(required_cmds),
        unsupported_runtimes=tuple(unsupported_runtimes),
        has_hook=has_hook,
        config_path=config_path,
        config_format=config_format,
        mcp_style=mcp_style,
        note=note,
        server_spec=dict(server_spec or {}),
        cli_add=cli_add,
        extra=dict(extra or {}),
    )


# --------------------------------------------------------------------------
# Server shapes (verified; see module docstring)
# --------------------------------------------------------------------------

#: Standard "mcpServers" object: command string, args array, env object.
SPEC_BASELINE: Dict[str, str] = {"kind": "flat"}

#: opencode v1 / kilo: `type: local`, command ARRAY (exe first), `environment`.
SPEC_OPENCODE_V1: Dict[str, str] = {
    "kind": "flat", "type": "local", "command_style": "array", "env_key": "environment",
    "disable": "enabled",
}

#: opencode v2: servers under `mcp.servers`, disable via `disabled: true`.
SPEC_OPENCODE_V2: Dict[str, str] = {
    "kind": "flat", "nested": "servers", "type": "local", "command_style": "array",
    "env_key": "environment", "disable": "disabled", "disable_polarity": "false-on",
}

#: muse: `mcp_servers`, requires `schema_version: 1` and per-server `transport`.
SPEC_MUSE: Dict[str, str] = {
    "kind": "flat", "root_override": "mcp_servers", "requires_schema_version": "1",
    "server_defaults": "transport=stdio,mode=required", "disable": "enabled",
}

#: zcode: two-level `mcp.servers`, `enable: false` (not `enabled`).
SPEC_ZCODE: Dict[str, str] = {"kind": "flat", "nested": "servers", "disable": "enable"}

#: VS Code workspace/profile: root `servers`, `type: stdio` mandatory.
SPEC_VSCODE: Dict[str, str] = {
    "kind": "flat", "root_override": "servers", "type": "stdio",
    "type_key": "type", "no_disable": "1",
}

#: codex TOML table + mandatory child table `.env`.
SPEC_TOML: Dict[str, str] = {"kind": "toml", "env_table": "1", "disable": "enabled"}

#: goose YAML: `extensions` map with cmd / envs / type: stdio / name.
SPEC_GOOSE: Dict[str, str] = {
    "kind": "yaml", "yaml_kind": "goose", "root_override": "extensions",
    "command_key": "cmd", "env_key": "envs", "type": "stdio", "disable": "enabled",
}

#: hermes YAML: `mcp_servers` mapping with command / args / env.
SPEC_HERMES: Dict[str, str] = {"kind": "yaml", "yaml_kind": "hermes"}

#: dsh YAML: plugin patch file, id `mcp-<name>`; not an MCP config at all.
#: dsh: a plugin *patch* file. The MCP server is a plugin entry (`id:
#: mcp-<name>`) inside the `plugins:` sequence - not an mcp_servers table.
SPEC_DSH: Dict[str, str] = {"kind": "yaml", "yaml_kind": "dsh",
                            "root_override": "plugins", "container": "sequence",
                            "sequence_item_key": "id"}


def _with(base: Dict[str, str], **over) -> Dict[str, str]:
    out = dict(base)
    out.update(over)
    return out


# --------------------------------------------------------------------------
# Path overrides and fallbacks
# --------------------------------------------------------------------------

#: Env var -> (agent id, join table or literal path, attribute).
#: ("path", "mcp.json") joins the env value; ("file", "notedir/x.json") takes
#: the env value as the FILE; ("str", name) is a subdirectory to append.
PATH_OVERRIDES: Dict[str, Tuple[str, str, str]] = {
    "CLAUDE_CONFIG_DIR": ("claude", ".claude.json", "join"),
    "CODEX_HOME": ("codex", "config.toml", "join"),
    "COPILOT_HOME": ("copilot", "mcp-config.json", "join"),
    "HERMES_HOME": ("hermes", "config.yaml", "join"),
    "DSH_HOME": ("dsh", "cordis.patch.yml", "join"),
    "GEMINI_CLI_HOME": ("gemini", ".gemini/settings.json", "join"),
    "CLINE_MCP_SETTINGS_PATH": ("cline", "", "file"),
    "XDG_CONFIG_HOME": ("opencode", "opencode/opencode.json", "join"),
}

#: Second choice for an agent whose primary location has no file. Also used as
#: read-only fallbacks when looking for an existing registration.
PATH_FALLBACKS: Dict[str, Tuple[str, ...]] = {
    "antigravity": (
        "~/.gemini/antigravity/mcp_config.json",
        "~/.gemini/antigravity-cli/mcp_config.json",
    ),
    "codebuddy": (
        "~/.codebuddy/.mcp.json",
        "~/.codebuddy/mcp.json",
        "~/.codebuddy.json",
    ),
    "opencode": ("~/.config/opencode/opencode.jsonc",),
    "pi": ("~/.config/mcp/mcp.json",),
    "kilo": ("~/.config/kilo/kilo.json",),
}

#: $XDG_CONFIG_HOME also relocates these; only applied when the override is set
#: AND the agent's own base directory lives under ~/.config.
_XDG_AGENTS: Dict[str, str] = {
    "amp": "amp/settings.json",
    "goose": "goose/config.yaml",
    "muse": "muse/settings.json",
    "devin": "devin/mcp_config.json",
    "opencode": "opencode/opencode.json",
    "kilo": "kilo/kilo.jsonc",
}


def path_override(agent_id: str, environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve an environment override for this agent's config location.

    Honoured (per shapes.md): CLAUDE_CONFIG_DIR, CODEX_HOME, COPILOT_HOME,
    HERMES_HOME, DSH_HOME, GEMINI_CLI_HOME, CLINE_MCP_SETTINGS_PATH,
    XDG_CONFIG_HOME.
    """
    env = os.environ if environ is None else environ
    for var, (aid, target, how) in PATH_OVERRIDES.items():
        if aid != agent_id:
            continue
        value = env.get(var)
        if not value:
            continue
        if how == "file":
            return value
        return os.path.join(value, target)
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and agent_id in _XDG_AGENTS and agent_id != "opencode":
        return os.path.join(xdg, _XDG_AGENTS[agent_id])
    return None


def path_overrides_help() -> str:
    return ", ".join(sorted(PATH_OVERRIDES))


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

AGENTS: List[Agent] = [
    # -- JSON with root key "mcpServers" ---------------------------------
    _agent("claude", "claude", has_hook=True,
           config_path="~/.claude.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("codebuddy", "codebuddy", aliases=["cbc"], has_hook=True,
           config_path="unknown", config_format="unknown",
           mcp_style="json-mcpServers",
           note="three candidate files; write the one that already exists",
           server_spec=SPEC_BASELINE, extra={"path_candidates": "3"}),
    _agent("qoder", "qodercli", aliases=["qoder"], has_hook=True,
           config_path="~/.qoder/settings.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("qoder-cn", "qoderclicn", aliases=["qodercn"], has_hook=True),
    _agent("gemini", "gemini", has_hook=True,
           config_path="~/.gemini/settings.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("antigravity", "agy", has_hook=True,
           config_path="~/.gemini/config/mcp_config.json", config_format="json",
           mcp_style="json-mcpServers",
           note="official path, doc-verified only; disabling is `disabled: true`",
           server_spec=_with(SPEC_BASELINE, remote_url_key="serverUrl"),
           extra={"disable": "disabled"}),
    _agent("amp", "amp", has_hook=True,
           config_path="~/.config/amp/settings.json", config_format="json",
           mcp_style="json-mcpServers",
           note="root key is the literal dotted string 'amp.mcpServers'",
           server_spec=_with(SPEC_BASELINE, root_override="amp.mcpServers",
                             no_disable="1")),
    _agent("kiro", "kiro-cli", has_hook=True,
           config_path="~/.kiro/settings/mcp.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("aug", "auggie",
           config_path="~/.augment/settings.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("cline", "cline",
           config_path="~/.cline/data/settings/cline_mcp_settings.json",
           config_format="json", mcp_style="json-mcpServers",
           note="disabling is `disabled: true`; autoApprove: [] is conventional",
           server_spec=_with(SPEC_BASELINE, disable="disabled",
                             disable_polarity="false-on", auto_approve="[]"),
           extra={"honor": "CLINE_MCP_SETTINGS_PATH"}),
    _agent("cursor", "cursor-agent", has_hook=True,
           config_path="~/.cursor/mcp.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("droid", "droid", has_hook=True,
           config_path="~/.factory/mcp.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("kimi", "kimi", aliases=["kimi-code"], has_hook=True,
           config_path="~/.kimi-code/mcp.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE,
           note="mcp.json is the MCP file; config.toml is the hook file"),
    _agent("qwen-code", "qwen", has_hook=True,
           config_path="~/.qwen/settings.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),
    _agent("copilot", "copilot", has_hook=True,
           config_path="~/.copilot/mcp-config.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE,
           note="portable mcpServers shape, not the VS Code `servers` shape"),
    _agent("devin", "devin", has_hook=True,
           config_path="~/.config/devin/mcp_config.json", config_format="json",
           mcp_style="json-mcpServers", server_spec=SPEC_BASELINE),

    # -- TOML ------------------------------------------------------------
    _agent("codex", "codex", has_hook=True,
           config_path="~/.codex/config.toml", config_format="toml",
           mcp_style="toml-table", server_spec=SPEC_TOML,
           note="env must be its own table [mcp_servers.<name>.env]"),

    # -- JSON with a root key other than "mcpServers" --------------------
    _agent("opencode", "opencode",
           config_path="~/.config/opencode/opencode.json",
           config_format="json/jsonc", mcp_style="json-mcp",
           server_spec=SPEC_OPENCODE_V1,
           note="v1 schema only (mcp.<name>); opencode v2 uses mcp.servers"),
    _agent("opencode2", "opencode2", config_format="jsonc", mcp_style="unknown",
           note="regional/dialect variant; assumed opencode v2 (mcp.servers) "
                "but UNVERIFIED, so left unknown"),
    _agent("kilo", "kilo",
           config_path="~/.config/kilo/kilo.jsonc", config_format="jsonc",
           mcp_style="json-mcp", server_spec=SPEC_OPENCODE_V1),
    # Only the root key ("mcp") is evidenced; the server-object layout is
    # inferred from opencode, not quoted from OpenClaw docs. Writing a guessed
    # shape into a real config is worse than printing one, so: print-only.
    _agent("openclaw", "openclaw",
           config_path="~/.openclaw/openclaw.json", config_format="json",
           mcp_style="unknown",
           note="'mcp' root key is evidenced, the server-object layout is "
                "not -> snippet only until a doc or sample confirms it"),
    _agent("zcode", "zcode", has_hook=True,
           config_path="~/.zcode/cli/config.json", config_format="json",
           mcp_style="json-mcp", server_spec=SPEC_ZCODE,
           note="servers under mcp.servers; disabling is `enable: false`"),
    _agent("muse", "muse", has_hook=True,
           config_path="~/.config/muse/settings.json", config_format="json",
           mcp_style="json-mcp", server_spec=SPEC_MUSE,
           note="mcp_servers + schema_version: 1 are mandatory, or every "
                "command fails as 'malformed settings file'"),

    # -- YAML (snippet only: no parser dependency) -----------------------
    _agent("goose", "goose",
           config_path="~/.config/goose/config.yaml", config_format="yaml",
           mcp_style="yaml", server_spec=SPEC_GOOSE,
           note="extensions map with cmd/envs; goose's own CLI owns this file"),
    _agent("hermes", "hermes", has_hook=True,
           config_path="~/.hermes/config.yaml", config_format="yaml",
           mcp_style="yaml", server_spec=SPEC_HERMES,
           note="mcp_servers mapping; YAML is printed for manual paste"),
    _agent("dsh", "dsh-tui", aliases=["dst"], required_cmds=["dsh"], has_hook=True,
           config_path="$DSH_HOME/cordis.patch.yml", config_format="yaml",
           mcp_style="yaml", server_spec=SPEC_DSH,
           note="plugin patch file; the server is a 'plugins:' entry with "
                "id mcp-<name>, not an mcp_servers table"),

    # -- CLI subcommand, no stable file ----------------------------------
    _agent("crush", "crush", mcp_style="cli-add",
           cli_add="crush mcp add {name} {exe}",
           note="migrated to crushrc + `mcp add`; JSON config is deprecated"),

    # -- No MCP ----------------------------------------------------------
    _agent("aider", "aider", mcp_style="none"),

    # -- Detected but MCP support unverified (left unknown on purpose) ---
    _agent("claude-agent-teams", "orca", aliases=["orca-dev", "orca-ide"],
           required_cmds=["claude"], unsupported_runtimes=["win32", "wsl"],
           note="not a separate agent: `orca claude-teams` launch mode"),
    _agent("openclaude", "openclaude", has_hook=True),
    _agent("autohand", "autohand"),
    _agent("ante", "ante"),
    _agent("trae", "traecli"),
    _agent("mimo-code", "mimo", note="regional/dialect variant of opencode"),
    _agent("pi", "pi",
           note="Pi 0.99+ has native MCP (<Pi agent dir>/mcp.json); two "
                "possible locations -> unknown"),
    _agent("omp", "omp"),
    _agent("prime-agent", "prime-agent"),
    _agent("freebuff", "freebuff"),
    _agent("codebuff", "codebuff"),
    _agent("command-code", "command-code", has_hook=True),
    _agent("continue", "cn",
           note="global MCP config is a YAML list; user-level location undocumented"),
    _agent("mistral-vibe", "vibe", aliases=["mistral-vibe"]),
    _agent("rovo", "acli"),
    _agent("grok", "grok", has_hook=True,
           note="docs.x.ai says ~/.grok/config.toml (TOML), agentsync says "
                "~/.grok/user-settings.json; different CLIs -> unreconciled"),
    _agent("jcode", "jcode", has_hook=True),
]

_BY_ID: Dict[str, Agent] = {a.id: a for a in AGENTS}
_BY_NAME: Dict[str, Agent] = {}
for _a in AGENTS:
    for _n in _a.detected_names:
        _BY_NAME.setdefault(_n, _a)  # first writer wins: the owning binary

if len(_BY_ID) != len(AGENTS):
    raise AssertionError("duplicate agent id in AGENTS")
if len(AGENTS) != 45:
    raise AssertionError(f"expected 45 seeded agents, found {len(AGENTS)}")


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

def get_agent(agent_id: str) -> Agent:
    """Exact id lookup. Raises KeyError for an unknown id."""
    return _BY_ID[agent_id]


def all_agents() -> List[Agent]:
    return list(AGENTS)


def resolve(id_or_alias: str) -> Agent:
    """Map an id, executable name or alias to its agent.

    `claude` the binary and `claude` the agent id are the same entry; `claude`
    also happens to be claude-agent-teams' required_cmd, which does not make
    it an alias of that agent.
    """
    key = (id_or_alias or "").strip()
    if not key:
        raise KeyError("empty agent id")
    if key in _BY_ID:
        return _BY_ID[key]
    if key in _BY_NAME:
        return _BY_NAME[key]
    lowered = key.lower()
    for table in (_BY_ID, _BY_NAME):
        for name, agent in table.items():
            if name.lower() == lowered:
                return agent
    raise KeyError(
        f"unknown agent: {key!r} (known: {', '.join(sorted(_BY_ID))})"
    )


def resolve_or_none(id_or_alias: str) -> Optional[Agent]:
    try:
        return resolve(id_or_alias)
    except KeyError:
        return None


def ids_by_level(level: str) -> List[str]:
    return [a.id for a in AGENTS if a.integration_level == level]


# --------------------------------------------------------------------------
# Runtime detection
# --------------------------------------------------------------------------

_IS_WSL: Optional[bool] = None


def is_wsl() -> bool:
    """True inside Windows Subsystem for Linux. Cached; never raises."""
    global _IS_WSL
    if _IS_WSL is not None:
        return _IS_WSL
    result = False
    if platform.system().lower() == "linux":
        try:
            if "microsoft" in platform.uname().release.lower():
                result = True
            else:
                with open("/proc/version", "r", encoding="utf-8",
                          errors="replace") as fh:
                    result = "microsoft" in fh.read().lower()
        except OSError:
            result = False
    _IS_WSL = result
    return result


def current_runtime() -> str:
    """"win32" on native Windows, "wsl" inside WSL, else platform.system().lower()."""
    name = platform.system().lower()
    if name == "windows":
        return "win32"
    if is_wsl():
        return "wsl"
    return name


# --------------------------------------------------------------------------
# Caveats carried over from the seed data (kept where setup.py can show them)
# --------------------------------------------------------------------------

SEED_CAVEATS: Dict[str, str] = {
    "grok": "TOML per docs.x.ai vs JSON per agentsync; unreconciled -> unknown",
    "antigravity": "official ~/.gemini/config/mcp_config.json preferred; "
                   "two community candidates kept as read-only fallbacks",
    "codebuddy": "three candidate files; the one that already exists wins",
    "pi": "native MCP since 0.99; two candidate locations -> unknown",
    "continue": "YAML list, no documented user-level path",
    "crush": "crushrc + `mcp add`; JSON deprecated",
    "muse": "schema_version: 1 and transport are mandatory",
    "kimi": "config.toml is the hook file; mcp.json is the MCP file",
    "dsh": "cordis.patch.yml is a plugin patch, not an MCP config",
    "goose": "cmd/envs/uri, not command/env/url",
    "opencode": "v2 moves servers to mcp.servers and flips disable to "
                "disabled:true; only v1 is written",
}


if __name__ == "__main__":
    print(f"{len(AGENTS)} agents  ({ATTRIBUTION})")
    for _a in all_agents():
        print(f"{_a.id:20s} {_a.mcp_style:16s} {_a.integration_level}")
