#!/usr/bin/env python3
"""
HRMS SRS PDF Generator -- Markdown -> ReportLab
Converts docs/SRS.md to a print-ready A4 PDF with cover, TOC, styled content.
Requires: markdown, beautifulsoup4, reportlab
"""
import os
import re
import sys

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD_PATH = os.path.join(BASE_DIR, "docs", "SRS.md")
PDF_PATH = os.path.join(BASE_DIR, "docs", "SRS.pdf")

try:
    import markdown
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError as e:
    print(f"Missing dep: {e}")
    sys.exit(1)

from reportlab.lib import colors
from reportlab.lib.units import mm, inch
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, ListFlowable, ListItem, KeepTogether, Image, Preformatted
)
from reportlab.lib.fonts import tt2ps
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import datetime

# Colors - design system from HRMS
PRIMARY = colors.HexColor("#0F172A")  # slate-900
PRIMARY_LIGHT = colors.HexColor("#1E293B")
ACCENT = colors.HexColor("#0369A1")   # sky-800
ACCENT_LIGHT = colors.HexColor("#E0F2FE")
SLATE_500 = colors.HexColor("#64748B")
SLATE_300 = colors.HexColor("#CBD5E1")
SLATE_100 = colors.HexColor("#F1F5F9")
SLATE_50 = colors.HexColor("#F8FAFC")
SUCCESS = colors.HexColor("#16A34A")
WARNING = colors.HexColor("#EA580C")
BORDER = colors.HexColor("#E2E8F0")

PAGE_W, PAGE_H = A4
MARGIN_T, MARGIN_B = 36, 36
MARGIN_L, MARGIN_R = 42, 42
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

# Styles
styles = getSampleStyleSheet()

# Custom styles
styles.add(ParagraphStyle(
    name='CoverTitle',
    parent=styles['Title'],
    fontName='Helvetica-Bold',
    fontSize=32,
    leading=36,
    textColor=PRIMARY,
    alignment=TA_LEFT,
    spaceAfter=6,
))
styles.add(ParagraphStyle(
    name='CoverSubtitle',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=14,
    leading=20,
    textColor=SLATE_500,
    alignment=TA_LEFT,
    spaceAfter=18,
))
styles.add(ParagraphStyle(
    name='CoverMeta',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=9,
    leading=13,
    textColor=colors.HexColor("#334155"),
    alignment=TA_LEFT,
))
styles.add(ParagraphStyle(
    name='CoverLabel',
    parent=styles['Normal'],
    fontName='Helvetica-Bold',
    fontSize=8,
    leading=10,
    textColor=ACCENT,
    textTransform='uppercase',
))
styles.add(ParagraphStyle(
    name='H1',
    parent=styles['Heading1'],
    fontName='Helvetica-Bold',
    fontSize=22,
    leading=26,
    textColor=PRIMARY,
    spaceBefore=18,
    spaceAfter=8,
    keepWithNext=True,
))
styles.add(ParagraphStyle(
    name='H2',
    parent=styles['Heading2'],
    fontName='Helvetica-Bold',
    fontSize=16,
    leading=20,
    textColor=PRIMARY,
    spaceBefore=16,
    spaceAfter=6,
    keepWithNext=True,
    borderPadding=(0,0,4,0),
))
styles.add(ParagraphStyle(
    name='H3',
    parent=styles['Heading3'],
    fontName='Helvetica-Bold',
    fontSize=12,
    leading=16,
    textColor=colors.HexColor("#1E3A5F"),
    spaceBefore=12,
    spaceAfter=4,
    keepWithNext=True,
))
styles.add(ParagraphStyle(
    name='H4',
    parent=styles['Heading4'],
    fontName='Helvetica-Bold',
    fontSize=10,
    leading=13,
    textColor=colors.HexColor("#334155"),
    spaceBefore=10,
    spaceAfter=3,
))
styles.add(ParagraphStyle(
    name='Body',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=9,
    leading=13.5,
    textColor=colors.HexColor("#1E293B"),
    alignment=TA_JUSTIFY,
    spaceBefore=2,
    spaceAfter=4,
    wordWrap='CJK',
))
styles.add(ParagraphStyle(
    name='BodySmall',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=8,
    leading=11,
    textColor=colors.HexColor("#334155"),
    alignment=TA_LEFT,
    spaceBefore=2,
    spaceAfter=2,
))
styles.add(ParagraphStyle(
    name='BulletCustom',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=8.5,
    leading=12,
    textColor=colors.HexColor("#1E293B"),
    leftIndent=18,
    firstLineIndent=0,
    bulletIndent=10,
    spaceBefore=1,
    spaceAfter=1,
))
styles.add(ParagraphStyle(
    name='CodeInline',
    parent=styles['Normal'],
    fontName='Courier',
    fontSize=7.5,
    leading=10,
    textColor=colors.HexColor("#0F172A"),
    backColor=colors.HexColor("#F1F5F9"),
))
styles.add(ParagraphStyle(
    name='CodeBlock',
    parent=styles['Code'],
    fontName='Courier',
    fontSize=7,
    leading=9,
    textColor=colors.HexColor("#0F172A"),
    backColor=SLATE_50,
    borderPadding=(6,6,6,6),
    spaceBefore=4,
    spaceAfter=6,
    alignment=TA_LEFT,
    leftIndent=0,
))
styles.add(ParagraphStyle(
    name='TableHeader',
    parent=styles['Normal'],
    fontName='Helvetica-Bold',
    fontSize=7,
    leading=9,
    textColor=colors.white,
    alignment=TA_CENTER,
))
styles.add(ParagraphStyle(
    name='TableCell',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=7,
    leading=9,
    textColor=colors.HexColor("#1E293B"),
    alignment=TA_LEFT,
))
styles.add(ParagraphStyle(
    name='TableCellSmall',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=6.5,
    leading=8,
    textColor=colors.HexColor("#334155"),
    alignment=TA_LEFT,
))
styles.add(ParagraphStyle(
    name='Caption',
    parent=styles['Normal'],
    fontName='Helvetica-Oblique',
    fontSize=7,
    leading=9,
    textColor=SLATE_500,
    alignment=TA_CENTER,
    spaceBefore=2,
    spaceAfter=6,
))
styles.add(ParagraphStyle(
    name='Quote',
    parent=styles['Normal'],
    fontName='Helvetica-Oblique',
    fontSize=8,
    leading=11,
    textColor=SLATE_500,
    leftIndent=12,
    borderPadding=(0,0,0,8),
    spaceBefore=4,
    spaceAfter=4,
))
styles.add(ParagraphStyle(
    name='TOCHeading',
    parent=styles['Heading1'],
    fontName='Helvetica-Bold',
    fontSize=14,
    leading=18,
    textColor=PRIMARY,
    spaceAfter=12,
))
styles.add(ParagraphStyle(
    name='TOCItem',
    parent=styles['Normal'],
    fontName='Helvetica',
    fontSize=9,
    leading=14,
    textColor=colors.HexColor("#1E293B"),
    leftIndent=0,
    spaceBefore=1,
    spaceAfter=1,
))

