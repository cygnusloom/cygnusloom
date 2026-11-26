"""Uploaded source documents: the adventure a campaign is based on.

A published module is copyrighted, so it is never bundled with the app. The owner uploads it when
the campaign is created, the text is extracted once, and the campaign stores that text with a
section index. The AI GM reads it as plain text through the document tools: it can list the
index, read any character range, and search for a word. Only text is stored, so a PDF is never
served back and no player page shows the source.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, CampaignDocument
from cygnusloom.services import credits as credit_service
from cygnusloom.services.common import InvalidInput, session_scope

# A module is a whole book, so the upload cap is generous. The stored-text cap keeps one file
# from dominating the database, the prompt budget, and every search.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_CHARACTERS = 2_000_000
MAX_PDF_PAGES = 3000
MAX_SECTIONS = 400
# A section index is part of a tool result, so a book with hundreds of headings still gets a
# readable overview; search reaches the rest.
LISTED_SECTIONS = 80
# One credit per 100,000 characters of extracted text, rounded up, with a one-credit minimum.
# The extracted text is about 25,000 words, or one long turn's worth of input tokens, so the
# price tracks what the GM's reading adds to the campaign's model use.
CHARACTERS_PER_CREDIT = 100_000
READ_LENGTH = 6000
MAX_READ_LENGTH = 20000
SEARCH_RESULTS = 8
# How far around a query word another query word may sit and still share a passage's score.
_SEARCH_WINDOW = 260
_SEARCH_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_PASSAGE_CHARS = 8000
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_CHAPTER_HEADING = re.compile(
    r"^[ \t]*(part|chapter|appendix|book|act|scene|introduction|prologue|epilogue|interlude)"
    r"\b[^\n]{0,80}$",
    re.IGNORECASE | re.MULTILINE,
)
_CAPS_HEADING = re.compile(r"^[ \t]*([A-Z0-9][A-Z0-9 '&:,.!?()/-]{2,59})[ \t]*$", re.MULTILINE)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentSection:
    """One entry of a document's index: a heading and where its text starts."""

    title: str
    offset: int
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"title": self.title, "offset": self.offset}
        if self.page is not None:
            entry["page"] = self.page
        return entry


@dataclass(frozen=True)
class DocumentSummary:
    """Just what the turn orientation needs, so a turn never reads a book's text to name it."""

    name: str
    kind: str
    characters: int


@dataclass(frozen=True)
class DocumentText:
    """Extracted text and its index, ready to store."""

    kind: str
    text: str
    sections: tuple[DocumentSection, ...]
    title: str = ""
    pages: int = 0


