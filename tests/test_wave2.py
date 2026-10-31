"""Wave 2: memory-quality contracts.

Covers SCOPE-WRITEONCE-1, WRITE-CLOBBER-1, REINFORCE-NEGATIVE-IMMUTABLE-1
and MAINT-UNCALLED-1 (maintenance wired into MCP + CLI).
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from omp_memory import db  # noqa: E402


def _make_conn(tmp: str):
    path = os.path.join(tmp, "m.db")
    conn = db.connect(path)
    db.init_db(conn, path)
    return conn


def _age(conn, rid: str, days: int):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE records SET updated_at=? WHERE id=?", (old, rid))
    conn.commit()


class TestScopeWriteOnce(unittest.TestCase):
    """SCOPE-WRITEONCE-1: re-saving under a new project must widen scope."""

    def test_project_id_merges_not_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "shared finding", source="s1",
                                  project_id="p1")["id"]
                db.remember(conn, "FACT", "shared finding", source="s2",
                            project_id="p2")
                rec = db.get_record(conn, rid)
                self.assertEqual(rec["project_ids"], "p1,p2")
                self.assertEqual(rec["project_id"], "p1,p2")
            finally:
                conn.close()

    def test_same_project_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "shared finding", source="s1",
                                  project_id="p1")["id"]
                db.remember(conn, "FACT", "shared finding", source="s1",
                            project_id="p1")
                self.assertEqual(db.get_record(conn, rid)["project_id"], "p1")
            finally:
                conn.close()


class TestWriteClobber(unittest.TestCase):
    """WRITE-CLOBBER-1: a weaker rewrite must not lower confidence/verified."""

    def test_confidence_never_decreases(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "the answer is 42",
                                  confidence=0.9)["id"]
                db.remember(conn, "FACT", "the answer is 42", confidence=0.3)
                self.assertEqual(db.get_record(conn, rid)["confidence"], 0.9)
            finally:
                conn.close()

    def test_verified_is_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "the answer is 42",
                                  verified=True)["id"]
                db.remember(conn, "FACT", "the answer is 42", verified=False)
                self.assertEqual(db.get_record(conn, rid)["verified"], 1)
            finally:
                conn.close()

    def test_higher_confidence_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "the answer is 42",
                                  confidence=0.3)["id"]
                db.remember(conn, "FACT", "the answer is 42", confidence=0.95)
                self.assertEqual(db.get_record(conn, rid)["confidence"], 0.95)
            finally:
                conn.close()


class TestReinforceNegativeImmutable(unittest.TestCase):
    """REINFORCE-NEGATIVE-IMMUTABLE-1: failure/decay never refresh a record."""

    def test_decay_does_not_bump_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "suspect claim")["id"]
                before = db.get_record(conn, rid)["updated_at"]
                db.reinforce(conn, rid, "decay")
                self.assertEqual(db.get_record(conn, rid)["updated_at"], before)
            finally:
                conn.close()

    def test_failure_does_not_bump_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "suspect claim")["id"]
                before = db.get_record(conn, rid)["updated_at"]
                db.reinforce(conn, rid, "failure")
                self.assertEqual(db.get_record(conn, rid)["updated_at"], before)
                self.assertLess(db.get_record(conn, rid)["confidence"], 0.5)
            finally:
                conn.close()

    def test_success_still_bumps_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                rid = db.remember(conn, "FACT", "solid claim")["id"]
                before = db.get_record(conn, rid)["updated_at"]
                time.sleep(0.01)
                db.reinforce(conn, rid, "success")
                self.assertGreater(db.get_record(conn, rid)["updated_at"], before)
            finally:
                conn.close()

    def test_forget_cold_ignores_decay_but_honours_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            try:
                decayed = db.remember(conn, "FACT", "discredited claim",
                                      confidence=0.15)["id"]
                db.reinforce(conn, decayed, "decay")
                _age(conn, decayed, 400)

                protected = db.remember(conn, FACT := "FACT", "recently confirmed claim",
                                        confidence=0.15)["id"]
                db.reinforce(conn, protected, "success")
                _age(conn, protected, 400)

                report = db.forget_cold(conn, apply=True)
                self.assertIn(decayed, report["candidates"])
                self.assertNotIn(protected, report["candidates"])
                self.assertIsNotNone(db.get_record(conn, protected))
                self.assertIsNone(db.get_record(conn, decayed))
            finally:
                conn.close()


class TestMaintenanceTools(unittest.TestCase):
    """MAINT-UNCALLED-1: decay/forget_cold/promote reachable from MCP + CLI."""

    def _spawn(self, env_extra):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env.update(env_extra)
        return subprocess.Popen(
            [sys.executable, "-m", "omp_memory.mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True)

    def test_mcp_maintenance_decay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "m.db")
            conn = _make_conn(tmp)
            rid = db.remember(conn, "INFERENCE", "probably a pattern",
                              confidence=0.9, verified=True)["id"]
            db.init_db  # noqa: B018 - keep the import used
            conn.close()

            proc = self._spawn({
                "OMP_MEMORY_DB": db_path,
                "OMP_MEMORY_BACKUP_DIR": os.path.join(tmp, "bk"),
                "HONCHO_TRANSPORT": "stdio",
            })
            try:
                def send(obj):
                    proc.stdin.write(json.dumps(obj) + "\n")
                    proc.stdin.flush()

                send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {}})
                json.loads(proc.stdout.readline())
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "maintenance",
                                 "arguments": {"op": "decay"}}})
                resp = json.loads(proc.stdout.readline())
                self.assertNotIn("error", resp)
                out = json.loads(resp["result"]["content"][0]["text"])
                self.assertIn("decayed", out)
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)

    def test_mcp_maintenance_forget_cold_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "m.db")
            conn = _make_conn(tmp)
            rid = db.remember(conn, "FACT", "cold and unloved",
                              confidence=0.15)["id"]
            _age(conn, rid, 400)
            conn.close()

            proc = self._spawn({
                "OMP_MEMORY_DB": db_path,
                "OMP_MEMORY_BACKUP_DIR": os.path.join(tmp, "bk"),
                "HONCHO_TRANSPORT": "stdio",
            })
            try:
                def send(obj):
                    proc.stdin.write(json.dumps(obj) + "\n")
                    proc.stdin.flush()

                send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {}})
                json.loads(proc.stdout.readline())
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "maintenance",
                                 "arguments": {"op": "forget_cold"}}})
                resp = json.loads(proc.stdout.readline())
                out = json.loads(resp["result"]["content"][0]["text"])
                self.assertEqual(out["forgotten"], 0)
                self.assertEqual(len(out["candidates"]), 1)
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)

    def test_cli_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "m.db")
            conn = _make_conn(tmp)
            rid = db.remember(conn, "FACT", "cold and unloved", confidence=0.15)["id"]
            _age(conn, rid, 400)
            conn.close()

            env = dict(os.environ)
            env["PYTHONPATH"] = str(SRC)
            env["OMP_MEMORY_DB"] = db_path
            env["OMP_MEMORY_BACKUP_DIR"] = os.path.join(tmp, "bk")
            out = subprocess.run(
                [sys.executable, "-m", "omp_memory.cli", "maintenance",
                 "forget_cold"],
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual(len(report["candidates"]), 1)

            out = subprocess.run(
                [sys.executable, "-m", "omp_memory.cli", "maintenance",
                 "forget_cold", "--apply"],
                capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual(report["forgotten"], 1)

    def test_cli_maintenance_unknown_op_rejected(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        out = subprocess.run(
            [sys.executable, "-m", "omp_memory.cli", "maintenance", "nonsense"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