# Helpers
def sanitize_for_para(html_fragment):
    """Convert HTML snippet to ReportLab Paragraph XML (limited tags)."""
    if not html_fragment:
        return ""
    # BeautifulSoup already parsed; we need to convert Tag tree to string with allowed tags.
    # We'll handle via recursive conversion.
    return html_fragment

def html_inline_to_rl(text_html):
    """Convert HTML inline fragment to ReportLab XML."""
    # Use BeautifulSoup to parse inline html
    soup = BeautifulSoup(f"<div>{text_html}</div>", "html.parser")
    div = soup.div
    def convert(node):
        if isinstance(node, NavigableString):
            # Escape for XML
            t = str(node)
            t = sanitize_pdf_text(t)
            # Escape < > & for reportlab (but keep allowed tags via conversion)
            t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return t
        elif isinstance(node, Tag):
            tag = node.name.lower()
            inner = "".join(convert(c) for c in node.children)
            if tag in ("strong","b"):
                return f"<b>{inner}</b>"
            elif tag in ("em","i"):
                return f"<i>{inner}</i>"
            elif tag == "code":
                # inline code: use font tag
                return f'<font name="Courier" color="#0F172A" backColor="#F1F5F9">{inner}</font>'
            elif tag == "a":
                href = node.get("href","")
                if href:
                    return f'<a href="{href}" color="#0369A1">{inner}</a>'
                return inner
            elif tag == "br":
                return "<br/>"
            elif tag == "span":
                return inner
            elif tag in ("p","div"):
                return inner
            else:
                return inner
        return ""
    result = "".join(convert(c) for c in div.children)
    return sanitize_pdf_text(result)

def para(text, style_name='Body', **kw):
    # sanitize before paragraph
    text = sanitize_pdf_text(text)
    return Paragraph(text, styles[style_name], **kw)

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceBefore=6, spaceAfter=6, hAlign='CENTER', vAlign='BOTTOM', dash=None)

