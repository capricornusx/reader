from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import dagcbor

DEFAULT_GATEWAYS = (
    "https://ipfs.io",
    "https://dweb.link",
    "https://cloudflare-ipfs.com",
    "https://gateway.pinata.cloud",
)
DEFAULT_KUBO = "http://localhost:5001"

_CID_RE = re.compile(
    r"^(z1[A-HJ-NP-Za-km-z1-9]{44}|Qm[A-Za-z0-9]{44}|b[A-Za-z2-7]{58,})$"
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


def is_cid(value: str) -> bool:
    return bool(_CID_RE.match(value))


def _gateway_url(gateway: str, cid: str, fmt: str | None = None) -> str:
    base = gateway.rstrip("/")
    url = f"{base}/ipfs/{cid}"
    if fmt:
        url += f"?format={fmt}"
    return url


def probe(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS, kubo: str | None = DEFAULT_KUBO) -> BlockInfo:
    """Быстрая проверка доступности блока: HEAD по шлюзам, затем Kubo stat."""
    for gateway in gateways:
        try:
            req = urllib.request.Request(
                _gateway_url(gateway, cid), method="HEAD"
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                size = resp.headers.get("Content-Length")
                return BlockInfo(
                    cid=cid,
                    size=int(size) if size and size.isdigit() else None,
                    available=True,
                )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
    if kubo:
        info = _kubo_stat(cid, kubo)
        if info is not None:
            return info
    return BlockInfo(cid=cid, size=None, available=False)


def fetch_manifest(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS, kubo: str | None = DEFAULT_KUBO) -> dict:
    """Скачивает манифест книги и возвращает его как dict.

    Сначала пытается получить dag-json (ссылки как {"/": cid}), затем
    декодирует сырой DAG-CBOR блок. Если публичные шлюзы недоступны,
    пробует локальный Kubo.
    """
    for gateway in gateways:
        manifest = _fetch_dag_json(gateway, cid)
        if manifest is not None:
            return manifest
        manifest = _fetch_raw_block(gateway, cid)
        if manifest is not None:
            return manifest
    if kubo:
        manifest = _kubo_dag_get(cid, kubo)
        if manifest is not None:
            return manifest
        manifest = _kubo_block_get(cid, kubo)
        if manifest is not None:
            return manifest
    raise IpfsError(f"не удалось получить манифест {cid} ни через шлюзы, ни через Kubo")


def fetch_blob(cid: str, gateways: tuple[str, ...] = DEFAULT_GATEWAYS, kubo: str | None = DEFAULT_KUBO) -> bytes:
    """Скачивает сырой блок (текст главы, обложка) по CID."""
    for gateway in gateways:
        try:
            req = urllib.request.Request(_gateway_url(gateway, cid))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue
    if kubo:
        try:
            return _kubo_cat(cid, kubo)
        except IpfsError:
            pass
    raise IpfsError(f"не удалось скачать блок {cid}")


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


def _kubo_post(url: str) -> bytes:
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


def _kubo_stat(cid: str, kubo: str) -> BlockInfo | None:
    try:
        data = _kubo_post(f"{kubo.rstrip('/')}/api/v0/block/stat?arg={cid}")
        info = json.loads(data)
        size = info.get("Size")
        return BlockInfo(cid=cid, size=int(size) if size else None, available=True)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _kubo_dag_get(cid: str, kubo: str) -> dict | None:
    try:
        data = _kubo_post(f"{kubo.rstrip('/')}/api/v0/dag/get?arg={cid}")
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _kubo_block_get(cid: str, kubo: str) -> dict | None:
    try:
        data = _kubo_post(f"{kubo.rstrip('/')}/api/v0/block/get?arg={cid}")
        decoded = dagcbor.decode(data)
        if isinstance(decoded, dict):
            return dagcbor.to_json_compatible(decoded)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, dagcbor.DagCborError, ValueError):
        return None
    return None


def _kubo_cat(cid: str, kubo: str) -> bytes:
    data = _kubo_post(f"{kubo.rstrip('/')}/api/v0/cat?arg={cid}")
    return data
