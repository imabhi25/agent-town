"""Token bucket in Redis, atomic via a Lua script.

One script call = read the bucket, refill it for the time that passed, take a token if there is
one, write it back. Redis runs a script without interleaving other commands, so concurrent callers
(many API requests, many workers, several processes) can never both take the last token. The
script reads the clock from Redis (TIME), not from the caller, so callers with skewed clocks agree.
"""

from redis.asyncio import Redis

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
elseif rate > 0 then
  retry_after = (cost - tokens) / rate
else
  retry_after = 3600
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
-- Idle buckets disappear: after a full refill an absent bucket behaves identically.
local ttl = 3600000
if rate > 0 then ttl = math.ceil(capacity / rate * 1000) + 1000 end
redis.call('PEXPIRE', key, ttl)
return {allowed, tostring(retry_after)}
"""


class TokenBucket:
    def __init__(self, redis: Redis):
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def take(
        self, key: str, capacity: float, refill_per_sec: float, cost: float = 1
    ) -> tuple[bool, float]:
        """Try to take `cost` tokens. Returns (allowed, seconds_until_it_would_be_allowed)."""
        allowed, retry_after = await self._script(keys=[key], args=[capacity, refill_per_sec, cost])
        return bool(allowed), float(retry_after)
