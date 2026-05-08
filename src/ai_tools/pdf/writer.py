"""PDF writers using ReportLab Platypus.

Three renderers are provided:

* ``markdown_to_pdf`` – structured markdown (headings, tables, bold/italic, HR).
* ``text_to_pdf``     – plain/preformatted text (logs, configs, terminal output)
                        rendered in Courier monospace, whitespace preserved exactly.
* ``rtf_to_pdf``      – RTF files converted via macOS ``textutil`` then rendered
                        with bold/italic/table support from the HTML intermediate.

All three share the same landscape-A4 layout, footer, and timezone helpers.
"""

from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    Preformatted,
)

KNOWN_TIMEZONES: dict[str, timezone] = {
    "UTC": timezone.utc,
    "EDT": timezone(timedelta(hours=-4)),
    "EST": timezone(timedelta(hours=-5)),
    "PDT": timezone(timedelta(hours=-7)),
    "PST": timezone(timedelta(hours=-8)),
    "IST": timezone(timedelta(hours=5, minutes=30)),
}

_UTC_TIME_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(\.\d+)?\b")


def convert_utc_timestamps(text: str, target_tz: timezone, tz_label: str) -> str:
    """Shift every HH:MM:SS[.mmm] in *text* from UTC to *target_tz*."""
    offset_hours = int(target_tz.utcoffset(None).total_seconds() // 3600)  # type: ignore[union-attr]

    def _replace(m: re.Match[str]) -> str:
        hh, mm, ss, frac = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""
        new_hh = (hh + offset_hours) % 24
        return f"{new_hh:02d}:{mm:02d}:{ss:02d}{frac}"

    return _UTC_TIME_RE.sub(_replace, text)


def _inline(text: str) -> str:
    """Convert **bold** and *italic* markdown to ReportLab XML markup."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # Escape bare ampersands not already part of an entity
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos);)", "&amp;", text)
    return text


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontSize=16, spaceAfter=8),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontSize=13, spaceAfter=6),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontSize=11, spaceAfter=4),
        "body": ParagraphStyle("Body", parent=base["Normal"], fontSize=8.5, leading=12, spaceAfter=4),
        "table_header": ParagraphStyle("TableHeader", parent=base["Normal"], fontSize=8.5, leading=12,
                                       textColor=colors.white, fontName="Helvetica-Bold"),
        "meta": ParagraphStyle("Meta", parent=base["Normal"], fontSize=8, textColor=colors.HexColor("#555555")),
    }


_TABLE_HEADER_BG = colors.HexColor("#2C3E50")
_TABLE_ALT_BG = colors.HexColor("#F2F4F5")


def _build_table(rows: list[list[str]], styles: dict[str, ParagraphStyle]) -> Table:
    header_style = styles["table_header"]
    cell_style = styles["body"]
    data = [
        [Paragraph(_inline(c.strip()), header_style if i == 0 else cell_style) for c in row]
        for i, row in enumerate(rows)
    ]
    col_count = max(len(r) for r in data)

    # Distribute width: landscape A4 minus margins = ~25 cm
    page_w = landscape(A4)[0] - 3 * cm
    col_w = page_w / col_count

    tbl = Table(data, colWidths=[col_w] * col_count, repeatRows=1)
    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _TABLE_HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _TABLE_ALT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])
    tbl.setStyle(ts)
    return tbl


def _parse_markdown(content: str, styles: dict[str, ParagraphStyle]) -> list:
    """Convert markdown text to a list of ReportLab flowables."""
    flowables: list = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("# "):
            flowables.append(Paragraph(_inline(stripped[2:]), styles["h1"]))
            i += 1
        elif stripped.startswith("## "):
            flowables.append(Spacer(1, 0.2 * cm))
            flowables.append(Paragraph(_inline(stripped[3:]), styles["h2"]))
            i += 1
        elif stripped.startswith("### "):
            flowables.append(Paragraph(_inline(stripped[4:]), styles["h3"]))
            i += 1
        elif stripped.startswith("|") and not re.match(r"^\|[-| :]+\|$", stripped):
            # Collect table rows
            rows: list[list[str]] = []
            while i < len(lines):
                ln = lines[i].strip()
                if not ln.startswith("|"):
                    break
                if re.match(r"^\|[-| :]+\|$", ln):
                    i += 1
                    continue
                cells = [c for c in ln.split("|") if c != ""]
                rows.append(cells)
                i += 1
            if rows:
                flowables.append(_build_table(rows, styles))
                flowables.append(Spacer(1, 0.2 * cm))
        elif stripped == "---":
            flowables.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#AAAAAA")))
            flowables.append(Spacer(1, 0.1 * cm))
            i += 1
        elif stripped == "":
            flowables.append(Spacer(1, 0.15 * cm))
            i += 1
        else:
            flowables.append(Paragraph(_inline(stripped), styles["body"]))
            i += 1
    return flowables


def markdown_to_pdf(
    content: str,
    output_path: str | Path,
    title: str = "",
    tz_label: str = "UTC",
    convert_from_utc_to: str | None = None,
) -> Path:
    """Render *content* (markdown) to a PDF at *output_path*.

    Args:
        content: Markdown text to render.
        output_path: Destination file path (created/overwritten).
        title: Document title shown in the PDF metadata.
        tz_label: Timezone label shown in the document footer.
        convert_from_utc_to: If given (e.g. "EDT"), convert UTC timestamps in
            the content to that timezone before rendering.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if convert_from_utc_to:
        tz_key = convert_from_utc_to.upper()
        target_tz = KNOWN_TIMEZONES.get(tz_key, timezone.utc)
        content = convert_utc_timestamps(content, target_tz, tz_key)

    styles = _build_styles()
    now = datetime.now(tz=timezone.utc)
    tz_obj = KNOWN_TIMEZONES.get(tz_label.upper(), timezone.utc)
    now_display = now.astimezone(tz_obj)

    def _on_page(canvas, doc):  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        footer = (
            f"{title}  |  Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}"
            f"  |  Page {doc.page}"
        )
        canvas.drawCentredString(landscape(A4)[0] / 2, 0.6 * cm, footer)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=title,
        author="ai-tools",
    )

    # Add generation timestamp at the top
    meta_text = f"<i>Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}  |  All timestamps in {tz_label}</i>"
    story = [Paragraph(meta_text, styles["meta"]), Spacer(1, 0.3 * cm)]
    story.extend(_parse_markdown(content, styles))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return output_path



