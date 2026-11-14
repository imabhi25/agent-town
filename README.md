# Agent Town

A live city map of a real distributed job engine. Type a goal, a Gemini planner breaks it into tasks, and workers ("agents") process them through Redis Streams with retries, a dead-letter queue, crash recovery and human approval gates. Every move on the map is a real event from the backend.

> Status: week 1 in progress. See `PLAN.md`.

## Stack
FastAPI + asyncio, Redis Streams, PostgreSQL, Gemini (`google-genai`), React + TypeScript + PixiJS (week 3), Docker Compose, pytest, GitHub Actions.

## Run locally
```bash
cp .env.example .env
docker compose up -d postgres redis
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

## Delivery guarantee
At-least-once delivery plus an idempotent completion transaction, which gives effectively-once results. See `DECISIONS.md`.
