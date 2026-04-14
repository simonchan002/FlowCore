# Interview Preparation — FlowCore

This file consolidates every concept you need to articulate fluently in an interview about this project.

---

## The three [INTERVIEW GOLD] steps

### 1. Exponential Backoff (`worker/retry.py`)

**The formula**: `delay = min(BASE * 2^attempt, cap)`

| attempt | delay |
|---|---|
| 0 | 2s |
| 1 | 4s |
| 2 | 8s |
| 3 | 16s |
| 4 | 32s |
| 5+ | 60s (capped) |

**Why exponential, not linear?**
Thundering herd: if 100 workers fail simultaneously and retry linearly (every 5 seconds), they all hit the downstream service simultaneously again — recreating the exact overload that caused the failure. Exponential backoff spreads retries across a growing window, reducing concurrent pressure.

**What's jitter?**
Adding `random.uniform(-0.5, 0.5) * delay` to the scheduled time. If multiple jobs fail together (e.g., during a downstream outage), they would retry at the exact same time even with exponential backoff. Jitter desynchronises them. This is the topic of the canonical [AWS architecture blog post on backoff and jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).

**Where else is this used?**
- TCP retransmission (RFC 6298)
- AWS SQS visibility timeout extension
- HTTP 429 Retry-After header handling
- Kubernetes pod restart backoff

---

### 2. Delayed Sorted Set (`worker/scheduler.py`)

**The data structure**: Redis Sorted Set, score = Unix timestamp of when to execute.

**Why a sorted set and not a second queue?**
You need time-ordered access: "give me all jobs whose execute_at timestamp is in the past." A Redis List gives you FIFO order — there's no way to efficiently query by score without scanning everything. `ZRANGEBYSCORE delayed_set 0 <now>` does this in O(log N + M) where M is the number of ready jobs.

**The two-structure design**:
```
Main queue (Redis List):    ready-to-run jobs → BRPOP by workers
Delayed set (Sorted Set):   scheduled-for-future jobs → scheduler polls every 1s
```

**ZREM before LPUSH — the critical ordering decision**:

| Order | If crash between operations | Delivery semantic |
|---|---|---|
| ZREM → LPUSH (FlowCore) | Job is lost | at-most-once |
| LPUSH → ZREM | Job is double-processed | at-least-once |

FlowCore chooses at-most-once for simplicity. Production financial systems typically choose at-least-once with idempotency keys (e.g., a payment request with a unique `payment_id` that the processor deduplicates).

**Where else is this pattern used?**
- Sidekiq (Ruby) uses a Redis sorted set for scheduled jobs
- Celery beat uses a similar mechanism
- BullMQ (Node.js) uses Redis sorted sets for delayed jobs

---

### 3. Full Failure Decision Tree (`worker/worker.py` — `handle_failure`)

Draw this on the whiteboard:

```
execute_task(payload)
        │
    exception
        │
increment_attempts()  ← atomic HINCRBY, returns new count
        │
  new_attempts < max_retries?
   ┌────┴────┐
  YES       NO
   │         │
schedule    update status → DEAD
 retry      publish to RabbitMQ DLQ
   │
update status → PENDING
   │
zadd to delayed set
(score = now + backoff)
   │
[scheduler promotes when timestamp passes]
   │
lpush to main queue
   │
worker BRPOPs, processes again
```

**Why `max_retries` is per-job, not global?**
Different job types have different retry tolerances. A payment notification might retry 5 times. A cache-warming job might retry 0 times (fail fast — it's not critical). A DLQ-sensitive audit log write might retry 10 times. Per-job config makes FlowCore generic. The `MAX_RETRIES` env var is the default only.

**Why status resets to `PENDING` before scheduling a retry (not `FAILED`)?**
`FAILED` would be a lie — the job is not permanently failed, it is waiting for another attempt. The worker's guard clause checks `status == "PENDING"` before processing. If the status were `FAILED`, the worker would skip the job when the scheduler promotes it back.

**What does `DEAD` communicate that `FAILED` does not?**
`DEAD` = exhausted, permanent, needs human intervention. `FAILED` = the most recent attempt failed (may retry again). Without a distinct terminal status, you cannot write a simple query for "all jobs that need human intervention today."

---

## The job state machine (draw this from memory)

```
             submit
PENDING ──────────────► RUNNING
   ▲                      │
   │              ┌───────┴──────────┐
   │            success           failure
   │              │                  │
   │         COMPLETED        increment attempts
   │                                 │
   │                    ┌────────────┴─────────────┐
   │               attempts < max            attempts >= max
   │                    │                          │
   └── scheduler ──── PENDING                    DEAD ──► RabbitMQ DLQ
       (via delayed     (delayed set)
        sorted set)
```

---

## Commonly asked interview questions and answers

**Q: What happens if a worker crashes mid-job?**
A: The job stays in `RUNNING` status forever. There is no automatic recovery in FlowCore v1 — it is a documented known limitation. The fix is a background timeout watcher: sweep jobs that have been in `RUNNING` longer than N seconds back to `PENDING`. This is future work.

**Q: Can the same job be processed twice?**
A: In theory yes, but it is guarded against. The `process_job` function checks `status == "PENDING"` before processing. If two workers somehow receive the same job_id (which Redis's atomic BRPOP prevents, but could happen if the scheduler and a worker race on the same job), the second one will find status `RUNNING` and skip it.

**Q: Why RabbitMQ for the dead-letter queue instead of just another Redis list?**
A: Redis is in-memory first. A Redis list used as DLQ would lose messages if Redis restarts without AOF persistence enabled. RabbitMQ with `durable=True` + `delivery_mode=2` writes messages to disk before acknowledging receipt — durability is guaranteed by default. RabbitMQ also provides a management UI for inspecting, requeuing, and consuming DLQ messages, which is valuable for operations teams.

**Q: How would you handle at-least-once delivery instead of at-most-once?**
A: Reverse the ZREM/LPUSH order in the scheduler (LPUSH before ZREM) and add idempotency keys to the payload. Workers would check whether a job with this idempotency key has already been successfully processed before executing — typically by checking a set in Redis or a database. This adds complexity but eliminates silent job loss.

**Q: How does this scale?**
A: Horizontally. `docker-compose up --scale worker=N` adds more consumers. They all BRPOP from the same Redis list — Redis's atomic dequeue guarantees no two workers process the same job. The bottleneck becomes Redis throughput (~100K ops/sec for a single instance). For higher scale, Redis Cluster shards the keyspace across multiple nodes, or you switch to Redis Streams with consumer groups which provide explicit acknowledgment and fanout.

**Q: What would you change if this were a real production system?**
A: Four things:
1. Enable Redis AOF persistence (`appendonly yes`) to survive restarts
2. Use Redis Streams instead of Lists for at-least-once delivery + consumer group fanout
3. Add a `RUNNING` timeout watcher for crashed-worker recovery
4. Replace the simulated `execute_task` with a real task registry (a dict mapping `task_type` to a handler function)

---

## Redis data structures used and why

| Structure | Key | Operations | Purpose |
|---|---|---|---|
| List | `flowcore:jobs` | LPUSH (enqueue), BRPOP (dequeue) | Main job queue — FIFO, atomic |
| Hash | `job:{uuid}` | HSET, HGETALL, HINCRBY | Job state — O(1) lookup by ID |
| Sorted Set | `flowcore:delayed` | ZADD, ZRANGEBYSCORE, ZREM | Delayed retries — score = execute_at timestamp |

Know the time complexity of each operation you use. BRPOP, LPUSH, HSET, HGETALL are all O(1). ZRANGEBYSCORE is O(log N + M).
