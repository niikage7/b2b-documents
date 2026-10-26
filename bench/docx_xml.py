"""Сборка DOCX из XML для тестов и стендов: полный контроль над нумерацией, стилями и правками."""

from __future__ import annotations

import zipfile
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_DECL = (
    f'xmlns:w="{W_NS}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
)

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
</Relationships>"""

DEFAULT_STYLES = f"""<w:styles {NS_DECL}>
<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
  <w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
  <w:tblPr><w:tblBorders><w:top w:val="single"/><w:left w:val="single"/><w:bottom w:val="single"/>
  <w:right w:val="single"/><w:insideH w:val="single"/><w:insideV w:val="single"/></w:tblBorders></w:tblPr></w:style>
{{extra}}
</w:styles>"""


def p(text: str = "", *, ppr: str = "", runs: str | None = None) -> str:
    """Абзац с одним run или с готовыми runs."""
    body = runs if runs is not None else (r(text) if text else "")
    return f"<w:p>{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}{body}</w:p>"


def r(text: str, rpr: str = "") -> str:
    return f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def numpr(num_id: int, ilvl: int = 0) -> str:
    return f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>'


def lvl(ilvl: int, fmt: str, text: str, *, start: int = 1, extra: str = "") -> str:
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/><w:numFmt w:val="{fmt}"/>'
        f'<w:lvlText w:val="{text}"/>{extra}</w:lvl>'
    )


def numbering(abstracts: dict[int, str], nums: dict[int, int | tuple[int, str]]) -> str:
    """abstracts: id → уровни; nums: numId → abstractId или (abstractId, lvlOverride-xml)."""
    parts = [f"<w:numbering {NS_DECL}>"]
    for aid, levels in abstracts.items():
        parts.append(f'<w:abstractNum w:abstractNumId="{aid}">{levels}</w:abstractNum>')
    for nid, spec in nums.items():
        aid, overrides = (spec, "") if isinstance(spec, int) else spec
        parts.append(f'<w:num w:numId="{nid}"><w:abstractNumId w:val="{aid}"/>{overrides}</w:num>')
    parts.append("</w:numbering>")
    return "".join(parts)


def make_docx(
    path: Path,
    body: str,
    *,
    numbering_xml: str | None = None,
    styles_extra: str = "",
    footnotes_xml: str | None = None,
    sect: str = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>',
) -> Path:
    document = f'<w:document {NS_DECL}><w:body>{body}{sect}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", DEFAULT_STYLES.replace("{extra}", styles_extra))
        zf.writestr("word/numbering.xml", numbering_xml or f"<w:numbering {NS_DECL}/>")
        zf.writestr(
            "word/footnotes.xml",
            footnotes_xml
            or f'<w:footnotes {NS_DECL}><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote></w:footnotes>',
        )
    return path
