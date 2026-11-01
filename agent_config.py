#!/usr/bin/env python3
"""
agent_config.py - detect agents, render their MCP entry, merge it into config.

Design rules, each one backed by a test in test_agents.py:

  merge, never replace
      The only key path added is the server object itself. Every other key,
      JSONC comment and TOML table in the file survives; a user file holding
      their own servers keeps them. YAML is edited as *text* inside the target
      block so comments in the rest of the file are untouched.
  fail closed
      A file that does not parse is reported and left byte-for-byte untouched.
      "Repairing" a file we cannot read would destroy data we cannot see.
  backup, atomic, permissions preserved
      Before the first write the original is copied to <file>.honcho-backup-<ts>
      (mode 0600). New content goes to a temp file in the same directory, is
      fsynced, gets the original's mode, then os.replace()s the target. A crash
      leaves the old file or the new one, never a mixture.
  idempotent
      An identical entry is reported "already configured" and no file is
      touched. A different entry under the same name is a conflict and is
      refused: it is the user's entry until they remove it.
  dry-run is the same code path
      The merged document is computed first, verified by re-parsing it with the
      real parser, and only then committed. --dry-run stops before the commit,
      so what it reports is exactly what a real run would write.

Formats: JSON and JSONC (comment stripping honours string literals), TOML
(tomllib on 3.11+, a subset parser on 3.10), YAML (a block-mapping parser for
the goose/hermes/dsh subset; anything outside it is refused, not guessed).
No third-party imports.

Shape facts per variant come from /tmp/mcp-shapes.md (vendor-verified
2026-11-01) and live in agent_registry.Agent.server_spec.
"""

from __future__ import annotations

import ast
import datetime as _dt
import difflib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import agent_registry as reg
from agent_registry import Agent

try:  # Python 3.11+
    import tomllib as _tomllib  # type: ignore
except ModuleNotFoundError:  # Python 3.10
    _tomllib = None


SERVER_NAME = "honcho"
DEFAULT_DATA_DIR = "~/.honcho"
BACKUP_INFIX = ".honcho-backup-"

JSON_STYLES = ("json-mcpServers", "json-mcp", "json-servers")


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

STATUS_OK = "ok"               # entry added
STATUS_EXISTS = "exists"       # identical entry already there; untouched
STATUS_CONFLICT = "conflict"   # different entry with our name; untouched
STATUS_ERROR = "error"         # refused or failed; file untouched
STATUS_DRY_RUN = "dry-run"     # would write
STATUS_SKIPPED = "skipped"     # not writable by honcho; snippet supplied


@dataclass
class Result:
    agent: str
    status: str
    message: str
    path: Optional[str] = None
    backup: Optional[str] = None
    snippet: str = ""
    command: str = ""
    diff: str = ""
    #: The path asked for (may be a symlink) and the file actually written.
    requested: Optional[str] = None
    effective: Optional[str] = None
    #: Full traceback for an unexpected exception (printed by setup.py -v).
    traceback: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_EXISTS, STATUS_DRY_RUN,
                               STATUS_SKIPPED)

    def __str__(self) -> str:
        target = f" {self.path}" if self.path else ""
        return f"{self.agent}: {self.status}{target} - {self.message}"


class ConfigError(Exception):
    """A config file exists but cannot be parsed, so honcho refuses to write."""


# --------------------------------------------------------------------------
# Server spec (format-neutral)
# --------------------------------------------------------------------------

def server_command() -> str:
    """Interpreter running honcho. sys.executable guarantees >= 3.10."""
    return sys.executable or "python3"


def server_script() -> str:
    return str(Path(__file__).resolve().parent / "honcho_mcp.py")


def data_home() -> str:
    return os.path.expanduser(os.environ.get("HONCHO_HOME") or DEFAULT_DATA_DIR)


