# FlowCore — Implementation Guide

This folder contains step-by-step implementation guides for each phase of the project.

| File | Phase | Time |
|---|---|---|
| [phase-1-foundation.md](phase-1-foundation.md) | Infrastructure — Docker Compose, Dockerfiles, deps | 2–3 hrs |
| [phase-2-api.md](phase-2-api.md) | Job Submission API — FastAPI, Redis, job state model | 3–4 hrs |
| [phase-3-worker.md](phase-3-worker.md) | Worker Pool — BRPOP loop, threading concurrency | 3–4 hrs |
| [phase-4-failure-handling.md](phase-4-failure-handling.md) | **Failure Handling — retry, backoff, DLQ** ← interview-critical | 4–5 hrs |
| [phase-5-observability.md](phase-5-observability.md) | Prometheus metrics + Grafana dashboard | 3–4 hrs |
| [phase-6-testing-cicd.md](phase-6-testing-cicd.md) | pytest test suite + GitHub Actions CI | 4–5 hrs |
| [phase-7-documentation.md](phase-7-documentation.md) | README, architecture diagram, failure runbook | 2–3 hrs |
| [interview-prep.md](interview-prep.md) | All interview concepts consolidated — read before interviews | — |

## Step tags

- `[BUILD YOURSELF]` — write every line; must be explainable in an interview
- `[USE AI]` — boilerplate and config; generate with AI assistance
- `[CHECKPOINT]` — verify before advancing; never skip
- `[INTERVIEW GOLD]` — the 3 most critical steps for interview conversations

## Rule

Never advance to the next phase until all checkpoints in the current phase pass.
