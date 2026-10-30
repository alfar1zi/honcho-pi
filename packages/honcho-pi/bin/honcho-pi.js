#!/usr/bin/env node
// honcho-pi - one-command setup and auto-update for the honcho-pi memory CLI.
//
//   npx honcho-pi@latest setup       install (POSIX or Windows, platform detected)
//   npx honcho-pi@latest update      upgrade an existing install to the newest release
//   npx honcho-pi@latest status      show installed version vs newest release
//
// Requires Python 3.10+ on PATH; everything else is downloaded by the installer.

import { main } from "../lib/main.js";

main(process.argv.slice(2)).catch((err) => {
  console.error(`error: ${err.message ?? err}`);
  process.exit(1);
});