# ── Plain-text → PDF ─────────────────────────────────────────────────────────

def text_to_pdf(
    content: str,
    output_path: str | Path,
    title: str = "",
    tz_label: str = "UTC",
) -> Path:
    """Render plain/preformatted text to a PDF preserving indentation and line breaks.

    Uses Courier monospace so aligned columns, log output, and config files
    look exactly as they do in the source.  No markdown parsing is applied;
    every character is rendered as-is.

    Args:
        content: Raw text to render.
        output_path: Destination PDF path (created/overwritten).
        title: Document title shown in PDF metadata and footer.
        tz_label: Timezone label shown in footer (display only, no conversion).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base = getSampleStyleSheet()
    pre_style = ParagraphStyle(
        "PlainPre", parent=base["Normal"],
        fontName="Courier", fontSize=8, leading=11,
    )
    meta_style = ParagraphStyle(
        "PlainMeta", parent=base["Normal"],
        fontSize=8, textColor=colors.HexColor("#555555"),
    )

    now = datetime.now(tz=timezone.utc)
    tz_obj = KNOWN_TIMEZONES.get(tz_label.upper(), timezone.utc)
    now_display = now.astimezone(tz_obj)

    def _on_page(canvas, doc):  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        footer = (
            f"{title}  |  Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}"
            f"  |  Page {doc.page}"
        )
        canvas.drawCentredString(landscape(A4)[0] / 2, 0.6 * cm, footer)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=title, author="ai-tools",
    )

    meta_text = f"<i>Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}</i>"
    story: list = [Paragraph(meta_text, meta_style), Spacer(1, 0.3 * cm)]
    # Preformatted preserves every space, tab (expanded), and newline exactly.
    story.append(Preformatted(content.expandtabs(4), pre_style))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return output_path


# ── RTF → PDF ────────────────────────────────────────────────────────────────

class _RtfHtmlParser(HTMLParser):
    """Minimal HTML-to-flowables parser for ``textutil``-generated HTML.

    Handles: block tags (p, div, h1-h6, li, blockquote), inline tags
    (b/strong, i/em), br, hr, and pipe tables (table/tr/td/th).
    Unknown tags are skipped transparently.
    """

    _SKIP_TAGS = frozenset({"head", "style", "script", "meta", "link", "title"})
    _HEADING_MAP: dict[str, str] = {
        "h1": "h1", "h2": "h2", "h3": "h3",
        "h4": "h3", "h5": "h3", "h6": "h3",
    }
    _BLOCK_TAGS = frozenset({"p", "div", "li", "blockquote", "pre"})

    def __init__(self, styles: dict[str, ParagraphStyle]) -> None:
        super().__init__(convert_charrefs=True)
        self.styles = styles
        self.flowables: list = []
        self._buf = ""
        self._skip_depth = 0
        self._bold = 0
        self._italic = 0
        self._in_body = False
        self._block_style = "body"
        self._rows: list[list[str]] = []
        self._cells: list[str] = []
        self._in_cell = False
        self._cell_buf = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1; return
        if self._skip_depth: return
        if tag == "body": self._in_body = True; return
        if not self._in_body: return

        if tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._italic += 1
        elif tag == "br":
            self._buf += "<br/>"
        elif tag in self._HEADING_MAP:
            self._flush(); self._block_style = self._HEADING_MAP[tag]
        elif tag in self._BLOCK_TAGS:
            self._flush()
        elif tag == "hr":
            self._flush()
            self.flowables.append(
                HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#AAAAAA"))
            )
        elif tag == "table":
            self._flush(); self._rows = []
        elif tag == "tr":
            self._cells = []
        elif tag in ("td", "th"):
            self._flush(); self._in_cell = True; self._cell_buf = ""

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1); return
        if self._skip_depth or not self._in_body: return

        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        elif tag in self._HEADING_MAP:
            self._flush(); self._block_style = "body"
        elif tag in self._BLOCK_TAGS:
            self._flush()
        elif tag in ("td", "th"):
            self._cells.append(self._cell_buf.strip())
            self._in_cell = False; self._cell_buf = ""
        elif tag == "tr":
            if self._cells:
                self._rows.append(self._cells[:])
        elif tag == "table":
            if self._rows:
                self.flowables.append(_build_table(self._rows, self.styles))
                self.flowables.append(Spacer(1, 0.2 * cm))

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._in_body: return
        # Re-escape: convert_charrefs=True gives us decoded text; ReportLab needs XML.
        text = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if self._bold:   text = f"<b>{text}</b>"
        if self._italic: text = f"<i>{text}</i>"
        if self._in_cell:
            self._cell_buf += text
        else:
            self._buf += text

    def _flush(self) -> None:
        text = self._buf.strip()
        self._buf = ""
        if not text:
            return
        s = self._block_style
        if s == "h1":
            self.flowables.append(Paragraph(text, self.styles["h1"]))
        elif s == "h2":
            self.flowables.append(Spacer(1, 0.2 * cm))
            self.flowables.append(Paragraph(text, self.styles["h2"]))
        elif s == "h3":
            self.flowables.append(Paragraph(text, self.styles["h3"]))
        else:
            self.flowables.append(Paragraph(text, self.styles["body"]))
        self._block_style = "body"

    def close(self) -> None:
        super().close()
        self._flush()  # flush any trailing content not closed by a block tag


def rtf_to_pdf(
    rtf_path: str | Path,
    output_path: str | Path,
    title: str = "",
    tz_label: str = "UTC",
) -> Path:
    """Convert an RTF file to PDF, preserving bold, italic, and tables.

    Uses macOS ``textutil`` to convert the RTF to HTML first, then parses
    the HTML with :class:`_RtfHtmlParser` and renders via ReportLab.

    Args:
        rtf_path: Source RTF file path.
        output_path: Destination PDF path (created/overwritten).
        title: Document title shown in PDF metadata and footer.
        tz_label: Timezone label shown in footer (display only, no conversion).

    Raises:
        subprocess.CalledProcessError: If ``textutil`` is unavailable or fails.
        FileNotFoundError: If *rtf_path* does not exist.
    """
    rtf_path = Path(rtf_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["textutil", "-convert", "html", "-stdout", str(rtf_path)],
        capture_output=True, text=True, check=True,
    )

    styles = _build_styles()
    now = datetime.now(tz=timezone.utc)
    tz_obj = KNOWN_TIMEZONES.get(tz_label.upper(), timezone.utc)
    now_display = now.astimezone(tz_obj)

    def _on_page(canvas, doc):  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        footer = (
            f"{title}  |  Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}"
            f"  |  Page {doc.page}"
        )
        canvas.drawCentredString(landscape(A4)[0] / 2, 0.6 * cm, footer)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=title, author="ai-tools",
    )

    parser = _RtfHtmlParser(styles)
    parser.feed(result.stdout)
    parser.close()

    meta_text = f"<i>Generated: {now_display.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}</i>"
    story: list = [Paragraph(meta_text, styles["meta"]), Spacer(1, 0.3 * cm)]
    story.extend(parser.flowables)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return output_path
