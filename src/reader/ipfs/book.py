from __future__ import annotations

from dataclasses import dataclass

from ..importers.ipfsbook import IpfsBook, ManifestError
from ..models import ParsedBook
from . import gateway

DEFAULT_GATEWAYS = gateway.DEFAULT_GATEWAYS
DEFAULT_KUBO_RPC = gateway.DEFAULT_KUBO_RPC
DEFAULT_KUBO_GATEWAY = gateway.DEFAULT_KUBO_GATEWAY


@dataclass
class DownloadProgress:
    cid: str
    available: bool
    size: int | None
    stage: str


def open_by_cid(
    cid: str,
    gateways: tuple[str, ...] = DEFAULT_GATEWAYS,
    kubo: str | None = DEFAULT_KUBO_RPC,
    kubo_gateway: str | None = DEFAULT_KUBO_GATEWAY,
    on_progress=None,
) -> ParsedBook:
    """Открывает книгу по CID манифеста: проверка, скачивание, разбор.

    gateways - публичные HTTP-шлюзы для скачивания.
    kubo_gateway - локальный HTTP-шлюз Kubo (:8080), идёт первым в списке.
    kubo - RPC API Kubo (:5001) для dag/get/stat/cat, fallback когда шлюзы молчат.
    on_progress вызывается на каждой стадии (probe, fetch-manifest,
    fetch-blob) - для статуса в UI.
    """
    if not gateway.is_cid(cid):
        raise ManifestError(f"некорректный CID: {cid}")

    if on_progress:
        on_progress(DownloadProgress(cid=cid, available=False, size=None, stage="probe"))

    info = gateway.probe(cid, gateways=gateways, kubo=kubo, kubo_gateway=kubo_gateway)
    if not info.available:
        raise ManifestError(
            f"блок {cid} недоступен: ни один шлюз и локальный Kubo не ответили"
        )

    if on_progress:
        on_progress(
            DownloadProgress(
                cid=cid, available=True, size=info.size, stage="fetch-manifest"
            )
        )

    manifest = gateway.fetch_manifest(
        cid, gateways=gateways, kubo=kubo, kubo_gateway=kubo_gateway
    )
    if not isinstance(manifest, dict):
        raise ManifestError("манифест не является объектом")

    def fetch_blob(blob_cid: str) -> bytes:
        if on_progress:
            on_progress(
                DownloadProgress(cid=blob_cid, available=False, size=None, stage="fetch-blob")
            )
        return gateway.fetch_blob(
            blob_cid, gateways=gateways, kubo=kubo, kubo_gateway=kubo_gateway
        )

    book = IpfsBook(fetch_blob=fetch_blob).parse(manifest)
    if on_progress:
        on_progress(
            DownloadProgress(cid=cid, available=True, size=info.size, stage="done")
        )
    return book
