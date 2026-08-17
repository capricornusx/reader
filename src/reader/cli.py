from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import LibraryDB
from .library import import_book, import_directory
from .ui.app import ReaderApp


def _default_db_path() -> Path:
    base = Path.home() / ".local" / "share" / "reader"
    return base / "library.db"


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="reader",
        description="Консольная читалка книг (TXT, EPUB, FB2). "
        "Без аргументов открывает библиотеку; можно передать файл книги, папку или CID.",
    )
    parser.add_argument(
        "path", nargs="?", type=str,
        help="файл книги, папка или CID манифеста IPFS",
    )
    parser.add_argument("--library", type=Path, default=_default_db_path(), help="путь к файлу библиотеки SQLite")
    parser.add_argument(
        "--import", dest="import_dir", metavar="DIR",
        help="рекурсивно импортировать книги из папки и выйти",
    )
    parser.add_argument(
        "--save", metavar="DIR",
        help="сохранить книгу (CID) в каталог вместо открытия в читалке",
    )
    parser.add_argument(
        "--format", choices=("native", "fb2", "epub"), default="native",
        help="формат сохранения книги из IPFS (по умолчанию нативный манифест)",
    )
    parser.add_argument("--ipfs-gateway", action="append", help="публичный шлюз IPFS (можно несколько)")
    parser.add_argument("--kubo", default="http://localhost:5001", help="адрес RPC API локального Kubo (:5001)")
    parser.add_argument(
        "--kubo-gateway", default="http://localhost:8080",
        help="адрес локального HTTP-шлюза Kubo (:8080)",
    )

    args = parser.parse_args(raw)

    args.library.parent.mkdir(parents=True, exist_ok=True)
    db = LibraryDB(args.library)

    if args.import_dir:
        results = import_directory(db, Path(args.import_dir))
        ok = sum(1 for _, s in results if s)
        print(f"Импортировано: {ok}, ошибок: {len(results) - ok}")
        for path, success in results:
            print(("  OK  " if success else "  ERR ") + path)
        db.close()
        return 0

    if args.path is not None and _looks_like_cid(args.path):
        db.close()
        return _open_cid(args.path, args)

    if args.path is not None and not Path(args.path).exists():
        print(f"reader: путь не существует: {args.path}", file=sys.stderr)
        db.close()
        return 2

    open_path = Path(args.path) if args.path is not None else None
    try:
        ReaderApp(args.library, open_path=open_path).run()
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка запуска: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def _looks_like_cid(value: str) -> bool:
    from .ipfs.gateway import is_cid

    return is_cid(value)


def _manifest_from_book(book, cid: str) -> dict:
    from .importers.ipfsbook import MANIFEST_CONTEXT

    return {
        "@context": MANIFEST_CONTEXT,
        "releaseId": cid,
        "contentType": "book",
        "title": book.title,
        "authors": book.authors,
        "year": book.year,
        "language": book.language,
        "description": book.description,
        "components": [
            {"title": ch.title, "position": i + 1, "kind": "chapter",
             "text": "\n\n".join(ch.paragraphs)}
            for i, ch in enumerate(book.chapters)
        ],
    }


def _safe_filename(title: str) -> str:
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip()
    return safe or "book"


def _open_cid(cid: str, args) -> int:
    from .ipfs import book as ipfs_book
    from .ipfs.gateway import DEFAULT_GATEWAYS

    gateways = tuple(args.ipfs_gateway) if args.ipfs_gateway else DEFAULT_GATEWAYS
    kubo = args.kubo if args.kubo else None
    kubo_gateway = args.kubo_gateway if args.kubo_gateway else None

    def on_progress(p):
        if p.stage == "probe":
            print(f"Проверяю доступность блока {p.cid} ...")
        elif p.stage == "fetch-manifest":
            size_str = f" ({p.size} байт)" if p.size else ""
            print(f"Доступен{size_str}. Скачиваю манифест ...")
        elif p.stage == "fetch-blob":
            print(f"  Скачиваю главу: {p.cid[:16]} ...")
        elif p.stage == "done":
            print("Готово.")

    try:
        book = ipfs_book.open_by_cid(
            cid, gateways=gateways, kubo=kubo, kubo_gateway=kubo_gateway, on_progress=on_progress
        )
    except Exception as e:  # noqa: BLE001
        print(f"reader: {e}", file=sys.stderr)
        return 3

    if args.save:
        save_dir = Path(args.save)
        save_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_filename(book.title)
        if args.format == "native":
            out = save_dir / f"{name}.ipfsbook"
            out.write_text(
                json.dumps(_manifest_from_book(book, cid), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            from .exporters import convert

            out = save_dir / f"{name}.{args.format}"
            out.write_bytes(convert(book, args.format))
        print(f"Сохранено: {out}")
        return 0

    args.library.parent.mkdir(parents=True, exist_ok=True)
    db = LibraryDB(args.library)
    tmp_path: Path | None = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".ipfsbook", delete=False) as tmp:
            tmp.write(
                json.dumps(_manifest_from_book(book, cid), ensure_ascii=False, indent=2).encode("utf-8")
            )
            tmp_path = Path(tmp.name)
        import_book(db, tmp_path)
        ReaderApp(args.library, open_path=tmp_path).run()
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка запуска: {e}", file=sys.stderr)
        return 1
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        db.close()
    return 0
