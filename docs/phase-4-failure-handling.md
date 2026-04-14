# Phase 4 — Failure Handling
**Estimated time: 4–5 hours**

This is the most interview-critical phase in the project. Every question about "what happens when a worker crashes?", "how do you prevent retry storms?", or "what's a dead-letter queue?" is answered by this code. **Write every line. Understand every decision.**

---

## Files to create this phase

```
worker/
├── retry.py      ← Step 4.1 [BUILD YOURSELF] [INTERVIEW GOLD]
├── scheduler.py  ← Step 4.2 [BUILD YOURSELF] [INTERVIEW GOLD]
├── dlq.py        ← Step 4.3 [BUILD YOURSELF]
└── worker.py     ← Step 4.4 [BUILD YOURSELF] [INTERVIEW GOLD] (replace stub)
```

---

## The complete job state machine

Print this and keep it visible while implementing:

```
PENDING ──► RUNNING ──► COMPLETED
                │
           exception
                │
                ▼
         increment attempts
                │
        ┌───────┴────────┐
        │                │
  attempts < max_retries  attempts >= max_retries
        │                │
        ▼                ▼
    PENDING            DEAD ──► RabbitMQ DLQ
  (delayed set)
        │
  [scheduler promotes
   when timestamp passes]
        │
        ▼
     PENDING (main queue)
```

Every status transition has exactly one log line. Every log line includes `job_id`.

---

## Step 4.1 [BUILD YOURSELF] [INTERVIEW GOLD] — `worker/retry.py`

**Deliverable**: Two functions implementing exponential backoff.

```python
import time

BASE_DELAY_SECONDS = 2

def calculate_backoff(attempt: int) -> float:
    """
    Returns delay in seconds before the next retry attempt.

    Formula: BASE_DELAY * 2^attempt, capped at 60 seconds.
      attempt=0 → 2s
      attempt=1 → 4s
      attempt=2 → 8s
      attempt=3 → 16s
      attempt=4 → 32s
      attempt=5 → 60s (cap)

    This is binary exponential backoff — the same algorithm used in
    TCP retransmission (RFC 6298) and AWS SQS visibility timeout extension.

    The 60-second cap prevents unbounded delays. Without it, attempt=10
    would produce a 17-minute delay — acceptable for a batch job,
    unacceptable for a payment notification or alert system.
    """
    return min(BASE_DELAY_SECONDS * (2 ** attempt), 60)


def schedule_retry(redis_client, job_id: str, attempt: int,
                   delayed_set: str, queue_name: str) -> None:
    """
    Schedules a job for future retry via a Redis Sorted Set.

    The sorted set score is a Unix timestamp (execute_after).
    A scheduler process polls the set and moves ready jobs back
    to the main queue when their timestamp has passed.

    This is the delayed job pattern — identical to how Sidekiq,
    Celery beat, and BullMQ implement scheduled retries.
    """
    delay = calculate_backoff(attempt)
    execute_at = time.time() + delay
    redis_client.zadd(delayed_set, {job_id: execute_at})
```

### Interview prep: thundering herd

If 100 workers all fail at the same time and retry immediately (linear backoff), they all hit the downstream service simultaneously again — the same overload condition that caused the failures. Exponential backoff spreads retries across a growing time window, reducing concurrent pressure on the dependency.

**Production enhancement — jitter**: Add `random.uniform(-0.5, 0.5) * delay` to `execute_at`. If multiple jobs were submitted simultaneously and fail together, they would otherwise retry at exactly the same time even with backoff. Jitter desynchronises them. AWS's "[Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)" article is the canonical reference.

---

## Step 4.2 [BUILD YOURSELF] [INTERVIEW GOLD] — `worker/scheduler.py`

**Deliverable**: The background thread that promotes delayed jobs back to the main queue.

