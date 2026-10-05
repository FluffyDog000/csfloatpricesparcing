"""A minimal .xlsx writer: sheets of plain rows, a bold frozen header.

An Excel file is a zip of a few XML parts. Writing them directly covers what
an export of the plan needs - numbers stay numbers, text stays text - without
a dependency to install on the server.
"""
from __future__ import annotations

import io
import zipfile
from typing import Iterable, Sequence
from xml.sax.saxutils import escape

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell(ref: str, value, bold: bool = False) -> str:
    style = ' s="1"' if bold else ""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = "да" if value else "нет"
    if isinstance(value, (int, float)) and value == value \
            and value not in (float("inf"), float("-inf")):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(str(value))
    return (f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">'
            f"{text}</t></is></c>")


def _sheet(header: Sequence[str], rows: Iterable[Sequence],
           widths: Sequence[int] | None = None) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0">'
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
           '</sheetView></sheetViews>']
    if widths:
        out.append("<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    out.append('<row r="1">' + "".join(
        _cell(f"{_col(i)}1", h, bold=True) for i, h in enumerate(header)) + "</row>")
    for n, row in enumerate(rows, 2):
        out.append(f'<row r="{n}">' + "".join(
            _cell(f"{_col(i)}{n}", v) for i, v in enumerate(row)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def workbook(sheets: Sequence[tuple]) -> bytes:
    """`sheets`: (name, header, rows[, widths]) each. Returns the file."""
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, sheet in enumerate(sheets, 1):
            name, header, rows = sheet[0], sheet[1], sheet[2]
            widths = sheet[3] if len(sheet) > 3 else None
            # Excel's own limits on a sheet name.
            clean = "".join(c for c in str(name) if c not in '[]:*?/\\')[:31] or f"Лист{i}"
            names.append(clean)
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(header, rows, widths))
        z.writestr("[Content_Types].xml", _CT.format(sheets="".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(names) + 1))))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   "<sheets>" + "".join(
                       f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>'
                       for i, n in enumerate(names, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(
                       f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                       f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                       for i in range(1, len(names) + 1))
                   + f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
    return buf.getvalue()
