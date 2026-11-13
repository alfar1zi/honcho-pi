"""PASS/FAIL checker over harness.py output.

Runs the harness once, parses its JSON, and asserts each group produced the
measurements it promises. A group whose API is absent inside the *module* is not
a harness failure: it is a finding, and is reported as SKIP with the reason.
Exit code is non-zero when any group is FAIL.

Run: python3 verify.py [--json PATH]   (default: run the harness now)
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "harness.py")


def run_harness():
    """Execute the harness in a child process and return its parsed stdout.

    A child process keeps the measured module's import-time constants from
    leaking into this checker, and proves the entrypoint works as advertised.
    """
    proc = subprocess.run([sys.executable, HARNESS], cwd=HERE,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("harness exited %d\nstderr:\n%s"
                         % (proc.returncode, proc.stderr))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("harness stdout is not JSON: %s" % exc)


def check_number(v, path):
    """Collect a failure unless v is a finite number; None means unmeasured."""
    if v is None:
        return ["%s: null (unmeasured)" % path]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return ["%s: not numeric (%r)" % (path, v)]
    return []


def check_group_a(grp):
    """Dedup must report a row count for every write mode it can reach."""
    fails = []
    if grp["status"] != "OK":
        return ["status %s: %s" % (grp["status"], grp.get("reason"))]
    modes = grp["data"].get("modes") or {}
    if not modes:
        return ["no write modes reported"]
    measured = 0
    for name, block in modes.items():
        if block.get("status") != "OK":
            continue
        measured += 1
        units = block.get("units_for_one_fact")
        fails += check_number(units, "A.%s.units" % name)
        fails += check_number(block.get("dedup_ratio"), "A.%s.dedup_ratio" % name)
        if units is not None and not (1 <= units <= grp["data"]["variants_written"]):
            fails.append("A.%s.units out of range: %s" % (name, units))
    if measured == 0:
        fails.append("A: no write mode measured (all API TIDAK ADA)")
    return fails


def check_group_b(grp):
    """Retrieval must produce R@5/hit@1/MRR/latency at both corpus sizes."""
    fails = []
    if grp["status"] != "OK":
        return ["status %s: %s" % (grp["status"], grp.get("reason"))]
    scale = grp["data"].get("scale") or {}
    if set(scale) != {"200", "2000"}:
        fails.append("B: expected scales 200 and 2000, got %s" % sorted(scale))
    for n, block in scale.items():
        for kind, res in block["results"].items():
            for key in ("recall_at_k", "hit_at_1", "mrr", "win_rate",
                        "latency_ms_p50", "latency_ms_p95"):
                fails += check_number(res.get(key), "B.N=%s.%s.%s" % (n, kind, key))
            r = res.get("recall_at_k")
            if r is not None and not (0.0 <= r <= 1.0):
                fails.append("B.N=%s.%s.recall out of [0,1]: %s" % (n, kind, r))
    return fails


def check_group_c(grp):
    """Token sizes must be reported for both estimators and the ratio."""
    fails = []
    if grp["status"] != "OK":
        return ["status %s: %s" % (grp["status"], grp.get("reason"))]
    data = grp["data"]
    fails += check_number(data.get("compact_over_full"), "C.compact_over_full")
    for side in ("full", "compact"):
        block = data.get(side) or {}
        for key in ("chars", "est_chars_div4", "est_words_x135"):
            fails += check_number(block.get(key), "C.%s.%s" % (side, key))
    if not data.get("rows"):
        fails.append("C: probe returned no rows")
    return fails


def check_group_d(grp):
    """Staleness must state whether a supersede marker exists and how it ranked."""
    fails = []
    if grp["status"] != "OK":
        return ["status %s: %s" % (grp["status"], grp.get("reason"))]
    data = grp["data"]
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        fails.append("D: no verdict string")
    if data.get("writer") == "API TIDAK ADA":
        fails.append("D: no write API (%s)" % data.get("write_reason"))
        return fails
    fails += check_number(data.get("distinct_rows"), "D.distinct_rows")
    ranking = data.get("ranking")
    if ranking is None:
        fails.append("D: ranking null (%s)" % data.get("ranking_reason"))
    elif not isinstance(ranking.get("verified_wins"), bool):
        fails.append("D: ranking.verified_wins not boolean")
    return fails


def check_group_e(grp):
    """Integrity must prove row/FTS agreement and that RULE/verified rows survive."""
    fails = []
    if grp["status"] != "OK":
        return ["status %s: %s" % (grp["status"], grp.get("reason"))]
    data = grp["data"]
    for key in ("records", "fts", "rule_rows", "verified_rows"):
        fails += check_number(data.get(key), "E.%s" % key)
    if data.get("quick_check") != "ok":
        fails.append("E: quick_check=%r" % data.get("quick_check"))
    if not data.get("records_equal_fts"):
        fails.append("E: records(%s) != fts(%s)" % (data.get("records"), data.get("fts")))
    if not data.get("rule_intact"):
        fails.append("E: RULE rows lost: %s/%s" % (data.get("rule_rows"), data.get("rule_expected")))
    if not data.get("verified_intact"):
        fails.append("E: verified rows lost: %s/%s"
                     % (data.get("verified_rows"), data.get("verified_expected")))
    return fails


CHECKS = (
    ("A_dedup", check_group_a),
    ("B_retrieval", check_group_b),
    ("C_token", check_group_c),
    ("D_staleness", check_group_d),
    ("E_integrity", check_group_e),
)


def main(argv):
    """Run, check, print PASS/FAIL/SKIP per group, exit non-zero on any FAIL."""
    path = None
    if "--json" in argv:
        path = argv[argv.index("--json") + 1]
    report = run_harness() if path is None else json.load(open(path))
    if path:
        # A file without a module digest is not attributable to a code state.
        if not report.get("module_md5"):
            raise SystemExit("%s: missing module_md5 header" % path)

    print("module: %s  md5: %s" % (report.get("measured_module"), report.get("module_md5")))
    failures = 0
    for name, checker in CHECKS:
        grp = report["groups"].get(name)
        if grp is None:
            print("%-12s FAIL  missing from report" % name)
            failures += 1
            continue
        if grp["status"] == "API TIDAK ADA":
            print("%-12s SKIP  %s" % (name, grp.get("reason")))
            continue
        problems = checker(grp)
        if problems:
            failures += 1
            print("%-12s FAIL" % name)
            for p in problems:
                print("             - %s" % p)
        else:
            print("%-12s PASS" % name)
    print("%d/%d groups failed" % (failures, len(CHECKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
