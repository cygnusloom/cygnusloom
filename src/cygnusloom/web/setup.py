"""The single-player first-run wizard: choose a provider and add the first model."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from cygnusloom.auth.deps import admin_user, client_ip, verify_csrf
from cygnusloom.llm.presets import PRESETS, ProviderPreset, preset_for
from cygnusloom.llm.probe import ProbeError, list_models, probe_model
from cygnusloom.services import admin as admin_service
from cygnusloom.services.common import Conflict, InvalidInput, NotFound
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/setup")


def _solo(request: Request) -> None:
    if not request.app.state.settings.single_player:
        raise NotFound("setup is only available in single-player mode")


def _provider_name(preset: ProviderPreset) -> str:
    return "fake" if preset.key == "demo" else preset.key


def _preset_for_form(request_form, default: str = "ollama") -> ProviderPreset:
    return preset_for(form_text(request_form, "preset")) or preset_for(default) or PRESETS[0]


def _render(
    request: Request,
    admin,
    *,
    error: str | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "setup.html",
        admin=admin,
        setup_presets=PRESETS,
        setup_models=request.app.state.catalog.all_specs(),
        setup_error=error,
        setup_values=values or {"preset": "ollama"},
        setup_heading="h1",
        status_code=status_code,
    )


@router.get("")
def setup_page(request: Request) -> Response:
    _solo(request)
    admin = admin_user(request)
    if request.app.state.settings.desktop:
        # The desktop wizard lives in the Play on my device tab, beside the server list.
        force = request.query_params.get("force") == "1"
        target = "/campaigns?setup=1#device" if force else "/campaigns#device"
        return RedirectResponse(target, status_code=303)
    if request.app.state.catalog.model_ids() and request.query_params.get("force") != "1":
        return RedirectResponse("/campaigns", status_code=303)
    return _render(request, admin)


@router.post("/detect")
async def detect_models(request: Request) -> JSONResponse:
    _solo(request)
    admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    selected = _preset_for_form(form)
    if selected.key == "demo":
        return JSONResponse({"models": ["scripted"], "error": ""})
    base_url = form_text(form, "base_url").strip() or selected.base_url
    api_key = form_text(form, "api_key").strip()
    try:
        models = await list_models(base_url, api_key)
    except ProbeError as exc:
        return JSONResponse({"models": [], "error": str(exc)})
    return JSONResponse({"models": list(models), "error": ""})


@router.post("/test")
async def test_model(request: Request) -> JSONResponse:
    _solo(request)
    admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    selected = _preset_for_form(form)
    model = form_text(form, "model").strip() or selected.model
    base_url = form_text(form, "base_url").strip() or selected.base_url
    result = await probe_model(
        _provider_name(selected),
        base_url,
        form_text(form, "api_key").strip(),
        model,
    )
    return JSONResponse(
        {"ok": result.ok, "message": result.message, "latency_ms": result.latency_ms}
    )


@router.post("")
async def save_setup(request: Request) -> Response:
    _solo(request)
    admin = admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    selected = _preset_for_form(form)
    provider = _provider_name(selected)
    model = form_text(form, "model").strip() or selected.model
    base_url = form_text(form, "base_url").strip() or selected.base_url
    api_key = form_text(form, "api_key").strip()
    values = {
        "preset": selected.key,
        "model": model,
        "base_url": base_url,
        # Never echo a secret back into the page.
        "api_key": "",
    }
    try:
        spec = request.app.state.catalog.upsert(provider, model, base_url, api_key, is_default=True)
    except (Conflict, InvalidInput) as exc:
        if request.app.state.settings.desktop:
            # Keep the desktop wizard inside the Play on my device tab.
            request.session["flash"] = {"message": str(exc), "kind": "error"}
            request.session["setup_values"] = values
            return RedirectResponse("/campaigns?setup=1#device", status_code=303)
        return _render(request, admin, error=str(exc), values=values, status_code=400)
    admin_service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.added",
        target_type="model",
        target_id=spec.model_id,
        details={"default": spec.is_default, "setup": True},
        ip=client_ip(request),
    )
    request.session["flash"] = {
        "message": f"{spec.model_id} is ready. Create your first campaign.",
        "kind": "info",
    }
    return RedirectResponse("/campaigns/new", status_code=303)