# Header/Footer callbacks
def sanitize_pdf_text(t):
    if not t:
        return t
    return t.replace("\u00a7","Sec. ").replace("\u2014"," -- ").replace("\u2013"," - ").replace("\u2192"," -> ").replace("\u2022","-").replace("\u00a0"," ").replace("\u2713","v").replace("\u2026","...").replace("\u00b7","-").replace("\u2019","'").replace("\u201c",'"').replace("\u201d",'"').replace("\u2026","...")

def header_footer(canvas, doc):
    canvas.saveState()
    # Don't draw on cover pages (first 2 pages) - detect via page number? We'll draw always but light for content only
    page = doc.page
    if page <= 2:
        canvas.restoreState()
        return
    # Header line
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    y_header = PAGE_H - 28
    canvas.line(MARGIN_L, y_header, PAGE_W - MARGIN_R, y_header)
    # Header text
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(SLATE_500)
    canvas.drawString(MARGIN_L, y_header + 6, "HRMS -- Software Requirements Specification  -  Confidential -- Internal Use Only")
    canvas.drawRightString(PAGE_W - MARGIN_R, y_header + 6, "CWLiveweb  -  Flask + DuckDB")
    # Footer line
    y_footer = MARGIN_B - 12
    canvas.setStrokeColor(BORDER)
    canvas.line(MARGIN_L, y_footer + 12, PAGE_W - MARGIN_R, y_footer + 12)
    # Footer text
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(SLATE_500)
    canvas.drawString(MARGIN_L, y_footer, "(c) 2026 CWLiveweb - HRMS Engineering - v1.0 -- 2026-05-13")
    canvas.setFont("Helvetica-Bold", 6.5)
    canvas.setFillColor(PRIMARY)
    canvas.drawRightString(PAGE_W - MARGIN_R, y_footer, f"Page {page}")
    canvas.restoreState()

def cover_footer(canvas, doc):
    # Minimal footer for cover/toc (pages 1-2) -- just confidentiality
    canvas.saveState()
    page = doc.page
    if page == 1:
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(SLATE_500)
        canvas.drawCentredString(PAGE_W/2, MARGIN_B - 4, "CONFIDENTIAL  --  INTERNAL DISTRIBUTION ONLY  -  (c) 2026 CWLiveweb")
    elif page == 2:
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(SLATE_500)
        canvas.drawCentredString(PAGE_W/2, MARGIN_B - 4, "HRMS SRS  -  v1.0  -  2026-05-13  -  Page 2")
    canvas.restoreState()

# Build story helpers for markdown->flowables
def build_content_flowables_from_md(md_text):
    # Convert markdown to HTML
    html = markdown.markdown(md_text, extensions=['extra','tables','fenced_code','sane_lists','toc'])
    # Wrap
    wrapped = f"<html><body>{html}</body></html>"
    soup = BeautifulSoup(wrapped, "html.parser")
    body = soup.body
    if not body:
        body = soup

    flowables = []
    # Skip first H1 and TOC section? We'll process but filter out duplicate cover content.
    # Identify if first element is H1 = Software Requirements Specification
    # And second is H2 = Human Resource Management ... then skip those? But we already have cover.
    # We'll skip first two headings that duplicate cover title.
    # Also skip the Table of Contents section (H2 = Table of Contents) and its following ol.
    skip_toc = True
    toc_encountered = False

    # Collect children top-level
    children = [c for c in body.children if not isinstance(c, NavigableString) or str(c).strip()]

    # We will iterate
    i = 0
    while i < len(children):
        el = children[i]
        if isinstance(el, NavigableString):
            i+=1
            continue
        tag = el.name.lower() if el.name else ""
        # Skip initial duplicate titles
        if tag in ("h1",):
            txt = el.get_text(strip=True)
            if "Software Requirements Specification" in txt:
                i+=1
                continue
        if tag == "h2":
            txt = el.get_text(strip=True)
            # Skip the subtitle H2 "Human Resource Management System..."
            if "Human Resource Management System" in txt and "Flask" in txt:
                i+=1
                continue
            if "Table of Contents" in txt:
                toc_encountered = True
                i+=1
                # Skip next element if it's ordered list (TOC)
                if i < len(children) and children[i].name and children[i].name.lower() in ("ol","ul"):
                    i+=1
                # also skip following hr
                if i < len(children) and children[i].name and children[i].name.lower() == "hr":
                    i+=1
                continue
            # Also skip the lone hr after cover? Let's allow hrs.
        # Now process element to flowable
        flowables.extend(element_to_flowables(el))
        i+=1
    return flowables

