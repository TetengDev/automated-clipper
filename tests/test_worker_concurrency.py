"""Two worker processes on one DB file (CLP-34): every job stage runs exactly once.

Real processes, not threads or mocks: each runs `Worker.run_once` against the shared
SQLite file with stub stages that record their calls. The lease is 4 s, but `lease_until`
is stored to the whole second, so a claim lasts 3 to 4 s; the heartbeat renews it every
1 s, which leaves at least two beats of slack. One stage takes 5.5 s, longer than any
lease, while the other worker has little else to do and polls every 50 ms: without the
heartbeat it takes that stage over and runs it a second time.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from clipper.db import connect, migrate
from clipper.jobs import JobStatus, enqueue
from clipper.urls import SourceRef

JOBS = 20
STAGES = ("fetching_audio", "transcribing", "selecting", "fetching_clips", "rendering")
WORKER = """
import sys, time
from datetime import timedelta
from pathlib import Path
from clipper.config import AppConfig
from clipper.db import connect
from clipper.jobs import JobStatus, StageDef, Worker
from clipper.jobs.stages import DEFAULT_STAGES

db, media, calls, ready, worker_id = sys.argv[1:6]

def stage(status):
    def run(job, ctx):
        with open(calls, "a") as fh:
            fh.write(f"{job.id} {status.value} {worker_id}\\n")
        slow = status is JobStatus.TRANSCRIBING and job.id == 7
        time.sleep(5.5 if slow else 0.01)  # outlives the lease
        (ctx.media_dir / f"{status.value}.done").write_text(worker_id)
    return StageDef(run, f"{status.value}.done")

stages = {**DEFAULT_STAGES, **{JobStatus(s): stage(JobStatus(s)) for s in sys.argv[6:]}}
conn = connect(Path(db))
worker = Worker(
    conn=conn, config=AppConfig(), worker_id=worker_id, stages=stages,
    media_root=Path(media), lease=timedelta(seconds=4),
)
(Path(ready) / worker_id).touch()
while len(list(Path(ready).iterdir())) < 2:
    time.sleep(0.01)
while True:
    if worker.run_once():
        continue
    open_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('clips_ready', 'failed', 'skipped')"
    ).fetchone()[0]
    if not open_jobs:
        break
    time.sleep(0.05)
"""


def test_two_worker_processes_run_each_job_stage_exactly_once(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    now = datetime.now(UTC)
    with closing(connect(db)) as conn:
        migrate(conn)
        for n in range(JOBS):
            enqueue(conn, SourceRef("youtube", f"vid{n:08d}"), now, creator="manual")
    calls = tmp_path / "calls.log"
    ready = tmp_path / "ready"
    ready.mkdir()
    argv = [sys.executable, "-c", WORKER, str(db), str(tmp_path / "media"), str(calls), str(ready)]
    procs = [subprocess.Popen([*argv, wid, *STAGES]) for wid in ("w1", "w2")]  # noqa: S603
    try:
        codes = [p.wait(timeout=120) for p in procs]
    finally:
        for p in procs:
            p.kill()

    assert codes == [0, 0]
    lines = [line.split() for line in calls.read_text().splitlines()]
    runs = Counter((int(job_id), status) for job_id, status, _ in lines)
    assert runs == Counter({(j, s): 1 for j in range(1, JOBS + 1) for s in STAGES})
    assert {wid for *_, wid in lines} == {"w1", "w2"}  # both took part
    with closing(connect(db)) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM jobs")]
    assert statuses == [JobStatus.CLIPS_READY.value] * JOBS
