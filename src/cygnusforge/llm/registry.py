from __future__ import annotations

from cygnusforge.config import Settings
from cygnusforge.llm.base import LLMError, Provider
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.llm.openai_compat import OpenAICompatProvider

DEFAULT_BASE_URLS = {"ollama": "http://localhost:11434/v1"}


class ProviderFactory:
    """Turns a model id like `ollama/gemma4:e2b` into a provider and the model name it sends."""

    def __init__(self, settings: Settings, fake: FakeProvider | None = None):
        self._settings = settings
        self._fake = fake
        self._cache: dict[str, Provider] = {}

    def __call__(self, model_id: str) -> tuple[Provider, str]:
        name, sep, model = model_id.partition("/")
        if not sep or not model:
            raise LLMError(f"model id {model_id!r} must look like provider/model")
        if name not in self._cache:
            self._cache[name] = self._build(name)
        return self._cache[name], model

    def _build(self, name: str) -> Provider:
        if name == "fake":
            return self._fake or FakeProvider()
        base_url = self._settings.provider_base_urls.get(name) or DEFAULT_BASE_URLS.get(name)
        if base_url is None:
            raise LLMError(f"no provider configured for {name!r}")
        return OpenAICompatProvider(
            name, base_url, api_key=self._settings.provider_api_keys.get(name)
        )
