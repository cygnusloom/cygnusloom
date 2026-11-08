"""Portable campaign files: one raw JSON document for everything a campaign owns.

A campaign is its row plus its event log. Characters, maps, notes, objects, summaries, and
pending turns all live in that log, so an export is the row and every event, and an import
rebuilds them under a new owner. Event ids are local to a database, so an import allocates new
ones and rewrites the few payload fields that refer to another event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import MISSING, dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select

from cygnusloom import __version__
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary, read_table_genres
from cygnusloom.db.models import Campaign, Event, utcnow
from cygnusloom.services.campaigns import (
    MAX_MODEL,
    MAX_NAME,
    MAX_SETTING,
    _exclusive,
    get_owned_campaign,
)
from cygnusloom.services.common import Conflict, InvalidInput, session_scope
from cygnusloom.services.events import EVENT_TYPES, Channel, GameEvent, load_state
from cygnusloom.services.names import next_campaign_label
from cygnusloom.services.sessions import as_utc

EXPORT_FORMAT = "cygnusloom.campaign"
EXPORT_VERSION = 1
MAX_IMPORT_BYTES = 256 * 1024 * 1024
MAX_IMPORT_EVENTS = 500_000
MAX_COLUMN = 64
_SLUG = re.compile(r"[^a-z0-9]+")
_CHANNELS = {channel.value for channel in Channel}


@dataclass(frozen=True)
class ImportResult:
    campaign: Campaign
    model_replaced: str = ""
    model_used: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Entry:
    source_id: int
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: datetime
    cls: type[GameEvent]


def export_filename(campaign: Campaign) -> str:
    slug = _SLUG.sub("-", campaign.name.lower()).strip("-")[:40] or "campaign"
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return f"cygnusloom-{slug}-{stamp}.json"


def export_campaign(engine: Engine, owner_id: int, campaign_id: int) -> tuple[str, bytes]:
    """The campaign's file name and JSON bytes. Refused while its GM is still writing."""
    campaign = get_owned_campaign(engine, owner_id, campaign_id)
    with _exclusive(campaign_id), session_scope(engine) as db:
        rows = list(
            db.execute(
                select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq)
            ).scalars()
        )
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row.payload)
        except ValueError as exc:
            raise InvalidInput(f"event {row.id} has a malformed payload") from exc
        events.append(
            {
                "seq": row.seq,
                "id": row.id,
                "kind": row.kind,
                "channel": row.channel,
                "created_at": as_utc(row.created_at).isoformat(),
                "payload": payload,
            }
        )
    try:
        genres = json.loads(campaign.table_genres) if campaign.table_genres else []
    except ValueError:
        genres = []
    if not isinstance(genres, list):
        genres = []
    document = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "app_version": __version__,
        "source": {
            "campaign_id": campaign.id,
            "event_count": len(events),
            "last_event_id": events[-1]["id"] if events else 0,
        },
        "campaign": {
            "name": campaign.name,
            "world_template": campaign.world_template,
            "rule_set": campaign.rule_set,
            "model": campaign.model,
            "game_version": campaign.game_version,
            "setting": campaign.setting,
            "table_genres": genres,
            "use_inventory_space": campaign.use_inventory_space,
            "created_at": as_utc(campaign.created_at).isoformat(),
        },
        "events": events,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return export_filename(campaign), payload


