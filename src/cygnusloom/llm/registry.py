from __future__ import annotations

from cygnusloom.config import Settings
from cygnusloom.llm.base import LLMError, Provider
from cygnusloom.llm.catalog import ModelCatalog
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.llm.openai_compat import OpenAICompatProvider

DEFAULT_BASE_URLS = {"ollama": "http://localhost:11434/v1"}


class ProviderFactory:
    """Turns a model id like `ollama/gemma4:e2b` into a provider and the model name it sends."""

    def __init__(
        self,
        settings: Settings,
        fake: FakeProvider | None = None,
        catalog: ModelCatalog | None = None,
    ):
        self._settings = settings
        self._fake = fake
        self._catalog = catalog
        self._cache: dict[tuple[str, str | None, str | None, bool], Provider] = {}

    @property
    def settings(self) -> Settings:
        """The deployment settings, used by credit exemption checks."""
        return self._settings

    def __call__(self, model_id: str) -> tuple[Provider, str]:
        name, sep, model = model_id.partition("/")
        if not sep or not model:
            raise LLMError(f"model id {model_id!r} must look like provider/model")
        base_url, api_key = self._config(name)
        stream_usage = self._catalog.stream_usage(model_id) if self._catalog else True
        cache_key = (name, base_url, api_key, stream_usage)
        if cache_key not in self._cache:
            try:
                self._cache[cache_key] = self._build(name, base_url, api_key, stream_usage)
            except LLMError:
                raise
            except Exception as exc:
                # Building the client can fail before any request, such as when the machine
                # cannot create a TLS context. Report it as a provider error so the turn
                # records the reason instead of an unexpected 500.
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                raise LLMError(f"provider {name!r} could not start: {detail}") from exc
        return self._cache[cache_key], model

    def _config(self, name: str) -> tuple[str | None, str | None]:
        if self._catalog is not None:
            base_url, api_key = self._catalog.provider_config(name)
            return base_url or DEFAULT_BASE_URLS.get(name), api_key
        base_url = self._settings.provider_base_urls.get(name) or DEFAULT_BASE_URLS.get(name)
        return base_url, self._settings.provider_api_keys.get(name)

    def _build(
        self, name: str, base_url: str | None, api_key: str | None, stream_usage: bool
    ) -> Provider:
        if name == "fake":
            return self._fake or FakeProvider()
        if base_url is None:
            raise LLMError(f"no provider configured for {name!r}")
        return OpenAICompatProvider(name, base_url, api_key=api_key, stream_usage=stream_usage)
