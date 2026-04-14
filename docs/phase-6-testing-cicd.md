# Phase 6 — Testing and CI/CD
**Estimated time: 4–5 hours**

Goal: An automated test suite that covers core logic, plus a CI pipeline that runs on every push and gates merges.

Tests are not optional for a portfolio project aimed at fintech/platform roles. They demonstrate that you understand production readiness, not just that you can make features work.

---

## Files to create this phase

```
tests/
├── conftest.py                             ← Step 6.5 [USE AI]
├── unit/
│   ├── test_retry_logic.py                 ← Step 6.1 [BUILD YOURSELF]
│   └── test_job_model.py                   ← Step 6.2 [BUILD YOURSELF]
└── integration/
    ├── test_api_submit.py                  ← Step 6.3 [BUILD YOURSELF]
    └── test_dlq_routing.py                 ← Step 6.4 [BUILD YOURSELF]

.github/
└── workflows/
    └── ci.yml                              ← Step 6.6 [USE AI]
```

---

## Step 6.1 [BUILD YOURSELF] — `tests/unit/test_retry_logic.py`

**Deliverable**: Unit tests for backoff calculation and retry scheduling. No Docker, no Redis, no network — runs in milliseconds.

```python
import time
import unittest.mock as mock
from worker.retry import calculate_backoff, schedule_retry

def test_backoff_attempt_0():
    assert calculate_backoff(0) == 2

def test_backoff_attempt_1():
    assert calculate_backoff(1) == 4

def test_backoff_attempt_2():
    assert calculate_backoff(2) == 8

def test_backoff_is_capped_at_60():
    assert calculate_backoff(10) == 60
    assert calculate_backoff(100) == 60

def test_backoff_is_strictly_increasing():
    values = [calculate_backoff(i) for i in range(5)]
    assert values == sorted(values)

def test_schedule_retry_calls_zadd():
    redis_mock = mock.MagicMock()
    before = time.time()

    schedule_retry(redis_mock, "job-123", attempt=1,
                   delayed_set="flowcore:delayed", queue_name="flowcore:jobs")

    after = time.time()
    redis_mock.zadd.assert_called_once()

    call_args = redis_mock.zadd.call_args
    set_name, mapping = call_args[0]
    assert set_name == "flowcore:delayed"
    score = mapping["job-123"]

    # Score should be approximately now + calculate_backoff(1) = now + 4
    assert before + 4 <= score <= after + 4 + 0.1

def test_schedule_retry_uses_correct_key():
    redis_mock = mock.MagicMock()
    schedule_retry(redis_mock, "abc-456", attempt=0,
                   delayed_set="flowcore:delayed", queue_name="flowcore:jobs")
    args = redis_mock.zadd.call_args[0]
    assert "abc-456" in args[1]
```

### Why mock-based unit tests matter

The `test_schedule_retry_calls_zadd` test verifies:
1. That the function calls `zadd` (not `lpush` or some other Redis command)
2. That it uses the correct key name
3. That the score is approximately correct (within the expected time window)

This is what interviewers mean when they ask "how would you test code that depends on an external system?" — you inject a mock that records calls, then assert on what the code did without needing a real Redis server.

---

## Step 6.2 [BUILD YOURSELF] — `tests/unit/test_job_model.py`

**Deliverable**: Pydantic model validation tests.

```python
import pytest
from pydantic import ValidationError
from api.models.job import JobStatus, JobSubmitRequest, JobResponse

def test_job_submit_request_valid():
    req = JobSubmitRequest(payload={"task": "send_email"})
    assert req.max_retries == 3  # default

def test_job_submit_request_custom_retries():
    req = JobSubmitRequest(payload={}, max_retries=5)
    assert req.max_retries == 5

def test_job_submit_request_zero_retries_valid():
    req = JobSubmitRequest(payload={}, max_retries=0)
    assert req.max_retries == 0

def test_job_submit_request_negative_retries_invalid():
    with pytest.raises(ValidationError):
        JobSubmitRequest(payload={}, max_retries=-1)

def test_job_status_has_all_values():
    expected = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "DEAD"}
    actual = {s.value for s in JobStatus}
    assert actual == expected

def test_job_response_serializes_status_as_string():
    resp = JobResponse(job_id="abc", status=JobStatus.PENDING, created_at="2024-01-01T00:00:00Z")
    data = resp.model_dump()
    assert data["status"] == "PENDING"
    assert isinstance(data["status"], str)
```

---

## Step 6.3 [BUILD YOURSELF] — `tests/integration/test_api_submit.py`

**Deliverable**: Integration tests against a real Redis instance.