def render_server_spec(name: str = SERVER_NAME,
                       command: Optional[str] = None,
                       script: Optional[str] = None,
                       home: Optional[str] = None,
                       env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The one neutral description every writer derives its shape from."""
    full_env = {"HONCHO_HOME": home or data_home()}
    if env:
        full_env.update(env)
    return {
        "name": name,
        "command": command or server_command(),
        "args": [script or server_script()],
        "env": full_env,
    }


def server_name(agent: Agent, spec: Optional[Dict[str, Any]] = None) -> str:
    """The registered name for this run (SERVER_NAME unless --name was given).

    Planners and checkers take the name from the caller's spec rather than the
    module constant, so `--name another` registers and later finds that name.
    """
    if spec and spec.get("name"):
        return spec["name"]
    return SERVER_NAME


def _exe_and_args(spec: Dict[str, Any]) -> List[str]:
    return [spec["command"]] + list(spec.get("args") or [])


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

def _flat_server(spec: Dict[str, Any], shape: Dict[str, str]) -> Dict[str, Any]:
    """Server object for the flat families (mcpServers / mcp / mcp_servers / servers).

    shape decides the command layout, env key, mandatory fields and the
    disable switch, so one function yields every verified variant:
      mcpServers     command str, args list, env          (claude, cursor, ...)
      amp.mcpServers root_override only                   (amp)
      mcp            type local, command ARRAY, environment, enabled  (opencode v1, kilo)
      mcp.servers    + disabled: true                     (opencode v2)
      mcp_servers    + schema_version 1, transport        (muse)
      mcp.servers    + enable                             (zcode)
      servers        + type: stdio, no disable            (VS Code)
    """
    entry: Dict[str, Any] = {}
    if shape.get("type"):
        entry[shape.get("type_key", "type")] = shape["type"]
    if shape.get("command_style") == "array":
        entry["command"] = _exe_and_args(spec)
    else:
        entry[shape.get("command_key", "command")] = spec["command"]
        entry["args"] = list(spec["args"])
    if spec.get("env"):
        entry[shape.get("env_key", "env")] = dict(spec["env"])
    if shape.get("auto_approve") is not None:
        entry["autoApprove"] = json.loads(shape["auto_approve"])
    for item in filter(None, shape.get("server_defaults", "").split(",")):
        key, _, value = item.partition("=")
        entry[key] = value
    disable = shape.get("disable")
    if disable and not shape.get("no_disable"):
        # enabled/disable/enable: true means "on"; disabled: false means "on".
        entry[disable] = shape.get("disable_polarity") != "false-on"
    if shape.get("yaml_kind") == "goose":
        entry["name"] = spec["name"]
    return entry


def render_json_entry(spec: Dict[str, Any], shape: Dict[str, str]) -> Dict[str, Any]:
    return _flat_server(spec, shape)


def render_toml_entry(spec: Dict[str, Any], shape: Dict[str, str]) -> Dict[str, Any]:
    """Codex: env is a child table (serialised by toml_block), enabled = true."""
    entry: Dict[str, Any] = {"command": spec["command"], "args": list(spec["args"])}
    if spec.get("env"):
        entry["env"] = dict(spec["env"])
    if shape.get("disable"):
        entry[shape["disable"]] = True
    return entry


DSH_PLUGIN = "@deepseek-ai/dsh-mcp-client"


def render_dsh_entry(spec: Dict[str, Any]) -> Dict[str, Any]:
    """DeepSeek Harness: one plugin entry in the `plugins:` sequence.

    Not an MCP config file - the mcp-client plugin carries the server under
    its own `config:` key, and the entry is identified by id `mcp-<name>`.
    """
    return {
        "id": "mcp-" + spec["name"],
        "name": DSH_PLUGIN,
        "config": {
            "serverName": spec["name"],
            "transport": "stdio",
            "command": spec["command"],
            "args": list(spec["args"]),
            "env": dict(spec["env"]),
        },
    }


def render_yaml_entry(spec: Dict[str, Any], shape: Dict[str, str]) -> Dict[str, Any]:
    if shape.get("yaml_kind") == "dsh":
        return render_dsh_entry(spec)
    entry = _flat_server(spec, shape)
    if shape.get("yaml_kind") == "goose":
        # goose reads `cmd`/`envs` and requires type + name, in that order.
        ordered: Dict[str, Any] = {"type": entry.pop("type")}
        ordered.update(entry)
        return ordered
    return entry


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def path_table(path: Optional[str]) -> Dict[str, str]:
    """name -> full path for every entry on PATH. One scan, then dict lookups."""
    table: Dict[str, str] = {}
    for directory in (path or "").split(os.pathsep):
        if not directory:
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            full = os.path.join(directory, name)
            # Only a regular executable counts. A directory named `claude`,
            # or a 0644 file dropped on PATH, is not a runnable agent.
            if not os.path.isfile(full) or not os.access(full, os.X_OK):
                continue
            table.setdefault(name, full)
    return table


def detect_installed(path: Optional[str] = None,
                     runtime: Optional[str] = None,
                     environ: Optional[Dict[str, str]] = None) -> List[str]:
    """Ids of agents present on PATH, in registry order.

    Honours aliases (any of binary/aliases), required_cmds (all must also be
    present) and unsupported_runtimes (suppressed there).
    """
    env = os.environ if environ is None else environ
    table = path_table(env.get("PATH") if path is None else path)
    # Windows filesystems are case-insensitive, so `CLAUDE.EXE` is `claude`.
    # Matching case-insensitively on Linux would claim agents that are absent.
    lowered = ({k.lower(): v for k, v in table.items()}
               if os.name == "nt" else {})

    def which(name: str) -> Optional[str]:
        return table.get(name) or lowered.get(name.lower())

    return [a.id for a in reg.all_agents() if a.is_installed(which, runtime)]


def probe_version(binary: str, environ: Optional[Dict[str, str]] = None,
                  timeout: float = 5.0) -> Optional[Tuple[int, ...]]:
    """First x.y[.z] number in `<binary> --version`, or None.

    Used only as advisory evidence (opencode v1 vs v2); the writer never
    switches schema on a broken probe - it reports and asks.
    """
    exe = shutil.which(binary, path=(environ or os.environ).get("PATH"))
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout + " " + proc.stderr)
    if not match:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def version_warning(agent: Agent, environ: Optional[Dict[str, str]] = None
                    ) -> Optional[str]:
    """Warn when an installed opencode looks like v2 (different schema)."""
    if agent.id != "opencode":
        return None
    version = probe_version(agent.binary, environ)
    if version and version[0] >= 2:
        return (f"detected {agent.binary} {'.'.join(map(str, version))}: v2 moved "
                "servers to mcp.servers and uses disabled:true; honcho writes "
                "the v1 schema only")
    return None


# --------------------------------------------------------------------------
# JSONC
# --------------------------------------------------------------------------

def strip_jsonc(text: str) -> str:
    """Drop //, /* */ comments and trailing commas outside string literals.

    Character-wise, not regex: `"https://x"` keeps its slashes, `"a\\"//b"`
    keeps them after an escaped quote, and newlines are preserved so line
    numbers in error messages still match the original.
    """
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * text.count("\n", i, end))
            i = end
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_jsonc(text: str) -> Any:
    try:
        return json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON/JSONC: {exc}") from exc


# --------------------------------------------------------------------------
# TOML
# --------------------------------------------------------------------------

class TomlError(ConfigError):
    pass


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_strip_comment(line: str) -> str:
    quote = ""
    buf: List[str] = []
    for ch in line:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "#":
            break
        else:
            buf.append(ch)
    return "".join(buf)


def _split_top(raw: str, sep: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    for ch in raw:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    if parts and parts[-1].strip() == "" and len(parts) > 1:
        parts.pop()  # trailing separator ("[a, b,]") does not start a new item
    return parts


def _toml_key(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        value = _toml_value(raw)
        if not isinstance(value, str):
            raise TomlError(f"bad key {raw!r}")
        return value
    if not _BARE_KEY.fullmatch(raw):
        raise TomlError(f"unsupported key syntax {raw!r}")
    return raw


def _toml_table_key(raw: str) -> List[str]:
    if not raw.strip():
        raise TomlError("empty table header")
    return [_toml_key(p) for p in _split_top(raw.strip(), ".")]


def _toml_complete_array(raw: str) -> bool:
    depth = 0
    quote = ""
    for ch in _toml_strip_comment(raw):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth <= 0


def _toml_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise TomlError("missing value")
    if raw[0] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TomlError(f"bad basic string: {exc}") from exc
        if not isinstance(value, str):
            raise TomlError("bad basic string")
        return value
    if raw[0] == "'":
        if len(raw) < 2 or raw[-1] != "'":
            raise TomlError("unterminated literal string")
        return raw[1:-1]
    if raw[0] == "[":
        if not raw.endswith("]"):
            raise TomlError("unterminated array")
        body = raw[1:-1].strip()
        return [_toml_value(p) for p in _split_top(body, ",")] if body else []
    if raw[0] == "{":
        raise TomlError("inline tables are not supported on Python 3.10")
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", raw):
        return float(raw)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([Tt ].*)?", raw):
        try:
            return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        raise TomlError(f"unsupported TOML value: {raw!r}") from None


def _toml_insert(table: Dict[str, Any], keys: Sequence[str], value: Any) -> None:
    for key in keys[:-1]:
        child = table.get(key)
        if not isinstance(child, dict):
            child = {}
            table[key] = child
        table = child
    table[keys[-1]] = value


def _toml_split_kv(line: str) -> Tuple[str, Optional[str]]:
    quote = ""
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "=":
            return line[:idx], line[idx + 1:]
    return line, None


def parse_toml_subset(text: str) -> Dict[str, Any]:
    """TOML parser for the files honcho merges into (used when tomllib is absent).

    Supports comments, [table] and [[array-of-tables]] headers, dotted/quoted
    keys, basic and literal strings, arrays (single-line or continued over
    lines), booleans, integers, floats, ISO datetimes. Inline tables are
    rejected rather than mis-parsed; Python 3.11+ uses tomllib, which handles
    them.
    """
    root: Dict[str, Any] = {}
    current = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = _toml_strip_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[["):
            if not stripped.endswith("]]"):
                raise TomlError(f"line {i}: bad array-of-tables header")
            keys = _toml_table_key(stripped[2:-2])
            parent = root
            for key in keys[:-1]:
                child = parent.get(key)
                if not isinstance(child, dict):
                    child = {}
                    parent[key] = child
                parent = child
            arr = parent.setdefault(keys[-1], [])
            if not isinstance(arr, list):
                raise TomlError(f"line {i}: {'.'.join(keys)} is not an array")
            current = {}
            arr.append(current)
            continue
        if stripped.startswith("["):
            if not stripped.endswith("]"):
                raise TomlError(f"line {i}: bad table header")
            current = root
            for key in _toml_table_key(stripped[1:-1]):
                child = current.get(key)
                if not isinstance(child, dict):
                    child = {}
                    current[key] = child
                current = child
            continue
        raw_key, raw_val = _toml_split_kv(stripped)
        if raw_val is None:
            raise TomlError(f"line {i}: expected key = value")
        if raw_val.strip().startswith("["):
            joined = raw_val
            while not _toml_complete_array(joined) and i < len(lines):
                joined += " " + _toml_strip_comment(lines[i]).strip()
                i += 1
            raw_val = joined
        _toml_insert(current, _toml_table_key(raw_key), _toml_value(raw_val))
    return root


def load_toml(text: str) -> Dict[str, Any]:
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except Exception as exc:  # TOMLDecodeError class differs across versions
            raise TomlError(f"invalid TOML: {exc}") from exc
    try:
        return parse_toml_subset(text)
    except TomlError as exc:
        raise TomlError(f"invalid TOML: {exc}") from exc


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise TomlError(f"cannot serialise {type(value).__name__} to TOML")


def _toml_key_repr(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key)


def toml_block(server_name: str, entry: Dict[str, Any]) -> str:
    """`[mcp_servers.<name>]` with scalar keys, then `[...env]` child table."""
    head = "mcp_servers." + _toml_key_repr(server_name)
    lines = [f"[{head}]"]
    for key, value in entry.items():
        if not isinstance(value, dict):
            lines.append(f"{_toml_key_repr(key)} = {_toml_scalar(value)}")
    for key, value in entry.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{head}.{_toml_key_repr(key)}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{_toml_key_repr(sub_key)} = {_toml_scalar(sub_value)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# YAML (block-mapping subset, no dependency)
# --------------------------------------------------------------------------

class YamlError(Exception):
    """The document uses YAML honcho will not touch (refused, not guessed)."""


_KEY_LINE = re.compile(
    r"""^(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"\-?:\[{][^:#]*?)\s*:(?:\s+(?P<val>.*))?$"""
)


def _yaml_meanful(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped


def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_yaml_comment(raw: str) -> str:
    """Drop a trailing ` #comment` from a plain scalar (quoted values keep it)."""
    quote = ""
    for idx, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or raw[idx - 1] in " \t"):
            return raw[:idx].rstrip()
    return raw


def yaml_scalar(raw: str) -> Any:
    text = _strip_yaml_comment(raw).strip()
    if text == "" or text in ("~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"':
            raise YamlError(f"unterminated double-quoted scalar: {text!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlError(f"bad double-quoted scalar: {exc}") from exc
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise YamlError(f"unterminated single-quoted scalar: {text!r}")
        return text[1:-1].replace("''", "'")
    if text[0] == "[":
        if not text.endswith("]"):
            raise YamlError(f"multi-line flow sequence unsupported: {text!r}")
        body = text[1:-1].strip()
        return [yaml_scalar(p) for p in _split_top(body, ",")] if body else []
    if text[0] in "|>":
        # Opaque marker: the body lines are skipped by the block parsers and
        # never interpreted. honcho only reads/writes the MCP block, so not
        # understanding a block scalar's content is fine; refusing the whole
        # file over one would make every realistic hermes config unwritable.
        return _BlockScalar(text)
    if text[0] in "!":
        raise YamlError(f"tagged scalar unsupported: {text!r}")
    if text[0] in "&*":
        return _Anchored(text)
    if text[0] == "{":
        raise YamlError(f"flow mapping unsupported: {text!r}")
    if text[0] == "%":
        raise YamlError(f"directive unsupported: {text!r}")
    if text.startswith("- "):
        raise YamlError(f"block sequence on the same line unsupported: {text!r}")
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", text):
        return float(text)
    return text


_BS_HEADER = re.compile(r"(?:[^:#]+?):\s+[|>][-+]?\d*\s*(?:#.*)?$")


def _block_scalar_header(text: str) -> bool:
    """True for `key: |` style values, whose body is text, not structure."""
    return bool(_BS_HEADER.match(text))


class _BlockScalar:
    """A `|` / `>` value. Opaque: kept as a marker, body lines skipped."""

    def __init__(self, header: str):
        self.header = header

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _BlockScalar) and other.header == self.header

    def __hash__(self) -> int:
        return hash(self.header)

    def __repr__(self) -> str:
        return f"<block scalar {self.header!r}>"


class _Anchored:
    """`&anchor [value]` / `*alias`. Opaque marker; content never interpreted."""

    def __init__(self, raw: str):
        self.raw = raw

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Anchored) and other.raw == self.raw

    def __hash__(self) -> int:
        return hash(self.raw)

    def __repr__(self) -> str:
        return f"<anchor {self.raw!r}>"


def _is_block_scalar(value: Any) -> bool:
    return isinstance(value, _BlockScalar)


OPAQUE = (_BlockScalar, _Anchored)


def _skip_block_scalar(lines: List[str], key_indent: int, start: int) -> int:
    """Index of the first line after a block scalar body that began at `start`.

    The body is every following line that is blank, more indented than the
    key, or a comment at any indentation (YAML allows comments flush-left in
    a scalar body only if indented; we stop at the first non-indented content).
    """
    idx = start
    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped == "":
            idx += 1
            continue
        if _yaml_indent(lines[idx]) <= key_indent:
            break
        idx += 1
    return idx


def _yaml_key(raw: str) -> str:
    key = raw.strip()
    if key[:1] in ("'", '"'):
        value = yaml_scalar(key)
        if not isinstance(value, str):
            raise YamlError(f"bad key {raw!r}")
        return value
    return key


class _YamlSeq(list):
    """Block sequence; its item offset is the line its first '-' sits on."""

    line = 0


def parse_yaml_subset(text: str) -> Dict[str, Any]:
    """Block mappings and block sequences, nested by indentation, scalars,
    and single-line flow lists.

    This is the subset goose's config.yaml, hermes's config.yaml, dsh's
    cordis.patch.yml and Mistral Vibe's config.toml use for their MCP blocks
    (dsh in particular edits a sequence of plugin entries). Block scalars,
    anchors, tags, flow mappings and multi-line flow collections raise
    YamlError, so the caller refuses instead of writing a mangled file.

    Each container records its line via _YamlSeq.line / the key line, and
    parse with _yaml_parse_map when positions are needed.
    """
    return _parse_yaml_document(text)


def _parse_yaml_document(text: str) -> Tuple[Any, int, bool]:
    """(value, first_content_line, is_mapping) for one block node at indent 0."""
    lines = text.splitlines()
    content = [(i, l) for i, l in enumerate(lines) if _yaml_meanful(l) is not None]
    if not content:
        return {}, 0, True
    first_idx, first_line = content[0]
    if _yaml_meanful(first_line).startswith("-"):
        return _parse_yaml_seq(lines, first_idx, _yaml_indent(first_line))
    return _parse_yaml_map(lines, first_idx)


def _parse_yaml_map(lines: List[str], first: int, base: int = 0,
                    consumed: Optional[set] = None) -> Dict[str, Any]:
    """Mapping whose keys sit at `base`.

    `consumed` holds line indices that belong to a nested sequence item's
    children; they must not be read as keys of an enclosing mapping.
    """
    consumed = set() if consumed is None else consumed
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Any]] = [(base - 1, root)]
    for idx in range(first, len(lines)):
        if idx in consumed:
            continue
        line = lines[idx]
        body = line[: len(line) - len(line.lstrip())]
        if "\t" in body:
            raise YamlError(f"line {idx + 1}: tab indentation")
        stripped = _yaml_meanful(line)
        if stripped is None:
            continue
        if stripped in ("---", "..."):
            if stripped == "---" and idx == 0:
                continue
            if stripped == "..." and idx == len(lines) - 1:
                break
            raise YamlError(f"line {idx + 1}: multiple documents unsupported")
        indent = _yaml_indent(line)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise YamlError(f"line {idx + 1}: unexpected indentation")
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            raise YamlError(f"line {idx + 1}: mapping under a sequence item")
        if stripped.startswith("-"):
            raise YamlError(f"line {idx + 1}: unexpected sequence item")
        match = _KEY_LINE.match(stripped)
        if not match:
            raise YamlError(f"line {idx + 1}: expected 'key: value'")
        key = _yaml_key(match.group("key"))
        if key in parent:
            raise YamlError(f"line {idx + 1}: duplicate key {key!r}")
        val = (match.group("val") or "").strip()
        nxt = idx + 1
        while nxt < len(lines) and _yaml_meanful(lines[nxt]) is None:
            nxt += 1
        if not val or val.startswith("#"):
            # `key:` then `[]` / `{}` on the next, more-indented line is a
            # flow collection, not a nested block (no space before `[`).
            if (nxt < len(lines) and _yaml_indent(lines[nxt]) > indent
                    and _yaml_meanful(lines[nxt])[:1] in ("[", "{")):
                parent[key] = yaml_scalar(_yaml_meanful(lines[nxt]))
                consumed.add(nxt)
                continue
            # A sequence may be indented under the key or, as YAML allows,
            # start at the key's own column ("tools:" / "- read").
            if nxt < len(lines) and _yaml_indent(lines[nxt]) >= indent \
                    and _yaml_meanful(lines[nxt]).startswith("-"):
                item_indent = _yaml_indent(lines[nxt])
                child: Any = _parse_yaml_seq(lines, nxt, item_indent)
                # Everything under this sequence belongs to it, not to a
                # later key of the enclosing mapping.
                span = nxt
                while span < len(lines) and (
                        _yaml_meanful(lines[span]) is None
                        or _yaml_indent(lines[span]) > indent
                        or _yaml_meanful(lines[span]).startswith("-")):
                    consumed.add(span)
                    span += 1
            else:
                child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            value = yaml_scalar(val)
            parent[key] = value
            if _is_block_scalar(value):
                # Skip the body: it is text, not YAML, and may contain
                # anything (including lines that look like our keys).
                end = _skip_block_scalar(lines, indent, idx + 1)
                consumed.update(range(idx + 1, end))
    return root


