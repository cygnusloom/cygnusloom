"""Model catalog: database-backed LLM configuration with an environment seed for development."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, select
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import LlmModel, utcnow
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope

_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_MODEL_ID = 120
_MAX_TURN_PRICE = 1_000_000
_MAX_CALCULATOR_PRICE = Decimal(1000000000)

# The keys the model cost calculator saves. Prices are per 1M tokens; the two time fields are
# an optional UTC peak window.
CALCULATOR_FIELDS: tuple[str, ...] = (
    "hit-peak",
    "miss-peak",
    "output-peak",
    "hit-off-peak",
    "miss-off-peak",
    "output-off-peak",
    "peak-start",
    "peak-end",
)
_PRICE_FIELDS = frozenset(CALCULATOR_FIELDS[:6])
_TIME_FIELDS = frozenset(CALCULATOR_FIELDS[6:])


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(f"cygnusloom-llm:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(secret: str, value: str) -> str:
    if not value:
        return ""
    return _fernet(secret).encrypt(value.encode()).decode()


def decrypt_api_key(secret: str, value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet(secret).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


@dataclass(frozen=True)
class ModelSpec:
    """One configured model. `api_key` is decrypted for the provider factory and must never be
    put in a template; `api_key_set` is the only key information the admin page shows.

    `credits_per_turn` is shown in the campaign model picker. `stream_usage` asks the provider
    for exact token counts in streamed replies; usage measurements never affect the charge.
    `calculator` holds the administrator's saved cost-calculator prices and peak hours."""

    row_id: int | None
    provider: str
    model: str
    base_url: str
    api_key: str
    api_key_set: bool
    credits_per_turn: int
    stream_usage: bool
    enabled: bool
    is_default: bool
    calculator: dict[str, str]

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model}"


def _identity(provider: str, model: str) -> tuple[str, str, str]:
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not _PROVIDER.fullmatch(provider):
        raise InvalidInput("provider must use letters, digits, dots, dashes, or underscores")
    if not model:
        raise InvalidInput("model name is required")
    model_id = f"{provider}/{model}"
    if len(model_id) > _MAX_MODEL_ID:
        raise InvalidInput(f"model id must be at most {_MAX_MODEL_ID} characters")
    return provider, model, model_id


