"""Per-provider circuit breaker, shared by all workers through Redis (atomic Lua scripts).

closed    : calls flow; consecutive failures are counted.
open      : after `threshold` consecutive failures. Calls are refused (jobs are delayed, not
            failed and not charged an attempt) until `open_ms` has passed.
half-open : after that, exactly ONE probe call is allowed. Success closes the circuit, failure
            opens it again. A probe that never reports back expires after `probe_ms`, so a
            crashed worker cannot hold the circuit half-open forever.
"""

from redis.asyncio import Redis

ALLOW_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local open_ms = tonumber(ARGV[1])
local probe_ms = tonumber(ARGV[2])
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'closed' then return {1, 0} end
if state == 'open' then
  local opened = tonumber(redis.call('HGET', KEYS[1], 'opened_at') or '0')
  local remaining = open_ms - (now - opened)
  if remaining > 0 then return {0, remaining} end
  redis.call('HSET', KEYS[1], 'state', 'half_open', 'probe_at', now)
  return {2, 0}
end
local probe_at = tonumber(redis.call('HGET', KEYS[1], 'probe_at') or '0')
if now - probe_at >= probe_ms then
  redis.call('HSET', KEYS[1], 'probe_at', now)
  return {2, 0}
end
return {0, probe_ms - (now - probe_at)}
"""

RECORD_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local ok = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local state = redis.call('HGET', KEYS[1], 'state')
if not state then state = 'closed' end
if state == 'open' then return '' end -- late result from a call started before it opened
if ok == 1 then
  if state == 'half_open' then
    redis.call('HSET', KEYS[1], 'state', 'closed', 'failures', 0)
    return 'closed'
  end
  redis.call('HSET', KEYS[1], 'failures', 0)
  return ''
end
if state == 'half_open' then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now)
  return 'opened'
end
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if failures >= threshold then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now)
  return 'opened'
end
return ''
"""

RELEASE_LUA = """
if redis.call('HGET', KEYS[1], 'state') == 'half_open' then
  redis.call('HSET', KEYS[1], 'probe_at', 0)
end
return 1
"""


def circuit_key(provider: str) -> str:
    return f"circuit:{provider}"


class CircuitBreaker:
    def __init__(self, redis: Redis, threshold: int, open_seconds: float, probe_timeout: float):
        self.redis = redis
        self.threshold = threshold
        self.open_ms = int(open_seconds * 1000)
        self.probe_ms = int(probe_timeout * 1000)
        self._allow = redis.register_script(ALLOW_LUA)
        self._record = redis.register_script(RECORD_LUA)
        self._release = redis.register_script(RELEASE_LUA)

    async def allow(self, provider: str) -> tuple[str, float]:
        """('closed'|'probe'|'open', seconds_to_wait). 'probe' means: you are the one test call."""
        decision, wait_ms = await self._allow(
            keys=[circuit_key(provider)], args=[self.open_ms, self.probe_ms]
        )
        return {1: "closed", 2: "probe", 0: "open"}[decision], float(wait_ms) / 1000

    async def record(self, provider: str, success: bool) -> str:
        """Report a call result. Returns 'opened', 'closed' (a transition) or ''."""
        result = await self._record(
            keys=[circuit_key(provider)], args=[1 if success else 0, self.threshold]
        )
        return result or ""

    async def release_probe(self, provider: str) -> None:
        """Give back an unused probe slot (the call was refused for another reason)."""
        await self._release(keys=[circuit_key(provider)])

    async def state(self, provider: str) -> str:
        state = await self.redis.hget(circuit_key(provider), "state")
        return state or "closed"
