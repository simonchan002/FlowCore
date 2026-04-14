# Phase 1 — Project Foundation
**Estimated time: 2–3 hours**

Goal: All six containers start healthy. No application logic exists yet — this phase is purely infrastructure.

---

## Overview

You are standing up six Docker services that will talk to each other over a shared network:

| Service | Image | Port | Role |
|---|---|---|---|
| `api` | built from `./api` | 8000 | FastAPI job submission |
| `worker` | built from `./worker` | — | Job consumer (scales horizontally) |
| `redis` | `redis:7-alpine` | 6379 | Primary queue + job state store |
| `rabbitmq` | `rabbitmq:3-management` | 5672, 15672 | Dead-letter queue |
| `prometheus` | `prom/prometheus:latest` | 9090 | Metrics scraper |
| `grafana` | `grafana/grafana:latest` | 3000 | Metrics dashboard |

---

## Step 1.1 [USE AI] — `.gitignore`

**File**: `FlowCore/.gitignore`
**Status**: Generated ✓

Covers: Python bytecode, virtual environments, `.env`, pytest cache, IDE files, Docker artifacts.
The critical rule: `.env` is gitignored. Configuration lives in `.env.example` (committed) and `.env` (local only, never committed).

---

## Step 1.2 [USE AI] — `.env.example`

**File**: `FlowCore/.env.example`
**Status**: Generated ✓

All environment variables the system needs, with a comment on each explaining what it controls. Copy to `.env` before running locally: `cp .env.example .env`.

Variables:
- `REDIS_HOST`, `REDIS_PORT` — Redis connection
- `REDIS_QUEUE_NAME` — name of the Redis List used as the job queue
- `REDIS_DELAYED_SET` — name of the Redis Sorted Set for scheduled retries
- `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD` — RabbitMQ connection
- `RABBITMQ_DLQ_QUEUE` — name of the durable dead-letter queue
- `MAX_RETRIES` — default retry limit per job (overridable per-job)
- `WORKER_CONCURRENCY` — threads per worker container
- `LOG_LEVEL` — structured log verbosity

---

## Step 1.3 [BUILD YOURSELF] — `docker-compose.yml`

**File**: `FlowCore/docker-compose.yml`
**Write this yourself.** Understanding service dependencies and Docker networking is interview-required knowledge for any platform role.

### Concepts to understand before writing

**Named network**: Add a top-level `networks` block with a single network `flowcore-net`. Attach every service to it. This is why services reach each other by name (e.g., `redis:6379`) instead of `localhost:6379`. Without a shared network, containers are isolated.

**Healthchecks**: `condition: service_healthy` in `depends_on` means the dependent service waits until the dependency's healthcheck passes — not just until the container starts. A container that started but crashed immediately would still satisfy a plain `depends_on` without healthchecks.

**Worker has no exposed port**: Only `api` needs to be reachable from outside Docker. Workers pull from the queue internally. You scale workers with `docker-compose up --scale worker=3`, not by exposing more ports.

### Service-by-service spec

**`redis`**
```yaml
image: redis:7-alpine
ports:
  - "6379:6379"
healthcheck:
  test: ["CMD", "redis-cli", "ping"]
  interval: 5s
  timeout: 3s
  retries: 5
networks:
  - flowcore-net
```

**`rabbitmq`**
```yaml
image: rabbitmq:3-management
ports:
  - "5672:5672"    # AMQP
  - "15672:15672"  # Management UI
healthcheck:
  test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
  interval: 10s
  timeout: 5s
  retries: 5
networks:
  - flowcore-net
```

**`api`**
```yaml
build: ./api
ports:
  - "8000:8000"
env_file:
  - .env
depends_on:
  redis:
    condition: service_healthy
  rabbitmq:
    condition: service_healthy
networks:
  - flowcore-net
restart: always
```