```python
import time
import logging
from api.services.job_store import update_job_status

logger = logging.getLogger(__name__)

def run_scheduler(redis_client, delayed_set: str, queue_name: str) -> None:
    """
    Runs in a background daemon thread.

    Every second:
      1. ZRANGEBYSCORE delayed_set 0 <now>  — find jobs whose timestamp has passed
      2. ZREM delayed_set job_id            — remove from delayed set
      3. LPUSH queue_name job_id            — promote to main queue

    The ZREM-before-LPUSH ordering is a deliberate design decision.
    See the critical comment below.
    """
    while True:
        now = time.time()
        ready_jobs = redis_client.zrangebyscore(delayed_set, 0, now)

        for job_id_bytes in ready_jobs:
            job_id = job_id_bytes.decode("utf-8") if isinstance(job_id_bytes, bytes) else job_id_bytes

            # CRITICAL: ZREM before LPUSH.
            #
            # If we crash between ZREM and LPUSH: the job is lost.
            #   → at-most-once delivery (may miss a retry)
            #
            # If we LPUSH before ZREM and crash between the two: the job is
            # promoted AND still in the delayed set → double processing.
            #   → at-least-once delivery (may process twice)
            #
            # FlowCore chooses at-most-once for simplicity.
            # Financial systems (payments, ledger writes) typically choose
            # at-least-once + idempotency keys to ensure no operation is
            # silently skipped. Know this trade-off cold.
            redis_client.zrem(delayed_set, job_id)
            redis_client.lpush(queue_name, job_id)
            update_job_status(redis_client, job_id, "PENDING")
            logger.info("job_retry_promoted", job_id=job_id)

        time.sleep(1)
```

### Interview prep: why a sorted set and not a second list

A second Redis List would give you FIFO order — but you need **time-ordered** access. You want jobs sorted by when they should execute, and you need to efficiently query "all jobs ready to run now." `ZRANGEBYSCORE set 0 <now>` does this in O(log N + M) where M is the number of ready jobs. A list gives you no way to efficiently find the right items without scanning the whole thing.

The sorted set with score-as-timestamp pattern is used by every major job queue library. Knowing the data structure behind the feature is the answer that distinguishes senior engineers from junior ones.

---

## Step 4.3 [BUILD YOURSELF] — `worker/dlq.py`

**Deliverable**: `publish_to_dlq(job_id, job_data)` — publishes a permanently failed job to RabbitMQ.

```python
import json
import os
import pika

def publish_to_dlq(job_id: str, job_data: dict) -> None:
    """
    Publishes a permanently failed job to the RabbitMQ dead-letter queue.

    Called when a job has exhausted all retry attempts.

    RabbitMQ provides durable message storage — messages persist on disk
    until explicitly consumed or expired. This gives operations teams a
    durable audit trail of every job that failed permanently, and a
    starting point for manual reprocessing or alerting.
    """
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.environ["RABBITMQ_HOST"],
            port=int(os.environ["RABBITMQ_PORT"]),
            credentials=pika.PlainCredentials(
                os.environ["RABBITMQ_USER"],
                os.environ["RABBITMQ_PASSWORD"],
            ),
        )
    )
    channel = connection.channel()

    # durable=True: the queue survives RabbitMQ restarts.
    # If you declare durable=False and the broker restarts, the queue and
    # all its messages are gone.
    channel.queue_declare(queue=os.environ["RABBITMQ_DLQ_QUEUE"], durable=True)

    channel.basic_publish(
        exchange="",
        routing_key=os.environ["RABBITMQ_DLQ_QUEUE"],
        body=json.dumps({"job_id": job_id, "job_data": job_data}),
        properties=pika.BasicProperties(
            delivery_mode=2,  # Persistent message — survives broker restart.
                              # delivery_mode=1 is transient (memory only).
        ),
    )
    connection.close()
```

### Why `durable=True` AND `delivery_mode=2`

These are two separate durability guarantees:

- `durable=True` (queue declaration) — the **queue definition** survives a RabbitMQ restart. Without it, the queue disappears on restart and new messages have nowhere to go.
- `delivery_mode=2` (message properties) — the **message content** is written to disk before RabbitMQ acknowledges receipt. Without it, messages are stored in memory only and are lost if the broker crashes before flushing.

You need both. A durable queue with non-persistent messages still loses messages on crash. Persistent messages in a non-durable queue still lose the queue definition on restart (the messages would be orphaned).

---

## Step 4.4 [BUILD YOURSELF] [INTERVIEW GOLD] — Wire failure handling into `worker/worker.py`

**Deliverable**: Replace the `handle_failure` stub from Phase 3 with the full implementation. Also add the scheduler thread.

### `handle_failure()`