def _parse_yaml_seq(lines: List[str], first: int, base: int) -> _YamlSeq:
    """Sequence of mappings (`- key: v`) or scalars, items at indent `base`."""
    seq = _YamlSeq()
    seq.line = first
    i = first
    while i < len(lines):
        line = lines[i]
        stripped = _yaml_meanful(line)
        if stripped is None:
            i += 1
            continue
        if _yaml_indent(line) < base or not stripped.startswith("-"):
            break
        if _yaml_indent(line) > base:
            raise YamlError(f"line {i + 1}: unexpected indentation in sequence")
        rest = stripped[1:].strip()
        if not rest:
            # Nested structure on the following, more-indented lines.
            j = i + 1
            while j < len(lines) and _yaml_meanful(lines[j]) is None:
                j += 1
            if j < len(lines) and _yaml_indent(lines[j]) > base:
                child_indent = _yaml_indent(lines[j])
                blk = _collect_indented(lines, j, child_indent)
                if _yaml_meanful(lines[j]).startswith("-"):
                    seq.append(_parse_yaml_seq(lines, j, child_indent))
                else:
                    seq.append(_parse_yaml_map(blk, 0))
                i = j + len(blk)
            else:
                seq.append(None)
                i += 1
            continue
        if not _KEY_LINE.match(rest):
            value = yaml_scalar(rest)
            seq.append(value)
            i = (_skip_block_scalar(lines, base, i + 1) if _is_block_scalar(value)
                 else i + 1)
            continue
        # `- key: value` opens a mapping whose further keys are indented to
        # the column of `key`.
        # Key column = offset of the first key after "- ", not base + 2: the
        # dash may be followed by several spaces.
        dash_indent = _yaml_indent(line)
        key_col = dash_indent + (len(line.strip()) - len(rest))
        inner = []
        j = i
        while j < len(lines):
            raw = lines[j]
            if _yaml_meanful(raw) is None:
                inner.append(raw)
                j += 1
                continue
            if j > i and _yaml_indent(raw) <= base:
                break
            if j == i:
                inner.append(" " * key_col + rest)
            else:
                if _yaml_indent(raw) < key_col:
                    raise YamlError(f"line {j + 1}: under-indented mapping key "
                                    f"inside a sequence item")
                inner.append(raw)
            j += 1
        seq.append(_parse_yaml_map(inner, 0))
        i = j
    return seq


