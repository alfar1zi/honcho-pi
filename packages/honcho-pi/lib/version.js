// Version discovery against the GitHub repository.
//
// honcho-pi has no npm releases and no releases list; the source of truth is
// the default branch. We pin installs to an exact commit so that a later
// "update" has a deterministic "from" version to compare and upgrade.

import { execFileSync } from "node:child_process";

const REPO = "alfar1zi/honcho-pi";

// Latest commit hash of origin/main, or null when offline.
export function latestSha() {
  try {
    return execFileSync("git", ["ls-remote", `https://github.com/${REPO}.git`, "HEAD"], {
      encoding: "utf8",
      timeout: 15_000,
    })
      .split(/\s+/)[0]
      .trim() || null;
  } catch {
    return null;
  }
}

export function repoUrl() {
  return `https://github.com/${REPO}`;
}

// What this install is running: best effort, never fatal.
export function installedVersion(markerFile, { readFileSync }) {
  try {
    return JSON.parse(readFileSync(markerFile, "utf8")).sha;
  } catch {
    return null;
  }
}
