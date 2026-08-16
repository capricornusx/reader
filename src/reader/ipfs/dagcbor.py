from __future__ import annotations

import struct

CID_TAG = 42

MAJOR_UINT = 0
MAJOR_NEGINT = 1
MAJOR_BYTES = 2
MAJOR_STR = 3
MAJOR_ARRAY = 4
MAJOR_MAP = 5
MAJOR_TAG = 6
MAJOR_OTHER = 7


class DagCborError(ValueError):
    pass


class Link:
    """IPLD-ссылка на блок по CID."""

    __slots__ = ("cid",)

    def __init__(self, cid: str):
        self.cid = cid

    def __repr__(self) -> str:
        return f"Link({self.cid!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Link) and self.cid == other.cid

    def __hash__(self) -> int:
        return hash(self.cid)


def _read_len(data: bytes, head: int, pos: int) -> tuple[int, int]:
    extra = head & 0x1F
    if extra < 24:
        return extra, pos
    if extra == 24:
        return data[pos], pos + 1
    if extra == 25:
        return struct.unpack_from(">H", data, pos)[0], pos + 2
    if extra == 26:
        return struct.unpack_from(">I", data, pos)[0], pos + 4
    if extra == 27:
        return struct.unpack_from(">Q", data, pos)[0], pos + 8
    raise DagCborError(f"неподдерживаемая длина: {extra}")


def _decode_cid(blob: bytes) -> str:
    """CID из байт-строки тега 42 (с префиксом multibase identity 0x00)."""
    if blob and blob[0] == 0x00:
        blob = blob[1:]
    return _cid_to_str(blob)


def _cid_to_str(raw: bytes) -> str:
    version = raw[0]
    if version == 0x12:
        return _base58btc(raw)
    if version == 0x01:
        codec = raw[1]
        if codec == 0x55:
            return "bafkrei" + _base32(raw[2:]).lower()
        return _base32(raw).lower()
    return _base58btc(raw)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58btc(data: bytes) -> str:
    n = int.from_bytes(data, "big") if data else 0
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    leading = 0
    for b in data:
        if b == 0:
            leading += 1
        else:
            break
    return "z" + "1" * leading + out


def _base32(data: bytes) -> str:
    import base64

    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def decode(data: bytes) -> object:
    value, pos = _decode_item(data, 0)
    if pos != len(data):
        raise DagCborError("лишние байты после объекта DAG-CBOR")
    return value


def _decode_item(data: bytes, pos: int) -> tuple[object, int]:
    if pos >= len(data):
        raise DagCborError("неожиданный конец данных")
    head = data[pos]
    major = head >> 5
    pos += 1

    if major == MAJOR_UINT:
        length, pos = _read_len(data, head, pos)
        return length, pos
    if major == MAJOR_NEGINT:
        length, pos = _read_len(data, head, pos)
        return -1 - length, pos
    if major == MAJOR_BYTES:
        length, pos = _read_len(data, head, pos)
        return data[pos : pos + length], pos + length
    if major == MAJOR_STR:
        length, pos = _read_len(data, head, pos)
        return data[pos : pos + length].decode("utf-8", errors="replace"), pos + length
    if major == MAJOR_ARRAY:
        length, pos = _read_len(data, head, pos)
        items: list = []
        for _ in range(length):
            item, pos = _decode_item(data, pos)
            items.append(item)
        return items, pos
    if major == MAJOR_MAP:
        length, pos = _read_len(data, head, pos)
        result: dict = {}
        for _ in range(length):
            key, pos = _decode_item(data, pos)
            if not isinstance(key, str):
                raise DagCborError("ключ карты не строка")
            val, pos = _decode_item(data, pos)
            result[key] = val
        return result, pos
    if major == MAJOR_TAG:
        tag, pos = _read_len(data, head, pos)
        body, pos = _decode_item(data, pos)
        if tag == CID_TAG and isinstance(body, bytes):
            return Link(_decode_cid(body)), pos
        raise DagCborError(f"неподдерживаемый тег {tag}")
    if major == MAJOR_OTHER:
        extra = head & 0x1F
        if extra == 20:
            return False, pos
        if extra == 21:
            return True, pos
        if extra == 22:
            return None, pos
        if extra == 23:
            raise DagCborError("undefined не поддерживается")
        if extra in (25, 26, 27):
            length = {25: 2, 26: 4, 27: 8}[extra]
            return struct.unpack_from(">d", data, pos)[0], pos + length
    raise DagCborError(f"неподдерживаемый major type {major}")


def to_json_compatible(value: object) -> object:
    """Превращает Link в {"/": cid}, байты в строки - для единообразия с dag-json."""
    if isinstance(value, Link):
        return {"/": value.cid}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [to_json_compatible(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json_compatible(v) for k, v in value.items()}
    return value