def _collect_indented(lines: List[str], start: int, indent: int) -> List[str]:
    out: List[str] = []
    j = start
    while j < len(lines):
        if _yaml_meanful(lines[j]) is not None and _yaml_indent(lines[j]) < indent:
            break
        out.append(lines[j])
        j += 1
    return out


def _yaml_repr(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # Always a JSON double-quoted string. That is a valid YAML flow scalar for
    # any content, including text that would otherwise be read as a mapping
    # (`a: b`), a comment (`x # y`) or a number ("007").
    return json.dumps(str(value))


def yaml_dump_node(node: Dict[str, Any], indent: int) -> List[str]:
    """Render a mapping as block YAML lines (no dependency, no guessing)."""
    pad = " " * indent
    lines: List[str] = []
    for key, value in node.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(yaml_dump_node(value, indent + 2))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}: [{', '.join(_yaml_repr(v) for v in value)}]")
        elif value is None:
            lines.append(f"{pad}{key}:")
        else:
            lines.append(f"{pad}{key}: {_yaml_repr(value)}")
    return lines


# --------------------------------------------------------------------------
# Filesystem guards
# --------------------------------------------------------------------------

def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")


def atomic_write(path: str, text: str) -> None:
    """Temp file in the same dir -> fsync -> chmod to original mode -> rename.

    A UTF-8 BOM is written back if the file had one, so an editor's choice
    survives the edit and the parser never sees it.
    """
    directory = os.path.dirname(path) or "."
    try:
        with open(path, "rb") as fh:
            had_bom = fh.read(3) == b"\xef\xbb\xbf"
    except OSError:
        had_bom = False
    encoded = (BOM + text) if had_bom else text
    os.makedirs(directory, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix=".honcho-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


MAX_BACKUPS = 10


def list_backups(path: str) -> List[str]:
    """Backups for this config, oldest first (names sort chronologically)."""
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + BACKUP_INFIX
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = [os.path.join(directory, n) for n in names if n.startswith(prefix)]
    return sorted(found)


def prune_backups(path: str, keep: int = MAX_BACKUPS) -> List[str]:
    """Delete the oldest backups so at most `keep` remain. Returns deleted."""
    backups = list_backups(path)
    excess = backups[: max(0, len(backups) - keep)]
    removed = []
    for old in excess:
        try:
            os.unlink(old)
        except OSError:
            continue
        removed.append(old)
    return removed


def backup_file(path: str, keep: int = MAX_BACKUPS) -> str:
    """Copy the config aside (mode 0600), then keep only the newest `keep`.

    Unbounded backups would flood the user's config directory: every install
    of a different name, every re-run after a change, adds one.
    """
    dest = f"{path}{BACKUP_INFIX}{_timestamp()}"
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    prune_backups(path, keep)
    return dest


class Locked(Exception):
    """Another honcho process holds the lock for this file."""


class FileLock:
    """Exclusive advisory lock for one config file, with nothing left behind.

    Prevents two concurrent `setup.py install` runs from both reading the file
    and then writing over each other (a lost update that atomic replace cannot
    prevent).

    The lock is a *directory* `<path>.lock/`, flock'ed on the directory's own
    descriptor. A plain lock file cannot be cleaned up safely: unlinking it
    lets a waiter that already opened it and a newcomer that recreates it both
    believe they hold the lock. With a directory, `mkdir` is the atomic
    creation, the holder `rmdir`s it while still holding flock, and a waiter
    that finds it gone simply retries. No separate lock file is needed, so a
    normal run leaves nothing behind.

    If advisory locking is unavailable on the platform (Windows), the write
    proceeds and a warning is recorded on `self.warning`.
    """

    def __init__(self, path: str, timeout: float = 10.0):
        self.path = path
        self.dir = path + ".lock"
        self.timeout = timeout
        self.fd: Optional[int] = None
        self.warning = ""

    def _open_dir(self) -> Optional[int]:
        try:
            os.mkdir(self.dir, 0o700)
        except FileExistsError:
            pass
        except OSError:
            return None
        try:
            return os.open(self.dir, os.O_RDONLY)
        except OSError:
            return None

    def acquire(self) -> None:
        """Block (up to timeout) until the lock is held."""
        deadline = _dt.datetime.now() + _dt.timedelta(seconds=self.timeout)
        try:
            os.makedirs(os.path.dirname(self.dir) or ".", exist_ok=True)
        except OSError as exc:
            self.warning = f"cannot create lock directory ({exc}); writing unlocked"
            return
        while True:
            fd = self._open_dir()
            if fd is not None:
                try:
                    _lock_fd(fd, blocking=False)
                except BlockingIOError:
                    os.close(fd)
                    fd = None
                except OSError as exc:  # platform without advisory locks
                    os.close(fd)
                    self.warning = f"flock unavailable ({exc}); writing unlocked"
                    return
                else:
                    # Held. Confirm the directory we locked still exists: the
                    # previous holder rmdir's it while holding the flock we
                    # could not take, so this can only be true for the live one.
                    try:
                        st = os.stat(self.dir)
                        if (os.fstat(fd).st_ino == st.st_ino
                                and os.fstat(fd).st_dev == st.st_dev):
                            self.fd = fd
                            return
                    except OSError:
                        pass
                    os.close(fd)
            if _dt.datetime.now() >= deadline:
                raise Locked(f"another honcho process is writing {self.path} "
                             f"(lock {self.dir})")
            time.sleep(0.02)

    def release(self) -> None:
        """Drop the lock and remove the lock directory."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            try:
                os.rmdir(self.dir)   # while still holding the flock
            except OSError:
                pass
        finally:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


try:
    import fcntl as _fcntl

    def _lock_fd(fd: int, blocking: bool) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB))

    def _unlock_fd(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)

except ImportError:  # Windows
    try:
        import msvcrt as _msvcrt

        def _lock_fd(fd: int, blocking: bool) -> None:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK if not blocking else _msvcrt.LK_LOCK, 1)

        def _unlock_fd(fd: int) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)

    except ImportError:  # exotic platform: no advisory locks
        def _lock_fd(fd: int, blocking: bool) -> None:
            raise OSError("advisory locking not supported on this platform")

        def _unlock_fd(fd: int) -> None:
            return None


BOM = "\ufeff"


def read_text(path: str) -> Optional[str]:
    """File text with any UTF-8 BOM stripped (atomic_write re-emits it)."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"not UTF-8 text: {exc}") from exc


def effective_path(path: str) -> str:
    """The file actually written: a symlink is followed, never replaced.

    Dotfile managers (stow, chezmoi, Nix home-manager) present configs as
    symlinks into a repo. os.replace() would swap the link for a regular file
    and silently detach the dotfile. Writing through to the target keeps the
    link intact. A dangling link is an error: creating its target would write
    to a path nobody asked for.
    """
    if not os.path.islink(path):
        return path
    target = os.path.realpath(path)
    if not os.path.exists(target):
        raise OSError(f"{path} is a symlink to a missing file ({target}); "
                      "refusing to create it")
    return target


def choose_path(agent: Agent, environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """First existing candidate (env override, table path, fallback), else the
    first candidate that could be created."""
    candidates = agent.config_candidates(environ)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else None


def _diff(path: Optional[str], old: str, new: str) -> str:
    """Unified diff that always ends in a newline (keeps `--- \\ No newline` out)."""
    label = path or "<new file>"
    text = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{label} (current)", tofile=f"{label} (after)", lineterm="\n",
    ))
    if text and not text.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------
# Key paths
# --------------------------------------------------------------------------

_DEFAULT_ROOT = {
    "json-mcpServers": "mcpServers",
    "json-mcp": "mcp",
    "json-servers": "servers",
}


def container_keys(agent: Agent) -> List[str]:
    """Keys from the file root down to the object holding server names.

    Defaults follow the style's documented root key (json-mcp -> "mcp"), so a
    shape that only overrides the key *name* (muse, amp) still lands right.
    """
    if agent.mcp_style == "yaml":
        default = "mcp_servers"
    else:
        default = _DEFAULT_ROOT.get(agent.mcp_style, "mcpServers")
    root = agent.server_spec.get("root_override") or default
    keys = [root]
    nested = agent.server_spec.get("nested")
    if nested:
        keys.extend(nested.split("."))
    return keys


def _yaml_existing(agent: Agent, found: Any, entry: Dict[str, Any],
                   name: str = SERVER_NAME) -> List[Any]:
    """Existing entries matching `entry` in a YAML container.

    A mapping (hermes) is keyed by server name; a sequence (dsh) is matched by
    the item's identifying field (`id`).
    """
    if isinstance(found, dict):
        return [found[name]] if name in found else []
    if isinstance(found, list):
        key = agent.server_spec.get("sequence_item_key", "id")
        return [item for item in found
                if isinstance(item, dict) and item.get(key) == entry.get(key)]
    return []


