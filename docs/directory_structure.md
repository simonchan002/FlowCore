
FlowCore/
├── docker-compose.yml
├── .env.example / .env
├── .gitignore
├── README.md
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── routes/jobs.py          # POST /jobs, GET /jobs/{id}
│   ├── models/job.py           # Pydantic models + JobStatus enum
│   ├── services/
│   │   ├── queue.py            # LPUSH / LLEN wrappers
│   │   └── job_store.py        # HSET / HGETALL wrappers (repository pattern)
│   └── metrics.py              # jobs_submitted_total, queue_depth
├── worker/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── worker.py               # BRPOP loop + threading concurrency
│   ├── executor.py             # execute_task() — simulates work (20% failure)
│   ├── retry.py                # calculate_backoff(), schedule_retry()
│   ├── scheduler.py            # Delayed set promoter (background thread)
│   ├── dlq.py                  # RabbitMQ publisher via pika
│   └── metrics.py              # jobs_failed_total, jobs_retried_total, duration histogram
├── tests/
│   ├── conftest.py             # Redis flush + RabbitMQ purge fixtures
│   ├── unit/
│   │   ├── test_retry_logic.py
│   │   └── test_job_model.py
│   └── integration/
│       ├── test_api_submit.py
│       └── test_dlq_routing.py
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
│       ├── datasource.yml
│       └── dashboards/
│           ├── dashboards.yml
│           └── flowcore.json
└── .github/workflows/ci.yml