def _check_base_url(provider: str, base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise InvalidInput("base URL must start with http:// or https://")
    if not base_url and provider != "fake":
        raise InvalidInput("base URL is required for this provider")
    return base_url


def _check_turn_price(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_TURN_PRICE:
        raise InvalidInput(f"credits per turn must be a whole number from 0 to {_MAX_TURN_PRICE}")
    return value


def _parse_calculator(raw: str) -> dict[str, str]:
    """Read saved calculator values from JSON, ignoring unknown or malformed entries."""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    values: dict[str, str] = {}
    for key in CALCULATOR_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and len(value) <= 64:
            values[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values[key] = str(value)
    return values


def _clean_calculator(settings: Mapping[str, str]) -> str:
    """Validate the calculator form and return the JSON stored on the model row."""
    values: dict[str, str] = {}
    for key in CALCULATOR_FIELDS:
        raw = settings.get(key, "")
        if not isinstance(raw, str):
            raise InvalidInput("calculator values must be text")
        value = raw.strip()
        if key in _PRICE_FIELDS:
            if not value:
                values[key] = "0"
                continue
            if len(value) > 32:
                raise InvalidInput("calculator prices must be short numbers")
            try:
                price = Decimal(value)
            except InvalidOperation:
                raise InvalidInput("calculator prices must be non-negative numbers") from None
            if not price.is_finite() or price < 0 or price > _MAX_CALCULATOR_PRICE:
                raise InvalidInput("calculator prices must be non-negative numbers")
            values[key] = value
            continue
        if key in _TIME_FIELDS:
            if not value:
                values[key] = ""
                continue
            try:
                clock = time.fromisoformat(value)
            except ValueError:
                raise InvalidInput("peak hours must look like HH:MM") from None
            values[key] = clock.strftime("%H:%M")
    return json.dumps(values, sort_keys=True)


class ModelCatalog:
    """Reads the current model list. Changes are made by administrators through the admin UI."""

    def __init__(self, engine: Engine, settings: Settings):
        self.engine = engine
        self.settings = settings

    def _rows(self) -> list[LlmModel]:
        with session_scope(self.engine) as db:
            return list(
                db.execute(select(LlmModel).order_by(LlmModel.created_at, LlmModel.id)).scalars()
            )

    def _env_specs(self) -> tuple[ModelSpec, ...]:
        specs = []
        default = self.settings.llm_default_model
        for model_id in self.settings.llm_models:
            provider, sep, model = model_id.partition("/")
            if not sep or not provider or not model:
                raise InvalidInput(f"model id {model_id!r} must look like provider/model")
            specs.append(
                ModelSpec(
                    row_id=None,
                    provider=provider,
                    model=model,
                    base_url=self.settings.provider_base_urls.get(provider, ""),
                    api_key=self.settings.provider_api_keys.get(provider, ""),
                    api_key_set=bool(self.settings.provider_api_keys.get(provider)),
                    credits_per_turn=0,
                    stream_usage=True,
                    enabled=True,
                    is_default=model_id == default,
                    calculator={},
                )
            )
        if specs and not any(spec.is_default for spec in specs):
            first = specs[0]
            specs[0] = ModelSpec(
                row_id=first.row_id,
                provider=first.provider,
                model=first.model,
                base_url=first.base_url,
                api_key=first.api_key,
                api_key_set=first.api_key_set,
                credits_per_turn=first.credits_per_turn,
                stream_usage=first.stream_usage,
                enabled=first.enabled,
                is_default=True,
                calculator=first.calculator,
            )
        return tuple(specs)

    def _spec(self, row: LlmModel) -> ModelSpec:
        return ModelSpec(
            row_id=row.id,
            provider=row.provider,
            model=row.model,
            base_url=row.base_url,
            api_key=decrypt_api_key(self.settings.secret_key, row.api_key),
            api_key_set=bool(row.api_key),
            credits_per_turn=row.credits_per_turn,
            stream_usage=row.stream_usage,
            enabled=row.enabled,
            is_default=row.is_default,
            calculator=_parse_calculator(row.calculator_json),
        )

    def source(self) -> str:
        return "database" if self._rows() else "none"

    def all_specs(self) -> tuple[ModelSpec, ...]:
        return tuple(self._spec(row) for row in self._rows())

    def get(self, row_id: int) -> ModelSpec:
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            return self._spec(row)

    def specs(self) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self.all_specs() if spec.enabled)

    def model_ids(self) -> tuple[str, ...]:
        return tuple(spec.model_id for spec in self.specs())

    def default_model_id(self) -> str:
        specs = self.specs()
        for spec in specs:
            if spec.is_default:
                return spec.model_id
        return specs[0].model_id if specs else ""

    def provider_config(self, provider: str) -> tuple[str | None, str | None]:
        for spec in self.specs():
            if spec.provider == provider:
                return spec.base_url or None, spec.api_key or None
        return None, None

    def stream_usage(self, model_id: str) -> bool:
        """Whether this model should ask for token usage in streamed replies."""
        provider, separator, model = model_id.partition("/")
        if not separator:
            return True
        with session_scope(self.engine) as db:
            value = db.execute(
                select(LlmModel.stream_usage).where(
                    LlmModel.provider == provider, LlmModel.model == model
                )
            ).scalar_one_or_none()
        return True if value is None else bool(value)

    def seed_from_env(self) -> int:
        """Copy the environment list into an empty catalog, once. Existing rows are never
        overwritten, so administrator edits survive restarts."""
        if self._rows():
            return 0
        specs = self._env_specs()
        if not specs:
            return 0
        with session_scope(self.engine) as db:
            for spec in specs:
                db.add(
                    LlmModel(
                        provider=spec.provider,
                        model=spec.model,
                        base_url=spec.base_url,
                        api_key=encrypt_api_key(self.settings.secret_key, spec.api_key),
                        credits_per_turn=spec.credits_per_turn,
                        stream_usage=spec.stream_usage,
                        enabled=True,
                        is_default=spec.is_default,
                        created_at=utcnow(),
                    )
                )
            db.commit()
        return len(specs)

    def add(
        self,
        provider: str,
        model: str,
        base_url: str = "",
        api_key: str = "",
        *,
        credits_per_turn: int = 0,
        stream_usage: bool = True,
        is_default: bool = False,
    ) -> ModelSpec:
        provider, model, _ = _identity(provider, model)
        base_url = _check_base_url(provider, base_url)
        credits_per_turn = _check_turn_price(credits_per_turn)
        # An environment-only list becomes editable before the first database change.
        self.seed_from_env()
        with session_scope(self.engine) as db:
            existing = (
                db.execute(
                    select(LlmModel).where(LlmModel.provider == provider, LlmModel.model == model)
                )
                .scalars()
                .first()
            )
            if existing is not None:
                raise Conflict("that model is already configured")
            if is_default:
                for row in db.execute(select(LlmModel)).scalars():
                    row.is_default = False
                    db.add(row)
            row = LlmModel(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=encrypt_api_key(self.settings.secret_key, api_key),
                credits_per_turn=credits_per_turn,
                stream_usage=stream_usage,
                enabled=True,
                is_default=is_default,
            )
            db.add(row)
            db.flush()
            self._ensure_default(db)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def upsert(
        self,
        provider: str,
        model: str,
        base_url: str = "",
        api_key: str = "",
        *,
        credits_per_turn: int | None = None,
        stream_usage: bool | None = None,
        is_default: bool = True,
    ) -> ModelSpec:
        """Add a model, or make an existing row editable again. Used by the setup wizard, so
        running it twice with the same details is harmless. Pricing settings are left alone when
        the caller omits them."""
        provider, model, _ = _identity(provider, model)
        base_url = _check_base_url(provider, base_url)
        if credits_per_turn is not None:
            credits_per_turn = _check_turn_price(credits_per_turn)
        self.seed_from_env()
        with session_scope(self.engine) as db:
            row = (
                db.execute(
                    select(LlmModel).where(LlmModel.provider == provider, LlmModel.model == model)
                )
                .scalars()
                .first()
            )
            if row is None:
                row = LlmModel(
                    provider=provider,
                    model=model,
                    base_url=base_url,
                    api_key=encrypt_api_key(self.settings.secret_key, api_key),
                    credits_per_turn=credits_per_turn or 0,
                    stream_usage=True if stream_usage is None else stream_usage,
                    enabled=True,
                    is_default=False,
                )
                db.add(row)
                db.flush()
            else:
                row.base_url = base_url
                row.enabled = True
                # A blank key means "keep the stored one", matching the admin page.
                if api_key:
                    row.api_key = encrypt_api_key(self.settings.secret_key, api_key)
                if credits_per_turn is not None:
                    row.credits_per_turn = credits_per_turn
                if stream_usage is not None:
                    row.stream_usage = stream_usage
                db.add(row)
            if is_default:
                for other in db.execute(select(LlmModel)).scalars():
                    other.is_default = other.id == row.id
                    db.add(other)
            self._ensure_default(db)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def update(
        self,
        row_id: int,
        *,
        base_url: str,
        api_key: str | None = None,
        credits_per_turn: int | None = None,
        stream_usage: bool | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ModelSpec:
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            row.base_url = _check_base_url(row.provider, base_url)
            row.enabled = enabled
            if api_key is not None:
                row.api_key = encrypt_api_key(self.settings.secret_key, api_key)
            if credits_per_turn is not None:
                row.credits_per_turn = _check_turn_price(credits_per_turn)
            if stream_usage is not None:
                row.stream_usage = stream_usage
            if make_default:
                for other in db.execute(select(LlmModel)).scalars():
                    other.is_default = False
                    db.add(other)
                row.is_default = True
            db.add(row)
            self._ensure_default(db)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def set_default(self, row_id: int) -> ModelSpec:
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            if not row.enabled:
                row.enabled = True
            for other in db.execute(select(LlmModel)).scalars():
                other.is_default = False
                db.add(other)
            row.is_default = True
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def set_enabled(self, row_id: int, enabled: bool) -> ModelSpec:
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            row.enabled = enabled
            db.add(row)
            self._ensure_default(db)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def set_calculator(self, row_id: int, settings: Mapping[str, str]) -> ModelSpec:
        """Save one model's cost-calculator prices and optional peak hours."""
        encoded = _clean_calculator(settings)
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            row.calculator_json = encoded
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._spec(row)

    def clear_calculators(self) -> int:
        """Reset the saved calculator values for every model. Returns the number of rows."""
        with session_scope(self.engine) as db:
            rows = list(db.execute(select(LlmModel)).scalars())
            for row in rows:
                row.calculator_json = "{}"
                db.add(row)
            db.commit()
            return len(rows)

    def delete(self, row_id: int) -> None:
        with session_scope(self.engine) as db:
            row = db.get(LlmModel, row_id)
            if row is None:
                raise NotFound("model not found")
            db.delete(row)
            db.flush()
            self._ensure_default(db)
            db.commit()

    @staticmethod
    def _ensure_default(db: Session) -> None:
        rows = list(
            db.execute(
                select(LlmModel)
                .where(LlmModel.enabled.is_(True))
                .order_by(LlmModel.created_at, LlmModel.id)
            ).scalars()
        )
        if not rows:
            return
        chosen = next((row for row in rows if row.is_default), rows[0])
        for row in rows:
            desired = row.id == chosen.id
            if row.is_default != desired:
                row.is_default = desired
                db.add(row)