**`worker`**
```yaml
build: ./worker
env_file:
  - .env
depends_on:
  - api
  - redis
  - rabbitmq
networks:
  - flowcore-net
restart: always
# No ports — workers pull from queue internally
```

**`prometheus`**
```yaml
image: prom/prometheus:latest
ports:
  - "9090:9090"
volumes:
  - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
networks:
  - flowcore-net
```

**`grafana`**
```yaml
image: grafana/grafana:latest
ports:
  - "3000:3000"
depends_on:
  - prometheus
volumes:
  - ./monitoring/grafana/datasource.yml:/etc/grafana/provisioning/datasources/datasource.yml
  - ./monitoring/grafana/dashboards/dashboards.yml:/etc/grafana/provisioning/dashboards/dashboards.yml
  - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards
networks:
  - flowcore-net
```

**Top-level networks block**
```yaml
networks:
  flowcore-net:
    driver: bridge
```

---

## Step 1.4 [USE AI] — `requirements.txt` files

**Files**: `api/requirements.txt`, `worker/requirements.txt`
**Status**: Generated ✓

All versions are pinned. Floating versions in distributed systems create reproducibility issues — if a CI run pulls a different minor version than your local build, the containers differ silently.

**api dependencies explained:**
- `fastapi` + `uvicorn` — web framework + ASGI server
- `redis` — Python Redis client (LPUSH, BRPOP, HSET, HGETALL)
- `pydantic` — request/response validation and serialization
- `prometheus-client` — expose `/metrics` endpoint
- `python-dotenv` — loads `.env` file into environment variables
- `structlog` — structured JSON logging

**worker dependencies explained:**
- `redis` — same client; worker BRPOPs from the queue
- `pika` — Python AMQP client for RabbitMQ (dead-letter queue publishing)
- `prometheus-client` — expose worker metrics on port 8001
- `python-dotenv`, `structlog` — same as API

---

## Step 1.5 [BUILD YOURSELF] — Dockerfiles

**Files**: `api/Dockerfile`, `worker/Dockerfile`
**Write these yourself.** Multi-stage awareness and image selection matter in interviews.

### Why `python:3.12-slim`

The `slim` variant strips build tools, documentation, and many OS packages not needed at runtime. Result: ~130MB vs ~900MB for the full image. Smaller images mean faster pulls in CI, smaller attack surface, less to scan for CVEs.

### `api/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy and install dependencies first — Docker layer caching means this
# layer is only rebuilt when requirements.txt changes, not on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Why `--host 0.0.0.0`**: Without this, uvicorn binds to `127.0.0.1` (loopback only) and is unreachable from outside the container. `0.0.0.0` means "accept connections on all interfaces."

### `worker/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "worker.py"]
```

---

## Step 1.6 [CHECKPOINT] — Verify All Containers Start

Run: `docker-compose up --build`

**All five checks must pass before moving to Phase 2:**

| Check | Command | Expected |
|---|---|---|
| All containers running | `docker-compose ps` | All show `healthy` or `running` |
| API is up | `curl http://localhost:8000` | HTTP response (404 is fine — no routes yet) |
| RabbitMQ UI | Open `http://localhost:15672` | Management UI loads; login `guest`/`guest` |
| Redis responding | `docker exec flowcore-redis-1 redis-cli ping` | `PONG` |
| Prometheus UI | Open `http://localhost:9090` | Prometheus UI loads |

If a container crashes, diagnose with `docker-compose logs <service>` before guessing. Common causes:
- `api` or `worker` fail to start → dependency not healthy yet (check healthcheck intervals)
- RabbitMQ slow to start → increase `retries` in the healthcheck
- Port already in use → another process is on 6379 or 5672

---

## What you've built at the end of Phase 1

A complete local dev environment where six services start, stay healthy, and can reach each other by hostname. No application code yet — just the infrastructure skeleton every line of Phases 2–7 builds on.

**Next**: Phase 2 — Job Submission API
