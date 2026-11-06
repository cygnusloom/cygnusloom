"""Model catalog: database-backed LLM configuration with an environment seed for development."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, select
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import LlmModel, utcnow
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope

_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_MODEL_ID = 120


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
    put in a template; `api_key_set` is the only key information the admin page shows."""

    row_id: int | None
    provider: str
    model: str
    base_url: str
    api_key: str
    api_key_set: bool
    enabled: bool
    is_default: bool

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
                    enabled=True,
                    is_default=model_id == default,
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
                enabled=first.enabled,
                is_default=True,
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
            enabled=row.enabled,
            is_default=row.is_default,
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
        is_default: bool = False,
    ) -> ModelSpec:
        provider, model, _ = _identity(provider, model)
        base_url = _check_base_url(provider, base_url)
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
        is_default: bool = True,
    ) -> ModelSpec:
        """Add a model, or make an existing row editable again. Used by the setup wizard, so
        running it twice with the same details is harmless."""
        provider, model, _ = _identity(provider, model)
        base_url = _check_base_url(provider, base_url)
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