def document_name(filename: str) -> str:
    """The uploaded file's own name, without any path or control characters, capped for the
    database. A name reaches the GM's prompt, so a newline in it must not become an instruction."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f]+", " ", base).strip()
    return (base or "Adventure source")[:200]


def document_kind(filename: str) -> str:
    lower = filename.casefold()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".md", ".markdown")):
        return "markdown"
    return "text"


def document_price(characters: int) -> int:
    """What one upload costs: one credit per 100,000 characters, at least one credit."""
    return max(1, math.ceil(max(0, characters) / CHARACTERS_PER_CREDIT))


def prepare(filename: str, data: bytes) -> DocumentText:
    """Extract the text and section index from an uploaded file. Raises `InvalidInput` with a
    player-facing reason for an empty, unreadable, scanned, or oversized file."""
    if not data:
        raise InvalidInput("that file is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        limit = MAX_DOCUMENT_BYTES // (1024 * 1024)
        raise InvalidInput(f"that file is larger than {limit} MB")
    if data[:5] == b"%PDF-":
        return _prepare_pdf(filename, data)
    return _prepare_text(filename, data)


def _prepare_pdf(filename: str, data: bytes) -> DocumentText:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise InvalidInput("that file could not be read as a PDF") from exc
    if reader.is_encrypted:
        raise InvalidInput("that PDF is password protected; remove its password and try again")
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise InvalidInput("that file could not be read as a PDF") from exc
    if page_count > MAX_PDF_PAGES:
        raise InvalidInput(f"that PDF has more than {MAX_PDF_PAGES:,} pages")
    try:
        title = str(reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        logger.debug("could not read the PDF title", exc_info=True)
        title = ""
    parts: list[str] = []
    page_starts: list[int] = []
    position = 0
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            logger.debug("could not extract one PDF page", exc_info=True)
            page_text = ""
        part = page_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").strip()
        page_starts.append(position)
        parts.append(part)
        position += len(part) + 2
    text = "\n\n".join(parts).strip()
    if len(text) < 100:
        raise InvalidInput(
            "that PDF has no readable text; a scanned PDF needs OCR before it can be uploaded"
        )
    sections: list[DocumentSection] = []
    try:
        _walk_outline(reader.outline, reader, page_starts, parts, sections)
    except Exception:
        logger.debug("could not read the PDF outline", exc_info=True)
        sections = []
    if not sections:
        sections = _sections_from_text(text)
        if len(sections) < 2 and page_count > 1:
            sections = [
                DocumentSection(f"Page {index}", start, index + 1)
                for index, start in enumerate(page_starts)
                if parts[index]
            ]
    return _finish(document_kind(filename), text, sections, title=title, pages=page_count)


def _walk_outline(
    items: Sequence[Any],
    reader: Any,
    page_starts: Sequence[int],
    page_texts: Sequence[str],
    out: list[DocumentSection],
) -> None:
    """Turn PDF bookmarks into index entries. A bookmark names a page; the title is looked up
    on that page so reading starts at the heading rather than the top of the page."""
    for item in items:
        if isinstance(item, list):
            _walk_outline(item, reader, page_starts, page_texts, out)
            continue
        title = _clean_title(str(getattr(item, "title", "") or ""))
        if not title:
            continue
        try:
            page_number = reader.get_destination_page_number(item)
        except Exception:
            logger.debug("could not resolve a PDF bookmark", exc_info=True)
            continue
        if not 0 <= page_number < len(page_starts):
            continue
        offset = page_starts[page_number]
        found = page_texts[page_number].casefold().find(title.casefold())
        if found >= 0:
            offset += found
        out.append(DocumentSection(title=title, offset=offset, page=page_number + 1))


def _clean_title(title: str) -> str:
    cleaned = title.replace("\r", "").replace("\n", " ")
    return re.sub(r"\s+", " ", cleaned).strip()[:120]


def _prepare_text(filename: str, data: bytes) -> DocumentText:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n").strip()
    if len(text) < 100:
        raise InvalidInput("that file has almost no readable text")
    return _finish(document_kind(filename), text, _sections_from_text(text))


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _sections_from_text(text: str) -> list[DocumentSection]:
    sections = [
        DocumentSection(match.group(2).strip()[:120], match.start())
        for match in _MARKDOWN_HEADING.finditer(text)
        if match.group(2).strip()
    ]
    if sections:
        return sections
    sections = [
        DocumentSection(match.group(0).strip()[:120], match.start())
        for match in _CHAPTER_HEADING.finditer(text)
        if match.group(0).strip()
    ]
    for match in _CAPS_HEADING.finditer(text):
        line = match.group(1).strip()
        if sum(character.isalpha() for character in line) < 3:
            continue
        if line.endswith((".", ",", ";", ":")):
            continue
        # Only a standalone line is a heading, not a shouted word inside a paragraph.
        before = text[: match.start()].rstrip(" \t")
        if before and not before.endswith("\n\n"):
            continue
        sections.append(DocumentSection(line[:120], match.start()))
    if len(sections) < 2 and len(text) > _PASSAGE_CHARS:
        return _passage_sections(text)
    return sections


def _passage_sections(text: str) -> list[DocumentSection]:
    """Fixed-size passages for a book with no headings, so the GM can jump around it."""
    sections: list[DocumentSection] = []
    for start in range(0, len(text), _PASSAGE_CHARS):
        first_line = text[start : start + _PASSAGE_CHARS].strip().split("\n", 1)[0].strip()
        title = (first_line or f"Passage {len(sections) + 1}")[:80]
        sections.append(DocumentSection(title=title, offset=start))
    return sections


def restore(
    text: str,
    kind: str = "text",
    sections: object = None,
    pages: int = 0,
) -> DocumentText:
    """Rebuild a document from an exported campaign file. A missing or malformed index is
    rebuilt from the text, so an old or hand-edited export still gives the GM a usable index."""
    if not isinstance(text, str) or len(text.strip()) < 100:
        raise InvalidInput("an exported source document has no readable text")
    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise InvalidInput(
            f"an exported source document has more than {MAX_DOCUMENT_CHARACTERS:,} characters"
        )
    usable_kind = kind if isinstance(kind, str) and kind in ("pdf", "markdown", "text") else "text"
    parsed: list[DocumentSection] = []
    if isinstance(sections, list):
        for entry in sections:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            offset = entry.get("offset")
            page = entry.get("page")
            if isinstance(title, str) and type(offset) is int:
                parsed.append(DocumentSection(title, offset, page if type(page) is int else None))
    if not parsed:
        parsed = _sections_from_text(text)
    return _finish(
        usable_kind,
        text,
        parsed,
        pages=pages if type(pages) is int and pages > 0 else 0,
    )


def _finish(
    kind: str,
    text: str,
    sections: Sequence[DocumentSection],
    *,
    title: str = "",
    pages: int = 0,
) -> DocumentText:
    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise InvalidInput(
            f"that file has more than {MAX_DOCUMENT_CHARACTERS:,} characters of text; "
            "split it into smaller files"
        )
    kept: list[DocumentSection] = []
    seen: set[tuple[str, int]] = set()
    for section in sections:
        if not 0 <= section.offset < len(text):
            continue
        key = (section.title.casefold(), section.offset)
        if key in seen:
            continue
        seen.add(key)
        kept.append(DocumentSection(section.title, section.offset, section.page))
    kept.sort(key=lambda section: section.offset)
    if not kept or kept[0].offset != 0:
        kept.insert(0, DocumentSection("Beginning", 0))
    return DocumentText(
        kind=kind,
        text=text,
        sections=tuple(kept[:MAX_SECTIONS]),
        title=_clean_title(title),
        pages=pages,
    )


def document_credits(
    engine: Engine, settings: Settings | None, campaign: Campaign, chars: int
) -> int:
    """The upload's price after the campaign's model and the owner's exemption are applied.
    A free model or an exempt account makes the document free, exactly like a reply."""
    if credit_service.model_price(engine, campaign.model) <= 0:
        return 0
    if credit_service.is_exempt(engine, settings, campaign):
        return 0
    return document_price(chars)


def add_document(
    engine: Engine,
    settings: Settings | None,
    campaign: Campaign,
    prepared: DocumentText,
    name: str,
) -> tuple[int, int]:
    """Store an extracted document and charge its price. Returns its id and the credits charged.
    The price is checked before the row is written, so an unaffordable upload stores nothing."""
    price = document_credits(engine, settings, campaign, len(prepared.text))
    if price > 0:
        current = credit_service.balance(engine, campaign.owner_id)
        if current < price:
            noun = "credit" if price == 1 else "credits"
            raise InvalidInput(
                f"that source document would cost {price} {noun}, but the owner has {current}. "
                "An administrator can add credits."
            )
    with session_scope(engine) as db:
        row = _store(db, campaign.id or 0, prepared, name, price)
        document_id = row.id or 0
        db.commit()
    if price > 0:
        credit_service.charge_document(
            engine,
            campaign.owner_id,
            price,
            campaign_id=campaign.id or 0,
            note=f"source document {name}",
        )
    return document_id, price


def store_document(
    db: Session,
    campaign_id: int,
    prepared: DocumentText,
    name: str,
) -> CampaignDocument:
    """Write a document without charging for it. Campaign import uses this."""
    return _store(db, campaign_id, prepared, name, 0)


def _store(
    db: Session,
    campaign_id: int,
    prepared: DocumentText,
    name: str,
    credits: int,
) -> CampaignDocument:
    row = CampaignDocument(
        campaign_id=campaign_id,
        name=document_name(name),
        kind=prepared.kind,
        characters=len(prepared.text),
        credits=credits,
        text=prepared.text,
        sections=json.dumps([section.as_dict() for section in prepared.sections]),
    )
    db.add(row)
    db.flush()
    return row


def campaign_documents(engine: Engine, campaign_id: int) -> tuple[CampaignDocument, ...]:
    """The campaign's source documents, oldest first."""
    with session_scope(engine) as db:
        rows = db.execute(
            select(CampaignDocument)
            .where(CampaignDocument.campaign_id == campaign_id)
            .order_by(CampaignDocument.id)
        ).scalars()
        return tuple(rows)


