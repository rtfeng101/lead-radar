"""Thin structured-output wrapper so the matching code never imports a vendor SDK directly
(tests substitute a fake by patching `get_llm`)."""

from typing import Protocol, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)


class LLMNotConfigured(RuntimeError):
    pass


class LLMRefused(RuntimeError):
    pass


class LLM(Protocol):
    def parse(self, *, model: str, system: str, user: str, schema: type[T]) -> T: ...


class OpenAILLM:
    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise LLMNotConfigured("OPENAI_API_KEY is not set")
        # The SDK already retries 429 / 5xx with backoff; anything that still escapes goes to the job queue.
        self._client = OpenAI(api_key=settings.openai_api_key, timeout=120.0)

    def parse(self, *, model: str, system: str, user: str, schema: type[T]) -> T:
        response = self._client.responses.parse(model=model, instructions=system, input=user, text_format=schema)
        if response.output_parsed is None:
            raise LLMRefused(f"no parsed output (status={response.status})")
        return response.output_parsed


def get_llm() -> LLM:
    return OpenAILLM()
