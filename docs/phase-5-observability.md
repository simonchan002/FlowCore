# Phase 5 — Observability
**Estimated time: 3–4 hours**

Goal: Make the system's internal state visible through metrics and dashboards. A system you cannot measure is a system you cannot operate.

---

## Files to create this phase

```
api/
└── metrics.py                          ← Step 5.1 [BUILD YOURSELF]

worker/
└── metrics.py                          ← Step 5.1 [BUILD YOURSELF]

api/main.py                             ← Step 5.3 [USE AI] (add /metrics mount)
worker/worker.py                        ← Step 5.3 [USE AI] (add start_http_server)

monitoring/
├── prometheus.yml                      ← Step 5.4 [USE AI]
└── grafana/
    ├── datasource.yml                  ← Step 5.5 [USE AI]
    └── dashboards/
        ├── dashboards.yml              ← Step 5.6 [BUILD YOURSELF]
        └── flowcore.json               ← Step 5.6 [BUILD YOURSELF] (exported from UI)
```

---

## The five required metrics

| Metric name | Type | Where | What it measures |
|---|---|---|---|
| `jobs_submitted_total` | Counter | API | Total jobs enqueued since service start |
| `queue_depth` | Gauge | API | Current number of jobs waiting in Redis |
| `jobs_failed_total` | Counter | Worker | Execution failures, labelled by reason |
| `jobs_retried_total` | Counter | Worker | Retry attempts scheduled |
| `job_processing_duration_seconds` | Histogram | Worker | Time from job start to terminal state |

---

## Step 5.1 [BUILD YOURSELF] — Define metrics in `api/metrics.py` and `worker/metrics.py`

### `api/metrics.py`

```python
from prometheus_client import Counter, Gauge

JOBS_SUBMITTED_TOTAL = Counter(
    "jobs_submitted_total",
    "Total number of jobs submitted to the queue",
    ["status"],  # label: always "PENDING" at submission time
)

QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Current number of jobs waiting in the Redis main queue",
)
```

### `worker/metrics.py`

```python
from prometheus_client import Counter, Histogram

JOBS_FAILED_TOTAL = Counter(
    "jobs_failed_total",
    "Total number of job execution failures",
    ["reason"],  # label: "transient" (will retry) or "permanent" (DLQ)
)

JOBS_RETRIED_TOTAL = Counter(
    "jobs_retried_total",
    "Total number of job retry attempts scheduled",
)

JOB_PROCESSING_DURATION_SECONDS = Histogram(
    "job_processing_duration_seconds",
    "Time in seconds from job start to completion or permanent failure",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)
```

### Interview prep: Histogram vs Gauge for duration

**Gauge** holds a single current value — it goes up and down. Suitable for `queue_depth` (current count) or memory usage. Not suitable for latency, because it can only show the most recent value.

**Histogram** records the distribution of observations across buckets. It exposes three time series:
- `_bucket{le="N"}` — count of observations ≤ N seconds
- `_count` — total number of observations
- `_sum` — sum of all observation values

This enables **percentile queries** in PromQL:
```
histogram_quantile(0.95, rate(job_processing_duration_seconds_bucket[5m]))
```
That gives you P95 latency over the last 5 minutes — something a Gauge cannot provide. This distinction is frequently asked in SRE and platform interviews.

The buckets `[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]` are chosen to cover the expected range of `execute_task` (0.1s–0.5s sleep) with headroom for retries and slow paths.

---

## Step 5.2 [BUILD YOURSELF] — Instrument the API and worker code

### In `api/routes/jobs.py` — after successful `enqueue_job`

```python
from api.metrics import JOBS_SUBMITTED_TOTAL, QUEUE_DEPTH
import os

QUEUE_NAME = os.environ["REDIS_QUEUE_NAME"]

# Inside POST /jobs, after enqueue_job succeeds:
JOBS_SUBMITTED_TOTAL.labels(status="PENDING").inc()
QUEUE_DEPTH.set(queue.get_queue_depth(redis_client, QUEUE_NAME))
```

### In `worker/worker.py` — inside `process_job` and `handle_failure`

```python
from worker.metrics import (
    JOBS_FAILED_TOTAL,
    JOBS_RETRIED_TOTAL,
    JOB_PROCESSING_DURATION_SECONDS,
)

# In process_job, on successful completion:
JOB_PROCESSING_DURATION_SECONDS.observe(time.time() - job_start_time)

# In handle_failure, when scheduling a retry:
JOBS_FAILED_TOTAL.labels(reason="transient").inc()
JOBS_RETRIED_TOTAL.inc()

# In handle_failure, when publishing to DLQ:
JOBS_FAILED_TOTAL.labels(reason="permanent").inc()
JOB_PROCESSING_DURATION_SECONDS.observe(time.time() - job_start_time)
```

### Placement rule: increment AFTER the operation succeeds

