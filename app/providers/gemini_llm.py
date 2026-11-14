"""Worker-side provider that can use real Gemini for goal tasks, capped per day.

Only jobs of type "task" ask for the real model (`real=True`); mock jobs (floods, chaos, tests)
never do, and neither does any call that sets a mock mode or latency. Over the daily cap, or with
no API key, it answers with the mock, so the demo keeps working without quota.
"""

from google.genai import errors

from app.budget import DailyCap
from app.providers.mock_llm import MockLLM, ProviderError


class RoutingProvider:
    routes_real = True  # the worker passes real=True for task jobs when this is set

    def __init__(self, mock: MockLLM, client, model: str, cap: DailyCap):
        self.mock, self.client, self.model, self.cap = mock, client, model, cap

    async def complete(self, prompt: str, *, mode=None, latency=None, real: bool = False) -> dict:
        if real and mode is None and latency is None and await self.cap.acquire():
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model, contents=prompt
                )
            except errors.APIError as e:
                raise ProviderError(int(e.code or 500), f"gemini: {e.message}") from e
            return {"text": response.text or "", "source": "gemini"}
        return await self.mock.complete(prompt, mode=mode, latency=latency)


def build_provider(redis, api_key: str, model: str, daily_cap: int):
    """The mock alone without a key; otherwise the capped router."""
    mock = MockLLM()
    if not api_key:
        return mock
    from google import genai

    return RoutingProvider(mock, genai.Client(api_key=api_key), model, DailyCap(redis, daily_cap))