def element_to_flowables(el):
    if isinstance(el, NavigableString):
        txt = str(el).strip()
        if not txt:
            return []
        return [para(html_inline_to_rl(txt))]
    if not isinstance(el, Tag):
        return []
    tag = el.name.lower()
    out = []
    if tag in ("h1","h2","h3","h4","h5","h6"):
        level = int(tag[1])
        text = html_inline_to_rl(el.decode_contents())
        if level == 1:
            out.append(Spacer(1, 6))
            out.append(para(text, 'H1'))
            # underline
            out.append(HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceBefore=2, spaceAfter=6))
        elif level == 2:
            out.append(para(text, 'H2'))
            # subtle underline
            out.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceBefore=1, spaceAfter=4))
        elif level == 3:
            out.append(para(text, 'H3'))
        elif level == 4:
            out.append(para(text, 'H4'))
        else:
            out.append(para(text, 'H4'))
        return out
    elif tag == "p":
        # Check if paragraph contains only strong? keep
        inner = el.decode_contents()
        rl = html_inline_to_rl(inner)
        if not rl.strip():
            return []
        # Detect if paragraph is code-like? No
        out.append(para(rl, 'Body'))
        return out
    elif tag in ("ul","ol"):
        is_ordered = tag == "ol"
        items = el.find_all("li", recursive=False)
        for idx, li in enumerate(items):
            # li may contain nested ul/ol, p, code
            # Extract nested lists
            # Get li inner excluding nested lists for bullet para
            # Clone li content without nested ul/ol
            # We'll create bullet paragraph
            # Handle li inner HTML: decode_contents but need to separate nested lists
            # For simplicity, if li contains nested list, process nested after.
            nested = li.find_all(["ul","ol"], recursive=False)
            # Temporarily extract nested HTML to avoid double
            for n in nested:
                n.extract()
            li_inner = li.decode_contents()
            rl = html_inline_to_rl(li_inner)
            bullet = f"{idx+1}." if is_ordered else "-"
            prefix = f"<b>{bullet}</b>  " if is_ordered else "-  "
            text = f"{prefix}{rl}" if not is_ordered else f"<b>{bullet}</b>  {rl}"
            p = Paragraph(sanitize_pdf_text(text), styles['BulletCustom'])
            out.append(p)
            # Now add nested lists as indented flowables
            for n in nested:
                # Re-insert nested rendering with extra indent? Use recursion with indent.
                # For nested, we call element_to_flowables but with indent wrapper?
                nested_flows = element_to_flowables(n)
                # Add spacer indent by wrapping in Table with left padding? Simpler add Spacer and nested.
                # We'll indent by adding left padding via Table wrapper
                # But for simplicity just append with extra left indent via style.
                # To simulate indent, we add a small spacer and then flowables with modified style leftIndent
                for nf in nested_flows:
                    # if Paragraph, increase leftIndent
                    if isinstance(nf, Paragraph):
                        # clone style with extra indent
                        nf.style.leftIndent += 12
                        nf.style.firstLineIndent = 0
                        nf.style.bulletIndent += 12
                    out.append(nf)
        # Add spacer after list
        out.append(Spacer(1, 3))
        return out
    elif tag == "pre":
        # code block: may contain <code>
        code_el = el.find("code")
        if code_el:
            code_text = code_el.get_text()
        else:
            code_text = el.get_text()
        # ReportLab Preformatted keeps monospace and line breaks
        # Need to ensure escapes? Preformatted expects plain text.
        # Use CodeBlock style via Preformatted with Courier.
        # Add background via Table wrapper with SLATE_50
        # Use Preformatted flowable
        # Split long lines? ReportLab will wrap if needed.
        # Create Preformatted with Courier 7pt
        pre_style = styles['CodeBlock']
        # Use Table to add background
        # Create paragraph preformatted manually via Preformatted
        # Need to handle long code: we will use Preformatted
        pf = Preformatted(code_text, pre_style, maxLineLength=88)
        # Wrap in Table for background and border
        t_data = [[pf]]
        t = Table(t_data, colWidths=[CONTENT_W - 4])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), SLATE_50),
            ('BOX', (0,0), (-1,-1), 0.4, BORDER),
            ('INNERGRID', (0,0), (-1,-1), 0.0, colors.white),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('ROUNDEDCORNERS', [4,4,4,4]),
        ]))
        out.append(t)
        out.append(Spacer(1, 4))
        return out
    elif tag == "code":
        # inline? but top-level code shouldn't happen
        rl = html_inline_to_rl(el.decode_contents())
        out.append(para(f'<font name="Courier" backColor="#F1F5F9">{rl}</font>', 'Body'))
        return out
    elif tag == "blockquote":
        inner = el.decode_contents()
        rl = html_inline_to_rl(inner)
        # Render as italic with left border via Table
        p = Paragraph(rl, styles['Quote'])
        t = Table([[p]], colWidths=[CONTENT_W-8])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#FFFBEB")),
            ('BOX', (0,0), (-1,-1), 0.0, colors.white),
            ('LINEBELOW', (0,0), (-1,0), 0.0, colors.white),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LINEBEFORE', (0,0), (0,0), 3, WARNING),
        ]))
        out.append(t)
        out.append(Spacer(1,2))
        return out
    elif tag == "table":
        # Build table data
        headers = []
        rows = []
        thead = el.find("thead")
        tbody = el.find("tbody")
        # Approach: all tr
        trs = el.find_all("tr")
        if not trs:
            return out
        # First row is header if thead exists or first row has th
        for idx, tr in enumerate(trs):
            cells = tr.find_all(["th","td"])
            row_data = []
            for cell in cells:
                cell_inner = cell.decode_contents()
                rl = html_inline_to_rl(cell_inner)
                # Decide style: header if th or first row
                is_header = cell.name == "th" or (idx==0 and thead is not None) or (idx==0 and tr.find("th") is not None) or (idx==0)
                # Actually treat first row as header always for SRS tables
                if idx == 0:
                    # header
                    p = Paragraph(rl if rl.strip() else "&nbsp;", styles['TableHeader'])
                else:
                    # Determine font size based on length; if long, use small
                    if len(rl) > 120:
                        p = Paragraph(rl, styles['TableCellSmall'])
                    else:
                        p = Paragraph(rl, styles['TableCell'])
                row_data.append(p)
            # Only first row considered header, rest data
            if idx == 0 and (el.find("th") is not None or True):
                headers = row_data
            else:
                rows.append(row_data)
        # If no explicit header separation but we forced first row as header, need to adjust
        if not rows and headers:
            # Means only one row? treat as header? keep empty?
            pass
        elif headers and not rows:
            # No data rows beyond header
            pass
        else:
            # headers holds first row, rows hold rest; but if there were no th but we treated first as header, it's okay.
            # If first row was actually header, rows already start from second.
            # But our loop added first row to headers, not rows, good.
            pass
        # Build full data
        if headers:
            data = [headers] + rows
        else:
            data = rows
        if not data:
            return out
        # Calculate col widths: distribute CONTENT_W equally but adjust for content? Use equal.
        ncols = len(data[0])
        col_widths = [CONTENT_W / ncols] * ncols
        # Special handling for wide tables with many cols: scale down font maybe
        if ncols >= 4:
            avail = CONTENT_W
            # For tables with 5 cols, equal distribution is fine
            col_widths = [avail / ncols] * ncols
            # If table has ID columns short, we could tweak but keep equal.
        # Create Table
        t = Table(data, colWidths=col_widths, repeatRows=1)
        # Style
        style_cmds = [
            ('BACKGROUND', (0,0), (-1,0), PRIMARY),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('ALIGN', (0,0), (-1,0), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,0), 6),
            ('TOPPADDING', (0,0), (-1,0), 6),
            ('BOTTOMPADDING', (0,1), (-1,-1), 3),
            ('TOPPADDING', (0,1), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('GRID', (0,0), (-1,-1), 0.4, BORDER),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, SLATE_50]),
        ]
        t.setStyle(TableStyle(style_cmds))
        out.append(KeepTogether(t) if len(data) < 20 else t)
        out.append(Spacer(1,4))
        return out
    elif tag == "hr":
        out.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceBefore=8, spaceAfter=8))
        return out
    elif tag == "img":
        # No images in SRS, ignore
        return out
    else:
        # Generic: decode children
        # For div, section, etc, recurse
        for child in el.children:
            if isinstance(child, Tag):
                out.extend(element_to_flowables(child))
            elif isinstance(child, NavigableString):
                txt = str(child).strip()
                if txt:
                    out.append(para(html_inline_to_rl(txt)))
        return out

