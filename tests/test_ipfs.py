from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from reader.exporters import convert, to_epub, to_fb2
from reader.importers.ipfsbook import IpfsBook, ManifestError, parse_manifest
from reader.ipfs import dagcbor, gateway
from reader.ipfs.book import open_by_cid
from reader.models import Chapter, Format, ParsedBook


def _encode_cbor(obj) -> bytes:
    """Минимальный CBOR-энкодер для тестов (без строгой каноничности)."""
    if obj is None:
        return b"\xf6"
    if obj is True:
        return b"\xf5"
    if obj is False:
        return b"\xf4"
    if isinstance(obj, int):
        if 0 <= obj <= 23:
            return bytes([obj])
        if 24 <= obj <= 255:
            return bytes([0x18, obj])
        return struct.pack(">BQ", 25, obj)
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        n = len(b)
        if n <= 23:
            return bytes([0x60 + n]) + b
        if n <= 255:
            return bytes([0x78, n]) + b
        return struct.pack(">BH", 0x79, n) + b
    if isinstance(obj, list):
        out = bytes([0x80 + len(obj)])
        for x in obj:
            out += _encode_cbor(x)
        return out
    if isinstance(obj, dict):
        items = sorted(obj.items())
        out = bytes([0xa0 + len(items)])
        for k, v in items:
            out += _encode_cbor(k) + _encode_cbor(v)
        return out
    raise TypeError(obj)


def _encode_link(cid_bytes: bytes) -> bytes:
    """Тег 42 (CID-ссылка) с префиксом multibase identity 0x00."""
    payload = b"\x00" + cid_bytes
    return bytes([0xd8, 0x2a, 0x58, len(payload)]) + payload


class TestDagCbor:
    def test_primitives(self):
        assert dagcbor.decode(_encode_cbor({"a": 1, "b": [2, 3], "c": None, "d": True})) == {
            "a": 1,
            "b": [2, 3],
            "c": None,
            "d": True,
        }

    def test_link_decodes_to_link(self):
        raw_cid = bytes([0x01, 0x55, 0x12, 0x20]) + b"\x11" * 32
        link = dagcbor.decode(_encode_link(raw_cid))
        assert isinstance(link, dagcbor.Link)
        assert link.cid.startswith("bafkrei")

    def test_to_json_compatible_link(self):
        raw_cid = bytes([0x01, 0x55, 0x12, 0x20]) + b"\x22" * 32
        link = dagcbor.decode(_encode_link(raw_cid))
        assert dagcbor.to_json_compatible(link) == {"/": link.cid}

    def test_rejects_extra_bytes(self):
        with pytest.raises(dagcbor.DagCborError):
            dagcbor.decode(_encode_cbor(1) + b"\x00")

    def test_rejects_non_string_key(self):
        # map с int-ключом: 0xa1 0x01 0x01
        with pytest.raises(dagcbor.DagCborError):
            dagcbor.decode(b"\xa1\x01\x01")


class TestGatewayCid:
    def test_is_cid_v0(self):
        assert gateway.is_cid("Qm" + "a" * 44)  # 46 chars total

    def test_is_cid_v1(self):
        assert gateway.is_cid("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou")

    def test_rejects_garbage(self):
        assert not gateway.is_cid("not-a-cid")
        assert not gateway.is_cid("")