Incrementing a counter before the operation risks overcounting if the operation fails partway through. Incrementing after means the count is accurate: a job that fails before being enqueued is not counted as submitted.

---

## Step 5.3 [USE AI] — Expose metrics endpoints

### API — add to `api/main.py`

```python
from prometheus_client import make_asgi_app

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
```

### Worker — add to `worker/worker.py` before starting threads

```python
from prometheus_client import start_http_server

# Start a minimal HTTP server on port 8001 serving /metrics
start_http_server(8001)
```

Also expose port `8001` on the worker service in `docker-compose.yml`:
```yaml
worker:
  ports:
    - "8001:8001"
```

---

## Step 5.4 [USE AI] — `monitoring/prometheus.yml`

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "flowcore-api"
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics

  - job_name: "flowcore-worker"
    static_configs:
      - targets: ["worker:8001"]
    metrics_path: /metrics
```

The hostnames `api` and `worker` resolve because all services share `flowcore-net`. Prometheus scrapes both every 15 seconds.

---

## Step 5.5 [USE AI] — `monitoring/grafana/datasource.yml`

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    url: http://prometheus:9090
    isDefault: true
    access: proxy
```

Mount this to `/etc/grafana/provisioning/datasources/datasource.yml` in `docker-compose.yml`. With auto-provisioning, Grafana connects to Prometheus on startup with no manual UI configuration needed.

---

## Step 5.6 [BUILD YOURSELF] — Grafana dashboard

**Deliverable**: Build the dashboard in the Grafana UI, then export as `monitoring/grafana/dashboards/flowcore.json`.

### Build it in the UI

1. Open `http://localhost:3000` (login: `admin` / `admin`)
2. Create a new dashboard
3. Add these five panels:

| Panel title | Visualization | PromQL query |
|---|---|---|
| Jobs Submitted (rate/s) | Time series | `rate(jobs_submitted_total[5m])` |
| Jobs Failed (rate/s) | Time series | `rate(jobs_failed_total[5m])` |
| Jobs Retried (rate/s) | Time series | `rate(jobs_retried_total[5m])` |
| Processing Duration P95 | Stat | `histogram_quantile(0.95, rate(job_processing_duration_seconds_bucket[5m]))` |
| Queue Depth | Gauge | `queue_depth` |

4. Save the dashboard
5. Export: Dashboard settings → JSON Model → Copy to clipboard
6. Save as `monitoring/grafana/dashboards/flowcore.json`

### Add provisioning config: `monitoring/grafana/dashboards/dashboards.yml`

```yaml
apiVersion: 1
providers:
  - name: flowcore
    folder: FlowCore
    type: file
    options:
      path: /etc/grafana/provisioning/dashboards
```

### Mount in `docker-compose.yml`

```yaml
grafana:
  volumes:
    - ./monitoring/grafana/datasource.yml:/etc/grafana/provisioning/datasources/datasource.yml
    - ./monitoring/grafana/dashboards/dashboards.yml:/etc/grafana/provisioning/dashboards/dashboards.yml
    - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards
```

With both provisioning files in place, the FlowCore dashboard loads automatically every time you run `docker-compose up`.

---

## Step 5.7 [CHECKPOINT] — Verify all metrics and dashboards

**All six checks must pass before moving to Phase 6:**

**Check 1 — API metrics endpoint**
```bash
curl http://localhost:8000/metrics | grep jobs_submitted_total
```
Expected: Prometheus text format with `jobs_submitted_total` counter.

**Check 2 — Worker metrics endpoint**
```bash
curl http://localhost:8001/metrics | grep job_processing_duration
```
Expected: `job_processing_duration_seconds_bucket` histogram entries.

**Check 3 — Prometheus scrape targets**
Open `http://localhost:9090/targets`.
Expected: Both `flowcore-api` and `flowcore-worker` show state `UP`.

**Check 4 — Grafana dashboard loads**
Open `http://localhost:3000`. Navigate to the FlowCore dashboard.
Expected: All five panels render without "No data" errors.

**Check 5 — Panels show data**
Submit 20 jobs. Wait for processing. Refresh the dashboard.
Expected: All five panels show non-zero values.

**Check 6 — P95 panel shows realistic value**
Expected: P95 processing duration between 0.1s and 0.5s — matching the `time.sleep(random.uniform(0.1, 0.5))` range in `execute_task`.

---

## What you've built at the end of Phase 5

Complete observability:
- Every job submission and execution is counted
- Queue depth is always visible
- Processing latency is measured at P50/P95/P99 granularity
- Failure rates are broken down by type (transient vs permanent)
- All metrics are visualised in a live Grafana dashboard

In an interview, this is the section that proves you understand production systems — not just that the code works, but that you can tell when it stops working and why.

**Next**: Phase 6 — Testing and CI/CD