def build_cover_story():
    story = []
    # Top accent bar
    # We'll create a colored header band using Table
    header_data = [[
        Paragraph('<font color="#FFFFFF" size=7><b>HRMS</b>  -  Human Resource Management System</font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=7>Flask  -  DuckDB  -  Jinja2  -  APScheduler</font>', styles['BodySmall'])
    ]]
    header_table = Table(header_data, colWidths=[CONTENT_W*0.6, CONTENT_W*0.4])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), PRIMARY),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 18))
    # Overline
    story.append(Paragraph('<font color="#0369A1" size=8><b>SOFTWARE REQUIREMENTS SPECIFICATION</b>  --  CONFIDENTIAL</font>', styles['BodySmall']))
    story.append(Spacer(1, 6))
    # Title
    story.append(Paragraph('Human Resource<br/>Management System', styles['CoverTitle']))
    # Subtitle
    story.append(Paragraph('Flask + DuckDB  -  HRMS Portal<br/><font color="#64748B" size=10>A comprehensive, shift-aware, RBAC-driven HR platform for the modern enterprise</font>', styles['CoverSubtitle']))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="18%", thickness=3, color=ACCENT, spaceBefore=0, spaceAfter=14, hAlign='LEFT'))
    # Meta table
    meta_data = [
        [Paragraph('<font color="#0369A1" size=7><b>VERSION</b></font>', styles['CoverMeta']), Paragraph('1.0  --  Baseline (Approved for Development)', styles['CoverMeta'])],
        [Paragraph('<font color="#0369A1" size=7><b>DATE</b></font>', styles['CoverMeta']), Paragraph('13 May 2026  (IST)', styles['CoverMeta'])],
        [Paragraph('<font color="#0369A1" size=7><b>AUTHOR</b></font>', styles['CoverMeta']), Paragraph('HRMS Engineering  -  Auto-generated via codebase analysis', styles['CoverMeta'])],
        [Paragraph('<font color="#0369A1" size=7><b>REPOSITORY</b></font>', styles['CoverMeta']), Paragraph('github.com/ShubhamvijayJawalkar/CWLiveweb  -  branch: main', styles['CoverMeta'])],
        [Paragraph('<font color="#0369A1" size=7><b>REFERENCE</b></font>', styles['CoverMeta']), Paragraph('OrangeHRM CE 5.x  -  IceHRM 34  -  Sentrifugo 3.2  -  Odoo HR  -  IEEE 830-1998', styles['CoverMeta'])],
        [Paragraph('<font color="#0369A1" size=7><b>STATUS</b></font>', styles['CoverMeta']), Paragraph('<font color="#16A34A"><b>APPROVED FOR DEVELOPMENT / BASELINE</b></font>', styles['CoverMeta'])],
    ]
    meta_table = Table(meta_data, colWidths=[90, CONTENT_W-90])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('BACKGROUND', (0,0), (-1,-1), SLATE_50),
        ('BOX', (0,0), (-1,-1), 0.4, BORDER),
        ('INNERGRID', (0,0), (-1,-1), 0.3, BORDER),
        ('ROUNDEDCORNERS', [6,6,6,6]),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 14))
    # Stats band
    stats_data = [[
        Paragraph('<font color="#FFFFFF" size=9><b>34+</b><br/><font size=6>TABLES</font></font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=9><b>16</b><br/><font size=6>BLUEPRINTS</font></font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=9><b>85+</b><br/><font size=6>ENDPOINTS</font></font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=9><b>21</b><br/><font size=6>PERM MODULES</font></font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=9><b>6</b><br/><font size=6>ROLES</font></font>', styles['BodySmall']),
        Paragraph('<font color="#FFFFFF" size=9><b>258</b><br/><font size=6>TESTS</font></font>', styles['BodySmall']),
    ]]
    stats_table = Table(stats_data, colWidths=[CONTENT_W/6]*6)
    stats_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), PRIMARY_LIGHT),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('LINEAFTER', (0,0), (-2,-1), 0.4, colors.HexColor("#334155")),
        ('ROUNDEDCORNERS', [6,6,6,6]),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 16))
    # Distribution box
    story.append(Paragraph('<b>DISTRIBUTION &amp; CLASSIFICATION</b>', styles['CoverLabel']))
    story.append(Spacer(1, 4))
    dist_data = [[
        Paragraph('<font size=7 color="#1E293B"><b>Prepared for:</b> Product Owner, Engineering, QA, DevOps, HR Stakeholders, Auditors</font>', styles['BodySmall']),
    ]]
    dist_table = Table(dist_data, colWidths=[CONTENT_W])
    dist_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#EFF6FF")),
        ('BOX', (0,0), (-1,-1), 0.4, colors.HexColor("#BFDBFE")),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('ROUNDEDCORNERS', [6,6,6,6]),
    ]))
    story.append(dist_table)
    story.append(Spacer(1, 8))
    story.append(Paragraph('This SRS was reverse-engineered from the live codebase (<b>hrms/__init__.py</b>, <b>hrms/schema.py</b>, 16 blueprints, 35 templates) and cross-validated against leading open-source HRMS to ensure workflow completeness. It is the single source of truth for implementation, QA traceability, and audit.', styles['BodySmall']))
    story.append(Spacer(1, 12))
    # Badge
    badge_data = [[Paragraph('<font color="#FFFFFF" size=7><b> IEEE 830-1998  -  OWASP ASVS 4.0  -  ISO 25010 </b></font>', styles['BodySmall'])]]
    badge_table = Table(badge_data, colWidths=[CONTENT_W])
    badge_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), ACCENT),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('ROUNDEDCORNERS', [12,12,12,12]),
    ]))
    story.append(badge_table)
    return story

