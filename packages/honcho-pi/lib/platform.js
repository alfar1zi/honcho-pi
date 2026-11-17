// Platform facts the installer needs, in one place.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

export const IS_WINDOWS = process.platform === "win32";

export function pythonCandidates() {
  return IS_WINDOWS ? ["python", "py", "python3"] : ["python3", "python"];
}

// First interpreter on PATH that is Python 3.10 or newer.
export function findPython() {
  for (const cmd of pythonCandidates()) {
    const probe = spawnSync(cmd, ["-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"], {
      encoding: "utf8",
      shell: IS_WINDOWS,
    });
    if (probe.status === 0) {
      const ver = Number(probe.stdout.toString().split(/\s+/).filter(Boolean).pop());
      if (ver >= 310) return { cmd, ver };
    }
  }
  return null;
}

export function defaultInstallDir() {
  if (process.env.OMP_MEMORY_HOME) return process.env.OMP_MEMORY_HOME;
  if (IS_WINDOWS) return path.join(process.env.APPDATA ?? path.join(process.env.USERPROFILE ?? "~", "AppData", "Roaming"), "omp-memory");
  return path.join(process.env.HOME ?? "~", ".omp", "memory");
}

// venv layout differs per platform; callers use these to build tool paths.
export function venvBin(dir) {
  return IS_WINDOWS ? path.join(dir, "Scripts") : path.join(dir, "bin");
}

export function venvPython(dir) {
  return path.join(venvBin(dir), IS_WINDOWS ? "python.exe" : "python");
}

export function onPath(file) {
  return existsSync(file);
}