def import_campaign(
    engine: Engine,
    library: ContentLibrary,
    allowed_models: Iterable[str],
    default_model: str,
    owner_id: int,
    data: bytes,
) -> ImportResult:
    """Rebuild a campaign from an exported file under `owner_id`."""
    document = _parse_document(data)
    campaign_data, entries = _read_document(document)
    exported_app_version = document.get("app_version")
    config = _read_campaign(
        campaign_data,
        library,
        exported_app_version if isinstance(exported_app_version, str) else "",
    )
    allowed = set(allowed_models)
    model = config["model"]
    model_replaced = ""
    warnings: list[str] = []
    if model not in allowed:
        if default_model:
            model_replaced, model = model, default_model
        elif model:
            warnings.append(
                f"the model {model} is not configured on this server; add it before playing"
            )
        else:
            raise InvalidInput("the campaign has no model and this server has no default model")
    with session_scope(engine) as db:
        existing = set(
            db.execute(select(Campaign.name).where(Campaign.owner_id == owner_id)).scalars()
        )
        name = config["name"]
        if not name:
            world_names = list(
                db.execute(
                    select(Campaign.name).where(
                        Campaign.owner_id == owner_id,
                        Campaign.world_template == config["world_template"],
                    )
                ).scalars()
            )
            template = library.world_templates[config["world_template"]]
            name = next_campaign_label(template.name, world_names)
        campaign = Campaign(
            owner_id=owner_id,
            name=_unique_name(name, existing),
            world_template=config["world_template"],
            rule_set=config["rule_set"],
            model=model,
            game_version=config["game_version"],
            setting=config["setting"],
            table_genres=json.dumps(list(config["table_genres"])),
            use_inventory_space=config["use_inventory_space"],
            created_at=config["created_at"],
        )
        db.add(campaign)
        db.flush()
        if campaign.id is None:
            raise RuntimeError("the imported campaign was not assigned an id")
        rows: list[Event] = []
        for index, entry in enumerate(entries):
            rows.append(
                Event(
                    campaign_id=campaign.id,
                    seq=index + 1,
                    channel=entry.cls.channel.value,
                    kind=entry.kind,
                    payload=json.dumps(entry.payload, ensure_ascii=False, separators=(",", ":")),
                    created_at=entry.created_at,
                )
            )
        db.add_all(rows)
        db.flush()
        new_by_seq = {
            seq: event_id
            for event_id, seq in db.execute(
                select(Event.id, Event.seq).where(Event.campaign_id == campaign.id)
            ).all()
        }
        old_to_new = {entry.source_id: new_by_seq[index + 1] for index, entry in enumerate(entries)}
        for row, entry in zip(rows, entries, strict=True):
            changed = False
            for field_name in entry.cls.id_refs:
                if field_name in entry.payload:
                    entry.payload[field_name] = old_to_new[entry.payload[field_name]]
                    changed = True
            # Account ids do not travel between servers. Every character and message in an
            # imported campaign belongs to the account importing it, which can share it again.
            if entry.kind in ("pc.added", "hireling.promoted", "turn.opening", "chat.player"):
                entry.payload["player_id"] = owner_id
                # BACKWARDS-COMPATIBILITY: 3 - older exports stored a player name snapshot.
                # New events store only the account id.
                entry.payload.pop("player_name", None)
                changed = True
            if changed:
                row.payload = json.dumps(entry.payload, ensure_ascii=False, separators=(",", ":"))
                db.add(row)
        db.flush()
        try:
            load_state(db, campaign.id)
        except Exception as exc:
            raise InvalidInput(f"the campaign log could not be replayed: {exc}") from exc
        db.commit()
        db.refresh(campaign)
    return ImportResult(
        campaign=campaign,
        model_replaced=model_replaced,
        model_used=model,
        warnings=tuple(warnings),
    )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput(f"the file repeats the key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")