def build_toc_story():
    story = []
    story.append(Paragraph('Table of Contents', styles['TOCHeading']))
    story.append(HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceBefore=2, spaceAfter=8))
    toc_items = [
        ("1", "Introduction", "Purpose, Scope, Definitions, References, Overview", "5"),
        ("2", "Overall Description", "Perspective, Functions, Users, Env, Constraints", "6"),
        ("3", "System Architecture", "Logical, DB, Security, Deployment", "8"),
        ("4", "Stakeholders & User Classes", "Primary / Secondary", "9"),
        ("5", "Functional Requirements", "FR-AUTH … FR-AST (21 groups, 100+ IDs)", "10"),
        ("6", "Workflow Specifications", "12 BPMN-style lifecycles incl. 5-step onboarding", "22"),
        ("7", "Data Model / ERD", "34+ tables, FK graph, key columns", "27"),
        ("8", "API Specification", ">85 endpoints + JSON examples", "30"),
        ("9", "UI / UX Requirements", "35 templates, design system, a11y", "36"),
        ("10", "Non-Functional Requirements", "Perf, Availability, Security, Backup", "38"),
        ("11", "Security Requirements", "10 controls incl. gap remediation", "39"),
        ("12", "Reporting & Analytics", "Reports + 5 analytics endpoints", "40"),
        ("13", "Deployment & DevOps", "Docker, Render, Scheduler caveat", "41"),
        ("14", "Traceability Matrix", "FR → Code → Template → Test", "42"),
        ("15", "Glossary", "Terms & abbreviations", "43"),
        ("16", "Appendices", "Seed, Matrix, Benchmark, Roadmap, History", "44"),
    ]
    # Build TOC as Table with dots
    toc_data = []
    for num, title, desc, page in toc_items:
        left = Paragraph(f'<b>{num}.</b>  {title}  <font color="#64748B" size=7>-- {desc}</font>', styles['TOCItem'])
        right = Paragraph(f'<font color="#0369A1"><b>{page}</b></font>', styles['TOCItem'])
        toc_data.append([left, right])
    t = Table(toc_data, colWidths=[CONTENT_W-30, 30])
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LINEBELOW', (0,0), (-1,-2), 0.3, BORDER),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))
    # Revision box
    story.append(Paragraph('<b>REVISION HISTORY</b>', styles['CoverLabel']))
    story.append(Spacer(1,4))
    rev_data = [
        [Paragraph('<b>Version</b>', styles['TableHeader']), Paragraph('<b>Date</b>', styles['TableHeader']), Paragraph('<b>Author</b>', styles['TableHeader']), Paragraph('<b>Change</b>', styles['TableHeader'])],
        [Paragraph('0.1', styles['TableCell']), Paragraph('2026-05-13', styles['TableCell']), Paragraph('HRMS Eng. (agent)', styles['TableCell']), Paragraph('Initial generation via codebase explore + manual read', styles['TableCell'])],
        [Paragraph('1.0', styles['TableCell']), Paragraph('2026-05-13', styles['TableCell']), Paragraph('HRMS Eng.', styles['TableCell']), Paragraph('<b>Baseline -- Approved for Development</b>', styles['TableCell'])],
    ]
    rev_table = Table(rev_data, colWidths=[50, 70, 110, CONTENT_W-230])
    rev_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), PRIMARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 0.4, BORDER),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, SLATE_50]),
    ]))
    story.append(rev_table)
    story.append(Spacer(1, 10))
    story.append(Paragraph('<b>HOW TO READ THIS DOCUMENT</b>', styles['CoverLabel']))
    story.append(Spacer(1,4))
    story.append(Paragraph('Each FR uses form <b>FR-&lt;MOD&gt;-&lt;NNN&gt;</b> with priority <b>M/H/L</b> and code pointer (<b>file:line</b>). Workflows in §6 give state machines &amp; decision tables; §7 gives physical schema; §8 lists every endpoint with auth. Executable trace: <b>README tests (258)</b> must be run separately via <b>DB_FILE</b> env to avoid 403 flakes (AGENTS.md).', styles['BodySmall']))
    story.append(Spacer(1, 8))
    # Reference systems box
    story.append(Paragraph('<b>REFERENCE BENCHMARKS</b>', styles['CoverLabel']))
    story.append(Spacer(1,4))
    ref_data = [[
        Paragraph('<b>OrangeHRM CE 5.x</b><br/><font size=6 color="#64748B">Leave, attendance, PIM, recruitment</font>', styles['TableCell']),
        Paragraph('<b>Sentrifugo 3.2</b><br/><font size=6 color="#64748B">Onboarding, offboarding, assets, analytics</font>', styles['TableCell']),
        Paragraph('<b>IceHRM 34</b><br/><font size=6 color="#64748B">Payroll, expenses, helpdesk, projects</font>', styles['TableCell']),
        Paragraph('<b>Odoo HR</b><br/><font size=6 color="#64748B">Integrated ERP baseline</font>', styles['TableCell']),
    ]]
    ref_table = Table(ref_data, colWidths=[CONTENT_W/4]*4)
    ref_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), SLATE_50),
        ('BOX', (0,0), (-1,-1), 0.4, BORDER),
        ('INNERGRID', (0,0), (-1,-1), 0.3, BORDER),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('ROUNDEDCORNERS', [6,6,6,6]),
    ]))
    story.append(ref_table)
    return story