def document_summaries(engine: Engine, campaign_id: int) -> tuple[DocumentSummary, ...]:
    """Name, kind, and size of the campaign's source documents, without their text. The turn
    orientation only needs this, and a turn must not read a whole book to list its name."""
    with session_scope(engine) as db:
        rows = db.execute(
            select(
                CampaignDocument.name,
                CampaignDocument.kind,
                CampaignDocument.characters,
            )
            .where(CampaignDocument.campaign_id == campaign_id)
            .order_by(CampaignDocument.id)
        ).all()
    return tuple(DocumentSummary(name, kind, characters) for name, kind, characters in rows)


def document_sections(row: CampaignDocument) -> tuple[DocumentSection, ...]:
    try:
        entries = json.loads(row.sections)
    except (TypeError, ValueError):
        return ()
    if not isinstance(entries, list):
        return ()
    sections: list[DocumentSection] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        offset = entry.get("offset")
        page = entry.get("page")
        if not isinstance(title, str) or type(offset) is not int:
            continue
        sections.append(
            DocumentSection(
                title=title,
                offset=offset,
                page=page if type(page) is int else None,
            )
        )
    return tuple(sections)


def state_text(rows: Sequence[DocumentSummary | CampaignDocument]) -> str:
    """The private orientation the GM reads every turn. Empty when nothing was uploaded."""
    if not rows:
        return ""
    listed = "\n".join(
        f"- {row.name} ({row.kind}, {row.characters:,} characters)" for row in rows[:8]
    )
    if len(rows) > 8:
        listed += f"\n- and {len(rows) - 8} more"
    return (
        "Adventure source: this campaign is based on the uploaded source documents below. "
        "Treat their places, people, events, and secrets as the campaign's canon, and follow "
        "them when play reaches what they describe. The source supplies this campaign's "
        "setting, so do not invent a different setting around it. Use list_documents to see "
        "each source's section index, read_document to read a passage before playing a scene "
        "it covers, and search_documents to find a name, place, or topic. Read the relevant "
        "passage before inventing anything it settles. The players may know the adventure, so "
        "never reveal what they have not learned in play. Where a source and the world or rule "
        "set disagree, the source settles the story and the rule set settles the rules.\n" + listed
    )


