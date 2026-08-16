from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Callable

from ..models import Chapter, Format, ParsedBook
from .titles import clean_title

MANIFEST_CONTEXT = "ipfs://schema/book/0.1.0"

_YEAR_RE = re.compile(r"\d{4}")


class ManifestError(ValueError):
    pass


class IpfsBook:
    """Книга из манифеста protocol-core: метаданные + главы по CID или inline."""

    def __init__(self, fetch_blob: Callable[[str], bytes] | None = None):
        self._fetch_blob = fetch_blob

    def parse(self, manifest: dict, manifest_dir: Path | None = None) -> ParsedBook:
        if not isinstance(manifest, dict):
            raise ManifestError("корень манифеста не объект")
        ctx = manifest.get("@context")
        if ctx not in (None, MANIFEST_CONTEXT):
            raise ManifestError(
                f"неизвестный @context: {ctx} (ожидался {MANIFEST_CONTEXT})"
            )

        book = ParsedBook(format=Format.IPFSBOOK, title="")
        book.title = _as_str(
            manifest.get("title") or manifest.get("bookTitle") or "Без названия",
            "title",
        )
        book.authors = _extract_contributors(manifest)

        details = _book_details(manifest)
        if details:
            book.language = details.get("language") or manifest.get("language")
            book.description = _as_str(details.get("description", ""), "description") or ""
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

        chapters = self._build_chapters(manifest, manifest_dir)
        if not chapters:
            raise ManifestError("в манифесте нет глав с текстом")
        book.chapters = chapters
        return book

    def _build_chapters(self, manifest: dict, manifest_dir: Path | None) -> list[Chapter]:
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
            paragraphs = self._component_paragraphs(comp, manifest_dir)
            if paragraphs:
                chapters.append(Chapter(title=title, paragraphs=paragraphs))

        if not chapters:
            text = self._work_text(manifest, manifest_dir)
            if text:
                paragraphs = _paragraphs(text)
                if paragraphs:
                    chapters.append(Chapter(title="", paragraphs=paragraphs))
        return chapters

    def _component_paragraphs(self, comp: dict, manifest_dir: Path | None) -> list[str]:
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
                text_parts.append(self._decode_blob(blob, manifest_dir))
            joined = "\n\n".join(t for t in text_parts if t)
            if joined:
                return _paragraphs(joined)

        blob = comp.get("content") or comp.get("blob")
        if isinstance(blob, dict):
            text = self._decode_blob(blob, manifest_dir)
            if text:
                return _paragraphs(text)
        return []

    def _work_text(self, manifest: dict, manifest_dir: Path | None) -> str:
        blob = manifest.get("textCid") or manifest.get("textBlob") or manifest.get("text")
        if isinstance(blob, dict):
            return self._decode_blob(blob, manifest_dir)
        if isinstance(blob, str) and blob.strip():
            return blob
        return ""

    def _decode_blob(self, blob: dict, manifest_dir: Path | None) -> str:
        """Текст главы: inline (text/base64), файл рядом или блок по CID из сети."""
        media_type = _as_str(blob.get("mediaType", "text/plain"), "mediaType").lower()
        raw = self._blob_bytes(blob, manifest_dir)
        if not raw:
            return ""
        if "xml" in media_type or "fb2" in media_type or "html" in media_type:
            return _strip_xml(raw)
        return raw.decode("utf-8", errors="replace")

    def _blob_bytes(self, blob: dict, manifest_dir: Path | None) -> bytes:
        if "text" in blob and isinstance(blob["text"], str):
            return blob["text"].encode("utf-8")
        if "data" in blob and isinstance(blob["data"], str):
            return base64.b64decode(blob["data"])
        if "path" in blob and isinstance(blob["path"], str) and manifest_dir is not None:
            return _read_sibling(manifest_dir, blob["path"])
        cid = _link_cid(blob)
        if cid:
            if self._fetch_blob is None:
                raise ManifestError(f"нет загрузчика для CID {cid}")
            return self._fetch_blob(cid)
        return b""


def _link_cid(blob: dict) -> str | None:
    """CID из dag-json-ссылки {"/": cid}, поля cid или из строки-ссылки."""
    link = blob.get("cid")
    if isinstance(link, dict) and isinstance(link.get("/"), str):
        return link["/"]
    if isinstance(link, str) and link.strip():
        return link.strip()
    if isinstance(blob.get("/"), str):
        return blob["/"]
    return None


def _read_sibling(manifest_dir: Path, rel: str) -> bytes:
    target = (manifest_dir / rel).resolve()
    try:
        target.relative_to(manifest_dir.resolve())
    except ValueError as e:
        raise ManifestError(f"путь {rel} вне каталога манифеста") from e
    return target.read_bytes()


def parse_manifest(
    manifest: dict,
    fetch_blob: Callable[[str], bytes] | None = None,
) -> ParsedBook:
    """Парсит манифест из dict (локальный JSON или скачанный dag-json/CBOR)."""
    return IpfsBook(fetch_blob=fetch_blob).parse(manifest)


def parse_ipfsbook(path: Path) -> ParsedBook:
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ManifestError(f"манифест не JSON: {e}") from e
    return IpfsBook().parse(manifest, manifest_dir=path.parent)


def _as_str(value, field: str) -> str:
    if isinstance(value, str):
        return value
    raise ManifestError(f"поле {field} должно быть строкой")


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
