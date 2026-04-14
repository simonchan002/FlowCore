# Phase 2 — Job Submission API
**Estimated time: 3–4 hours**

Goal: A working HTTP API that accepts jobs, stores their state in Redis, and returns a job ID. Workers do not exist yet — submitted jobs sit in the queue.

---

## Files to create this phase

```
api/
├── models/
│   └── job.py          ← Step 2.1 [BUILD YOURSELF]
├── services/
│   ├── job_store.py    ← Step 2.2 [BUILD YOURSELF]
│   └── queue.py        ← Step 2.3 [BUILD YOURSELF]
├── routes/
│   └── jobs.py         ← Step 2.4 [BUILD YOURSELF]
└── main.py             ← Step 2.5 [BUILD YOURSELF]
```

All five files are `[BUILD YOURSELF]`. This is the core system skeleton — every line must be explainable.

---

## Step 2.1 [BUILD YOURSELF] — `api/models/job.py`

**Deliverable**: One enum and two Pydantic models.

```python
from enum import Enum
from pydantic import BaseModel

class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEAD = "DEAD"

class JobSubmitRequest(BaseModel):
    payload: dict
    max_retries: int = 3

class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
```

### Why `str, Enum`

Subclassing both `str` and `Enum` makes the enum values behave as strings. FastAPI serializes them as `"PENDING"` in JSON responses without any custom serializer. Without `str`, FastAPI would serialize as the enum object, requiring an extra `.value` call everywhere.

### Why `payload: dict`

FlowCore is a generic queue — it does not care what the task is. A payment system, a notification system, a document processor can all submit jobs with different payload shapes. The worker unpacks the dict and routes it to the right executor. This is the extensibility argument you make in every interview about this project.

---

## Step 2.2 [BUILD YOURSELF] — `api/services/job_store.py`

**Deliverable**: Redis Hash abstraction. **All HSET/HGETALL calls live here and nowhere else** — this is the repository pattern applied to a key-value store.

### Key concepts

**Redis Hash**: `HSET job:{job_id} field value [field value ...]` stores a job as a flat map. `HGETALL job:{job_id}` returns all fields. O(1) lookup by job ID.

**Key format**: `job:{job_id}` — the `job:` prefix namespaces keys so they don't collide with the queue key (`flowcore:jobs`) or the delayed set (`flowcore:delayed`). In Redis CLI, you can find all job keys with `KEYS job:*`.

### Functions to implement

```python
import json
from datetime import datetime, timezone

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def create_job(redis_client, job_id: str, payload: dict, max_retries: int) -> None:
    """Write the initial job hash. Called once at submission time."""
    redis_client.hset(f"job:{job_id}", mapping={
        "status": "PENDING",
        "payload": json.dumps(payload),
        "attempts": 0,
        "max_retries": max_retries,
        "created_at": utcnow_iso(),
    })

def get_job(redis_client, job_id: str) -> dict | None:
    """Fetch all fields for a job. Returns None if job does not exist."""
    data = redis_client.hgetall(f"job:{job_id}")
    if not data:
        return None
    # Redis returns bytes; decode all keys and values
    return {k.decode(): v.decode() for k, v in data.items()}

def update_job_status(redis_client, job_id: str, status: str, **kwargs) -> None:
    """Update status and any additional fields in one HSET call."""
    fields = {"status": status, **kwargs}
    redis_client.hset(f"job:{job_id}", mapping=fields)

def increment_attempts(redis_client, job_id: str) -> int:
    """Atomically increment attempt counter. Returns new value."""
    return redis_client.hincrby(f"job:{job_id}", "attempts", 1)
```

### Why centralise Redis calls here

If you later change the storage backend (e.g., from Redis hashes to a SQL table), you change only this file. All other code imports `job_store` functions, not `redis_client` directly. This is also what makes unit testing easy — you mock `job_store`, not the Redis client.

---

## Step 2.3 [BUILD YOURSELF] — `api/services/queue.py`

**Deliverable**: Redis queue abstraction. Two functions. Write the docstring for `enqueue_job` explaining LPUSH/BRPOP — this is the explanation you give in every distributed systems interview about this project.