class DocumentToolsMixin:
    """The document tools `WorldTools` offers. Each call reads the campaign's stored text."""

    engine: Engine
    campaign_id: int

    def list_documents(self) -> dict[str, Any]:
        """List the campaign's uploaded source documents and their section index."""
        rows = campaign_documents(self.engine, self.campaign_id)
        if not rows:
            return {"documents": [], "note": "This campaign has no source documents."}
        documents: list[dict[str, Any]] = []
        for row in rows:
            sections = document_sections(row)
            documents.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "characters": row.characters,
                    "sections_total": len(sections),
                    "sections": [section.as_dict() for section in sections[:LISTED_SECTIONS]],
                }
            )
        return {
            "documents": documents,
            "note": (
                "Sections give a title and the character offset where its text starts. Read "
                "with read_document, passing the document's id or name and the offset."
            ),
        }

    def read_document(
        self,
        document: str,
        offset: int = 0,
        length: int = READ_LENGTH,
    ) -> dict[str, Any]:
        """Read a passage of a source document from a character offset."""
        rows = campaign_documents(self.engine, self.campaign_id)
        row = _find_document(rows, document)
        text = row.text
        size = max(200, min(int(length or READ_LENGTH), MAX_READ_LENGTH))
        start = max(0, min(int(offset or 0), len(text)))
        end = min(len(text), start + size)
        sections = document_sections(row)
        return {
            "document": row.name,
            "id": row.id,
            "offset": start,
            "next_offset": end,
            "characters": row.characters,
            "section": _section_at(sections, start),
            "text": text[start:end],
            "done": end >= len(text),
        }

    def search_documents(self, query: str, document: str = "") -> dict[str, Any]:
        """Search the source documents for words or a phrase. A passage that contains more of
        the query's words ranks first, so a question about a person in a place finds the passage
        where both are named rather than the first mention of either."""
        wanted = query.strip()
        terms = _search_terms(wanted)
        if not terms:
            raise InvalidInput("search for a word of at least two characters")
        all_rows = campaign_documents(self.engine, self.campaign_id)
        if not all_rows:
            return {
                "matches": [],
                "searched": [],
                "more": False,
                "note": "This campaign has no source documents.",
            }
        rows = (_find_document(all_rows, document),) if document.strip() else all_rows
        phrase = wanted.casefold()
        found: list[tuple[int, int, CampaignDocument]] = []
        for row in rows:
            for score, offset in _rank_search_hits(row.text, terms, phrase):
                found.append((score, offset, row))
        found.sort(key=lambda hit: (-hit[0], hit[1], hit[2].name.casefold()))
        more = len(found) > SEARCH_RESULTS
        matches = []
        longest = max(len(term) for term in terms)
        for score, offset, row in found[:SEARCH_RESULTS]:
            sections = document_sections(row)
            matches.append(
                {
                    "document": row.name,
                    "offset": offset,
                    "section": _section_at(sections, offset),
                    "matched": min(score, len(terms)),
                    "excerpt": _excerpt(row.text, offset, longest),
                }
            )
        note = ""
        if not matches:
            note = "No matches. Try a shorter or different word."
        return {
            "matches": matches,
            "searched": [row.name for row in rows],
            "more": more,
            "note": note,
        }


