from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

from .models import ParsedBook


def to_fb2(book: ParsedBook) -> bytes:
    """FB2 из разобранной книги. Сохраняет структуру глав и абзацев."""
    title = escape(book.title or "Без названия")
    lang = escape(book.language or "ru")
    authors_xml = ""
    for author in book.authors:
        parts = author.split()
        first = escape(parts[0]) if parts else ""
        last = escape(parts[-1]) if len(parts) > 1 else ""
        middle = escape(" ".join(parts[1:-1])) if len(parts) > 2 else ""
        authors_xml += f"<author><first-name>{first}</first-name>"
        if middle:
            authors_xml += f"<middle-name>{middle}</middle-name>"
        authors_xml += f"<last-name>{last}</last-name></author>"
    if not authors_xml:
        authors_xml = "<author><first-name/><last-name/></author>"

    annotation = ""
    if book.description:
        annotation = f"<annotation><p>{escape(book.description)}</p></annotation>"
    year_xml = f"<date>{book.year}</date>" if book.year else ""

    body_parts: list[str] = ["<body>"]
    for chapter in book.chapters:
        title_xml = f"<title><p>{escape(chapter.title)}</p></title>" if chapter.title else ""
        paras = "".join(f"<p>{escape(p)}</p>" for p in chapter.paragraphs)
        body_parts.append(f"<section>{title_xml}{paras}</section>")
    body_parts.append("</body>")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">\n'
        f"<description><title-info><genre>unrecognised</genre>"
        f"{authors_xml}<book-title>{title}</book-title>"
        f"{annotation}{year_xml}<lang>{lang}</lang>"
        "</title-info></description>\n"
        + "".join(body_parts)
        + "\n</FictionBook>"
    )
    return xml.encode("utf-8")


def to_epub(book: ParsedBook) -> bytes:
    """Минимальный валидный EPUB 3 из разобранной книги."""
    ns_xhtml = "http://www.w3.org/1999/xhtml"
    ns_dc = "http://purl.org/dc/elements/1.1/"
    ns_opf = "http://www.idpf.org/2007/opf"

    author = book.authors[0] if book.authors else "Неизвестный автор"
    title = book.title or "Без названия"
    lang = book.language or "ru"

    spine_items: list[tuple[str, str, str]] = []
    for i, chapter in enumerate(book.chapters, 1):
        heading = f"<h1>{escape(chapter.title)}</h1>" if chapter.title else ""
        paras = "".join(f"<p>{escape(p)}</p>" for p in chapter.paragraphs)
        html = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<html xmlns="{ns_xhtml}"><head><title>{escape(chapter.title or title)}</title>'
            f'</head><body>{heading}{paras}</body></html>'
        )
        spine_items.append((f"chapter{i}.xhtml", escape(chapter.title or f"Глава {i}"), html))

    manifest = "".join(
        f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>'
        for i, (name, _, _) in enumerate(spine_items, 1)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(spine_items) + 1))
    date_xml = f"<dc:date>{book.year}</dc:date>" if book.year else ""

    opf = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<package xmlns="{ns_opf}" version="3.0" unique-identifier="uid">'
        f'<metadata xmlns:dc="{ns_dc}">'
        f'<dc:identifier id="uid">reader-{hash(title) & 0xffffffff:08x}</dc:identifier>'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<dc:creator>{escape(author)}</dc:creator>"
        f"<dc:language>{escape(lang)}</dc:language>"
        f"{date_xml}</metadata>"
        f"<manifest>{manifest}</manifest>"
        f"<spine>{spine}</spine></package>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        container = (
            '<?xml version="1.0"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        )
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        for name, _, html in spine_items:
            zf.writestr(f"OEBPS/{name}", html)
    return buf.getvalue()


def convert(book: ParsedBook, fmt: str) -> bytes:
    """Конвертация в fb2 или epub. Возвращает байты файла."""
    if fmt == "fb2":
        return to_fb2(book)
    if fmt == "epub":
        return to_epub(book)
    raise ValueError(f"неподдерживаемый формат конвертации: {fmt}")
