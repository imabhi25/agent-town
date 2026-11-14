# Agent Town: development guide

## What this is
Co-op portfolio project. A live city map driven by a real job engine: prompt -> Gemini planner -> task DAG -> Redis Streams workers -> events -> WebSocket -> map. The owner must be able to explain every part in interviews, so keep code simple and explain design choices.

## Stack and rules
- Python 3.12, FastAPI, asyncio. Async libraries only in workers: asyncpg, redis.asyncio, google-genai async client. No blocking calls (no time.sleep, no sync DB drivers).
- Gemini: use the `google-genai` package (`from google import genai`, `client.aio.models.generate_content`) with `types.GenerateContentConfig(response_mime_type="application/json", response_schema=Model)`. Do not use the deprecated `google-generativeai`. Always validate output with Pydantic; on invalid output retry once, then use a fallback plan.
- Delivery: Redis Streams give at-least-once. Workers complete a job in ONE asyncpg transaction: update job `WHERE status != 'succeeded'`, insert output (unique on job_id), check affected rows, commit, THEN XACK. Never claim exactly-once.
- Mock LLM provider (`app/providers/mock_llm.py`) for tests, CI, floods and chaos. Real Gemini only for the planner and capped worker calls, with a hard daily cap and per-IP limits. API key stays server-side.
- Chaos "flood" button uses mock jobs only.

## Working style
- One day of `PLAN.md` per task. Plan first for anything non-trivial.
- Write tests alongside every feature; run `ruff check .` and `pytest` before finishing.
- Small commits, one working step each.
- Record design reasoning in `DECISIONS.md`.
- Engine first, visuals second. If time runs short, cut polish, not guarantees.