def _search_terms(query: str) -> list[str]:
    """The words of a query, lower-cased, in order and without duplicates. Punctuation and
    one-letter words are dropped, so a GM can paste a question's subject words freely."""
    terms = [term for term in _SEARCH_TERM.findall(query.casefold()) if len(term) >= 2]
    return list(dict.fromkeys(terms))


def _term_positions(haystack: str, term: str, limit: int = 200) -> list[int]:
    positions: list[int] = []
    at = haystack.find(term)
    while at >= 0 and len(positions) < limit:
        positions.append(at)
        at = haystack.find(term, at + len(term))
    return positions


def _rank_search_hits(text: str, terms: Sequence[str], phrase: str) -> list[tuple[int, int]]:
    """The best places in one document for a query, as (score, offset), highest first. A
    candidate sits at each occurrence of a query word; its score counts the distinct query words
    within a small window, and an exact phrase outranks any partial window. Overlapping
    candidates collapse to their best one, so a long paragraph is returned once, not per word."""
    haystack = text.casefold()
    positions = {term: _term_positions(haystack, term) for term in terms}
    hits: list[tuple[int, int]] = []
    if len(terms) > 1 and phrase in haystack:
        at = haystack.find(phrase)
        while at >= 0:
            hits.append((len(terms) + 1, at))
            at = haystack.find(phrase, at + len(phrase))
    for term in terms:
        for at in positions[term]:
            start = max(0, at - _SEARCH_WINDOW)
            end = min(len(haystack), at + len(term) + _SEARCH_WINDOW)
            score = sum(
                1 for other in terms if any(start <= found < end for found in positions[other])
            )
            hits.append((score, at))
    ranked: list[tuple[int, int]] = []
    for score, at in sorted(hits, key=lambda hit: (-hit[0], hit[1])):
        if any(abs(at - chosen) <= _SEARCH_WINDOW * 2 for _, chosen in ranked):
            continue
        ranked.append((score, at))
    return ranked


def _find_document(rows: Sequence[CampaignDocument], value: str) -> CampaignDocument:
    wanted = value.strip()
    if not wanted:
        raise InvalidInput("name a source document; list_documents shows them")
    if wanted.isdigit():
        for row in rows:
            if row.id == int(wanted):
                return row
    exact = [row for row in rows if row.name.casefold() == wanted.casefold()]
    if len(exact) == 1:
        return exact[0]
    partial = [row for row in rows if wanted.casefold() in row.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    if not rows:
        raise InvalidInput("this campaign has no source documents")
    options = ", ".join(repr(row.name) for row in rows)
    raise InvalidInput(f"there is no source document called {value!r}; the documents are {options}")


def _section_at(sections: Sequence[DocumentSection], offset: int) -> str:
    title = ""
    for section in sections:
        if section.offset > offset:
            break
        title = section.title
    return title


def _excerpt(text: str, position: int, length: int) -> str:
    # Wide enough to show the other query words that earned the passage its score.
    start = max(0, position - _SEARCH_WINDOW)
    end = min(len(text), position + length + _SEARCH_WINDOW)
    return re.sub(r"\s+", " ", text[start:end]).strip()
