from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..models import Chapter, Format, ParsedBook
from .titles import clean_title

MANIFEST_CONTEXT = "ipfs://schema/book/0.1.0"

_YEAR_RE = re.compile(r"\d{4}")


class ManifestError(ValueError):
    pass


def _as_str(value, field: str) -> str:
    if isinstance(value, str):
        return value
    raise ManifestError(f"поле {field} должно быть строкой")


def _as_int(value, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _year_from(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        m = _YEAR_RE.search(value)
        if m:
            return int(m.group())
    return None


def _decode_blob(blob: dict, manifest_dir: Path) -> str:
    """Текст главы из ContentBlob: inline (text/base64) или файл рядом с манифестом."""
    media_type = _as_str(blob.get("mediaType", "text/plain"), "mediaType").lower()
    raw: bytes
    if "text" in blob and isinstance(blob["text"], str):
        raw = blob["text"].encode("utf-8")
    elif "data" in blob and isinstance(blob["data"], str):
        raw = base64.b64decode(blob["data"])
    elif "path" in blob and isinstance(blob["path"], str):
        target = (manifest_dir / blob["path"]).resolve()
        try:
            target.relative_to(manifest_dir.resolve())
        except ValueError as e:
            raise ManifestError(f"путь {blob['path']} вне каталога манифеста") from e
        raw = target.read_bytes()
    else:
        return ""
    if "xml" in media_type or "fb2" in media_type or "html" in media_type:
        return _strip_xml(raw)
    return raw.decode("utf-8", errors="replace")


def _strip_xml(raw: bytes) -> str:
    from lxml import etree

    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return raw.decode("utf-8", errors="replace")
    parts: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = el.tag.split("}")[-1].lower()
        if local in ("p", "section", "div", "li", "dd", "dt", "tr", "blockquote", "pre"):
            text = " ".join(el.itertext()).strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _paragraphs(text: str) -> list[str]:
    out: list[str] = []
    for block in text.split("\n\n"):
        block = re.sub(r"\s+", " ", block).strip()
        if block:
            out.append(block)
    return out


def _extract_contributors(work: dict) -> list[str]:
    authors: list[str] = []
    for contrib in work.get("contributors", []) or []:
        if not isinstance(contrib, dict):
            continue
        name = contrib.get("name") or contrib.get("fullName") or ""
        role = (contrib.get("role") or "author").lower()
        if role in ("author", "writer", "") and isinstance(name, str) and name.strip():
            authors.append(name.strip())
    for author in work.get("authors", []) or []:
        if isinstance(author, str) and author.strip():
            authors.append(author.strip())
        elif isinstance(author, dict):
            name = author.get("name") or author.get("fullName") or ""
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for a in authors:
        key = a.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def _book_details(work: dict) -> dict:
    details = work.get("bookDetails") or work.get("metadata", {}).get("bookDetails")
    if isinstance(details, dict):
        return details
    return {}


def parse_ipfsbook(path: Path) -> ParsedBook:
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ManifestError(f"манифест не JSON: {e}") from e
    if not isinstance(manifest, dict):
        raise ManifestError("корень манифеста не объект")

    if manifest.get("@context") not in (None, MANIFEST_CONTEXT):
        raise ManifestError(
            f"неизвестный @context: {manifest.get('@context')} (ожидался {MANIFEST_CONTEXT})"
        )

    book = ParsedBook(format=Format.IPFSBOOK, title="")

    book.title = _as_str(
        manifest.get("title") or manifest.get("bookTitle") or path.stem,
        "title",
    )

    book.authors = _extract_contributors(manifest)

    details = _book_details(manifest)
    if details:
        book.language = details.get("language") or manifest.get("language")
        book.description = (
            _as_str(details.get("description", ""), "description")
            if details.get("description")
            else manifest.get("description", "")
        )
        year = _year_from(details.get("year") or manifest.get("year"))
        if year is not None:
            book.year = year

    book.year = book.year or _year_from(
        manifest.get("originalDate") or manifest.get("publishedDate")
    )
    if book.language is None:
        book.language = manifest.get("language")
    if not book.description:
        book.description = manifest.get("description", "") or ""

    manifest_dir = path.parent
    chapters = _build_chapters(manifest, manifest_dir)
    if not chapters:
        raise ManifestError("в манифесте нет глав с текстом")
    book.chapters = chapters
    return book


def _build_chapters(manifest: dict, manifest_dir: Path) -> list[Chapter]:
    components = manifest.get("components") or manifest.get("chapters") or []
    if not isinstance(components, list):
        raise ManifestError("components должен быть списком")

    chapters: list[Chapter] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        kind = (comp.get("kind") or "chapter").lower()
        if kind not in ("chapter", "page", "part", "unknown", ""):
            continue
        title = clean_title(_as_str(comp.get("title", ""), "title"))
        paragraphs = _component_paragraphs(comp, manifest_dir)
        if paragraphs:
            chapters.append(Chapter(title=title, paragraphs=paragraphs))

    if not chapters:
        text_blob = _work_text(manifest, manifest_dir)
        if text_blob:
            paragraphs = _paragraphs(text_blob)
            if paragraphs:
                chapters.append(Chapter(title="", paragraphs=paragraphs))
    return chapters


def _component_paragraphs(comp: dict, manifest_dir: Path) -> list[str]:
    inline = comp.get("text")
    if isinstance(inline, str) and inline.strip():
        return _paragraphs(inline)

    resources = comp.get("resources")
    if isinstance(resources, list) and resources:
        text_parts: list[str] = []
        for blob in resources:
            if not isinstance(blob, dict):
                continue
            role = (blob.get("role") or "text").lower()
            if role not in ("text", "body", ""):
                continue
            text_parts.append(_decode_blob(blob, manifest_dir))
        joined = "\n\n".join(t for t in text_parts if t)
        if joined:
            return _paragraphs(joined)

    blob = comp.get("content") or comp.get("blob")
    if isinstance(blob, dict):
        text = _decode_blob(blob, manifest_dir)
        if text:
            return _paragraphs(text)
    return []


def _work_text(manifest: dict, manifest_dir: Path) -> str:
    blob = manifest.get("textCid") or manifest.get("textBlob") or manifest.get("text")
    if isinstance(blob, dict):
        return _decode_blob(blob, manifest_dir)
    if isinstance(blob, str) and blob.strip():
        return blob
    return ""