```python
from worker.retry import calculate_backoff, schedule_retry
from worker.dlq import publish_to_dlq
from api.services.job_store import get_job, update_job_status, increment_attempts, utcnow_iso
import os

DELAYED_SET = os.environ["REDIS_DELAYED_SET"]
QUEUE_NAME = os.environ["REDIS_QUEUE_NAME"]

def handle_failure(redis_client, job_id: str, error: Exception) -> None:
    """
    Called when execute_task raises an exception.

    Decision tree:
      1. Atomically increment attempt counter
      2. If attempts < max_retries: schedule retry with exponential backoff
      3. If attempts >= max_retries: mark DEAD, publish to RabbitMQ DLQ
    """
    attempts = increment_attempts(redis_client, job_id)
    job = get_job(redis_client, job_id)
    max_retries = int(job["max_retries"])

    logger.warning("job_failed",
                   job_id=job_id,
                   attempt=attempts,
                   max_retries=max_retries,
                   error=str(error))

    if attempts < max_retries:
        update_job_status(redis_client, job_id, "PENDING", last_error=str(error))
        schedule_retry(redis_client, job_id, attempts, DELAYED_SET, QUEUE_NAME)
        logger.info("job_scheduled_retry",
                    job_id=job_id,
                    attempt=attempts,
                    backoff_seconds=calculate_backoff(attempts))
    else:
        update_job_status(redis_client, job_id, "DEAD",
                          last_error=str(error),
                          failed_at=utcnow_iso())
        publish_to_dlq(job_id, job)
        logger.error("job_dead",
                     job_id=job_id,
                     total_attempts=attempts,
                     message="Published to RabbitMQ DLQ")
```

### Add scheduler thread to `__main__`

```python
if __name__ == "__main__":
    from worker.scheduler import run_scheduler
    import threading

    redis_client = ...  # same as Phase 3

    # Start scheduler as a daemon thread before worker threads
    scheduler_thread = threading.Thread(
        target=run_scheduler,
        args=(redis_client, DELAYED_SET, QUEUE_NAME),
        daemon=True,
    )
    scheduler_thread.start()

    # Then start worker threads (same as Phase 3)
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", 4))
    ...
```

### Why `max_retries` is per-job, not global

A payment notification might need 5 retries before giving up. A cache warming job might tolerate only 1. A DLQ-sensitive audit log write might have 0 retries (fail immediately to DLQ). Per-job configuration makes FlowCore a general-purpose queue, not a single-policy system. The global `MAX_RETRIES` env var is the default when `max_retries` is not specified in the request body.

### Why status resets to `PENDING` before a retry (not `FAILED`)

The job will be processed again. Setting it to `FAILED` would make the status lie — the job is not permanently failed, it is waiting for another attempt. When the scheduler promotes it back to the main queue, the worker's guard clause checks for `status == "PENDING"` before processing. If the status were `FAILED`, the worker would skip it.

### What `DEAD` communicates that `FAILED` does not

`FAILED` means "the most recent execution attempt failed." `DEAD` means "this job has exhausted all retries and will never be automatically retried again." `DEAD` is the terminal state that triggers the DLQ publish. Without a distinct `DEAD` status, you cannot easily query for "all jobs that need human intervention."

---

## Step 4.5 [CHECKPOINT] — Verify Full Failure Handling Pipeline

Run: `docker-compose up --build`

**All six checks must pass before moving to Phase 5:**

**Check 1 — Retries are scheduled and logged**
Submit 20 jobs. Wait 2–3 minutes. In `docker-compose logs worker`, you should see:
- `job_failed` — when execute_task raises
- `job_scheduled_retry` — with `backoff_seconds` field
- `job_started` — when the scheduler promotes and a worker picks it up again
- Eventually `job_completed` or `job_dead`

**Check 2 — Delayed set is active**
While jobs are processing, run in Redis CLI:
```bash
ZRANGE flowcore:delayed 0 -1 WITHSCORES
```
You should see job IDs appearing (when retries are scheduled) and disappearing (when the scheduler promotes them).

**Check 3 — DLQ receives permanently failed jobs**
Open `http://localhost:15672` → Queues tab → `flowcore:dlq`.
Message count should be non-zero after jobs exhaust retries.

**Check 4 — DLQ message content is correct**
In the RabbitMQ management UI, click the `flowcore:dlq` queue → "Get Messages".
The message body should be valid JSON containing `job_id` and `job_data`.

**Check 5 — Dead job status is correct**
```bash
curl http://localhost:8000/jobs/<dead_job_id>
```
Expected: `"status": "DEAD"` with `last_error` field populated.

**Check 6 — Zero-retry path**
In `.env`, set `MAX_RETRIES=0`. Restart: `docker-compose up -d`. Submit 10 jobs.
All failing jobs should go to DLQ immediately — no `job_scheduled_retry` log lines.

---

## What you've built at the end of Phase 4

The complete fault-tolerance layer:
- Transient failures retry with exponential backoff
- Permanently failed jobs are routed to a durable dead-letter queue in RabbitMQ
- Every failure decision is logged, traceable, and auditable
- The system is resilient to Redis and RabbitMQ restarts (durable messages)

This is the section of the project you demo and narrate in every interview. Know the state machine. Know the ZREM/LPUSH ordering. Know why per-job max_retries.

**Next**: Phase 5 — Observability