Uses FastAPI's `TestClient` + the `redis_client` and `test_app` fixtures from `conftest.py`.

```python
import json
from fastapi.testclient import TestClient

def test_submit_job_returns_pending(test_app, redis_client):
    response = test_app.post("/jobs", json={"payload": {"task": "test"}})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "PENDING"
    assert "job_id" in data
    assert "created_at" in data

def test_get_job_returns_submitted_job(test_app, redis_client):
    submit = test_app.post("/jobs", json={"payload": {"key": "value"}})
    job_id = submit.json()["job_id"]

    get = test_app.get(f"/jobs/{job_id}")
    assert get.status_code == 200
    data = get.json()
    assert data["job_id"] == job_id
    assert data["status"] == "PENDING"

def test_get_nonexistent_job_returns_404(test_app):
    response = test_app.get("/jobs/does-not-exist")
    assert response.status_code == 404

def test_submitted_jobs_appear_in_redis_queue(test_app, redis_client):
    import os
    queue_name = os.environ.get("REDIS_QUEUE_NAME", "flowcore:jobs")

    for i in range(10):
        test_app.post("/jobs", json={"payload": {"i": i}})

    depth = redis_client.llen(queue_name)
    assert depth == 10

def test_job_hash_has_required_fields(test_app, redis_client):
    response = test_app.post("/jobs", json={"payload": {"x": 1}})
    job_id = response.json()["job_id"]

    raw = redis_client.hgetall(f"job:{job_id}")
    fields = {k.decode() for k in raw.keys()}
    required = {"status", "payload", "attempts", "max_retries", "created_at"}
    assert required.issubset(fields)
```

---

## Step 6.4 [BUILD YOURSELF] — `tests/integration/test_dlq_routing.py`

**Deliverable**: End-to-end failure pipeline verification.

This is the test you demo in an interview when asked "how do you know your retry logic works?"

```python
import json
from unittest.mock import patch

def test_zero_retry_job_goes_to_dlq(redis_client, rabbitmq_channel, test_worker):
    """
    A job with max_retries=0 that fails should immediately go to DEAD
    and appear in the RabbitMQ DLQ.
    """
    from api.services.job_store import create_job, get_job
    from api.services.queue import enqueue_job
    import os, uuid

    job_id = str(uuid.uuid4())
    queue_name = os.environ.get("REDIS_QUEUE_NAME", "flowcore:jobs")
    dlq_name = os.environ.get("RABBITMQ_DLQ_QUEUE", "flowcore:dlq")

    create_job(redis_client, job_id, payload={"task": "fail_me"}, max_retries=0)
    enqueue_job(redis_client, queue_name, job_id)

    # Patch execute_task to always fail
    with patch("worker.executor.execute_task", side_effect=RuntimeError("forced failure")):
        test_worker.process_one_job()  # process exactly one job synchronously

    # Status should be DEAD
    job = get_job(redis_client, job_id)
    assert job["status"] == "DEAD"
    assert "forced failure" in job["last_error"]

    # DLQ should have exactly one message
    method, properties, body = rabbitmq_channel.basic_get(dlq_name, auto_ack=True)
    assert body is not None
    message = json.loads(body)
    assert message["job_id"] == job_id

def test_retry_job_reaches_dead_after_exhaustion(redis_client, rabbitmq_channel, test_worker):
    """A job with max_retries=2 should be retried twice then land in DLQ."""
    from api.services.job_store import create_job, get_job
    from api.services.queue import enqueue_job
    import os, uuid

    job_id = str(uuid.uuid4())
    queue_name = os.environ.get("REDIS_QUEUE_NAME", "flowcore:jobs")

    create_job(redis_client, job_id, payload={"task": "retry_me"}, max_retries=2)
    enqueue_job(redis_client, queue_name, job_id)

    with patch("worker.executor.execute_task", side_effect=RuntimeError("always fails")):
        # First attempt: fail → schedule retry
        test_worker.process_one_job()
        job = get_job(redis_client, job_id)
        assert job["attempts"] == "1"
        assert job["status"] == "PENDING"

        # Promote from delayed set manually (bypass scheduler timing)
        redis_client.zrem(os.environ.get("REDIS_DELAYED_SET", "flowcore:delayed"), job_id)
        redis_client.lpush(queue_name, job_id)

        # Second attempt: fail → dead (attempts=2 == max_retries=2)
        test_worker.process_one_job()
        job = get_job(redis_client, job_id)
        assert job["status"] == "DEAD"
        assert job["attempts"] == "2"
```

---

## Step 6.5 [USE AI] — `tests/conftest.py`

**Deliverable**: Shared pytest fixtures. Understand each one before using.

