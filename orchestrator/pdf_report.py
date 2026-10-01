"""
Render a Markdown company-intelligence report into a styled PDF.

Pure Python (markdown + reportlab), so it runs on AWS Lambda without
system libraries. Fonts are bundled in ./fonts so currency symbols such as
the rupee sign render correctly.
"""

import os
import re
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from io import BytesIO

import markdown
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)


# ============================================================
# THEME
# ============================================================

INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#6B7280")
NAVY = colors.HexColor("#14213D")
ACCENT = colors.HexColor("#2F6FDE")
RULE = colors.HexColor("#D9DEE5")
HEADER_BG = colors.HexColor("#EEF2F7")
ZEBRA = colors.HexColor("#F8FAFC")
QUOTE_BG = colors.HexColor("#F3F6FA")
WARN_BG = colors.HexColor("#FFF7E6")
WARN_BORDER = colors.HexColor("#F59E0B")
OK = colors.HexColor("#15803D")

PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
BAND_H = 42 * mm


# ============================================================
# FONTS
# ============================================================

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_FONTS_READY = False
BODY = "Helvetica"
BODY_B = "Helvetica-Bold"
BODY_I = "Helvetica-Oblique"
BODY_BI = "Helvetica-BoldOblique"
MONO = "Courier"
FALLBACK = None

_body_glyphs = None
_fallback_glyphs = None


