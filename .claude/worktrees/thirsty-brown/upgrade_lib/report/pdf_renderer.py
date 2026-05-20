"""
PDF renderer for the upgrade report.

Builds a polished, presentation-quality PDF using reportlab from the same
structured data that feeds the markdown report. Designed to be readable
by both technical and non-technical stakeholders.

Layout:
  Page 1 — Cover & at-a-glance metric tiles
  Page 2 — Executive summary (plain language)
  Decision summary, risk distribution, quality gate
  High-risk merges (manual review)
  Artifact overview table (full list)
  Per-merge details
  AI recommendations
  JIRA tracking
  Footer with page numbers on every page
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #

PRIMARY = HexColor("#1E3A5F")        # deep navy
ACCENT = HexColor("#2BB8A3")         # teal
INK = HexColor("#1F2937")            # body text
MUTED = HexColor("#6B7280")          # secondary text
SOFT = HexColor("#F3F4F6")           # row alt / card bg
BORDER = HexColor("#D1D5DB")         # cell borders

# Status colors
COLOR_HIGH = HexColor("#D32F2F")
COLOR_MED = HexColor("#F57C00")
COLOR_LOW = HexColor("#388E3C")
COLOR_PASS = HexColor("#388E3C")
COLOR_WARN = HexColor("#F57C00")
COLOR_FAIL = HexColor("#D32F2F")
COLOR_MERGE = HexColor("#1976D2")
COLOR_RETAIN = HexColor("#00897B")
COLOR_REMOVE = HexColor("#616161")

# Lighter background variants for badges
BG_HIGH = HexColor("#FDECEA")
BG_MED = HexColor("#FFF4E5")
BG_LOW = HexColor("#E8F5E9")
BG_MERGE = HexColor("#E3F2FD")
BG_RETAIN = HexColor("#E0F2F1")
BG_REMOVE = HexColor("#EEEEEE")

PAGE_W, PAGE_H = LETTER
MARGIN_L = MARGIN_R = 0.6 * inch
MARGIN_T = 0.7 * inch
MARGIN_B = 0.7 * inch
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #

def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s: dict[str, ParagraphStyle] = {}

    s["TitleHero"] = ParagraphStyle(
        "TitleHero", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=32, leading=38, textColor=PRIMARY, alignment=TA_LEFT,
        spaceAfter=4,
    )
    s["SubtitleHero"] = ParagraphStyle(
        "SubtitleHero", parent=base["Normal"], fontName="Helvetica",
        fontSize=14, leading=18, textColor=MUTED, alignment=TA_LEFT,
        spaceAfter=18,
    )
    s["H1"] = ParagraphStyle(
        "H1", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, textColor=PRIMARY, spaceBefore=14,
        spaceAfter=8,
    )
    s["H2"] = ParagraphStyle(
        "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=16, textColor=PRIMARY, spaceBefore=10,
        spaceAfter=4,
    )
    s["Body"] = ParagraphStyle(
        "Body", parent=base["Normal"], fontName="Helvetica",
        fontSize=10, leading=14, textColor=INK, spaceAfter=6,
    )
    s["Small"] = ParagraphStyle(
        "Small", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=INK,
    )
    s["SmallMuted"] = ParagraphStyle(
        "SmallMuted", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=MUTED,
    )
    s["Cell"] = ParagraphStyle(
        "Cell", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=INK, wordWrap="CJK",
    )
    s["CellMono"] = ParagraphStyle(
        "CellMono", parent=base["Normal"], fontName="Courier",
        fontSize=8, leading=10, textColor=INK, wordWrap="CJK",
    )
    s["CellHeader"] = ParagraphStyle(
        "CellHeader", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=9, leading=11, textColor=colors.white,
    )
    s["MetricLabel"] = ParagraphStyle(
        "MetricLabel", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=11, textColor=MUTED, alignment=TA_CENTER,
    )
    s["MetricValue"] = ParagraphStyle(
        "MetricValue", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=24, leading=28, textColor=PRIMARY, alignment=TA_CENTER,
    )
    s["BadgeText"] = ParagraphStyle(
        "BadgeText", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8.5, leading=10, alignment=TA_CENTER,
    )
    return s


STYLES = _styles()


# --------------------------------------------------------------------------- #
# Page templates — header & footer
# --------------------------------------------------------------------------- #

def _draw_page_chrome(canvas, doc, project_id: str) -> None:
    """Header and footer drawn on every page."""
    canvas.saveState()

    # Top accent bar
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, PAGE_H - 0.25 * inch, PAGE_W, 0.25 * inch, fill=1, stroke=0)

    # Header text (right side)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.white)
    canvas.drawRightString(
        PAGE_W - MARGIN_R, PAGE_H - 0.17 * inch,
        f"Upgrade Report  ·  {project_id}",
    )

    # Footer line
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN_L, MARGIN_B - 0.2 * inch, PAGE_W - MARGIN_R, MARGIN_B - 0.2 * inch)

    # Footer text
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(
        MARGIN_L, MARGIN_B - 0.35 * inch,
        "Generated by Wisetrix  ·  GTM Docker-to-Docker Upgrade Engine",
    )
    canvas.drawRightString(
        PAGE_W - MARGIN_R, MARGIN_B - 0.35 * inch,
        f"Page {doc.page}",
    )

    canvas.restoreState()


# --------------------------------------------------------------------------- #
# Reusable visual elements
# --------------------------------------------------------------------------- #

def _badge(text: str, fg: HexColor, bg: HexColor, width: float = 0.8 * inch) -> Table:
    """Colored pill-style badge for status labels."""
    p = Paragraph(
        f'<font color="{fg.hexval()}"><b>{text}</b></font>',
        STYLES["BadgeText"],
    )
    t = Table([[p]], colWidths=[width], rowHeights=[0.22 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.5, fg),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    return t


def _risk_badge(level: str) -> Any:
    if level == "HIGH":
        return _badge("HIGH", COLOR_HIGH, BG_HIGH, 0.6 * inch)
    if level == "MEDIUM":
        return _badge("MEDIUM", COLOR_MED, BG_MED, 0.7 * inch)
    if level == "LOW":
        return _badge("LOW", COLOR_LOW, BG_LOW, 0.55 * inch)
    return Paragraph("—", STYLES["Cell"])


def _decision_badge(decision: str) -> Any:
    d = (decision or "").strip()
    if d == "Merge":
        return _badge("MERGE", COLOR_MERGE, BG_MERGE, 0.65 * inch)
    if d == "Retain":
        return _badge("RETAIN", COLOR_RETAIN, BG_RETAIN, 0.65 * inch)
    if d == "Remove":
        return _badge("REMOVE", COLOR_REMOVE, BG_REMOVE, 0.7 * inch)
    if d == "ERROR":
        return _badge("ERROR", COLOR_FAIL, BG_HIGH, 0.65 * inch)
    return Paragraph(d or "—", STYLES["Cell"])


def _verdict_badge(verdict: str) -> Any:
    v = (verdict or "").strip().upper()
    if v == "PASS":
        return _badge("PASS", COLOR_PASS, BG_LOW, 0.55 * inch)
    if v == "WARN":
        return _badge("WARN", COLOR_WARN, BG_MED, 0.55 * inch)
    if v == "FAIL":
        return _badge("FAIL", COLOR_FAIL, BG_HIGH, 0.55 * inch)
    return Paragraph(verdict or "—", STYLES["Cell"])


def _metric_tile(label: str, value: str, accent: HexColor = PRIMARY) -> Table:
    """A single metric card — label on top, big number below."""
    inner = Table(
        [
            [Paragraph(label.upper(), STYLES["MetricLabel"])],
            [Paragraph(f'<font color="{accent.hexval()}">{value}</font>', STYLES["MetricValue"])],
        ],
        colWidths=[1.4 * inch],
        rowHeights=[0.3 * inch, 0.55 * inch],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
        ("LINEABOVE", (0, 0), (-1, 0), 3, accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return inner


def _metric_row(metrics: list[tuple[str, str, HexColor]]) -> Table:
    """A row of metric tiles, evenly spaced."""
    n = len(metrics)
    if n == 0:
        return Spacer(1, 0)
    cell_w = CONTENT_W / n
    cells = [_metric_tile(label, value, color) for label, value, color in metrics]
    row = Table([cells], colWidths=[cell_w] * n)
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return row


def _styled_table(
    header: list[str],
    rows: list[list[Any]],
    col_widths: list[float],
    align: list[str] | None = None,
) -> Table:
    """Standard data table — primary-colored header, alternating row bg, wrapping cells."""
    header_row = [Paragraph(h, STYLES["CellHeader"]) for h in header]
    data = [header_row] + rows
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, 0), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, PRIMARY),
        ("GRID", (0, 1), (-1, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if align:
        for col_idx, a in enumerate(align):
            style.append(("ALIGN", (col_idx, 0), (col_idx, -1), a.upper()))
    t.setStyle(TableStyle(style))
    return t


# --------------------------------------------------------------------------- #
# Markdown → reportlab Paragraph (lightweight)
# --------------------------------------------------------------------------- #

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_HEAD = re.compile(r"^(#{1,4})\s+(.+)$")
_BULL = re.compile(r"^\s*[-*]\s+(.+)$")


def _md_inline(text: str) -> str:
    """Apply minimal inline markdown → reportlab markup.

    Order matters: code spans must be carved out *before* bold/italic, otherwise
    asterisks inside code (e.g. `foo.*`) get matched as italic markers and
    produce malformed nesting like ``<font ...>foo.<i></font></i>``.
    """
    # Escape angle brackets first to keep reportlab happy
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 1) Pull out code spans, replace with placeholders so their contents
    #    are immune to bold/italic substitutions.
    placeholders: list[str] = []

    def _stash_code(m: re.Match) -> str:
        # Strip any markdown-meta chars from inside the code span — they are
        # rendered literally as monospace, never as bold/italic markers.
        inner = m.group(1)
        token = f"\x00CODE{len(placeholders)}\x00"
        placeholders.append(f'<font face="Courier" size="9">{inner}</font>')
        return token

    text = _CODE.sub(_stash_code, text)

    # 2) Apply bold and italic on the remaining text.
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITAL.sub(r"<i>\1</i>", text)

    # 2b) Strip any stray `*` chars left over from patterns like `**foo***`
    # (five asterisks) — non-greedy `_BOLD` leaves the extra `*` behind, which
    # then pairs with another stray `*` later in the doc and makes `_ITAL`
    # produce <i> spans that cross <b> boundaries → invalid ReportLab XML.
    text = text.replace("*", "")

    # 3) Restore code spans.
    for idx, html in enumerate(placeholders):
        text = text.replace(f"\x00CODE{idx}\x00", html)
    return text


def _safe_paragraph(text: str, style: Any) -> Any:
    """Build a Paragraph that won't crash on malformed inline markup.

    Some LLM markdown produces patterns like `**foo***` (five asterisks) that
    `_md_inline` turns into unbalanced `<b>` / `<i>` XML. ReportLab's parser
    then raises `Parse error: saw </i> instead of expected </b>` and the
    whole PDF render aborts. Fall back to a fully-escaped plain string so the
    report still renders, just without inline formatting on that line.
    """
    try:
        return Paragraph(text, style)
    except Exception:
        plain = (
            text.replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )
        # Re-escape any stray angle brackets that came from user content
        # (the original text was already escaped by _md_inline, so this only
        # protects against odd cases).
        try:
            return Paragraph(plain, style)
        except Exception:
            return Paragraph(
                plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
                style,
            )


def _md_to_flowables(md: str) -> list[Any]:
    """Convert simple markdown (headings, bullets, paragraphs, bold/italic/code) to flowables."""
    if not md:
        return []
    out: list[Any] = []
    buf: list[str] = []

    def flush_buf():
        if buf:
            text = " ".join(buf).strip()
            if text:
                out.append(_safe_paragraph(_md_inline(text), STYLES["Body"]))
            buf.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_buf()
            out.append(Spacer(1, 4))
            continue
        h = _HEAD.match(line)
        if h:
            flush_buf()
            level = len(h.group(1))
            style = STYLES["H1"] if level <= 2 else STYLES["H2"]
            out.append(_safe_paragraph(_md_inline(h.group(2)), style))
            continue
        b = _BULL.match(line)
        if b:
            flush_buf()
            out.append(
                _safe_paragraph(
                    "•&nbsp;&nbsp;" + _md_inline(b.group(1)), STYLES["Body"]
                )
            )
            continue
        buf.append(line)
    flush_buf()
    return out


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

def _section_cover(
    project_id: str,
    metadata: dict[str, Any] | None,
    comparison: dict[str, Any],
    risks: dict[str, Any] | None,
    merges: dict[str, Any],
) -> list[Any]:
    flow: list[Any] = []
    now = datetime.now(timezone.utc).strftime("%B %d, %Y · %H:%M UTC")

    flow.append(Spacer(1, 0.3 * inch))
    flow.append(Paragraph(f"Upgrade Report", STYLES["TitleHero"]))
    flow.append(Paragraph(
        f'<font color="{ACCENT.hexval()}"><b>{project_id}</b></font>'
        f'  &nbsp;·&nbsp;  {now}',
        STYLES["SubtitleHero"],
    ))

    # Metadata table (Source / Target / Branch / Commit)
    if metadata:
        meta_rows = []
        for k, v in metadata.items():
            meta_rows.append([
                Paragraph(f"<b>{k}</b>", STYLES["Small"]),
                Paragraph(str(v), STYLES["Small"]),
            ])
        meta_t = Table(meta_rows, colWidths=[1.5 * inch, CONTENT_W - 1.5 * inch])
        meta_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), SOFT),
            ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        flow.append(meta_t)
        flow.append(Spacer(1, 0.3 * inch))

    # Headline metric tiles
    total = len(comparison)
    by_dec = _count_decisions(comparison)
    high_risk = _count_risks(risks).get("HIGH", 0) if risks else 0
    merged_n = len(merges)

    flow.append(Paragraph("At a glance", STYLES["H2"]))
    flow.append(Spacer(1, 4))
    flow.append(_metric_row([
        ("Total Artifacts", str(total), PRIMARY),
        ("To Merge", str(by_dec.get("Merge", 0)), COLOR_MERGE),
        ("To Retain", str(by_dec.get("Retain", 0)), COLOR_RETAIN),
        ("To Remove", str(by_dec.get("Remove", 0)), COLOR_REMOVE),
    ]))
    flow.append(Spacer(1, 0.15 * inch))
    flow.append(_metric_row([
        ("Merged So Far", str(merged_n), ACCENT),
        ("High Risk", str(high_risk), COLOR_HIGH),
        ("Quality Issues", str(_count_quality_issues(merges)), COLOR_WARN),
        ("JIRA Linked", str(_count_jira_linked(merges)), PRIMARY),
    ]))

    flow.append(Spacer(1, 0.3 * inch))

    # Plain-language summary
    flow.append(Paragraph("What this report covers", STYLES["H2"]))
    flow.append(Paragraph(
        "This report describes the outcome of upgrading the customer's GTM "
        "customizations onto the new target version. It lists every customized artifact "
        "we found, the action taken for each (merge into the new version, keep as-is, "
        "or remove because it's now redundant), and the risk and quality status of every "
        "merge. The intent is that any reader — engineer or stakeholder — can see the "
        "shape of the upgrade and identify items that need attention.",
        STYLES["Body"],
    ))

    flow.append(PageBreak())
    return flow


def _section_decisions(comparison: dict[str, Any]) -> list[Any]:
    flow: list[Any] = []
    by_dec = _count_decisions(comparison)
    total = len(comparison) or 1

    flow.append(Paragraph("Decision Summary", STYLES["H1"]))
    flow.append(Paragraph(
        "Each artifact gets one of four decisions, computed deterministically by comparing "
        "the customer's version against the new SYSTEM release.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    rows = []
    for label, color, bg in (
        ("Merge", COLOR_MERGE, BG_MERGE),
        ("Retain", COLOR_RETAIN, BG_RETAIN),
        ("Remove", COLOR_REMOVE, BG_REMOVE),
        ("ERROR", COLOR_FAIL, BG_HIGH),
    ):
        count = by_dec.get(label, 0)
        if count == 0:
            continue
        pct = count / total * 100
        bar_w = max(0.05, pct / 100) * 2.5 * inch
        bar = Table([[""]], colWidths=[bar_w], rowHeights=[0.18 * inch])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), color),
            ("BOX", (0, 0), (-1, -1), 0, color),
        ]))
        rows.append([
            _decision_badge(label),
            Paragraph(f"<b>{count}</b>", STYLES["Cell"]),
            Paragraph(f"{pct:.1f}%", STYLES["Cell"]),
            bar,
        ])

    # Surface unexpected decisions too
    for dec, count in sorted(by_dec.items()):
        if dec in ("Merge", "Retain", "Remove", "ERROR"):
            continue
        pct = count / total * 100
        bar_w = max(0.05, pct / 100) * 2.5 * inch
        bar = Table([[""]], colWidths=[bar_w], rowHeights=[0.18 * inch])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), MUTED)]))
        rows.append([
            Paragraph(dec, STYLES["Cell"]),
            Paragraph(f"<b>{count}</b>", STYLES["Cell"]),
            Paragraph(f"{pct:.1f}%", STYLES["Cell"]),
            bar,
        ])

    if not rows:
        flow.append(Paragraph("<i>No decisions recorded.</i>", STYLES["Body"]))
        return flow

    table = _styled_table(
        ["Decision", "Count", "% of Total", "Distribution"],
        rows,
        [1.2 * inch, 0.8 * inch, 0.9 * inch, CONTENT_W - 2.9 * inch],
        align=["left", "center", "center", "left"],
    )
    flow.append(table)
    return flow


def _section_risk(comparison: dict[str, Any], risks: dict[str, Any] | None) -> list[Any]:
    if not risks:
        return []
    flow: list[Any] = []
    by_lvl = _count_risks(risks)
    total = sum(by_lvl.values()) or 1

    flow.append(Paragraph("Risk Distribution", STYLES["H1"]))
    flow.append(Paragraph(
        "Risk is scored deterministically by file count, change volume, category sensitivity, "
        "and presence of code files. HIGH-risk artifacts are flagged for manual review.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    rows = []
    for level, count in [("HIGH", by_lvl.get("HIGH", 0)),
                         ("MEDIUM", by_lvl.get("MEDIUM", 0)),
                         ("LOW", by_lvl.get("LOW", 0))]:
        pct = count / total * 100
        bar_w = max(0.05, pct / 100) * 2.5 * inch
        color = {"HIGH": COLOR_HIGH, "MEDIUM": COLOR_MED, "LOW": COLOR_LOW}[level]
        bar = Table([[""]], colWidths=[bar_w], rowHeights=[0.18 * inch])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), color)]))
        rows.append([
            _risk_badge(level),
            Paragraph(f"<b>{count}</b>", STYLES["Cell"]),
            Paragraph(f"{pct:.1f}%", STYLES["Cell"]),
            bar,
        ])

    flow.append(_styled_table(
        ["Risk Level", "Count", "% of Total", "Distribution"],
        rows,
        [1.2 * inch, 0.8 * inch, 0.9 * inch, CONTENT_W - 2.9 * inch],
        align=["left", "center", "center", "left"],
    ))
    return flow


def _section_quality(merges: dict[str, Any]) -> list[Any]:
    if not merges:
        return []
    verdicts: dict[str, int] = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for m in merges.values():
        qr = (m or {}).get("quality_result") or {}
        v = qr.get("verdict")
        if v in verdicts:
            verdicts[v] += 1
    if sum(verdicts.values()) == 0:
        return []

    flow: list[Any] = []
    flow.append(Paragraph("Quality Gate Results", STYLES["H1"]))
    flow.append(Paragraph(
        "Deterministic checks run after every merge: conflict markers, JSON/XML validity, "
        "duplicate methods, import-usage mismatches.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    rows = [
        [_verdict_badge("PASS"), Paragraph(f"<b>{verdicts['PASS']}</b>", STYLES["Cell"])],
        [_verdict_badge("WARN"), Paragraph(f"<b>{verdicts['WARN']}</b>", STYLES["Cell"])],
        [_verdict_badge("FAIL"), Paragraph(f"<b>{verdicts['FAIL']}</b>", STYLES["Cell"])],
    ]
    flow.append(_styled_table(
        ["Verdict", "Count"],
        rows,
        [1.5 * inch, 1.0 * inch],
        align=["left", "center"],
    ))

    # Issues per artifact
    problems = [
        (k, m) for k, m in merges.items()
        if (m or {}).get("quality_result", {}).get("verdict") in ("FAIL", "WARN")
    ]
    if problems:
        flow.append(Spacer(1, 8))
        flow.append(Paragraph("Issues found", STYLES["H2"]))
        for key, merge in problems:
            qr = merge.get("quality_result") or {}
            verdict = qr.get("verdict", "")
            findings = qr.get("findings", []) or []
            head = Paragraph(
                f'<b>{_md_inline(key)}</b> &nbsp; '
                f'<font color="{(COLOR_FAIL if verdict=="FAIL" else COLOR_WARN).hexval()}">'
                f'<b>[{verdict}]</b></font>',
                STYLES["Body"],
            )
            items = []
            for f in findings:
                sev = f.get("severity", "INFO")
                msg = f.get("message", "")
                file = f.get("file", "")
                items.append(Paragraph(
                    f'•&nbsp;&nbsp;<b>[{sev}]</b> '
                    f'<font face="Courier" size="9">{_md_inline(file)}</font>: '
                    f'{_md_inline(msg)}',
                    STYLES["Small"],
                ))
            flow.append(KeepTogether([head, *items, Spacer(1, 4)]))
    return flow


def _section_high_risk(
    merges: dict[str, Any],
    risks: dict[str, Any] | None,
) -> list[Any]:
    if not (risks and merges):
        return []
    high = []
    for key, merge in merges.items():
        r = risks.get(key) or {}
        if r.get("level") == "HIGH":
            high.append((key, merge, r))
    if not high:
        return []

    flow: list[Any] = []
    flow.append(Paragraph("High-Risk Merges — Manual Review Recommended", STYLES["H1"]))
    flow.append(Paragraph(
        "These artifacts scored HIGH on the risk model. A human should validate the merge "
        "output before deploying.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    rows = []
    for key, merge, risk in high:
        score = float(risk.get("score", 0) or 0)
        factors = ", ".join(risk.get("factors") or [])
        qr = merge.get("quality_result") or {}
        verdict = qr.get("verdict", "—")
        rows.append([
            Paragraph(_md_inline(key), STYLES["Cell"]),
            Paragraph(f"{score:.2f}", STYLES["Cell"]),
            Paragraph(_md_inline(factors) or "—", STYLES["Cell"]),
            _verdict_badge(verdict),
        ])

    flow.append(_styled_table(
        ["Artifact", "Score", "Risk Factors", "Quality"],
        rows,
        [
            CONTENT_W - 0.8 * inch - 2.0 * inch - 0.9 * inch,
            0.8 * inch,
            2.0 * inch,
            0.9 * inch,
        ],
        align=["left", "center", "left", "center"],
    ))
    return flow


def _section_artifact_overview(
    comparison: dict[str, Any],
    risks: dict[str, Any] | None,
    jira_tickets: dict[str, str] | None,
) -> list[Any]:
    flow: list[Any] = []
    flow.append(Paragraph("Artifact Overview", STYLES["H1"]))
    flow.append(Paragraph(
        f"Full list of {len(comparison)} artifacts with decision, risk and JIRA linkage.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    rows = []
    for key in sorted(comparison.keys()):
        entry = comparison[key]
        # Full path (bucket/category/name) — gives reviewers the context they need.
        full_path = entry.get("source_rel") or key
        decision = entry.get("decision", "Unknown")
        risk_level = "—"
        if risks:
            r = risks.get(key) or {}
            risk_level = r.get("level", "—")
        jira_key = (jira_tickets or {}).get(key, "—") if jira_tickets else "—"

        rows.append([
            Paragraph(_md_inline(full_path), STYLES["Cell"]),
            _decision_badge(decision),
            _risk_badge(risk_level),
            Paragraph(_md_inline(str(jira_key)), STYLES["CellMono"]),
        ])

    # Calibrated column widths that sum to CONTENT_W
    artifact_w = CONTENT_W - (0.85 + 0.75 + 1.1) * inch
    flow.append(_styled_table(
        ["Artifact", "Decision", "Risk", "JIRA"],
        rows,
        [artifact_w, 0.85 * inch, 0.75 * inch, 1.1 * inch],
        align=["left", "center", "center", "left"],
    ))
    return flow


def _section_merge_details(
    merges: dict[str, Any],
    jira_tickets: dict[str, str] | None,
) -> list[Any]:
    if not merges:
        return []
    flow: list[Any] = []
    flow.append(Paragraph("Merge Details", STYLES["H1"]))
    flow.append(Paragraph(
        f"Per-artifact merge record. {len(merges)} artifact(s) merged.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))

    for key, merge in merges.items():
        bucket = merge.get("bucket", "—") or "—"
        merged_at = merge.get("merged_at", "—") or "—"
        files = merge.get("files") or []
        explanation = merge.get("explanation") or "(no explanation)"
        diff_ok = merge.get("diff_generated", False)
        diff_err = merge.get("diff_error") or ""
        jira_key = (jira_tickets or {}).get(key, "—") if jira_tickets else "—"

        diff_state = "Generated" if diff_ok else "Not generated"
        if diff_err:
            diff_state += f" (error: {diff_err})"

        # Header row + meta key/value table + explanation paragraph,
        # all kept together so a card doesn't split awkwardly across pages.
        head = Table(
            [[Paragraph(
                f'<font color="white"><b>{_md_inline(key)}</b></font>',
                STYLES["Body"],
            )]],
            colWidths=[CONTENT_W],
        )
        head.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))

        meta_rows = [
            [Paragraph("<b>Bucket</b>", STYLES["Small"]),
             Paragraph(_md_inline(bucket), STYLES["Small"])],
            [Paragraph("<b>JIRA</b>", STYLES["Small"]),
             Paragraph(_md_inline(str(jira_key)), STYLES["CellMono"])],
            [Paragraph("<b>Merged at</b>", STYLES["Small"]),
             Paragraph(_md_inline(merged_at), STYLES["Small"])],
            [Paragraph("<b>Files</b>", STYLES["Small"]),
             Paragraph(_md_inline(", ".join(files) or "—"), STYLES["Small"])],
            [Paragraph("<b>_diff.json</b>", STYLES["Small"]),
             Paragraph(_md_inline(diff_state), STYLES["Small"])],
        ]
        meta_t = Table(meta_rows, colWidths=[1.1 * inch, CONTENT_W - 1.1 * inch])
        meta_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), SOFT),
            ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))

        # Explanation: render as light markdown
        expl_label = Paragraph("<b>Explanation</b>", STYLES["Small"])
        expl_body = _md_to_flowables(explanation)

        block = [head, meta_t, Spacer(1, 4), expl_label, *expl_body, Spacer(1, 10)]
        flow.append(KeepTogether(block) if len(block) < 12 else None or block[0])
        # If KeepTogether would be huge, append elements individually so they can split
        if len(block) >= 12:
            for item in block[1:]:
                flow.append(item)
    return flow


def _section_recommendations(narrative: str | None) -> list[Any]:
    if not narrative or not narrative.strip():
        return []
    flow: list[Any] = []
    flow.append(Paragraph("AI Recommendations", STYLES["H1"]))
    flow.append(Paragraph(
        "Narrative produced by the SummaryAgent based on the run results.",
        STYLES["SmallMuted"],
    ))
    flow.append(Spacer(1, 6))
    flow.extend(_md_to_flowables(narrative))
    return flow


def _section_jira(jira_state: dict[str, Any] | None) -> list[Any]:
    if not jira_state:
        return []
    epic_key = jira_state.get("epic_key", "")
    subtasks = jira_state.get("subtasks") or {}
    if not epic_key:
        return []

    flow: list[Any] = []
    flow.append(Paragraph("JIRA Tracking", STYLES["H1"]))
    flow.append(Paragraph(
        f"<b>Epic</b>: <font face='Courier'>{_md_inline(epic_key)}</font> &nbsp;·&nbsp; "
        f"<b>Subtasks</b>: {len(subtasks)}",
        STYLES["Body"],
    ))
    if subtasks:
        rows = []
        for artifact, issue_key in subtasks.items():
            rows.append([
                Paragraph(_md_inline(artifact), STYLES["Cell"]),
                Paragraph(_md_inline(str(issue_key)), STYLES["CellMono"]),
            ])
        flow.append(_styled_table(
            ["Artifact", "JIRA Issue"],
            rows,
            [CONTENT_W - 1.4 * inch, 1.4 * inch],
            align=["left", "left"],
        ))
    return flow


# --------------------------------------------------------------------------- #
# Helpers — counters
# --------------------------------------------------------------------------- #

def _count_decisions(comparison: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in comparison.values():
        d = (entry or {}).get("decision", "Unknown")
        out[d] = out.get(d, 0) + 1
    return out


def _count_risks(risks: dict[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    if not risks:
        return out
    for r in risks.values():
        level = (r or {}).get("level", "LOW")
        out[level] = out.get(level, 0) + 1
    return out


def _count_quality_issues(merges: dict[str, Any]) -> int:
    n = 0
    for m in merges.values():
        v = ((m or {}).get("quality_result") or {}).get("verdict")
        if v in ("WARN", "FAIL"):
            n += 1
    return n


def _count_jira_linked(merges: dict[str, Any]) -> int:
    n = 0
    for m in merges.values():
        # heuristic: merge record may carry a jira_key, otherwise fall back
        if (m or {}).get("jira_key"):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def render_pdf(
    project_id: str,
    comparison: dict[str, Any],
    merges: dict[str, Any],
    risks: dict[str, Any] | None = None,
    summary_narrative: str | None = None,
    jira_state: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    jira_tickets: dict[str, str] | None = None,
) -> bytes:
    """
    Render the upgrade report as a polished PDF and return the bytes.
    """
    buffer = BytesIO()

    frame = Frame(
        MARGIN_L, MARGIN_B, CONTENT_W, PAGE_H - MARGIN_T - MARGIN_B,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="content",
    )

    def _on_page(canvas, doc):
        _draw_page_chrome(canvas, doc, project_id)

    template = PageTemplate(id="default", frames=[frame], onPage=_on_page)
    doc = BaseDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T, bottomMargin=MARGIN_B,
        title=f"Upgrade Report — {project_id}",
        author="Wisetrix",
        pageTemplates=[template],
    )

    story: list[Any] = []
    story += _section_cover(project_id, metadata, comparison, risks or {}, merges or {})
    story += _section_decisions(comparison or {})
    story.append(Spacer(1, 0.15 * inch))
    story += _section_risk(comparison or {}, risks)
    story.append(Spacer(1, 0.15 * inch))
    story += _section_quality(merges or {})
    story.append(Spacer(1, 0.15 * inch))
    story += _section_high_risk(merges or {}, risks)
    story.append(PageBreak())
    story += _section_artifact_overview(comparison or {}, risks, jira_tickets)
    story.append(PageBreak())
    story += _section_merge_details(merges or {}, jira_tickets)
    story.append(Spacer(1, 0.15 * inch))
    story += _section_recommendations(summary_narrative)
    story.append(Spacer(1, 0.15 * inch))
    story += _section_jira(jira_state)

    doc.build(story)
    return buffer.getvalue()
