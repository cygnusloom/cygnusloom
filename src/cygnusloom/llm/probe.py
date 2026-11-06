"""Connection checks used by the setup wizard before a model is saved."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from cygnusloom.llm.base import LLMError
from cygnusloom.llm.openai_compat import OpenAICompatProvider

PROBE_PROMPT = "Reply with the single word: ready"
PROBE_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_LISTED_MODELS = 100


class ProbeError(Exception):
    """A connection check that failed in a way the wizard can explain."""


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    latency_ms: int = 0


def _check_endpoint(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ProbeError("The base URL must start with http:// or https://.")
    return base_url


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def list_models(
    base_url: str,
    api_key: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, ...]:
    """Model ids from an OpenAI-compatible `/models` endpoint."""
    base_url = _check_endpoint(base_url)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT)
    try:
        response = await client.get(f"{base_url}/models", headers=_headers(api_key))
        response.raise_for_status()
        data: Any = response.json()
    except httpx.HTTPStatusError as exc:
        raise ProbeError(
            f"The server answered HTTP {exc.response.status_code} when listing models."
        ) from exc
    except httpx.HTTPError as exc:
        raise ProbeError("Could not reach the server. Is it running?") from exc
    except ValueError as exc:
        raise ProbeError("The server did not return a model list.") from exc
    finally:
        if own_client:
            await client.aclose()
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProbeError("The server did not return a model list.")
    models = sorted(
        {
            str(row["id"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
        }
    )
    return tuple(models[:MAX_LISTED_MODELS])


async def probe_model(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Send one tiny message so the wizard knows the key, URL, and model all work."""
    model = (model or "").strip()
    if not model:
        return ProbeResult(False, "Choose or type a model name first.")
    if provider == "fake":
        return ProbeResult(True, "The scripted demo is ready.")
    try:
        base_url = _check_endpoint(base_url)
    except ProbeError as exc:
        return ProbeResult(False, str(exc))
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT)
    started = time.perf_counter()
    try:
        model_provider = OpenAICompatProvider(
            provider, base_url, api_key=api_key or None, client=client
        )
        reply = await model_provider.chat(model, [{"role": "user", "content": PROBE_PROMPT}])
    except LLMError as exc:
        return ProbeResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001 - a bad URL or transport must not break the wizard
        return ProbeResult(False, f"Connection failed: {exc}")
    finally:
        if own_client:
            await client.aclose()
    elapsed = int((time.perf_counter() - started) * 1000)
    answer = (reply.text or "").strip().replace("\n", " ")[:120]
    detail = f"Model answered: {answer}" if answer else "The model answered."
    return ProbeResult(True, detail, elapsed)
