"""Валидация CID по контракту IPFS/IPLD (specs.ipfs.tech/cid).

CIDv0: 46 символов base58btc, начинается с Qm, внутри sha2-256 multihash (0x12 0x20 + 32 байта).
CIDv1: multibase-строка (префикс b/z/f/k/B/F/Z), декодируется в
<version=0x01><codec-varint><multihash: hash-code + len + digest>.
"""
from __future__ import annotations

import base64

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

MULTIBASE_DECODERS: dict[str, callable] = {}


def _register(prefix: str):
    def deco(fn):
        MULTIBASE_DECODERS[prefix] = fn
        return fn
    return deco


@_register("b")
def _decode_base32(data: str) -> bytes:
    pad = "=" * (-len(data) % 8)
    return base64.b32decode(data.upper() + pad)


@_register("B")
def _decode_base32_upper(data: str) -> bytes:
    pad = "=" * (-len(data) % 8)
    return base64.b32decode(data + pad)


@_register("f")
def _decode_base16(data: str) -> bytes:
    return base64.b16decode(data.upper())


@_register("F")
def _decode_base16_upper(data: str) -> bytes:
    return base64.b16decode(data)


@_register("z")
def _decode_base58btc(data: str) -> bytes:
    n = 0
    for ch in data:
        if ch not in _B58_INDEX:
            raise ValueError(f"недопустимый символ base58btc: {ch}")
        n = n * 58 + _B58_INDEX[ch]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading = 0
    for ch in data:
        if ch == "1":
            leading += 1
        else:
            break
    return b"\x00" * leading + raw


@_register("k")
def _decode_base36(data: str) -> bytes:
    n = int(data, 36)
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            if shift > 0 and (b & 0x7F) == 0:
                raise ValueError("overlong varint")
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint слишком длинный")
    raise ValueError("varint обрывается")


def _validate_multihash(data: bytes, pos: int) -> int:
    """Проверяет multihash с позиции pos, возвращает позицию после."""
    _hash_code, pos = _read_varint(data, pos)
    if pos >= len(data):
        raise ValueError("multihash без длины")
    length = data[pos]
    pos += 1
    if pos + length > len(data):
        raise ValueError("digest короче заявленной длины")
    pos += length
    if pos != len(data):
        raise ValueError("лишние байты после multihash")
    return pos


def _decode_binary(data: bytes) -> None:
    """Проверяет бинарный CID по контракту. Поднимает ValueError если невалиден."""
    if len(data) == 34 and data[0] == 0x12 and data[1] == 0x20:
        _validate_multihash(data, 0)
        return
    version, pos = _read_varint(data, 0)
    if version != 0x01:
        raise ValueError(f"неподдерживаемая версия CID: {version}")
    # codec (тип контента) - пропускаем, но проверяем что varint валидный
    _, pos = _read_varint(data, pos)
    _validate_multihash(data, pos)


def is_cid(value: str) -> bool:
    """True если value - валидный CID по контракту IPFS/IPLD."""
    if not isinstance(value, str) or not value:
        return False
    # CIDv0: 46 символов base58btc, начинается с Qm, без multibase-префикса
    if len(value) == 46 and value.startswith("Qm"):
        try:
            raw = _decode_base58btc(value)
            _decode_binary(raw)
            return True
        except ValueError:
            return False
    # CIDv1: multibase-строка
    prefix = value[0]
    decoder = MULTIBASE_DECODERS.get(prefix)
    if decoder is None:
        return False
    try:
        raw = decoder(value[1:])
        _decode_binary(raw)
        return True
    except ValueError:
        return False
