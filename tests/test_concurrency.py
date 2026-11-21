"""Concurrent reinforce must not lose signals.

Regression for the read-modify-write lost update: 8 threads reinforcing the
same record each read the same base confidence, so the final UPDATE kept only
the last signal. The clamp now happens inside the UPDATE statement.
"""
import threading

from omp_memory import db


def test_parallel_reinforce_no_lost_update(tmp_path):
    conn = db.connect(str(tmp_path / "m.db"))
    db.init_db(conn)
    rec = db.remember(conn, rtype="FACT", content="shared", confidence=0.5)
    rid = rec["id"]
    conn.close()

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        c = db.connect(str(tmp_path / "m.db"))
        # Explicit delta: this test isolates the lost-update regression.
        # Without it the spacing factor (massed repeats worth 0.1x) makes
        # the final confidence timing-dependent on slow CI runners.
        db.reinforce(c, rid, signal="success", delta=0.1)
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = db.connect(str(tmp_path / "m.db"))
    row = conn.execute("SELECT confidence FROM records WHERE id=?", (rid,)).fetchone()
    n = conn.execute("SELECT count(*) FROM reinforcement WHERE record_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert row[0] >= 0.9, f"lost update: confidence {row[0]} after 8 signals"
    assert n == 8, f"reinforcement rows {n}/8"
