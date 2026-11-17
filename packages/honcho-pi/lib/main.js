// CLI routing: command name -> handler.

import path from "node:path";
import { defaultInstallDir } from "./platform.js";
import { setup, status, update } from "./installer.js";

const HELP = `honcho-pi - local-first SQLite memory for coding agents.

Usage:
  npx honcho-pi@latest setup     install (or reinstall) into ~/.omp/memory
  npx honcho-pi@latest update    upgrade the install to the newest release
  npx honcho-pi@latest status    compare installed vs newest

All commands work on Linux, macOS, and Windows. Python 3.10+ must be on
PATH; the installer creates a virtualenv in the install directory so the
system environment is never touched.

The installed CLI lives at <install>/venv/bin/omp-memory (Linux/macOS) or
<install>/venv\\Scripts\\omp-memory.exe (Windows).
`;

export async function main(argv) {
  const cmd = argv[0] ?? "setup";
  const log = (m) => console.log(m);

  if (cmd === "help" || cmd === "--help" || cmd === "-h") {
    console.log(HELP);
    return;
  }

  const ctx = { installDir: defaultInstallDir() };

  if (cmd === "setup") {
    setup(ctx, log);
    log("done. try: " + path.join(ctx.installDir, "venv") + " -> omp-memory health");
    return;
  }
  if (cmd === "update") {
    update(ctx, log);
    return;
  }
  if (cmd === "status") {
    status(ctx, log);
    return;
  }
  throw new Error(`unknown command '${cmd}'. try: npx honcho-pi@latest setup`);
}
