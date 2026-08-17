from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import dagcbor
from .cid import is_cid

DEFAULT_GATEWAYS = (
    "http://localhost:8080",
    "https://ipfs.io",
    "https://dweb.link",
    "https://cloudflare-ipfs.com",
    "https://gateway.pinata.cloud",
)

_DAG_JSON_ACCEPT = "application/vnd.ipld.dag-json"
_TIMEOUT = 30


class IpfsError(RuntimeError):
    pass


@dataclass
class BlockInfo:
    cid: str
    size: int | None
    available: bool


def _gateway_url(gateway: str, cid: str, fmt: str | None = None) -> str:
    base = gateway.rstrip("/")
    url = f"{base}/ipfs/{cid}"
    if fmt:
        url += f"?format={fmt}"
    return url


def probe(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS) -> BlockInfo:
    """Проверка доступности блока: HEAD по шлюзам. Локальный шлюз идёт первым."""
    for gw in gateways:
        try:
            req = urllib.request.Request(_gateway_url(gw, cid), method="HEAD")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                size = resp.headers.get("Content-Length")
                return BlockInfo(
                    cid=cid,
                    size=int(size) if size and size.isdigit() else None,
                    available=True,
                )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
    return BlockInfo(cid=cid, size=None, available=False)


def fetch_manifest(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS) -> dict:
    """Скачивает манифест книги и возвращает его как dict.

    Сначала dag-json (ссылки как {"/": cid}), затем сырой DAG-CBOR блок.
    Перебирает шлюзы по очереди, локальный идёт первым.
    """
    for gw in gateways:
        manifest = _fetch_dag_json(gw, cid)
        if manifest is not None:
            return manifest
        manifest = _fetch_raw_block(gw, cid)
        if manifest is not None:
            return manifest
    raise IpfsError(f"не удалось получить манифест {cid} ни через один шлюз")


def fetch_blob(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS) -> bytes:
    """Скачивает сырой блок (текст главы, обложка) по CID."""
    for gw in gateways:
        try:
            req = urllib.request.Request(_gateway_url(gw, cid))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
    raise IpfsError(f"не удалось скачать блок {cid} ни через один шлюз")


def _fetch_dag_json(gateway: str, cid: str) -> dict | None:
    try:
        req = urllib.request.Request(
            _gateway_url(gateway, cid, "dag-json"), headers={"Accept": _DAG_JSON_ACCEPT}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _fetch_raw_block(gateway: str, cid: str) -> dict | None:
    try:
        req = urllib.request.Request(_gateway_url(gateway, cid))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
        decoded = dagcbor.decode(data)
        if isinstance(decoded, dict):
            return dagcbor.to_json_compatible(decoded)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, dagcbor.DagCborError, ValueError):
        return None
    return None