def _parse_document(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_IMPORT_BYTES:
        raise InvalidInput(f"the file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidInput("the file is not valid UTF-8") from exc
    try:
        document = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except RecursionError as exc:
        raise InvalidInput("the file is nested too deeply") from exc
    except ValueError as exc:
        raise InvalidInput(f"the file is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InvalidInput("the file must contain a JSON object")
    return document


def _read_document(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[_Entry]]:
    if document.get("format") != EXPORT_FORMAT:
        raise InvalidInput("that is not a Cygnus Loom campaign file")
    version = document.get("version")
    if type(version) is not int or version != EXPORT_VERSION:
        raise InvalidInput(f"campaign file version {version!r} is not supported")
    campaign_data = document.get("campaign")
    events_data = document.get("events")
    if not isinstance(campaign_data, dict):
        raise InvalidInput("the file has no campaign details")
    if not isinstance(events_data, list):
        raise InvalidInput("the file has no event list")
    source = document.get("source")
    if isinstance(source, dict):
        declared = source.get("event_count")
        if declared is not None and declared != len(events_data):
            raise InvalidInput("the file's event count does not match its event list")
    entries = _read_entries(events_data)
    if isinstance(source, dict):
        declared_last = source.get("last_event_id")
        last = entries[-1].source_id if entries else 0
        if declared_last is not None and declared_last != last:
            raise InvalidInput("the file's last event id does not match its event list")
    return campaign_data, entries


def _read_entries(events_data: list[Any]) -> list[_Entry]:
    if len(events_data) > MAX_IMPORT_EVENTS:
        raise InvalidInput(f"the file has more than {MAX_IMPORT_EVENTS} events")
    entries: list[_Entry] = []
    seen_ids: set[int] = set()
    previous_seq = 0
    for index, raw in enumerate(events_data, start=1):
        where = f"event {index}"
        if not isinstance(raw, dict):
            raise InvalidInput(f"{where} must be an object")
        seq = raw.get("seq")
        if type(seq) is not int or seq <= 0:
            raise InvalidInput(f"{where}: seq must be a positive whole number")
        if seq <= previous_seq:
            raise InvalidInput(f"{where}: seq values must strictly increase")
        previous_seq = seq
        source_id = raw.get("id")
        if type(source_id) is not int or source_id <= 0:
            raise InvalidInput(f"{where}: id must be a positive whole number")
        if source_id in seen_ids:
            raise InvalidInput(f"{where}: event id {source_id} appears twice")
        seen_ids.add(source_id)
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            raise InvalidInput(f"{where}: unknown event kind {kind!r}")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise InvalidInput(f"{where}: payload must be an object")
        channel = raw.get("channel")
        if channel is not None and channel not in _CHANNELS:
            raise InvalidInput(f"{where}: unknown channel {channel!r}")
        cls = EVENT_TYPES[kind]
        _check_required(cls, payload, where)
        created_at = _read_time(raw.get("created_at"), where)
        entries.append(_Entry(source_id, seq, kind, payload, created_at, cls))
    _check_references(entries)
    return entries


def _check_required(cls: type[GameEvent], payload: dict[str, Any], where: str) -> None:
    try:
        declared = fields(cls)
    except TypeError:
        return
    for field in declared:
        if not field.init or field.name in payload:
            continue
        if field.default is MISSING and field.default_factory is MISSING:
            raise InvalidInput(f"{where}: {cls.kind} is missing {field.name}")


def _check_references(entries: list[_Entry]) -> None:
    index_by_id = {entry.source_id: index for index, entry in enumerate(entries)}
    for index, entry in enumerate(entries):
        for field_name, target_kinds in entry.cls.id_refs.items():
            if field_name not in entry.payload:
                continue
            target = entry.payload[field_name]
            target_index = index_by_id.get(target) if type(target) is int else None
            if target_index is None or target_index >= index:
                raise InvalidInput(
                    f"event {index + 1}: {field_name} must refer to an earlier event"
                )
            target_kind = entries[target_index].kind
            if target_kind not in target_kinds:
                raise InvalidInput(
                    f"event {index + 1}: {field_name} must refer to "
                    f"{', '.join(target_kinds)}, not {target_kind}"
                )


def _read_time(value: Any, where: str) -> datetime:
    if value is None:
        return utcnow()
    if not isinstance(value, str):
        raise InvalidInput(f"{where}: created_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput(f"{where}: created_at is not a valid timestamp") from exc
    return as_utc(parsed)


def _text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidInput(f"{key} must be text")
    value = value.strip()
    if len(value) > limit:
        raise InvalidInput(f"{key} must be at most {limit} characters")
    return value


def _read_campaign(
    data: dict[str, Any], library: ContentLibrary, exported_app_version: str = ""
) -> dict[str, Any]:
    world_template = _text(data, "world_template", MAX_COLUMN)
    rule_set = _text(data, "rule_set", MAX_COLUMN)
    if world_template not in library.world_templates:
        raise InvalidInput("that world template is not available on this server")
    if rule_set not in library.rule_sets:
        raise InvalidInput("that rule set is not available on this server")
    template = library.world_templates[world_template]
    if template.rule_set is not None and rule_set != template.rule_set:
        raise InvalidInput(
            f"that world only works with the {library.rule_sets[template.rule_set].name} rule set"
        )
    try:
        library.world_content(world_template, rule_set)
    except ContentError as error:
        raise InvalidInput(f"that world does not work with that rule set: {error}") from error
    rules = library.rule_sets[rule_set]
    if "table_genres" in data:
        raw_genres = data.get("table_genres")
        if raw_genres is None:
            raw_genres = []
        if isinstance(raw_genres, str):
            try:
                raw_genres = json.loads(raw_genres)
            except ValueError as exc:
                raise InvalidInput("table_genres must be a list of strings") from exc
        if not isinstance(raw_genres, list) or any(
            not isinstance(item, str) for item in raw_genres
        ):
            raise InvalidInput("table_genres must be a list of strings")
        try:
            genres = read_table_genres(raw_genres, "table genres")
        except ContentError as error:
            raise InvalidInput(str(error)) from error
    else:
        genres = rules.table_genres
    # A file exported before the option existed keeps the rule set's limits, as the campaign it
    # came from did.
    use_inventory_space = data.get("use_inventory_space", True)
    if not isinstance(use_inventory_space, bool):
        raise InvalidInput("use_inventory_space must be true or false")
    created_at = _read_time(data.get("created_at"), "campaign")
    # An exported campaign that records its version carries the key even when the value is
    # blank, meaning it began before version tracking. Only a file from an older app, which has
    # no key at all, falls back to the app version that exported it.
    if "game_version" in data:
        game_version = _text(data, "game_version", MAX_COLUMN)
    else:
        game_version = exported_app_version or __version__
    return {
        "name": _text(data, "name", MAX_NAME),
        "world_template": world_template,
        "rule_set": rule_set,
        "model": _text(data, "model", MAX_MODEL),
        "game_version": game_version,
        "setting": _text(data, "setting", MAX_SETTING),
        "table_genres": genres,
        "use_inventory_space": use_inventory_space,
        "created_at": created_at,
    }


def _unique_name(name: str, existing: set[str]) -> str:
    if name not in existing:
        return name
    for attempt in range(1, 1000):
        suffix = " (imported)" if attempt == 1 else f" (imported {attempt})"
        candidate = f"{name[: MAX_NAME - len(suffix)].rstrip()}{suffix}"
        if candidate not in existing:
            return candidate
    raise Conflict("there are too many campaigns with that name")
