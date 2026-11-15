import asyncio
import json


async def test_create_job_requires_idempotency_key(client):
    r = await client.post("/jobs", json={"type": "llm.mock"})
    assert r.status_code == 400


async def test_repeated_key_returns_same_job_and_one_outbox_row(client, pool):
    headers = {"Idempotency-Key": "abc-123"}
    first = await client.post("/jobs", json={"type": "llm.mock"}, headers=headers)
    second = await client.post("/jobs", json={"type": "llm.mock"}, headers=headers)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert await pool.fetchval("SELECT count(*) FROM jobs") == 1
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 1


async def test_concurrent_requests_with_same_key_create_one_job(client, pool):
    headers = {"Idempotency-Key": "race-key"}
    results = await asyncio.gather(
        *[client.post("/jobs", json={"type": "llm.mock"}, headers=headers) for _ in range(10)]
    )
    assert {r.json()["id"] for r in results} == {results[0].json()["id"]}
    assert sum(1 for r in results if r.json()["duplicate"] is False) == 1
    assert await pool.fetchval("SELECT count(*) FROM jobs") == 1
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 1


async def test_creating_a_job_emits_one_event_and_a_duplicate_emits_none(client, pool):
    body = {"type": "llm.mock", "payload": {"prompt": "hi"}}
    first = await client.post("/jobs", json=body, headers={"Idempotency-Key": "evt-1"})
    again = await client.post("/jobs", json=body, headers={"Idempotency-Key": "evt-1"})
    assert first.status_code == 201 and again.json()["duplicate"] is True
    rows = await pool.fetch("SELECT payload FROM events WHERE type = 'job.created'")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["job_id"] == first.json()["id"]