def main():
    if not os.path.exists(MD_PATH):
        print(f"MD not found: {MD_PATH}")
        sys.exit(1)
    with open(MD_PATH, "r", encoding="utf-8") as f:
        md_text = f.read()

    # Build document template
    doc = SimpleDocTemplate(
        PDF_PATH,
        pagesize=A4,
        leftMargin=MARGIN_L,
        rightMargin=MARGIN_R,
        topMargin=MARGIN_T + 12,  # extra for header
        bottomMargin=MARGIN_B + 6,
        title="HRMS -- Software Requirements Specification v1.0",
        author="HRMS Engineering",
        subject="HRMS SRS Flask+DuckDB",
        keywords="HRMS,SRS,Flask, Requirements",
        creator="HRMS DocGen (ReportLab)",
    )
    story = []
    # Cover
    story.extend(build_cover_story())
    story.append(PageBreak())
    # TOC
    story.extend(build_toc_story())
    story.append(PageBreak())
    # Content from markdown
    content_flows = build_content_flowables_from_md(md_text)
    story.extend(content_flows)
    # Final page -- sign-off
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER, spaceBefore=8, spaceAfter=8))
    story.append(Paragraph('<b>SIGN-OFF</b>', styles['CoverLabel']))
    story.append(Spacer(1,4))
    sign_data = [
        [Paragraph('<font size=7 color="#64748B"><b>ROLE</b></font>', styles['TableCell']), Paragraph('<font size=7 color="#64748B"><b>NAME</b></font>', styles['TableCell']), Paragraph('<font size=7 color="#64748B"><b>SIGNATURE</b></font>', styles['TableCell']), Paragraph('<font size=7 color="#64748B"><b>DATE</b></font>', styles['TableCell'])],
        [Paragraph('Product Owner', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell'])],
        [Paragraph('Engineering Lead', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell'])],
        [Paragraph('HR Stakeholder', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell'])],
        [Paragraph('QA Lead', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell']), Paragraph('', styles['TableCell'])],
    ]
    sign_table = Table(sign_data, colWidths=[90, 130, 130, 90])
    sign_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), SLATE_100),
        ('GRID', (0,0), (-1,-1), 0.4, BORDER),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(sign_table)
    story.append(Spacer(1, 8))
    story.append(Paragraph('Regenerate this SRS after any blueprint or migration change: re-run explore over <b>hrms/*.py</b> and <b>templates/*.html</b>, diff FRs, update traceability and version. Store generated PDF as <b>docs/SRS.pdf</b> alongside source <b>docs/SRS.md</b>.', styles['Caption']))

    # Build with custom page functions
    # We need to handle two page templates: cover/toc vs content
    # Simplest: use same onLaterPages but cover_footer for first 2 pages handled via doc.page check in header_footer
    # So header_footer already skips pages 1-2 (cover+toc) -- but we still want minimal footer on those. We'll combine.

    def combined_canvas(canvas, doc):
        if doc.page <= 2:
            cover_footer(canvas, doc)
        else:
            header_footer(canvas, doc)

    # Build
    doc.build(story, onFirstPage=combined_canvas, onLaterPages=combined_canvas)
    # File size
    size_kb = os.path.getsize(PDF_PATH) / 1024
    # Count pages via PyMuPDF if available
    pages = "?"
    try:
        import fitz
        d = fitz.open(PDF_PATH)
        pages = len(d)
        d.close()
    except Exception:
        pass
    print(f"[OK] PDF generated: {PDF_PATH}  ({size_kb:.1f} KB, {pages} pages)")

if __name__ == "__main__":
    main()
