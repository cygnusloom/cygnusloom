"""Provider presets shown by the first-run setup wizard.

The presets are data, not behavior: the wizard only pre-fills a base URL and model name and
tells the form whether an API key is expected. Any OpenAI-compatible endpoint can be used.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    summary: str
    base_url: str = ""
    model: str = ""
    needs_key: bool = False
    local: bool = False
    docs_url: str = ""


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        key="ollama",
        label="Ollama",
        summary="Free models running on this computer.",
        base_url="http://127.0.0.1:11434/v1",
        local=True,
        docs_url="https://ollama.com/download",
    ),
    ProviderPreset(
        key="lmstudio",
        label="LM Studio",
        summary="Local models served by LM Studio's OpenAI-compatible API.",
        base_url="http://127.0.0.1:1234/v1",
        local=True,
        docs_url="https://lmstudio.ai/",
    ),
    ProviderPreset(
        key="deepseek",
        label="DeepSeek",
        summary="Low-cost cloud model with an OpenAI-compatible API.",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        needs_key=True,
        docs_url="https://platform.deepseek.com/",
    ),
    ProviderPreset(
        key="openai",
        label="OpenAI",
        summary="OpenAI's hosted models.",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        needs_key=True,
        docs_url="https://platform.openai.com/api-keys",
    ),
    ProviderPreset(
        key="openrouter",
        label="OpenRouter",
        summary="One API key for many hosted models.",
        base_url="https://openrouter.ai/api/v1",
        needs_key=True,
        docs_url="https://openrouter.ai/keys",
    ),
    ProviderPreset(
        key="custom",
        label="Other endpoint",
        summary="Any server that speaks the OpenAI chat completions API.",
        needs_key=False,
    ),
    ProviderPreset(
        key="demo",
        label="Scripted demo",
        summary="Try the game without a model. Replies are scripted and repeat your text.",
        model="scripted",
    ),
)


def preset_for(key: str | None) -> ProviderPreset | None:
    for preset in PRESETS:
        if preset.key == key:
            return preset
    return None
