// Install and update logic.
//
// Strategy: a virtualenv in the install dir, pip install from the repository
// pinned to an exact commit. Cross-platform because everything runs through
// the venv python executable, never a shell.

import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { findPython, venvBin, venvPython } from "./platform.js";
import { latestSha, repoUrl } from "./version.js";

function fetchSource(sha, target) {
  rmSync(target, { recursive: true, force: true });
  const clone = spawnSync("git", ["clone", "--quiet", "--filter=blob:none", repoUrl(), target], { stdio: "inherit" });
  if (clone.status !== 0) throw new Error("git clone failed");
  const co = spawnSync("git", ["-C", target, "checkout", "--quiet", sha], { stdio: "inherit" });
  if (co.status !== 0) throw new Error(`git checkout ${sha} failed`);
}

function makeVenv(py, venvDir) {
  rmSync(venvDir, { recursive: true, force: true });
  const created = spawnSync(py.cmd, ["-m", "venv", venvDir], { stdio: "inherit" });
  if (created.status !== 0) throw new Error("venv creation failed (on Debian/Ubuntu install the python3-venv package)");
}

function pipInstall(venvDir, srcDir) {
  const py = venvPython(venvDir);
  const run = spawnSync(py, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", srcDir], { stdio: "inherit" });
  if (run.status !== 0) throw new Error("pip install failed");
}

function smokeTest(venvDir) {
  const cli = path.join(venvBin(venvDir), process.platform === "win32" ? "omp-memory.exe" : "omp-memory");
  if (!existsSync(cli)) throw new Error(`console script missing after install: ${cli}`);
  const health = spawnSync(cli, ["health"], { encoding: "utf8" });
  if (health.status !== 0) throw new Error(`omp-memory health failed:\n${health.stdout}\n${health.stderr}`);
}

// The venv bin dir is not on PATH, so a bare "omp-memory-mcp" in an MCP
// config never resolves. Write env.sh and print the absolute-path config.
function writeEnv(installDir, venvDir) {
  const bin = venvBin(venvDir);
  const envSh = path.join(installDir, "env.sh");
  writeFileSync(envSh, `export PATH="${bin}:$PATH"\n`);
  return envSh;
}

function mcpConfigBlock(venvDir) {
  const mcp = path.join(venvBin(venvDir), process.platform === "win32" ? "omp-memory-mcp.exe" : "omp-memory-mcp");
  return JSON.stringify({ mcpServers: { memory: { command: mcp } } }, null, 2);
}

// The graph UI ships no dist in the pip package; build it when npm exists.
// Best-effort: the memory engine works without the UI.
function buildWebUi(srcDir, installDir, log) {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const webDir = path.join(srcDir, "web");
  if (!existsSync(path.join(webDir, "package.json"))) return;
  const inst = spawnSync(npm, ["--prefix", webDir, "install", "--silent"], { stdio: "inherit" });
  if (inst.status !== 0) { log("web UI deps failed; skipping the graph view (memory engine unaffected)"); return; }
  const build = spawnSync(npm, ["--prefix", webDir, "run", "build"], { stdio: "inherit" });
  if (build.status !== 0) { log("web UI build failed; skipping the graph view"); return; }
  const dest = path.join(installDir, "web");
  mkdirSync(dest, { recursive: true });
  rmSync(path.join(dest, "dist"), { recursive: true, force: true });
  cpSync(path.join(webDir, "dist"), path.join(dest, "dist"), { recursive: true });
  log(`web UI deployed to ${dest}/dist (OMP_MEMORY_WEB_DIR=${dest})`);
}

function markerPath(installDir) {
  return path.join(installDir, "install.json");
}

function readSha(installDir) {
  try {
    return JSON.parse(readFileSync(markerPath(installDir), "utf8")).sha;
  } catch {
    return null;
  }
}

export function setup({ installDir }, log) {
  const py = findPython();
  if (!py) throw new Error("Python 3.10+ not found on PATH. Install Python, then re-run.");
  log(`python: ${py.cmd}`);

  const sha = latestSha();
  if (!sha) throw new Error("cannot reach github.com to pin the install. Check the network and re-run.");
  log(`installing honcho-pi @ ${sha.slice(0, 10)}`);

  mkdirSync(installDir, { recursive: true });
  const src = mkdtempSync(path.join(os.tmpdir(), "honcho-pi-src-"));
  const venvDir = path.join(installDir, "venv");
  let envSh;
  try {
    fetchSource(sha, src);
    makeVenv(py, venvDir);
    pipInstall(venvDir, src);
    smokeTest(venvDir);
    envSh = writeEnv(installDir, venvDir);
    buildWebUi(src, installDir, log);
    writeFileSync(markerPath(installDir), JSON.stringify({ sha, venv: venvDir }, null, 2) + "\n");
  } finally {
    rmSync(src, { recursive: true, force: true });
  }
  log(`installed to ${venvDir}`);
  log(`shell PATH: source ${envSh}`);
  log("MCP config (absolute path, works without PATH changes):");
  log(mcpConfigBlock(venvDir));
  return { sha, venvDir };
}

export function update({ installDir }, log) {
  const current = readSha(installDir);
  const newest = latestSha();
  if (!newest) throw new Error("cannot reach github.com to check for updates");
  if (current === newest) {
    log(`already at ${newest.slice(0, 10)}, nothing to do`);
    return { updated: false, sha: newest };
  }
  log(current ? `upgrading ${current.slice(0, 10)} -> ${newest.slice(0, 10)}` : `no local marker, installing ${newest.slice(0, 10)}`);
  const r = setup({ installDir }, log);
  return { updated: true, sha: newest };
}

export function status({ installDir }, log) {
  const current = readSha(installDir);
  const newest = latestSha();
  log(`installed: ${current ? current.slice(0, 10) : "none"}`);
  log(`newest:    ${newest ? newest.slice(0, 10) : "unreachable"}`);
  if (current && newest) log(current === newest ? "up to date" : "update available: npx honcho-pi@latest update");
  return { current, newest };
}