```python
def enqueue_job(redis_client, queue_name: str, job_id: str) -> None:
    """
    Enqueue a job_id onto the Redis List.

    LPUSH adds to the head (left). Workers call BRPOP on the tail (right).
    This is a FIFO queue — first submitted, first processed.

    Redis List operations are atomic. Two workers calling BRPOP simultaneously
    on the same list will each receive a different job_id — there is no
    double-dequeue race condition.

    Alternative: Redis Streams (XADD / XREADGROUP) provide consumer groups,
    message acknowledgment, and replay. Lists are simpler and sufficient for
    FlowCore's at-most-once delivery model.
    """
    redis_client.lpush(queue_name, job_id)

def get_queue_depth(redis_client, queue_name: str) -> int:
    """Return the number of jobs currently waiting in the queue."""
    return redis_client.llen(queue_name)
```

---

## Step 2.4 [BUILD YOURSELF] — `api/routes/jobs.py`

**Deliverable**: Two HTTP endpoints. Use FastAPI dependency injection for the Redis client.

### Dependency injection pattern

Define a `get_redis()` function that creates and yields a Redis connection:

```python
import os
import redis

def get_redis():
    client = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
    )
    try:
        yield client
    finally:
        client.close()
```

Inject it into route handlers with `Depends(get_redis)`. This makes every route independently testable — in tests you override `get_redis` with a fixture that points to a test Redis instance. Nothing in the route handler knows or cares where the Redis client came from.

### `POST /jobs`

```
1. Parse JobSubmitRequest from request body
2. Generate job_id = str(uuid.uuid4())
3. job_store.create_job(redis_client, job_id, payload, max_retries)
4. queue.enqueue_job(redis_client, QUEUE_NAME, job_id)
5. Log: logger.info("job_submitted", job_id=job_id, status="PENDING")
6. Return JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=...)
```

### `GET /jobs/{job_id}`

```
1. job = job_store.get_job(redis_client, job_id)
2. If job is None: raise HTTPException(status_code=404, detail="Job not found")
3. Deserialize payload: job["payload"] = json.loads(job["payload"])
4. Return job dict
```

---

## Step 2.5 [BUILD YOURSELF] — `api/main.py`

**Deliverable**: FastAPI entry point that wires everything together.

```python
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI
from api.routes.jobs import router as jobs_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure structlog for JSON output at startup
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    yield

app = FastAPI(title="FlowCore", version="0.1.0", lifespan=lifespan)

app.include_router(jobs_router, prefix="/jobs")

@app.get("/")
def health_check():
    return {"status": "ok"}
```

### Why a health check at `GET /`

The health check endpoint is not optional. It is used by:
- **Docker** restart policies to determine if the container is alive
- **Load balancers** to route traffic away from unhealthy instances
- **Prometheus** to verify the scrape target is up
- **CI pipelines** to confirm the service started before running integration tests

It must return a 200 in under 100ms with no external dependencies. Never put a Redis check here — if Redis is down, the API should still serve the health endpoint and let the client decide what to do.

---

## Step 2.6 [CHECKPOINT] — Verify Job Submission and State Polling

Run: `docker-compose up --build`

**All five checks must pass before moving to Phase 3:**

**Check 1 — Submit a job**
```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"payload": {"task": "send_email", "to": "test@example.com"}}'
```
Expected: `{"job_id": "...", "status": "PENDING", "created_at": "..."}`

**Check 2 — Poll job status**
```bash
curl http://localhost:8000/jobs/<job_id>
```
Expected: Full job hash with all fields populated.

**Check 3 — Queue depth in Redis**
```bash
docker exec -it flowcore-redis-1 redis-cli LLEN flowcore:jobs
```
Expected: Count equals number of jobs you submitted.

**Check 4 — Job hash in Redis**
```bash
docker exec -it flowcore-redis-1 redis-cli HGETALL job:<job_id>
```
Expected: All fields present — `status`, `payload`, `attempts`, `max_retries`, `created_at`.

**Check 5 — 404 on missing job**
```bash
curl http://localhost:8000/jobs/nonexistent-id
```
Expected: HTTP 404 with `{"detail": "Job not found"}`

---

## What you've built at the end of Phase 2

A complete producer side of the queue:
- Jobs enter via `POST /jobs`, get a UUID, are stored as a Redis hash, and are pushed onto a Redis list
- Status is readable at any time via `GET /jobs/{job_id}`
- The queue is growing but nothing is consuming it yet

**Next**: Phase 3 — Worker Pool (the consumer side)