```python
import os
import pytest
import redis as redis_lib
import pika
from fastapi.testclient import TestClient
from api.main import app
from api.routes.jobs import get_redis

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")

@pytest.fixture
def redis_client():
    """
    Provides a real Redis client pointing to the test Redis instance.
    FLUSHDB before and after each test — mandatory to prevent state leakage
    between tests. A test that leaves job hashes in Redis will cause the
    next test's LLEN assertions to fail.
    """
    client = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()

@pytest.fixture
def rabbitmq_channel():
    """
    Provides a RabbitMQ channel with the DLQ declared.
    Purges the queue before and after each test.
    """
    dlq_name = os.environ.get("RABBITMQ_DLQ_QUEUE", "flowcore:dlq")
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=RABBITMQ_HOST)
    )
    channel = connection.channel()
    channel.queue_declare(queue=dlq_name, durable=True)
    channel.queue_purge(dlq_name)
    yield channel
    channel.queue_purge(dlq_name)
    connection.close()

@pytest.fixture
def test_app(redis_client):
    """
    FastAPI TestClient with Redis dependency overridden to use the test instance.
    This is dependency injection in tests — same pattern as production,
    just pointing at a different Redis.
    """
    def override_get_redis():
        yield redis_client

    app.dependency_overrides[get_redis] = override_get_redis
    yield TestClient(app)
    app.dependency_overrides.clear()
```

---

## Step 6.6 [USE AI] — `.github/workflows/ci.yml`

**Deliverable**: Four-job GitHub Actions pipeline.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install flake8 black
      - run: black --check api/ worker/ tests/
      - run: flake8 api/ worker/ tests/ --max-line-length=100

  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r api/requirements.txt -r worker/requirements.txt pytest pytest-asyncio
      - run: pytest tests/unit/ -v --tb=short

  integration-tests:
    runs-on: ubuntu-latest
    needs: unit-tests
    services:
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 5
      rabbitmq:
        image: rabbitmq:3-management
        ports:
          - 5672:5672
        options: >-
          --health-cmd "rabbitmq-diagnostics -q ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      REDIS_HOST: localhost
      REDIS_PORT: 6379
      RABBITMQ_HOST: localhost
      RABBITMQ_PORT: 5672
      RABBITMQ_USER: guest
      RABBITMQ_PASSWORD: guest
      REDIS_QUEUE_NAME: flowcore:jobs
      REDIS_DELAYED_SET: flowcore:delayed
      RABBITMQ_DLQ_QUEUE: flowcore:dlq
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r api/requirements.txt -r worker/requirements.txt pytest pytest-asyncio httpx
      - run: pytest tests/integration/ -v --tb=short

  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build api/
      - run: docker build worker/
```

### Pipeline structure

```
push/PR
  ├── lint          (no deps — runs immediately)
  ├── unit-tests    (no deps — runs immediately)
  └── integration-tests  (depends on unit-tests)
  └── build         (no deps — purely a compile check)
```

`lint` and `unit-tests` run in parallel. Integration tests only run if unit tests pass — no point standing up Redis and RabbitMQ to run integration tests against code that fails unit tests.

---

## Step 6.7 [CHECKPOINT] — Verify full test suite and CI

**All six checks must pass before moving to Phase 7:**

**Check 1 — Unit tests pass locally**
```bash
pytest tests/unit/ -v
```
Expected: All tests pass, 0 failures.

**Check 2 — Integration tests pass locally**
```bash
# Requires redis and rabbitmq running
docker-compose up -d redis rabbitmq
pytest tests/integration/ -v
```
Expected: All tests pass.

**Check 3 — CI triggers on push**
Push to GitHub. Confirm the Actions workflow appears at:
`https://github.com/<username>/FlowCore/actions`

**Check 4 — All four CI jobs pass**
Each job should show a green checkmark.

**Check 5 — Add CI badge to README**
```markdown
![CI](https://github.com/<username>/FlowCore/actions/workflows/ci.yml/badge.svg)
```

**Check 6 — Prove the pipeline gates quality**
1. Deliberately break a unit test (change an assertion to fail)
2. Push — confirm CI fails on the `unit-tests` job
3. Fix the test, push again — confirm CI passes
This confirms the pipeline is not a rubber stamp.

---

## What you've built at the end of Phase 6

A complete quality gate:
- Unit tests verify core logic (backoff, models) in milliseconds with no external deps
- Integration tests verify the full API + Redis + RabbitMQ pipeline
- CI runs automatically on every push and every PR
- The build job confirms Docker images compile cleanly

**Next**: Phase 7 — Documentation