def _entry_container(agent: Agent, data: Dict[str, Any]) -> Any:
    """The object holding server names inside parsed `data`, or None.

    Uses the shape's container path, then retires a documented legacy
    container (`server_spec["legacy_key"]`) so an older file is still read -
    never written to - while the entry lives where the new schema expects it.
    """
    node: Any = data
    for key in container_keys(agent):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, dict):
        return node
    if isinstance(node, list) and agent.server_spec.get("container") == "sequence":
        return node
    legacy = agent.server_spec.get("legacy_key")
    if legacy:
        node = data
        for key in legacy.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        if isinstance(node, dict):
            return node
    return None


def lift_legacy_mcp(data: Dict[str, Any]) -> None:
    """zcode: legacy flat `mcp.<name>` entries become `mcp.servers.<name>`.

    Documented behaviour: with a flat `mcp`, a nested `mcp.servers` block plus
    flat siblings is ambiguous. Moving server-shaped siblings under `servers`
    loses nothing (same values) and leaves non-server keys (`mcp.enabled`,
    etc.) where they were. Only applied when a `servers` object exists.
    """
    servers = data.get("servers")
    if not isinstance(servers, dict):
        return
    server_keys = {"command", "args", "env", "url", "headers", "type"}
    for key in list(data):
        if key == "servers":
            continue
        value = data.get(key)
        if isinstance(value, dict) and server_keys & set(value):
            servers.setdefault(key, value)
            del data[key]


def _entry_now(agent: Agent, data: Dict[str, Any]) -> Any:
    node = _entry_container(agent, data)
    return node


# --------------------------------------------------------------------------
# Commands / snippets
# --------------------------------------------------------------------------

def yaml_container_key(agent: Agent) -> str:
    return agent.server_spec.get("root_override") or "mcp_servers"


def cli_command(agent: Agent, spec: Optional[Dict[str, Any]] = None) -> str:
    spec = spec or render_server_spec()
    template = agent.cli_add or "{binary} mcp add {name} {exe}"
    return template.format(binary=agent.binary, name=spec["name"],
                           exe=shlex.join(_exe_and_args(spec)),
                           script=spec["args"][0])


def snippet_for(agent: Agent, spec: Optional[Dict[str, Any]] = None) -> str:
    """A complete, minimal file (or CLI command) to paste by hand."""
    spec = spec or render_server_spec()
    shape = agent.server_spec
    style = agent.mcp_style
    if style == "none":
        return f"# {agent.id} does not support MCP; nothing to register."
    if style == "cli-add":
        return cli_command(agent, spec)
    if style == "unknown":
        cmd = shlex.join(_exe_and_args(spec))
        return (
            f"# {agent.id}: MCP config location/shape is not verified.\n"
            f"# Register a stdio server named '{spec['name']}':\n"
            f"#   command : {cmd}\n"
            f"#   env     : HONCHO_HOME={spec['env']['HONCHO_HOME']}\n"
            f"# ({(agent.note or 'no registry note')})"
        )
    if style in JSON_STYLES:
        data: Dict[str, Any] = {}
        node = data
        for key in container_keys(agent):
            node[key] = {}
            node = node[key]
        node[spec["name"]] = render_json_entry(spec, shape)
        if shape.get("requires_schema_version"):
            data = {"schema_version": int(shape["requires_schema_version"]), **data}
        return json.dumps(data, indent=2) + "\n"
    if style == "toml-table":
        return toml_block(spec["name"], render_toml_entry(spec, shape))
    if style == "yaml" and shape.get("container") == "sequence":
        return "\n".join([f"{yaml_container_key(agent)}:"]
                         + _yaml_dump_seq_item(render_yaml_entry(spec, shape), 2)) + "\n"
    if style == "yaml":
        entry = render_yaml_entry(spec, shape)
        return "\n".join([f"{yaml_container_key(agent)}:"]
                         + yaml_dump_node({spec["name"]: entry}, 2)) + "\n"
    raise ValueError(f"no snippet renderer for {style}")


# --------------------------------------------------------------------------
# Merge planners: (agent, path, entry, original_text) -> (new_text, status)
# --------------------------------------------------------------------------

class Refusal(Exception):
    pass


def has_jsonc_comments(text: str) -> bool:
    """True when the document contains a // or /* */ comment (string-aware)."""
    i, n, in_str = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            in_str = ch != '"'
        elif ch == '"':
            in_str = True
        elif ch == "/" and i + 1 < n and text[i + 1] in "/*":
            return True
        i += 1
    return False


class _JsonScan:
    """Minimal position-aware reader over JSON/JSONC text.

    Only used by the comment-preserving writer, which must know *where* an
    object ends in the user's own text. It never builds a document; values are
    skipped by bracket/string tracking, and parsed ones are handed to json.
    """

    def __init__(self, text: str, pos: int = 0):
        self.text = text
        self.pos = pos

    def skip_trivia(self) -> None:
        """Skip whitespace and comments (JSONC) between tokens."""
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch in " \t\r\n":
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end == -1 else end
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end == -1:
                    raise Refusal("unterminated block comment")
                self.pos = end + 2
            else:
                return

    def skip_trivia_or_zero(self) -> int:
        """skip_trivia() returning the resulting offset (0 for a root object)."""
        self.skip_trivia()
        return self.pos

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def string_end(self) -> int:
        """Index just past the string literal starting here."""
        i = self.pos + 1
        while i < len(self.text):
            if self.text[i] == "\\":
                i += 2
                continue
            if self.text[i] == '"':
                return i + 1
            i += 1
        raise Refusal("unterminated string in JSON")

    def value_end(self, start: int) -> int:
        """End of the scalar/object/array value beginning at `start`."""
        ch = self.text[start]
        if ch == '"':
            saved, self.pos = self.pos, start
            end = self.string_end()
            self.pos = saved
            return end
        if ch in "[{":
            depth, in_str, i = 0, False, start
            while i < len(self.text):
                c = self.text[i]
                if in_str:
                    if c == "\\":
                        i += 2
                        continue
                    if c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
            raise Refusal("unbalanced JSON value")
        i = start
        while i < len(self.text) and self.text[i] not in ",}] \t\r\n":
            i += 1
        return i

    def object_members(self, open_index: int) -> List[Tuple[str, int, int, int]]:
        """(key, key_start, value_start, value_end) for each member.

        Raises Refusal if the text at `open_index` is not a well-formed object
        literal, so the caller can fall back to refusing the write.
        """
        if open_index >= len(self.text) or self.text[open_index] != "{":
            raise Refusal("expected an object literal")
        members: List[Tuple[str, int, int, int]] = []
        self.pos = open_index + 1
        while True:
            self.skip_trivia()
            if self.peek() == "}":
                return members
            if self.peek() != '"':
                raise Refusal("unexpected token where an object key was expected")
            key_start = self.pos
            self.pos = self.string_end()
            try:
                name = json.loads(self.text[key_start:self.pos])
            except json.JSONDecodeError:
                raise Refusal(f"cannot read key {self.text[key_start:self.pos]!r}")
            self.skip_trivia()
            if self.peek() != ":":
                raise Refusal("expected ':' after an object key")
            self.pos += 1
            self.skip_trivia()
            value_start = self.pos
            value_end = self.value_end(value_start)
            members.append((name, key_start, value_start, value_end))
            self.pos = value_end
            self.skip_trivia()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "}":
                raise Refusal("expected ',' or '}' between object members")


def _object_insert_point(text: str, open_index: int) -> Tuple[int, str, bool]:
    """(offset, prefix, empty) for inserting a member into the object at open_index.

    An empty object gets its member directly after '{'. Otherwise the new
    member goes after the last value, before any comment that trails it, and
    the comma is added to that value.
    """
    members = _JsonScan(text).object_members(open_index)
    if not members:
        return open_index + 1, "", True
    _, _, _, last_end = members[-1]
    return last_end, ",", False


def _find_key_offsets(text: str, start: int, key: str) -> Optional[Tuple[int, int, int]]:
    """(key_start, value_start, value_end) of a top-level member in the object
    beginning at `start`, or None when the key is absent."""
    for name, key_start, value_start, value_end in _JsonScan(text, start).object_members(start):
        if name == key:
            return key_start, value_start, value_end
    return None