def _register_fonts():
    """Register bundled fonts once. Falls back to Helvetica if missing."""

    global _FONTS_READY, BODY, BODY_B, BODY_I, BODY_BI, MONO, FALLBACK
    global _body_glyphs, _fallback_glyphs

    if _FONTS_READY:
        return

    _FONTS_READY = True

    def path(name):
        return os.path.join(FONT_DIR, name)

    try:
        pdfmetrics.registerFont(TTFont("Body", path("Carlito-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Body-Bold", path("Carlito-Bold.ttf")))
        pdfmetrics.registerFont(TTFont("Body-Italic", path("Carlito-Italic.ttf")))
        pdfmetrics.registerFont(
            TTFont("Body-BoldItalic", path("Carlito-BoldItalic.ttf"))
        )
        pdfmetrics.registerFontFamily(
            "Body",
            normal="Body",
            bold="Body-Bold",
            italic="Body-Italic",
            boldItalic="Body-BoldItalic",
        )
        BODY, BODY_B, BODY_I, BODY_BI = (
            "Body", "Body-Bold", "Body-Italic", "Body-BoldItalic"
        )
        _body_glyphs = set(
            pdfmetrics.getFont("Body").face.charToGlyph.keys()
        )
    except Exception as e:
        print(f"PDF: bundled body font unavailable, using Helvetica ({e})")

    try:
        pdfmetrics.registerFont(TTFont("Mono", path("DejaVuSansMono.ttf")))
        MONO = "Mono"
    except Exception as e:
        print(f"PDF: bundled mono font unavailable, using Courier ({e})")

    try:
        pdfmetrics.registerFont(TTFont("Fallback", path("DejaVuSans.ttf")))
        FALLBACK = "Fallback"
        _fallback_glyphs = set(
            pdfmetrics.getFont("Fallback").face.charToGlyph.keys()
        )
    except Exception as e:
        print(f"PDF: fallback font unavailable ({e})")


def _safe_text(text):
    """
    Escape text for reportlab markup and handle characters the body font
    lacks: use the fallback font when it has the glyph, otherwise drop it
    (mostly emoji), so no black boxes appear in the PDF.
    """

    if _body_glyphs is None:
        # Helvetica path: only Latin-1 is safe.
        text = text.replace("₹", "Rs ")
        text = text.encode("latin-1", "ignore").decode("latin-1")
        return escape(text, quote=False)

    out = []

    for ch in text:
        cp = ord(ch)

        if cp in _body_glyphs or ch in "\n\t ":
            out.append(escape(ch, quote=False))
        elif _fallback_glyphs and cp in _fallback_glyphs:
            out.append(
                f'<font name="{FALLBACK}">{escape(ch, quote=False)}</font>'
            )
        elif 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:
            continue  # variation selectors / joiners from emoji
        else:
            continue

    return "".join(out)


# ============================================================
# STYLES
# ============================================================

def _styles():

    base = dict(fontName=BODY, textColor=INK, alignment=TA_LEFT)

    return {
        "body": ParagraphStyle(
            "body", fontSize=10.5, leading=15, spaceAfter=6, **base
        ),
        "h1": ParagraphStyle(
            "h1", fontName=BODY_B, fontSize=18, leading=22,
            textColor=NAVY, spaceBefore=10, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=BODY_B, fontSize=14.5, leading=18,
            textColor=NAVY, spaceBefore=14, spaceAfter=2,
        ),
        "h3": ParagraphStyle(
            "h3", fontName=BODY_B, fontSize=12, leading=15,
            textColor=INK, spaceBefore=10, spaceAfter=4,
        ),
        "h4": ParagraphStyle(
            "h4", fontName=BODY_B, fontSize=10.5, leading=14,
            textColor=MUTED, spaceBefore=8, spaceAfter=3,
        ),
        "li": ParagraphStyle(
            "li", fontSize=10.5, leading=14.5, spaceAfter=2, **base
        ),
        "cell": ParagraphStyle(
            "cell", fontSize=9, leading=12, **base
        ),
        "cell_head": ParagraphStyle(
            "cell_head", fontName=BODY_B, fontSize=9, leading=12,
            textColor=NAVY,
        ),
        "quote": ParagraphStyle(
            "quote", fontName=BODY_I, fontSize=10.5, leading=15,
            textColor=INK,
        ),
        "code": ParagraphStyle(
            "code", fontName=MONO, fontSize=8.5, leading=11,
            textColor=INK,
        ),
        "fact_label": ParagraphStyle(
            "fact_label", fontName=BODY, fontSize=8, leading=10,
            textColor=MUTED,
        ),
        "fact_value": ParagraphStyle(
            "fact_value", fontName=BODY_B, fontSize=11, leading=14,
            textColor=INK,
        ),
        "warn": ParagraphStyle(
            "warn", fontSize=9.5, leading=13, **base
        ),
        "small": ParagraphStyle(
            "small", fontSize=8.5, leading=11, textColor=MUTED,
            fontName=BODY,
        ),
    }


# ============================================================
# MARKDOWN -> SIMPLE TREE
# ============================================================

class _Node:

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.parent = parent


VOID = {"br", "hr", "img", "input", "meta", "link"}


class _TreeBuilder(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _parse_markdown(md_text):

    html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists"],
    )

    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()

    return builder.root


def clean_model_markdown(text):
    """Remove a ```markdown fence if the model wrapped the whole report."""

    text = (text or "").strip()

    match = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```$", text, re.S)

    if match:
        text = match.group(1).strip()

    return _normalise_list_indent(text)


_LIST_LINE = re.compile(r"^( +)([-*+]|\d+[.)]) ")


def _normalise_list_indent(text):
    """
    Python-Markdown needs 4 spaces per nested list level, but models
    usually indent with 2. If the report uses 2-space nesting, widen it.
    """

    indents = [
        len(m.group(1))
        for m in map(_LIST_LINE.match, text.splitlines())
        if m
    ]

    if not indents or all(i % 4 == 0 for i in indents):
        return text

    lines = []

    for line in text.splitlines():
        m = _LIST_LINE.match(line)
        if m:
            level = len(m.group(1)) // 2
            line = " " * (4 * level) + line[len(m.group(1)):]
        lines.append(line)

    return "\n".join(lines)


# ============================================================
# TREE -> REPORTLAB FLOWABLES
# ============================================================

BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table",
    "blockquote", "pre", "hr", "div",
}

CITATION_RE = re.compile(r"\[(S\d+(?:\s*[,;]\s*S\d+)*)\]")


class _Renderer:

    def __init__(self, styles, source_urls):
        self.s = styles
        self.source_urls = source_urls or {}

    # ---------------- inline ----------------

    def _citations(self, markup):
        """Style [S1] / [S1, S2] citations as small linked superscripts."""

        def repl(m):
            ids = re.split(r"\s*[,;]\s*", m.group(1))
            parts = []

            for sid in ids:
                url = self.source_urls.get(sid)
                if url:
                    parts.append(
                        f'<a href="{escape(url)}" color="{ACCENT.hexval()}">'
                        f"{sid}</a>"
                    )
                else:
                    parts.append(sid)

            return (
                f'<super><font size="7" color="{ACCENT.hexval()}">'
                f'[{",".join(parts)}]</font></super>'
            )

        return CITATION_RE.sub(repl, markup)

    def inline(self, node):

        if isinstance(node, str):
            return _safe_text(node)

        inner = "".join(self.inline(c) for c in node.children)
        tag = node.tag

        if tag in ("strong", "b"):
            return f"<b>{inner}</b>"
        if tag in ("em", "i"):
            return f"<i>{inner}</i>"
        if tag == "code":
            return (
                f'<font name="{MONO}" size="9" color="#9F1239">{inner}</font>'
            )
        if tag == "a":
            href = node.attrs.get("href", "")
            if not href:
                return inner
            return (
                f'<a href="{escape(href)}" color="{ACCENT.hexval()}">'
                f"{inner}</a>"
            )
        if tag == "br":
            return "<br/>"
        if tag == "img":
            return _safe_text(node.attrs.get("alt", ""))
        if tag in ("del", "s"):
            return f"<strike>{inner}</strike>"

        return inner

    def para(self, markup, style):

        markup = self._citations(markup.strip())

        if not markup:
            return None

        try:
            return Paragraph(markup, style)
        except Exception:
            # Bad markup from the model should never break the report.
            plain = re.sub(r"<[^>]+>", "", markup)
            return Paragraph(plain, style)

    # ---------------- blocks ----------------

    def blocks(self, node):

        out = []
        inline_buf = []

        def flush():
            if inline_buf:
                p = self.para("".join(inline_buf), self.s["body"])
                if p:
                    out.append(p)
                inline_buf.clear()

        for child in node.children:

            if isinstance(child, str):
                if child.strip():
                    inline_buf.append(_safe_text(child))
                continue

            if child.tag in BLOCK_TAGS:
                flush()
                out.extend(self.block(child))
            else:
                inline_buf.append(self.inline(child))

        flush()

        return self._group_headings(out)

    @staticmethod
    def _group_headings(flowables):
        """
        Bind each heading (and its rule) to the block after it so a
        heading never sits alone at the bottom of a page. Done explicitly
        because reportlab's keepWithNext will not chain onto a block that
        is already a KeepTogether (as short tables are).
        """

        grouped = []
        run = []

        for f in flowables:

            if getattr(f, "keepWithNext", False):
                run.append(f)
                continue

            if run:
                if isinstance(f, KeepTogether):
                    grouped.append(KeepTogether(run + list(f._content)))
                elif isinstance(f, CondPageBreak):
                    grouped.extend(run)
                    grouped.append(f)
                    run = []
                    continue
                else:
                    grouped.append(KeepTogether(run + [f]))
                run = []
            else:
                grouped.append(f)

        grouped.extend(run)

        return grouped

    def block(self, node):

        tag = node.tag
        s = self.s

        if tag == "h1":
            heading = self.para(self.inline(node), s["h1"])
            heading.keepWithNext = True
            return [CondPageBreak(45 * mm), heading]

        if tag == "h2":
            heading = self.para(self.inline(node), s["h2"])
            rule = HRFlowable(
                width="100%", thickness=0.8, color=RULE,
                spaceBefore=2, spaceAfter=6,
            )
            # Keep heading, rule and the following block on one page.
            heading.keepWithNext = True
            rule.keepWithNext = True
            return [CondPageBreak(40 * mm), heading, rule]

        if tag in ("h3", "h4", "h5", "h6"):
            style = s["h3"] if tag == "h3" else s["h4"]
            heading = self.para(self.inline(node), style)
            heading.keepWithNext = True
            return [CondPageBreak(25 * mm), heading]

        if tag == "p":
            p = self.para(self.inline(node), s["body"])
            return [p] if p else []

        if tag in ("ul", "ol"):
            return [self.list_(node)]

        if tag == "table":
            return [self.table(node), Spacer(1, 8)]

        if tag == "blockquote":
            inner = self.blocks(node)
            for f in inner:
                if isinstance(f, Paragraph):
                    f.style = s["quote"]
            t = Table([[inner]], colWidths=["100%"])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), QUOTE_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            return [t, Spacer(1, 8)]

        if tag == "pre":
            text = "".join(
                c if isinstance(c, str) else self._text(c)
                for c in node.children
            )
            code = XPreformatted(_safe_text(text.rstrip()), s["code"])
            t = Table([[code]], colWidths=["100%"])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), QUOTE_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            return [t, Spacer(1, 8)]

        if tag == "hr":
            return [HRFlowable(
                width="100%", thickness=0.6, color=RULE,
                spaceBefore=6, spaceAfter=8,
            )]

        return self.blocks(node)

    def _text(self, node):
        if isinstance(node, str):
            return node
        return "".join(self._text(c) for c in node.children)

    def _keep(self, flowable):
        return flowable

    def list_(self, node):

        items = []

        for li in node.children:

            if isinstance(li, str) or li.tag != "li":
                continue

            flows = []
            inline_buf = []

            for c in li.children:
                if isinstance(c, str):
                    inline_buf.append(_safe_text(c))
                elif c.tag in ("ul", "ol"):
                    if inline_buf:
                        p = self.para("".join(inline_buf), self.s["li"])
                        if p:
                            flows.append(p)
                        inline_buf = []
                    flows.append(self.list_(c))
                elif c.tag == "p":
                    inline_buf.append(self.inline(c) + " ")
                elif c.tag in BLOCK_TAGS:
                    flows.extend(self.block(c))
                else:
                    inline_buf.append(self.inline(c))

            if inline_buf:
                p = self.para("".join(inline_buf), self.s["li"])
                if p:
                    flows.append(p)

            if flows:
                items.append(ListItem(flows, leftIndent=14))

        ordered = node.tag == "ol"

        return ListFlowable(
            items,
            bulletType="1" if ordered else "bullet",
            start="1" if ordered else "•",
            bulletFontName=BODY,
            bulletFontSize=9 if not ordered else 10,
            bulletColor=ACCENT,
            leftIndent=14,
            spaceAfter=6,
        )

    def table(self, node):

        rows = []
        header_rows = 0

        def collect(n):
            nonlocal header_rows
            for c in n.children:
                if isinstance(c, str):
                    continue
                if c.tag == "thead":
                    before = len(rows)
                    collect(c)
                    header_rows += len(rows) - before
                elif c.tag in ("tbody", "tfoot"):
                    collect(c)
                elif c.tag == "tr":
                    cells = [
                        x for x in c.children
                        if not isinstance(x, str) and x.tag in ("th", "td")
                    ]
                    rows.append(cells)

        collect(node)

        if not rows:
            return Spacer(1, 0)

        ncols = max(len(r) for r in rows)
        avail = PAGE_W - 2 * MARGIN_X

        # Each column gets at least its longest word (so words never split
        # mid-word); spare width is shared in proportion to text length.
        pad = 12
        floors = [0.0] * ncols
        weights = [1.0] * ncols

        for ri, r in enumerate(rows):
            font = BODY_B if ri < header_rows else BODY
            for i, cell in enumerate(r):
                text = self._text(cell)
                longest = max(
                    (w for w in text.split() if not w.startswith("http")),
                    key=len,
                    default="",
                )
                floors[i] = max(
                    floors[i],
                    pdfmetrics.stringWidth(longest, font, 9) + pad,
                )
                weights[i] = max(weights[i], min(len(text), 80))

        floors = [min(f, avail * 0.35) for f in floors]

        if sum(floors) >= avail:
            widths = [avail * f / sum(floors) for f in floors]
        else:
            spare = avail - sum(floors)
            total = sum(weights)
            widths = [
                f + spare * w / total for f, w in zip(floors, weights)
            ]

        data = []
        for ri, r in enumerate(rows):
            is_head = ri < header_rows
            style = self.s["cell_head"] if is_head else self.s["cell"]
            row = []
            for i in range(ncols):
                if i < len(r):
                    row.append(
                        self.para(self.inline(r[i]), style) or ""
                    )
                else:
                    row.append("")
            data.append(row)

        t = Table(data, colWidths=widths, repeatRows=header_rows)

        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]

        if header_rows:
            cmds += [
                ("BACKGROUND", (0, 0), (-1, header_rows - 1), HEADER_BG),
                ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1),
                 1, NAVY),
            ]

        for ri in range(header_rows, len(data)):
            if (ri - header_rows) % 2 == 1:
                cmds.append(("BACKGROUND", (0, ri), (-1, ri), ZEBRA))

        t.setStyle(TableStyle(cmds))

        # Short tables move to the next page whole rather than splitting.
        if len(data) <= 8:
            return KeepTogether([t])

        return t


# ============================================================
# FIXED SECTIONS: KEY FACTS, WARNINGS, SOURCE REGISTER
# ============================================================

def _key_facts_panel(facts, styles):

    facts = [(k, v) for k, v in facts if v not in (None, "", [])]

    if not facts:
        return []

    cols = 3
    cells = []

    for label, value in facts:
        cells.append([
            Paragraph(_safe_text(label.upper()), styles["fact_label"]),
            Paragraph(_safe_text(str(value)), styles["fact_value"]),
        ])

    while len(cells) % cols:
        cells.append("")

    rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    width = (PAGE_W - 2 * MARGIN_X) / cols

    t = Table(rows, colWidths=[width] * cols)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.white),
    ]))

    return [t, Spacer(1, 10)]


def _warning_box(warnings, styles):

    if not warnings:
        return []

    lines = "<br/>".join(
        f"• {_safe_text(w)}" for w in warnings
    )
    body = Paragraph(
        f"<b>Data quality notice</b><br/>{lines}", styles["warn"]
    )

    t = Table([[body]], colWidths=[PAGE_W - 2 * MARGIN_X])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 3, WARN_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    return [t, Spacer(1, 10)]


def _source_register(sources, styles):

    if not sources:
        return []

    head = [
        Paragraph("ID", styles["cell_head"]),
        Paragraph("Source", styles["cell_head"]),
        Paragraph("Status", styles["cell_head"]),
    ]
    rows = [head]

    for src in sources:
        url = src.get("url", "")
        domain = src.get("domain") or ""
        link = (
            f'<b>{_safe_text(domain)}</b><br/>'
            f'<a href="{escape(url)}" color="{ACCENT.hexval()}">'
            f"{_safe_text(url)}</a>"
        )
        verified = src.get("verified")
        status = (
            f'<font color="{OK.hexval()}">Retrieved</font>'
            if verified
            else f'<font color="{WARN_BORDER.hexval()}">Unconfirmed</font>'
        )
        rows.append([
            Paragraph(_safe_text(src.get("id", "")), styles["cell"]),
            Paragraph(link, styles["cell"]),
            Paragraph(status, styles["cell"]),
        ])

    avail = PAGE_W - 2 * MARGIN_X
    t = Table(
        rows,
        colWidths=[avail * 0.08, avail * 0.74, avail * 0.18],
        repeatRows=1,
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 1, NAVY),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    return [
        CondPageBreak(40 * mm),
        Paragraph("Source register", styles["h2"]),
        HRFlowable(width="100%", thickness=0.8, color=RULE,
                   spaceBefore=2, spaceAfter=6),
        t,
        Spacer(1, 4),
        Paragraph(
            "Retrieved: returned by the web research search step. "
            "Unconfirmed: cited by the research model but not returned "
            "by search; check the link before relying on it.",
            styles["small"],
        ),
    ]


# ============================================================
# PAGE DECORATION
# ============================================================

def _make_canvas_class(company, generated_label):

    class NumberedCanvas(rl_canvas.Canvas):
        """Two-pass canvas so the footer can show 'Page X of Y'."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total):
            page = self._pageNumber

            if page == 1:
                self.setFillColor(NAVY)
                self.rect(0, PAGE_H - BAND_H, PAGE_W, BAND_H,
                          stroke=0, fill=1)
                self.setFillColor(colors.HexColor("#9DB2D6"))
                self.setFont(BODY_B, 8.5)
                self.drawString(
                    MARGIN_X, PAGE_H - 13 * mm,
                    "COMPANY INTELLIGENCE REPORT",
                )
                self.setFillColor(colors.white)
                self.setFont(BODY_B, 24)
                self.drawString(MARGIN_X, PAGE_H - 25 * mm, company)
                self.setFillColor(colors.HexColor("#C9D4E8"))
                self.setFont(BODY, 9.5)
                self.drawString(
                    MARGIN_X, PAGE_H - 33 * mm, generated_label
                )
            else:
                self.setFillColor(MUTED)
                self.setFont(BODY, 8.5)
                self.drawString(
                    MARGIN_X, PAGE_H - 12 * mm,
                    f"{company}  |  Company Intelligence Report",
                )
                self.setStrokeColor(RULE)
                self.setLineWidth(0.6)
                self.line(MARGIN_X, PAGE_H - 14 * mm,
                          PAGE_W - MARGIN_X, PAGE_H - 14 * mm)

            self.setStrokeColor(RULE)
            self.setLineWidth(0.6)
            self.line(MARGIN_X, 14 * mm, PAGE_W - MARGIN_X, 14 * mm)
            self.setFillColor(MUTED)
            self.setFont(BODY, 8)
            self.drawString(
                MARGIN_X, 9.5 * mm,
                "Generated automatically from public sources. "
                "Verify key facts before use.",
            )
            self.drawRightString(
                PAGE_W - MARGIN_X, 9.5 * mm, f"Page {page} of {total}"
            )

    return NumberedCanvas


# ============================================================
# PUBLIC API
# ============================================================

def _drop_title_line(md_text, company):
    """
    Remove the report's own title (the page band already shows it), but
    only when the very first line is an H1 naming the company or the
    report. Any other H1 is a real section heading and is kept.
    """

    lines = md_text.splitlines()

    if not lines:
        return md_text

    first = lines[0].strip()

    if re.match(r"^#\s", first):
        heading = first[2:].lower()
        if (company or "").lower() in heading or "report" in heading:
            return "\n".join(lines[1:]).lstrip("\n")

    return md_text


# ============================================================
# RESEARCH EVIDENCE APPENDIX
# ============================================================

def _cell(value):
    """Markdown-table-safe text for one cell."""

    if value is None:
        return ""

    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)

    text = str(value).replace("|", "/").replace("\n", " ").strip()

    return text


def _ids(record):
    ids = record.get("source_ids") or []
    return "[" + ", ".join(ids) + "]" if ids else "No source"


APPENDIX_SECTIONS = [
    (
        "executive_facts", "Executive facts",
        ["Fact", "Status", "Confidence", "Evidence", "Sources"],
        lambda r: [r.get("claim"), r.get("status"), r.get("confidence"),
                   r.get("evidence"), _ids(r)],
    ),
    (
        "claims", "Research claims",
        ["Claim", "Status", "Confidence", "Evidence", "Sources"],
        lambda r: [r.get("claim"), r.get("status"), r.get("confidence"),
                   r.get("evidence"), _ids(r)],
    ),
    (
        "customers", "Customers and relationships",
        ["Organization", "Classification", "Evidence", "Sources"],
        lambda r: [r.get("organization"),
                   str(r.get("classification", "")).replace("_", " "),
                   r.get("evidence"), _ids(r)],
    ),
    (
        "technology", "Technology",
        ["Area", "Claim", "Status", "Confidence", "Evidence", "Sources"],
        lambda r: [r.get("area"), r.get("claim"), r.get("status"),
                   r.get("confidence"), r.get("evidence"), _ids(r)],
    ),
    (
        "competitors", "Competitors",
        ["Company", "Category", "Overlap", "Difference", "Sources"],
        lambda r: [r.get("name"),
                   str(r.get("category", "")).replace("_", " "),
                   r.get("why_relevant"), r.get("difference"), _ids(r)],
    ),
    (
        "funding", "Funding history",
        ["Date", "Round", "Amount raised", "Lead investor",
         "Other investors", "Valuation", "Sources"],
        lambda r: [r.get("date"), r.get("round"), r.get("amount"),
                   r.get("lead_investor"), r.get("other_investors"),
                   r.get("valuation"), _ids(r)],
    ),
    (
        "unknowns", "Open questions",
        ["Question", "Why it is unanswered"],
        lambda r: [r.get("question"), r.get("reason")],
    ),
]


def research_appendix_markdown(packet):
    """
    Every record from the structured research packet as Markdown tables,
    so the PDF carries the full evidence, not only what the report
    model chose to mention.
    """

    if not packet:
        return ""

    parts = [
        "# Appendix: research evidence",
        "",
        "All findings from web research, with evidence status. "
        "CONFIRMED means a source directly supports the statement, "
        "INFERRED means it is a reasonable conclusion from several "
        "pieces of evidence, UNKNOWN means public evidence does not "
        "establish it.",
        "",
    ]

    for key, title, headers, row in APPENDIX_SECTIONS:

        records = [
            r for r in (packet.get(key) or []) if isinstance(r, dict)
        ]

        if not records:
            continue

        parts.append(f"## {title}")
        parts.append("")
        parts.append("| " + " | ".join(headers) + " |")
        parts.append("|" + "---|" * len(headers))

        for record in records:
            parts.append(
                "| " + " | ".join(_cell(v) for v in row(record)) + " |"
            )

        parts.append("")

    return "\n".join(parts) if len(parts) > 5 else ""


def render_report_pdf(
    markdown_text,
    company,
    key_facts=None,
    sources=None,
    warnings=None,
    generated_at=None,
    research_packet=None,
):
    """
    Build the PDF and return it as bytes.

    markdown_text : report body produced by the model
    company       : company name for the title band
    key_facts     : list of (label, value) for the summary panel
    sources       : [{"id","domain","url","verified"}] for the register
    warnings      : list of data-quality strings shown in a notice box
    research_packet : structured research (with source_ids); when given,
                    every record is added as an evidence appendix
    """

    _register_fonts()
    styles = _styles()

    generated_at = generated_at or datetime.now(timezone.utc)
    generated_label = (
        "Generated " + generated_at.strftime("%d %b %Y, %H:%M UTC")
    )

    md_text = _drop_title_line(clean_model_markdown(markdown_text), company)
    source_urls = {
        s.get("id"): s.get("url")
        for s in (sources or [])
        if s.get("id") and s.get("url")
    }

    renderer = _Renderer(styles, source_urls)
    body = renderer.blocks(_parse_markdown(md_text))

    # Only append the register if the model didn't write its own.
    has_sources_section = bool(
        re.search(r"^#{1,3}\s*(sources|references)\b", md_text,
                  re.I | re.M)
    )

    story = [NextPageTemplate("later")]
    story += _key_facts_panel(key_facts or [], styles)
    story += _warning_box(warnings or [], styles)
    story += body

    appendix_md = research_appendix_markdown(research_packet)

    if appendix_md:
        story += renderer.blocks(_parse_markdown(appendix_md))

    # Add the register when the model wrote no sources section, or when
    # some sources are unconfirmed (so that status is always visible).
    any_unconfirmed = any(not s.get("verified") for s in (sources or []))

    if sources and (not has_sources_section or any_unconfirmed):
        story += _source_register(sources, styles)

    buf = BytesIO()

    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title=f"Company Intelligence Report - {company}",
        author="Company Intelligence Pipeline",
    )

    frame_w = PAGE_W - 2 * MARGIN_X

    first = Frame(
        MARGIN_X, 20 * mm, frame_w,
        PAGE_H - BAND_H - 8 * mm - 20 * mm,
        id="first", leftPadding=0, rightPadding=0,
        topPadding=0, bottomPadding=0,
    )
    later = Frame(
        MARGIN_X, 20 * mm, frame_w,
        PAGE_H - 22 * mm - 20 * mm,
        id="later", leftPadding=0, rightPadding=0,
        topPadding=0, bottomPadding=0,
    )

    doc.addPageTemplates([
        PageTemplate(id="first", frames=[first]),
        PageTemplate(id="later", frames=[later]),
    ])

    doc.build(
        story,
        canvasmaker=_make_canvas_class(company, generated_label),
    )

    return buf.getvalue()
