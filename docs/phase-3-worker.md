# Phase 3 — Worker Pool
**Estimated time: 3–4 hours**

Goal: Workers pull jobs from Redis, execute them, and update state. Retry logic is stubbed this phase — Phase 4 replaces the stub with real failure handling.

---

## Files to create this phase

```
worker/
├── executor.py   ← Step 3.1 [BUILD YOURSELF]
└── worker.py     ← Steps 3.2 + 3.3 [BUILD YOURSELF]
```

---

## Step 3.1 [BUILD YOURSELF] — `worker/executor.py`

**Deliverable**: A single function `execute_task(payload: dict) -> dict` that simulates job execution.

```python
import time
import random

def execute_task(payload: dict) -> dict:
    """
    Simulates task execution. In a real system this would call an external
    service, run a model, send a notification, process a document, etc.

    Returns a result dict on success.
    Raises RuntimeError on failure (simulated 20% of the time).

    The 20% failure rate exists so you can observe retry and DLQ behaviour
    without manually breaking anything — chaos engineering at the unit level.
    """
    time.sleep(random.uniform(0.1, 0.5))  # simulate variable-duration work
    if random.random() < 0.2:
        raise RuntimeError(f"Simulated task failure for payload: {payload}")
    return {"result": "completed", "processed_payload": payload}
```

### Why this interface

`execute_task` takes a `dict` and returns a `dict`. The worker does not know what kind of task it is running — that is the caller's concern. If you wanted to extend FlowCore to support multiple task types, you would add a `task_type` field to the payload and route inside `execute_task` (or replace it with a registry). Either way, the worker loop does not change.

---

## Step 3.2 [BUILD YOURSELF] — Core worker loop in `worker/worker.py`

**Deliverable**: `run_worker()` and `process_job()`. Write every single line.

### `run_worker()`

```python
def run_worker(redis_client):
    while True:
        # BRPOP blocks for up to 5 seconds waiting for a job.
        # Returns None on timeout (normal — loop restarts).
        # Returns (queue_name_bytes, job_id_bytes) when a job arrives.
        result = redis_client.brpop(QUEUE_NAME, timeout=5)
        if result is None:
            continue
        _, job_id_bytes = result
        job_id = job_id_bytes.decode("utf-8")
        process_job(redis_client, job_id)
```

**Why `timeout=5`**: A blocking BRPOP with no timeout would hold the connection open indefinitely. A 5-second timeout lets the worker loop restart, check for shutdown signals, and reconnect if Redis dropped the connection.

**Why decode**: Redis returns bytes. `job_id_bytes.decode("utf-8")` gives you a Python string. Everything downstream expects strings.

### `process_job()`

```python
import json
import time
from worker.executor import execute_task
from api.services.job_store import get_job, update_job_status, utcnow_iso

def process_job(redis_client, job_id: str) -> None:
    job = get_job(redis_client, job_id)

    # Guard: skip if job does not exist or is not in PENDING state.
    # This protects against duplicate delivery — if two workers somehow
    # receive the same job_id, only the first one past this check will run it.
    if job is None or job["status"] != "PENDING":
        logger.warning("job_skipped", job_id=job_id, reason="not_pending_or_missing")
        return

    payload = json.loads(job["payload"])
    job_start_time = time.time()

    update_job_status(redis_client, job_id, "RUNNING", started_at=utcnow_iso())
    logger.info("job_started", job_id=job_id, attempt=job["attempts"])

    try:
        result = execute_task(payload)
        update_job_status(
            redis_client, job_id, "COMPLETED",
            completed_at=utcnow_iso(),
            result=json.dumps(result),
        )
        logger.info("job_completed", job_id=job_id,
                    duration_seconds=round(time.time() - job_start_time, 3))

    except Exception as error:
        # Phase 4 replaces this stub with real retry + DLQ logic.
        logger.error("job_failed", job_id=job_id, error=str(error))
        update_job_status(redis_client, job_id, "FAILED", last_error=str(error))
```

### Every log line must have `job_id`

This is the observability contract for the whole system: every state transition produces exactly one structured log line, and every log line includes `job_id`. In production, this lets you reconstruct the full lifecycle of any job by grepping for its ID — without any additional tracing infrastructure.

---

## Step 3.3 [BUILD YOURSELF] — Worker concurrency via `threading`

**Deliverable**: Modified `worker.py` `__main__` block that spawns N threads.

```python
import os
import threading
import redis as redis_lib

if __name__ == "__main__":
    redis_client = redis_lib.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
    )

    concurrency = int(os.environ.get("WORKER_CONCURRENCY", 4))
    threads = [
        threading.Thread(target=run_worker, args=(redis_client,), daemon=True)
        for _ in range(concurrency)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
```

### Why threading (not multiprocessing)

The worker is **I/O-bound** — it spends most of its time waiting on Redis (BRPOP), not doing CPU work. Python's GIL (Global Interpreter Lock) prevents true parallel CPU execution across threads, but it releases the GIL during I/O operations. So for an I/O-bound workload, threads are fast, cheap, and share memory (the Redis client, the logger) without serialization overhead.

If `execute_task` were CPU-bound (image processing, ML inference, cryptographic computation), you would use `multiprocessing.Pool` or spawn separate worker containers. Know this trade-off — it comes up in every concurrency interview question.

### Why `daemon=True`

Daemon threads die automatically when the main process exits. Without `daemon=True`, the process would hang on `t.join()` after a SIGTERM or Ctrl+C because the BRPOP call would block until the timeout. With `daemon=True`, the container shuts down cleanly.

---

## Step 3.4 [CHECKPOINT] — Verify End-to-End Job Processing

Run: `docker-compose up --build`

**All four checks must pass before moving to Phase 4:**

**Check 1 — Jobs are processed**
Submit 10 jobs, wait 10–15 seconds, then check worker logs:
```bash
docker-compose logs worker
```
Expected: Structured JSON log lines for each job — `job_started` followed by `job_completed` (or `job_failed` for the ~20% that hit the simulated failure rate). Both are correct behaviour at this stage.

**Check 2 — Status reflects the outcome**
```bash
curl http://localhost:8000/jobs/<job_id>
```
Expected: `status` is `COMPLETED` or `FAILED`. It will never stay `PENDING` after workers drain the queue.

**Check 3 — Queue is drained**
```bash
docker exec flowcore-redis-1 redis-cli LLEN flowcore:jobs
```
Expected: `0` — all submitted jobs have been popped by workers.

**Check 4 — Horizontal scaling works**
```bash
docker-compose up --scale worker=3 -d
```
Submit 20 jobs. In the logs, confirm different container names appear on different `job_started` lines — proving multiple workers are processing in parallel with no double-processing.

---

## What you've built at the end of Phase 3

A functioning queue system:
- Jobs submitted via API are enqueued in Redis
- Workers continuously BRPOP from the queue, execute tasks, and update state
- Multiple workers run concurrently without stepping on each other
- Every state transition is logged with job ID

**The only gap**: failures result in a terminal `FAILED` state with no retry. Phase 4 fixes this.

**Next**: Phase 4 — Failure Handling (the interview-critical phase)