def _line_indent(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    i = line_start
    while i < len(text) and text[i] in " \t":
        i += 1
    return text[line_start:i]


def _member_text(name: str, entry: Dict[str, Any], indent: str) -> List[str]:
    """Lines of `"name": { ... }`, the member itself at `indent`."""
    body = json.dumps(entry, indent=2).splitlines()
    out = [indent + json.dumps(name) + ": " + body[0]]
    out.extend(indent + line for line in body[1:])
    return out


def _object_close_index(text: str, open_index: int) -> int:
    """Index of the '}' that closes the object at `open_index` (string-aware)."""
    sc = _JsonScan(text, open_index)
    if text[open_index] != "{":
        raise Refusal("expected an object literal")
    sc.pos = open_index + 1
    while True:
        sc.skip_trivia()
        if sc.peek() == "}":
            return sc.pos
        if sc.peek() != '"':
            raise Refusal("unexpected token in object")
        sc.pos = sc.string_end()
        sc.skip_trivia()
        if sc.peek() != ":":
            raise Refusal("expected ':' in object")
        sc.pos += 1
        sc.skip_trivia()
        sc.pos = sc.value_end(sc.pos)
        sc.skip_trivia()
        if sc.peek() == ",":
            sc.pos += 1
        elif sc.peek() != "}":
            raise Refusal("expected ',' or '}' in object")


def _splice_object(text: str, open_index: int, name: str,
                   entry: Dict[str, Any]) -> str:
    """Insert `name: entry` into the object literal at `open_index`.

    The new member goes on its own line, indented like its siblings; the
    closing brace keeps its line. A one-line object (`{"a": 1}`) is left as a
    one-liner when the new value is short, because expanding it would rewrite
    the user's layout more than the change requires.
    """
    members = _JsonScan(text).object_members(open_index)
    close = _object_close_index(text, open_index)
    owner_indent = _line_indent(text, open_index)
    if not members:
        lines = _member_text(name, entry, owner_indent + "  ")
        return (text[: open_index + 1] + "\n" + "\n".join(lines) + "\n"
                + owner_indent + text[close:])
    child_indent = _line_indent(text, members[-1][1])
    _, _, _, last_end = members[-1]
    # The text between the last value and the closing brace holds the optional
    # trailing comma and any comments; the new member goes after those, so a
    # comma is only added when the user's file does not already have one.
    tail = text[last_end:close]
    comma = "" if "," in strip_jsonc(tail) else ","
    if "\n" not in strip_jsonc(tail):
        # Everything on one line: keep it that way and append a member.
        inline = json.dumps(name) + ": " + json.dumps(entry)
        return text[:last_end] + comma + " " + inline + text[last_end:]
    lines = _member_text(name, entry, child_indent)
    # Insert on the closing brace's own line (before its indentation).
    brace_line_start = text.rfind("\n", last_end, close) + 1
    return (text[:last_end] + comma + text[last_end:brace_line_start]
            + "".join(l + "\n" for l in lines) + text[brace_line_start:])


def _jsonc_splice(agent: Agent, text: str, entry: Dict[str, Any],
                  name: str = SERVER_NAME) -> str:
    """Insert the entry by editing the original text, keeping every comment.

    This is the JSONC path (kilo, opencode .jsonc). Reserialising a commented
    file would silently delete the user's comments, so the entry is spliced
    into the raw text and the caller re-parses the result. A missing container
    key is created in the same splice (comments elsewhere are untouched).
    """
    position = _JsonScan(text).skip_trivia_or_zero()
    for key in container_keys(agent):
        found = _find_key_offsets(text, position, key)
        if found is None:
            # Add the container empty to the parent object, then descend.
            text = _splice_object(text, position, key, {})
            found = _find_key_offsets(text, position, key)
            if found is None:
                raise Refusal(f"could not create the '{key}' container")
        position = found[1]
    members = _JsonScan(text).object_members(position)
    for member_name, key_start, value_start, value_end in members:
        if member_name != name:
            continue
        try:
            existing = json.loads(strip_jsonc(text[value_start:value_end]))
        except json.JSONDecodeError as exc:
            raise Refusal(f"cannot read the existing {name!r} member: {exc}")
        if existing == entry:
            raise _Already()
        raise _Conflict(existing)
    return _splice_object(text, position, name, entry)


def _plan_json(agent: Agent, path: str, entry: Dict[str, Any],
               original: Optional[str], name: str = SERVER_NAME) -> str:
    if original is not None and not original.strip():
        raise Refusal("file exists but is empty; not overwriting it")
    if original is not None and has_jsonc_comments(original):
        return _jsonc_splice(agent, original, entry, name)
    if original is None:
        data: Dict[str, Any] = {}
    else:
        data = load_jsonc(original)
        if not isinstance(data, dict):
            raise Refusal("file root is not a JSON object")
    container: Any = data
    for key in container_keys(agent):
        nxt = container.get(key)
        if nxt is None:
            nxt = container[key] = {}
        elif not isinstance(nxt, dict):
            raise Refusal(f"'{key}' already exists and is not an object")
        container = nxt
    existing = container.get(name)
    if existing is not None:
        if existing == entry:
            raise _Already()
        raise _Conflict(existing)
    container[name] = entry
    if agent.server_spec.get("requires_schema_version"):
        wanted = int(agent.server_spec["requires_schema_version"])
        current = data.get("schema_version", wanted)
        if current != wanted:
            raise Refusal(f"schema_version is {current!r}; {agent.id} requires {wanted}")
        data["schema_version"] = wanted
    if agent.server_spec.get("nested") == "servers":
        lift_legacy_mcp(data[container_keys(agent)[0]])
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _toml_region(text: str, server: str) -> Optional[Tuple[int, int]]:
    """[start, end) of the `[mcp_servers.<server>]` block, including any comment
    line directly above it, or None."""
    wanted = "mcp_servers." + server
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start: Optional[int] = None
    for idx, line in enumerate(lines):
        stripped = _toml_strip_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[["):
            if start is not None:
                return offsets[start], offsets[idx]
            continue
        if stripped.startswith("["):
            header = stripped[1:].split("]", 1)[0].strip()
            if header == wanted or header.startswith(wanted + "."):
                if start is None:
                    start = idx
                    while start > 0 and (not lines[start - 1].strip()
                                         or lines[start - 1].lstrip().startswith("#")):
                        start -= 1
            elif start is not None:
                return offsets[start], offsets[idx]
    if start is None:
        return None
    return offsets[start], len(text)


def _plan_toml(agent: Agent, path: str, entry: Dict[str, Any],
               original: Optional[str], name: str = SERVER_NAME) -> str:
    text = original or ""
    if original is not None and not original.strip():
        raise Refusal("file exists but is empty; not overwriting it")
    if original is not None:
        data = load_toml(original)
        servers = data.get("mcp_servers")
        if isinstance(servers, dict) and name in servers:
            if servers[name] == entry:
                raise _Already()
            raise _Conflict(servers[name])
        if servers is not None and not isinstance(servers, dict):
            raise Refusal("'mcp_servers' exists and is not a table")
    block = toml_block(name, entry)
    bounds = _toml_region(text, name)
    if bounds is not None:
        head = text[: bounds[0]]
        tail = text[bounds[1]:]
        if head and not head.endswith("\n"):
            head += "\n"
        new_text = head + block + (("\n" + tail) if tail else "")
    else:
        if not text:
            new_text = block
        elif text.endswith("\n\n"):
            new_text = text + block
        elif text.endswith("\n"):
            new_text = text + "\n" + block
        else:
            new_text = text + "\n\n" + block
    return new_text


def _region_of(lines: List[str], path: List[str],
               consumed: set) -> Tuple[Optional[int], Optional[int]]:
    """(first, last) of the block node at `path` inside `lines`, end exclusive.

    A sequential, indentation-aware walk: each level starts at the first key of
    the parent block and stops at the first line at the parent's own indent.
    Block-scalar bodies and value-only lines (`[]`) are skipped, so text that
    merely looks like a key can never be mistaken for one. Returns (None, None)
    when the path is absent; raises Refusal when it exists as a scalar.
    """
    start, end, parent_indent = 0, len(lines), -1
    for key in path:
        found = None
        idx = start
        while idx < end:
            if idx in consumed:
                idx += 1
                continue
            line = lines[idx]
            stripped = _yaml_meanful(line)
            if stripped is None:
                idx += 1
                continue
            indent = _yaml_indent(line)
            if indent <= parent_indent:
                break                      # end of the parent block
            match = _KEY_LINE.match(stripped)
            if match and _yaml_key(match.group("key")) == key:
                found = idx
                break
            if _block_scalar_header(stripped):
                idx = _skip_block_scalar(lines, indent, idx + 1)
                continue
            idx += 1
        if found is None:
            return None, None
        line = lines[found]
        stripped = _yaml_meanful(line)
        tail = stripped[stripped.index(":") + 1:].strip()
        key_indent = _yaml_indent(line)
        if _block_scalar_header(stripped) or (tail and not tail.startswith("#")):
            raise Refusal(f"'{key}' is a scalar value, not a block mapping")
        # Children: lines indented deeper than the key, up to the next sibling.
        last = found + 1
        idx = found + 1
        child_indent = None
        while idx < end:
            inner = _yaml_meanful(lines[idx])
            if inner is None:
                idx += 1
                continue
            if _yaml_indent(lines[idx]) <= key_indent:
                break
            if child_indent is None:
                child_indent = _yaml_indent(lines[idx])
            if _block_scalar_header(inner):
                idx = _skip_block_scalar(lines, _yaml_indent(lines[idx]), idx + 1)
            else:
                idx += 1
            last = idx
        if child_indent is None:
            return found, found + 1
        if _yaml_meanful(lines[found + 1]).startswith("-"):
            consumed.update(range(found + 1, last))
        start, end, parent_indent = found + 1, last, child_indent - 1
    return start, end


def yaml_region(lines: List[str], path: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """Lines of the block mapping at `path` (see _region_of)."""
    return _region_of(lines, path, set())


def yaml_seq_region(lines: List[str], path: List[str]) -> Optional[Tuple[int, int]]:
    """Lines of the block sequence at `path`, or None if it is not a sequence."""
    region = _region_of(lines, path, set())
    if region[0] is None:
        return None
    first = region[0]
    meaningful = [i for i in range(first, region[1])
                  if _yaml_meanful(lines[i]) is not None]
    if not meaningful or not _yaml_meanful(lines[meaningful[0]]).startswith("-"):
        return None
    return first, region[1]


def yaml_seq_insert(lines: List[str], path: List[str], item: Dict[str, Any],
                    indent: int) -> List[str]:
    """Append `item` to the block sequence at `path`, matching its indent.

    A missing sequence is created as nested `key:` lines plus its first item.
    The new item copies the indentation of the existing items, not `indent`:
    a file that indents its items by four spaces must not receive a second
    item indented by two.
    """
    region = yaml_seq_region(lines, path)
    if region is None:
        text = "".join(lines)
        header = _yaml_top_key_region(lines, path)
        body = "".join(l + "\n" for l in _yaml_dump_seq_item(item, indent))
        if header is not None:
            # `plugins:` already exists (with no items): add the first item
            # under it rather than emitting the header a second time.
            at = header[1]
            return lines[:at] + [body] + lines[at:]
        block = ("".join(" " * (2 * depth) + f"{key}:\n"
                         for depth, key in enumerate(path)) + body)
        prefix = "" if not text else ("\n\n" if not text.endswith("\n") else "\n")
        return [text + prefix + block]
    first, last = region
    item_indent = _yaml_indent(lines[first]) if first < len(lines) else indent
    body = [l + "\n" for l in _yaml_dump_seq_item(item, item_indent)]
    return lines[:last] + body + lines[last:]


def _yaml_top_key_region(lines: List[str], path: List[str]) -> Optional[Tuple[int, int, int]]:
    """(key_line, insert_at, key_indent) for a header with an empty body."""
    if len(path) != 1:
        return None
    key = path[0]
    for idx, line in enumerate(lines):
        stripped = _yaml_meanful(line)
        if stripped is None or _yaml_indent(line) != 0:
            continue
        if stripped == f"{key}:":
            return idx, idx + 1, 0
    return None


def _yaml_dump_seq_item(item: Dict[str, Any], indent: int) -> List[str]:
    pad = " " * indent
    lines: List[str] = []
    first = True
    for key, value in item.items():
        head = pad + ("- " if first else "  ") + f"{key}:"
        first = False
        if isinstance(value, dict):
            lines.append(head)
            lines.extend(yaml_dump_node(value, indent + 4))
        elif isinstance(value, list):
            lines.append(f"{head} [{', '.join(_yaml_repr(v) for v in value)}]")
        else:
            lines.append(f"{head} {_yaml_repr(value)}")
    return lines


def yaml_insert(lines: List[str], path: List[str], node: Dict[str, Any],
                pad: int) -> List[str]:
    """Return lines with `node` appended inside the mapping at `path`."""
    first, last = yaml_region(lines, path)
    body = [l + "\n" for l in yaml_dump_node(node, pad)]
    if first is None:
        block: List[str] = []
        for depth, key in enumerate(path):
            block.append(" " * (2 * depth) + f"{key}:\n")
        block.extend(l + "\n" for l in
                     yaml_dump_node(node, 2 * (len(path) - 1) + 2))
        text = "".join(lines)
        prefix = "" if not text else ("\n\n" if not text.endswith("\n") else "\n")
        return [text + prefix + "".join(block)]
    insert_at = last
    while insert_at > first and _yaml_meanful(lines[insert_at - 1]) is None:
        insert_at -= 1
    return lines[:insert_at] + body + lines[insert_at:]


def _plan_yaml(agent: Agent, path: str, entry: Dict[str, Any],
               original: Optional[str], name: str = SERVER_NAME) -> str:
    """Insert the entry under the agent's container, editing text in place.

    Text-level, so comments elsewhere in the file are preserved; the result is
    re-parsed by the caller before anything is written. For per-server files
    (one file = one server) the text is the whole entry.
    """
    if original is not None and not original.strip():
        raise Refusal("file exists but is empty; not overwriting it")
    if agent.server_spec.get("mode") == "per-server-file":
        if original is not None and original.strip():
            existing = parse_yaml_subset(original)
            if existing.get(name) == entry:
                raise _Already()
            raise _Conflict(existing)
        return "".join(yaml_dump_node({name: entry}, 0))
    text = original or ""
    if original is not None and original.strip():
        found = _entry_container(agent, parse_yaml_subset(text)) or {}
        for existing in _yaml_existing(agent, found, entry, name):
            if existing == entry:
                raise _Already()
            raise _Conflict(existing)
    lines = text.splitlines(keepends=True)
    path = yaml_container_key(agent).split(".")
    if agent.server_spec.get("container") == "sequence":
        _require_homogeneous_sequence(agent, text, path)
        return "".join(yaml_seq_insert(lines, path, entry, 0))
    return "".join(yaml_insert(lines, path, {name: entry}, 2))


def _require_homogeneous_sequence(agent: Agent, text: str, path: List[str]) -> None:
    """Refuse to append to a sequence whose items are not all of one kind.

    dsh's `plugins:` is a list of plugin entries. If the file also uses that
    list for plain strings or other schemas, appending an item would mean
    guessing what the other items are - so refuse instead.
    """
    if not text.strip():
        return
    node: Any = parse_yaml_subset(text)
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, list):
        return
    kind = agent.server_spec.get("sequence_item_key")
    for item in node:
        if not isinstance(item, dict) or (kind and kind not in item):
            raise Refusal(
                f"'{'.'.join(path)}' holds a non-{kind or 'mapping'} item "
                f"({type(item).__name__}); honcho will not guess how to add "
                "a plugin entry beside it - paste the snippet by hand")


class _Already(Exception):
    """Same name, same value: nothing to do."""


class _Conflict(Exception):
    def __init__(self, existing: Any):
        self.existing = existing


PLANNERS = {
    "json-mcpServers": _plan_json,
    "json-mcp": _plan_json,
    "json-servers": _plan_json,
    "toml-table": _plan_toml,
    "yaml": _plan_yaml,
}


def _verify(agent: Agent, new_text: str, entry: Dict[str, Any],
            name: str = SERVER_NAME) -> None:
    """Re-parse the merged text with the real parser and check the entry.

    Removing this call must break a test: it is the only thing standing
    between a planner bug and a written file (see test_verify_is_load_bearing).
    """
    style = agent.mcp_style
    if style in JSON_STYLES:
        data = load_jsonc(new_text)
        container = _entry_container(agent, data)
        got = container.get(name) if isinstance(container, dict) else None
        if agent.server_spec.get("requires_schema_version"):
            if data.get("schema_version") != int(agent.server_spec["requires_schema_version"]):
                raise Refusal("merged file lost schema_version")
    elif style == "toml-table":
        data = load_toml(new_text)
        servers = data.get("mcp_servers")
        got = servers.get(name) if isinstance(servers, dict) else None
    elif style == "yaml":
        doc = parse_yaml_subset(new_text)
        if agent.server_spec.get("mode") == "per-server-file":
            got = doc.get(name)
        else:
            matches = _yaml_existing(agent, _entry_container(agent, doc), entry, name)
            got = matches[0] if len(matches) == 1 else None
    else:  # pragma: no cover - guarded by install()
        raise Refusal(f"no verifier for {style}")
    if got != entry:
        raise Refusal("merged file does not contain the intended entry")


def plan_install(agent: Agent, path: str, entry: Dict[str, Any],
                 original: Optional[str], name: str = SERVER_NAME) -> Tuple[str, str]:
    """Return (new_text, status). Raises Refusal/_Already/_Conflict."""
    planner = PLANNERS.get(agent.mcp_style)
    if planner is None:
        raise Refusal(f"no writer for style {agent.mcp_style}")
    new_text = planner(agent, path, entry, original, name)
    _verify(agent, new_text, entry, name)
    return new_text, STATUS_OK


# --------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------

def install(agent: Agent, dry_run: bool = False,
            environ: Optional[Dict[str, str]] = None,
            spec: Optional[Dict[str, Any]] = None) -> Result:
    """Register honcho for one agent. Never destructive, never partial.

    Never raises for an agent-level problem: an unexpected exception (a parser
    bug, say) becomes a STATUS_ERROR for *this* agent only, so one bad agent
    cannot abort `install --all`.
    """
    try:
        return _install(agent, dry_run, environ, spec)
    except Exception as exc:  # noqa: BLE001 - isolation boundary, see docstring
        if os.environ.get("HONCHO_DEBUG"):
            raise
        return Result(agent.id, STATUS_ERROR,
                      f"internal error, file may or may not be changed "
                      f"({type(exc).__name__}: {exc})",
                      traceback=traceback.format_exc())


def decide_path(agent: Agent, dry_run: bool,
                environ: Optional[Dict[str, str]] = None) -> tuple:
    """(path, error) for reporting, without writing anything.

    A dry run of an agent whose location cannot be resolved (dsh without
    DSH_HOME) reports the path it *would* use - `$DSH_HOME/cordis.patch.yml` -
    instead of silently skipping, so `--all --dry-run` covers every writable
    agent.
    """
    chosen = choose_path(agent, environ)
    if chosen:
        return chosen, None
    candidates = agent.config_candidates(environ)
    if candidates:
        return candidates[0], None
    return agent.config_path, f"cannot resolve {agent.config_path} in this environment"


def write_entry(agent: Agent, env: Optional[Dict[str, str]] = None,
                spec: Optional[Dict[str, Any]] = None) -> Result:
    """Write the entry for real (used to verify a dry run, and by tests)."""
    return _install(agent, False, dict(os.environ if env is None else env), spec)


def _install(agent: Agent, dry_run: bool, environ: Optional[Dict[str, str]],
             spec: Optional[Dict[str, Any]]) -> Result:
    spec = spec or render_server_spec()
    name = server_name(agent, spec)
    if agent.mcp_style == "none":
        return Result(agent.id, STATUS_SKIPPED, "agent does not support MCP",
                      snippet=snippet_for(agent, spec))
    if agent.mcp_style == "cli-add":
        command = cli_command(agent, spec)
        return Result(agent.id, STATUS_SKIPPED, "no config file; run the command",
                      command=command, snippet=command)
    if agent.mcp_style == "unknown" or agent.framework_owned_config:
        # No verified location/shape, or the vendor CLI owns the file: hand
        # the user something correct to paste rather than guess at a write.
        reason = ("unverified MCP config" if agent.mcp_style == "unknown"
                  else "config owned by the agent's own CLI")
        return Result(agent.id, STATUS_SKIPPED,
                      f"{reason} ({agent.note or 'see registry'}); paste the "
                      "snippet yourself", snippet=snippet_for(agent, spec))

    requested = choose_path(agent, environ)
    if not requested:
        reason = agent.printed_only_reason or (
            f"cannot resolve {agent.config_path} in this environment "
            "(set the variable above if the agent is installed)")
        return Result(agent.id, STATUS_SKIPPED, reason,
                      snippet=snippet_for(agent, spec),
                      requested=agent.config_path)
    try:
        path = effective_path(requested)
    except OSError as exc:
        return Result(agent.id, STATUS_ERROR,
                      f"refusing to write, nothing changed ({exc})",
                      requested, requested=requested)
    via = "" if path == requested else f" (via symlink {requested})"

    shape = agent.server_spec
    if agent.mcp_style in JSON_STYLES:
        entry = render_json_entry(spec, shape)
    elif agent.mcp_style == "toml-table":
        entry = render_toml_entry(spec, shape)
    elif agent.mcp_style == "yaml":
        entry = render_yaml_entry(spec, shape)
    else:
        return Result(agent.id, STATUS_ERROR,
                      f"no writer for {agent.mcp_style}", requested)

    lock = None
    try:
        if not dry_run:
            lock = FileLock(path)
            lock.acquire()
        original = read_text(path)
        new_text, _ = plan_install(agent, path, entry, original, name)
    except _Already:
        return Result(agent.id, STATUS_EXISTS,
                      "already configured (identical entry); file untouched",
                      requested, requested=requested, effective=path)
    except _Conflict:
        return Result(agent.id, STATUS_CONFLICT,
                      f"a different {name!r} entry already exists; left untouched "
                      "in favour of it - remove it, or pass --name to register "
                      "under another name", requested, requested=requested,
                      effective=path)
    except Locked as exc:
        return Result(agent.id, STATUS_ERROR,
                      f"refusing to write, file untouched ({exc})", requested,
                      requested=requested, effective=path)
    except (ConfigError, YamlError, Refusal) as exc:
        return Result(agent.id, STATUS_ERROR,
                      f"refusing to write, file untouched ({exc})", requested,
                      requested=requested, effective=path,
                      snippet=snippet_for(agent, spec) if isinstance(exc, Refusal)
                      else "")
    except OSError as exc:
        return Result(agent.id, STATUS_ERROR, f"cannot read {path}: {exc}",
                      requested, requested=requested, effective=path)
    finally:
        if lock is not None and 'new_text' not in locals():
            lock.release()

    diff = _diff(requested, original or "", new_text)
    if dry_run:
        if lock is not None:
            lock.release()
        return Result(agent.id, STATUS_DRY_RUN,
                      "would write entry (no file changed)", requested,
                      diff=diff, requested=requested, effective=path)
    backup = None
    try:
        # Re-read under the lock: an editor may have saved between our read
        # and now. If it did, the plan is stale, so refuse instead of
        # clobbering the newer content.
        if read_text(path) != original:
            return Result(agent.id, STATUS_ERROR,
                          "refusing to write: the file changed while honcho was "
                          "preparing the edit; re-run", requested,
                          requested=requested, effective=path)
        # Checked under the lock, immediately before writing: a permission
        # check outside it could pass and then be revoked before the write.
        # os.replace() only needs the directory, so without this a config the
        # user made read-only would be "successfully" rewritten.
        if os.path.exists(path) and not os.access(path, os.W_OK):
            return Result(agent.id, STATUS_SKIPPED,
                          f"{path} is not writable (mode "
                          f"{oct(stat.S_IMODE(os.stat(path).st_mode))}); "
                          "paste the snippet yourself",
                          requested, requested=requested, effective=path,
                          snippet=snippet_for(agent, spec))
        if original is not None:
            backup = backup_file(path)
        atomic_write(path, new_text)
    except OSError as exc:
        return Result(agent.id, STATUS_ERROR, f"write failed: {exc}", requested,
                      backup=backup, requested=requested, effective=path)
    finally:
        if lock is not None:
            lock.release()
    msg = "entry written"
    if backup:
        msg += f"; backup {os.path.basename(backup)}"
    return Result(agent.id, STATUS_OK, msg + via, requested, backup=backup,
                  diff=diff, requested=requested, effective=path)


def read_entry(agent: Agent, environ: Optional[Dict[str, str]] = None,
               name: Optional[str] = None) -> Tuple[Optional[str], Any, Optional[str]]:
    """(path, stored entry or None, error or None).

    an error means the file is there but cannot be read as a config, which is
    a third state: `status` prints it as "?" rather than "no", because the fix
    is not "run install" (install will refuse).
    """
    name = name or SERVER_NAME
    path = choose_path(agent, environ)
    if not path:
        return None, None, None
    try:
        path = effective_path(path)
    except OSError as exc:
        return path, None, str(exc)
    try:
        text = read_text(path)
    except ConfigError as exc:
        return path, None, str(exc)
    if text is None:
        return path, None, None      # no file: readable absence, not an error
    if not text.strip():
        return path, None, "file is empty"
    try:
        if agent.mcp_style in JSON_STYLES:
            container = _entry_container(agent, load_jsonc(text))
            return path, (container.get(name) if isinstance(container, dict)
                          else None), None
        if agent.mcp_style == "toml-table":
            servers = load_toml(text).get("mcp_servers")
            return path, (servers.get(name) if isinstance(servers, dict)
                          else None), None
        if agent.mcp_style == "yaml":
            doc = parse_yaml_subset(text)
            if agent.server_spec.get("mode") == "per-server-file":
                return path, doc.get(name), None
            key = agent.server_spec.get("sequence_item_key", "id")
            wanted = {key: ("mcp-" + name)
                      if agent.server_spec.get("container") == "sequence" else name}
            matches = _yaml_existing(agent, _entry_container(agent, doc), wanted)
            return path, (matches[0] if len(matches) == 1 else None), None
        return path, None, None
    except (ConfigError, YamlError, Refusal) as exc:
        return path, None, str(exc)


def is_configured(agent: Agent, environ: Optional[Dict[str, str]] = None,
                  name: Optional[str] = None) -> Optional[bool]:
    """True/False, or None when it cannot be answered.

    None means: the location is unknown, or the file exists but does not
    parse (so "not configured" would be a guess). Callers print "?".
    """
    if agent.mcp_style in ("none", "unknown", "cli-add"):
        return None
    path, entry, error = read_entry(agent, environ, name)
    if path is None or error is not None:
        return None
    return entry is not None