class TestGatewayFetch:
    def _fake_response(self, data: bytes, status: int = 200, headers: dict | None = None):
        resp = mock.MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.read.return_value = data
        resp.__enter__ = mock.MagicMock(return_value=resp)
        resp.__exit__ = mock.MagicMock(return_value=False)
        return resp

    def test_probe_head_finds_size(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._fake_response(b"", headers={"Content-Length": "42"})
            info = gateway.probe("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)
        assert info.available and info.size == 42

    def test_probe_falls_through_to_unavailable(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("nope")):
            info = gateway.probe("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)
        assert not info.available

    def test_fetch_manifest_dag_json(self):
        manifest = {"@context": "ipfs://schema/book/0.1.0", "title": "X", "components": []}
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._fake_response(json.dumps(manifest).encode())
            result = gateway.fetch_manifest("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)
        assert result["title"] == "X"

    def test_fetch_manifest_raw_cbor_fallback(self):
        manifest = {"@context": "ipfs://schema/book/0.1.0", "title": "CBOR", "components": []}
        cbor_bytes = _encode_cbor(manifest)
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # dag-json запрос падает
                raise OSError("no dag-json")
            return self._fake_response(cbor_bytes)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = gateway.fetch_manifest("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)
        assert result["title"] == "CBOR"

    def test_fetch_blob_downloads_bytes(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = self._fake_response(b"chapter text")
            data = gateway.fetch_blob("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)
        assert data == b"chapter text"

    def test_fetch_blob_raises_when_all_fail(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("nope")):
            with pytest.raises(gateway.IpfsError):
                gateway.fetch_blob("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)


class TestManifestFromCid:
    """Манифест с dag-json ссылками {"/": cid} и их загрузкой."""

    def test_resolves_cid_links(self):
        manifest = {
            "@context": "ipfs://schema/book/0.1.0",
            "contentType": "book",
            "title": "Сетевая книга",
            "contributors": [{"name": "Автор", "role": "author"}],
            "components": [
                {
                    "title": "Глава",
                    "position": 1,
                    "kind": "chapter",
                    "resources": [
                        {"cid": {"/": "bafychapter1cid"}, "mediaType": "text/plain", "role": "text"}
                    ],
                }
            ],
        }

        fetched: list[str] = []

        def fetch(cid):
            fetched.append(cid)
            assert cid == "bafychapter1cid"
            return "Текст главы из сети.".encode("utf-8")

        book = parse_manifest(manifest, fetch_blob=fetch)
        assert book.title == "Сетевая книга"
        assert book.authors == ["Автор"]
        assert book.chapters[0].paragraphs == ["Текст главы из сети."]
        assert fetched == ["bafychapter1cid"]

    def test_open_by_cid_full_flow(self):
        manifest_cid = "bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou"
        blob_cid = "bafychapter1cid"
        manifest = {
            "@context": "ipfs://schema/book/0.1.0",
            "contentType": "book",
            "title": "Полный путь",
            "components": [
                {
                    "title": "Глава 1",
                    "position": 1,
                    "kind": "chapter",
                    "resources": [{"cid": {"/": blob_cid}, "mediaType": "text/plain", "role": "text"}],
                }
            ],
        }
        probe_resp = mock.MagicMock()
        probe_resp.headers = {"Content-Length": "100"}
        probe_resp.status = 200

        call = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call["n"] += 1
            url = getattr(req, "full_url", "")
            if req.get_method() == "HEAD":
                return _ctx(probe_resp)
            if "format=dag-json" in url:
                return _ctx(_resp(json.dumps(manifest).encode()))
            # raw block / blob
            return _ctx(_resp("Текст по CID.".encode("utf-8")))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            stages: list[str] = []
            book = open_by_cid(
                manifest_cid,
                gateways=("https://x",),
                kubo=None,
                on_progress=lambda p: stages.append(p.stage),
            )
        assert book.title == "Полный путь"
        assert book.chapters[0].paragraphs == ["Текст по CID."]
        assert "probe" in stages and "done" in stages

    def test_open_by_cid_unavailable(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("nope")):
            with pytest.raises(ManifestError, match="недоступен"):
                open_by_cid("bafyr4if6vvekqtazlxcqwaepsx4cwq4knpw5ueanghaukvhrqqxchf4oou", gateways=("https://x",), kubo=None)

    def test_open_by_cid_bad_cid(self):
        with pytest.raises(ManifestError, match="некорректный"):
            open_by_cid("garbage", gateways=("https://x",), kubo=None)


def _ctx(resp):
    resp.__enter__ = mock.MagicMock(return_value=resp)
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


def _resp(data: bytes, status: int = 200, headers: dict | None = None):
    r = mock.MagicMock()
    r.status = status
    r.headers = headers or {}
    r.read.return_value = data
    return r


class TestExporters:
    @pytest.fixture
    def book(self) -> ParsedBook:
        return ParsedBook(
            format=Format.IPFSBOOK,
            title="Тестовая книга",
            authors=["Иван Автор"],
            year=2020,
            language="ru",
            description="Аннотация.",
            chapters=[
                Chapter(title="Глава 1", paragraphs=["Первый абзац.", "Второй абзац."]),
                Chapter(title="Глава 2", paragraphs=["Текст второй главы."]),
            ],
        )

    def test_fb2_has_structure(self, book):
        data = to_fb2(book)
        xml = data.decode("utf-8")
        assert "FictionBook" in xml
        assert "Тестовая книга" in xml
        assert "Иван" in xml and "Автор" in xml
        assert "<section>" in xml
        assert "Первый абзац." in xml
        assert "<lang>ru</lang>" in xml

    def test_fb2_roundtrip_parse(self, book, tmp_path: Path):
        from reader.importers import parse

        p = tmp_path / "out.fb2"
        p.write_bytes(to_fb2(book))
        parsed = parse(p)
        assert parsed.format == Format.FB2
        assert parsed.title == "Тестовая книга"
        assert parsed.authors == ["Иван Автор"]
        assert [c.title for c in parsed.chapters] == ["Глава 1", "Глава 2"]
        assert parsed.chapters[0].paragraphs == ["Первый абзац.", "Второй абзац."]

    def test_epub_is_valid_zip(self, book):
        data = to_epub(book)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            assert "mimetype" in names
            assert "META-INF/container.xml" in names
            assert "OEBPS/content.opf" in names
            assert any(n.startswith("OEBPS/chapter") for n in names)
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            assert "Тестовая книга" in opf
            assert "Иван Автор" in opf

    def test_epub_roundtrip_parse(self, book, tmp_path: Path):
        from reader.importers import parse

        p = tmp_path / "out.epub"
        p.write_bytes(to_epub(book))
        parsed = parse(p)
        assert parsed.format == Format.EPUB
        assert parsed.title == "Тестовая книга"
        assert "Иван Автор" in parsed.authors
        assert len(parsed.chapters) == 2
        assert parsed.chapters[0].paragraphs == ["Первый абзац.", "Второй абзац."]

    def test_convert_unknown_format(self, book):
        with pytest.raises(ValueError):
            convert(book, "pdf")
