# ==========================================================================
# مولّد تقرير PDF لمشروع واحد (يعتمد على نفس بيانات داشبورد المشروع)
# مكتبات pure-Python بالكامل (reportlab + arabic_reshaper + python-bidi)
# بدون أي اعتماديات نظام تشغيل خارجية، عشان تشتغل من غير مشاكل على Render.
# ==========================================================================
import os
import io
import json
import math
import re
from datetime import date, datetime

import requests
import arabic_reshaper
from bidi.algorithm import get_display

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Wedge, Line, Circle, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.legends import Legend

# --------------------------------------------------------------------------
# تسجيل خط Amiri
# --------------------------------------------------------------------------
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
pdfmetrics.registerFont(TTFont("Arabic", os.path.join(FONT_DIR, "Amiri-Regular.ttf")))
pdfmetrics.registerFont(TTFont("Arabic-Bold", os.path.join(FONT_DIR, "Amiri-Bold.ttf")))

# ألوان الهوية
NAVY = colors.HexColor("#1a2b4c")
NAVY_DARK = colors.HexColor("#0f1a2e")
GOLD = colors.HexColor("#d4a373")
MUTED = colors.HexColor("#6c7a91")
BORDER = colors.HexColor("#e3e7ef")
BG_MUTED = colors.HexColor("#eef1f6")
WHITE = colors.white
SUCCESS = colors.HexColor("#1e8e5a")
DANGER = colors.HexColor("#d9463f")
WARNING = colors.HexColor("#d99425")
INFO = colors.HexColor("#2685c9")

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img", "logo.png")

_ARABIC_CHAR_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

def ar(text):
    if text is None:
        return ""
    text = str(text)
    if text.strip() == "":
        return ""
    if not _ARABIC_CHAR_RE.search(text):
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text

def _style(size=10, bold=False, color=NAVY, align=TA_RIGHT, leading=None):
    return ParagraphStyle(
        name=f"s{size}{bold}{align}",
        fontName="Arabic-Bold" if bold else "Arabic",
        fontSize=size,
        textColor=color,
        alignment=align,
        leading=leading or size * 1.45,
    )

def P(text, size=10, bold=False, color=NAVY, align=TA_RIGHT):
    return Paragraph(ar(text), _style(size=size, bold=bold, color=color, align=align))

def num(val):
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0

def fmt_num(val):
    try:
        n = float(val or 0)
        return f"{n:,.0f}"
    except (TypeError, ValueError):
        return "0"

def fmt_pct(val):
    try:
        return f"{round(float(val or 0) * 100)}%"
    except (TypeError, ValueError):
        return "0%"

def safe_date(val):
    if val is None or str(val).strip() == "":
        return "-"
    return str(val)

IMPACT_COLORS = {
    "كبير": (DANGER, colors.HexColor("#fdeceb")),
    "متوسط": (WARNING, colors.HexColor("#fdf3e2")),
    "منخفض": (MUTED, BG_MUTED),
    "غير مؤثر": (MUTED, BG_MUTED),
}

TXT = {
    "ar": {
        "header_company": "المعماريون السعوديون",
        "header_subtitle": "تقرير أداء المشروع",
        "generated_at": "تاريخ إصدار التقرير",
        "no_data": "لا توجد بيانات مسجلة لهذا المشروع بعد.",
        "sec_desc": "وصف المشروع",
        "no_desc": "لا يوجد وصف مسجل لهذا المشروع.",
        "sec_overview": "نظرة عامة",
        "lbl_manager": "مدير المشروع",
        "lbl_type": "نوع المشروع",
        "lbl_owner": "المالك",
        "lbl_developer": "المطور",
        "lbl_contractor": "المقاول",
        "lbl_last_update": "آخر تحديث بيانات",
        "kpi_actual": "الإنجاز الفعلي",
        "kpi_planned": "الإنجاز المخطط",
        "kpi_contractor_val": "قيمة عقد المقاول",
        "kpi_ncr_open": "NCR مفتوحة",
        "sec_progress": "التقدم: حالي وسابق (فعلي / مخطط)",
        "cat_prev_period": "الفترة السابقة",
        "cat_cur_period": "الفترة الحالية",
        "diff_actual": "فرق الفعلي (حالي - سابق)",
        "diff_planned": "فرق المخطط (حالي - سابق)",
        "sec_duration": "المدة التعاقدية (منقضٍ / متبقٍ)",
        "dur_elapsed": "منقضٍ",
        "dur_remaining": "متبقٍ",
        "lbl_total_duration": "إجمالي المدة",
        "lbl_elapsed": "المنقضي",
        "lbl_remaining": "المتبقي",
        "days_unit": "يوم",
        "sec_quality": "بيانات الجودة — المخططات (SD) وطلبات الاستلام (WIR) ومخالفات (NCR)",
        "sec_sd": "المخططات (SD)",
        "sec_wir": "طلبات الاستلام (WIR)",
        "sec_ncr": "مخالفات الجودة (NCR)",
        "cat_total": "الإجمالي",
        "cat_submitted": "مقدمة",
        "cat_approved": "معتمدة",
        "cat_review": "قيد المراجعة",
        "cat_closed": "مغلقة",
        "cat_open": "مفتوحة",
        "series_sd": "SD (المخططات)",
        "series_wir": "WIR (طلبات الاستلام)",
        "series_ncr": "NCR",
        "sec_eval": "التقييم الفني",
        "eval_labor": "توفر العمالة",
        "eval_equip": "توفر المعدات",
        "eval_financial": "القدرة المالية",
        "eval_hse": "الالتزام بالسلامة",
        "sec_spi": "مؤشر الأداء الجدولي (SPI)",
        "spi_label": "SPI - مؤشر الأداء الجدولي",
        "sec_financial": "البيانات المالية والتعاقدية (المقاول)",
        "lbl_mods_count": "أوامر التغيير (العدد)",
        "lbl_mods_val": "أوامر التغيير (القيمة)",
        "lbl_inv_count": "مستخلصات المقاول (العدد)",
        "lbl_inv_val": "مستخلصات المقاول (القيمة)",
        "lbl_start_contractual": "بداية العقد (تعاقدي)",
        "lbl_end_contractual": "نهاية العقد (تعاقدي)",
        "lbl_start_actual": "البداية الفعلية",
        "lbl_end_expected": "النهاية المتوقعة",
        "sec_photos": "صور التقدم الأسبوعية",
        "no_photos": "لا توجد صور تقدم مسجلة في آخر تحديث.",
        "sec_iso": "المسقط الأيزومتري (3D)",
        "no_iso": "لا توجد صورة أيزومتريك مسجلة.",
        "sec_works": "الأعمال (آخر تحديث)",
        "works_completed": "الأعمال المنجزة",
        "works_ongoing": "الأعمال الجارية",
        "works_planned": "الأعمال المخططة",
        "sec_obstacles": "المعوقات الحالية (مجمعة)",
        "no_obstacles": "لا توجد معوقات مسجلة.",
        "col_desc": "الوصف",
        "col_impact": "التأثير",
        "col_status": "الحالة",
        "impact_كبير": "كبير",
        "impact_متوسط": "متوسط",
        "impact_منخفض": "منخفض",
        "impact_غير مؤثر": "غير مؤثر",
        "status_resolved": "تم الحل",
        "status_open": "قائم",
        "footer": "تم إصدار هذا التقرير آلياً من نظام إدارة المشاريع (PMIS) — المعماريون السعوديون",
    },
    "en": {
        "header_company": "Saudi Architects",
        "header_subtitle": "Project Performance Report",
        "generated_at": "Report generated on",
        "no_data": "No data has been recorded for this project yet.",
        "sec_desc": "Project Description",
        "no_desc": "No description recorded for this project.",
        "sec_overview": "Overview",
        "lbl_manager": "Project Manager",
        "lbl_type": "Project Type",
        "lbl_owner": "Owner",
        "lbl_developer": "Developer",
        "lbl_contractor": "Contractor",
        "lbl_last_update": "Last Data Update",
        "kpi_actual": "Actual Progress",
        "kpi_planned": "Planned Progress",
        "kpi_contractor_val": "Contractor Contract Value",
        "kpi_ncr_open": "Open NCRs",
        "sec_progress": "Progress: Current & Previous (Actual / Planned)",
        "cat_prev_period": "Previous Period",
        "cat_cur_period": "Current Period",
        "diff_actual": "Actual Diff (Current - Previous)",
        "diff_planned": "Planned Diff (Current - Previous)",
        "sec_duration": "Contract Duration (Elapsed / Remaining)",
        "dur_elapsed": "Elapsed",
        "dur_remaining": "Remaining",
        "lbl_total_duration": "Total Duration",
        "lbl_elapsed": "Elapsed",
        "lbl_remaining": "Remaining",
        "days_unit": "days",
        "sec_quality": "Quality Data — Shop Drawings (SD), Work Inspection Requests (WIR) & NCRs",
        "sec_sd": "Shop Drawings (SD)",
        "sec_wir": "Inspection Requests (WIR)",
        "sec_ncr": "Quality Non-Conformances (NCR)",
        "cat_total": "Total",
        "cat_submitted": "Submitted",
        "cat_approved": "Approved",
        "cat_review": "Under Review",
        "cat_closed": "Closed",
        "cat_open": "Open",
        "series_sd": "SD (Shop Drawings)",
        "series_wir": "WIR (Inspection Requests)",
        "series_ncr": "NCR",
        "sec_eval": "Technical Evaluation",
        "eval_labor": "Labor Availability",
        "eval_equip": "Equipment Availability",
        "eval_financial": "Financial Capability",
        "eval_hse": "HSE Compliance",
        "sec_spi": "Schedule Performance Index (SPI)",
        "spi_label": "SPI - Schedule Performance Index",
        "sec_financial": "Financial & Contractual Data (Contractor)",
        "lbl_mods_count": "Change Orders (Count)",
        "lbl_mods_val": "Change Orders (Value)",
        "lbl_inv_count": "Contractor Certificates (Count)",
        "lbl_inv_val": "Contractor Certificates (Value)",
        "lbl_start_contractual": "Contract Start (Contractual)",
        "lbl_end_contractual": "Contract End (Contractual)",
        "lbl_start_actual": "Actual Start",
        "lbl_end_expected": "Expected End",
        "sec_photos": "Weekly Progress Photos",
        "no_photos": "No progress photos recorded in the latest update.",
        "sec_iso": "Isometric View (3D)",
        "no_iso": "No isometric image recorded.",
        "sec_works": "Works (Latest Update)",
        "works_completed": "Completed Works (Last Week)",
        "works_ongoing": "Ongoing Works",
        "works_planned": "Planned Works (Next Week)",
        "sec_obstacles": "Current Obstacles (Aggregated)",
        "no_obstacles": "No obstacles recorded.",
        "col_desc": "Description",
        "col_impact": "Impact",
        "col_status": "Status",
        "impact_كبير": "High",
        "impact_متوسط": "Medium",
        "impact_منخفض": "Low",
        "impact_غير مؤثر": "No Impact",
        "status_resolved": "Resolved",
        "status_open": "Open",
        "footer": "This report was generated automatically by the Project Management Information System (PMIS) — Saudi Architects",
    },
}

def t(lang, key):
    return TXT.get(lang, TXT["ar"]).get(key, TXT["ar"].get(key, key))

# استبدال الألوان الداكنة بألوان عصرية
NAVY = colors.HexColor("#1e293b")
BORDER = colors.HexColor("#e2e8f0")
BG_MUTED = colors.HexColor("#f8fafc")

# تعديل شكل عناوين الأقسام لتكون بخلفية رمادية فاتحة جداً بدلاً من الأزرق الكاتم
def _section_title(text, align=TA_RIGHT):
    tbl = Table([[P(text, size=11, bold=True, color=NAVY, align=align)]], colWidths=[170 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, -1), 1, BORDER) # خط سفلي رفيع للحدود
    ]))
    return tbl

def _kv_table(pairs, col_widths=(42 * mm, 43 * mm), align=TA_RIGHT):
    rows = []
    for i in range(0, len(pairs), 2):
        chunk = pairs[i:i + 2]
        row = []
        for label, value in chunk:
            row.append(P(label, size=9, color=MUTED, align=align))
            row.append(P(value, size=9.5, bold=True, align=align))
        if len(chunk) == 1:
            row.extend(["", ""])
        rows.append(row)
    tbl = Table(rows, colWidths=[col_widths[0], col_widths[1], col_widths[0], col_widths[1]])
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("BACKGROUND", (0, 0), (0, -1), BG_MUTED),
        ("BACKGROUND", (2, 0), (2, -1), BG_MUTED),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tbl

def _kpi_row(items, col_width=42.5 * mm):
    cells = []
    palette = [NAVY, GOLD, SUCCESS, INFO]
    for idx, (label, value) in enumerate(items):
        inner = Table(
            [[P(label, size=8, color=WHITE, align=TA_CENTER)],
             [P(value, size=13, bold=True, color=WHITE, align=TA_CENTER)]],
            colWidths=[col_width],
        )
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), palette[idx % len(palette)]),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        cells.append(inner)
    row = Table([cells], colWidths=[col_width + 1] * len(cells))
    row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return row

def _diff_pill_row(diff_actual, diff_planned, lang="ar"):
    def pill(label, diff):
        color = SUCCESS if diff > 0 else (DANGER if diff < 0 else MUTED)
        bg = colors.HexColor("#e7f6ee") if diff > 0 else (colors.HexColor("#fdeceb") if diff < 0 else BG_MUTED)
        sign = "+" if diff > 0 else ""
        cell = Table(
            [[P(label, size=8, color=color, align=TA_CENTER)],
             [P(f"{sign}{diff}%", size=13, bold=True, color=color, align=TA_CENTER)]],
            colWidths=[75 * mm],
        )
        cell.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        return cell
    row = Table([[pill(t(lang, "diff_actual"), diff_actual), pill(t(lang, "diff_planned"), diff_planned)]],
                colWidths=[76 * mm, 76 * mm])
    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return row

def _cluster_bar_chart(categories, series, series_names, palette, width=170 * mm, height=62 * mm, val_max=None, bar_label_fmt="%d"):
    bottom_margin = 15 * mm 
    chart_h = height - bottom_margin - (6 * mm)
    drawing = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 32
    bc.y = bottom_margin
    bc.height = chart_h
    bc.width = width - 45
    bc.data = series
    bc.categoryAxis.categoryNames = [ar(c) for c in categories]
    bc.categoryAxis.labels.fontName = "Arabic"
    bc.categoryAxis.labels.fontSize = 7.5
    bc.valueAxis.valueMin = 0
    if val_max: bc.valueAxis.valueMax = val_max
    bc.valueAxis.labels.fontName = "Arabic"
    bc.valueAxis.labels.fontSize = 7
    bc.groupSpacing = 12
    bc.barSpacing = 2
    bc.barLabelFormat = bar_label_fmt
    bc.barLabels.fontName = "Arabic"
    bc.barLabels.fontSize = 6.5
    bc.barLabels.nudge = 7
    for i, col in enumerate(palette):
        try: bc.bars[i].fillColor = col
        except: pass
    drawing.add(bc)
    return drawing

def _distributed_bar_chart(categories, values, colors_list, width=170 * mm, height=58 * mm, val_max=None, bar_label_fmt="%d"):
    bottom_margin = 14 * mm
    chart_h = height - bottom_margin - (6 * mm)
    drawing = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 32
    bc.y = bottom_margin
    bc.height = chart_h
    bc.width = width - 45
    bc.data = [values]
    bc.categoryAxis.categoryNames = [ar(c) for c in categories]
    bc.categoryAxis.labels.fontName = "Arabic"
    bc.categoryAxis.labels.fontSize = 7.5
    bc.valueAxis.valueMin = 0
    if val_max: bc.valueAxis.valueMax = val_max
    bc.valueAxis.labels.fontName = "Arabic"
    bc.valueAxis.labels.fontSize = 7
    bc.groupSpacing = 10
    bc.barWidth = (bc.width / max(len(categories), 1)) * 0.55
    bc.barLabelFormat = bar_label_fmt
    bc.barLabels.fontName = "Arabic-Bold"
    bc.barLabels.fontSize = 7
    bc.barLabels.nudge = 7
    for i, col in enumerate(colors_list):
        try: bc.bars[(0, i)].fillColor = col
        except: pass
    drawing.add(bc)
    return drawing

def _metric_with_total(label_total, total_val, chart, total_w=28 * mm, chart_w=142 * mm):
    total_box = Table(
        [[P(fmt_num(total_val), size=15, bold=True, color=GOLD, align=TA_CENTER)],
         [P(label_total, size=7.5, color=MUTED, align=TA_CENTER)]],
        colWidths=[total_w],
    )
    total_box.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    row = Table([[total_box, chart]], colWidths=[total_w, chart_w])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "CENTER")]))
    return row

def _legend_row(pairs, swatch=3.2 * mm, label_w=45 * mm, align=TA_RIGHT):
    cells, widths = [], []
    for color, label in pairs:
        sw = Table([[""]], colWidths=[swatch], rowHeights=[swatch])
        sw.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), color)]))
        cells.append(sw)
        widths.append(swatch)
        cells.append(P(label, size=8, color=NAVY, align=align))
        widths.append(label_w)
    row = Table([cells], colWidths=widths)
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return row

def _duration_donut(elapsed_pct, remaining_pct, lang="ar"):
    size = 42 * mm
    drawing = Drawing(170 * mm, 50 * mm)
    cx, cy = 27 * mm, 4 * mm + size / 2

    pie = Pie()
    pie.x = 5; pie.y = 4; pie.width = size; pie.height = size
    pie.data = [max(elapsed_pct, 0.0001), max(remaining_pct, 0.0001)]
    pie.labels = None
    pie.slices.strokeWidth = 1; pie.slices.strokeColor = WHITE
    pie.slices[0].fillColor = NAVY; pie.slices[1].fillColor = GOLD
    drawing.add(pie)

    hole_r = size * 0.30
    drawing.add(Circle(cx, cy, hole_r, fillColor=WHITE, strokeColor=WHITE, strokeWidth=0))
    drawing.add(String(cx, cy + 2, f"{round(elapsed_pct)}%", fontName="Arabic-Bold", fontSize=13, fillColor=NAVY, textAnchor="middle"))
    drawing.add(String(cx, cy - 9, ar(t(lang, "dur_elapsed")), fontName="Arabic", fontSize=7, fillColor=MUTED, textAnchor="middle"))

    leg = Legend()
    leg.x = 75 * mm; leg.y = cy + 6; leg.dx = 7; leg.dy = 7
    leg.fontName = "Arabic"; leg.fontSize = 8.5; leg.deltay = 11
    leg.colorNamePairs = [(NAVY, ar(t(lang, "dur_elapsed"))), (GOLD, ar(t(lang, "dur_remaining")))]
    drawing.add(leg)
    return drawing

def _spi_gauge(spi, lang="ar"):
    max_val = 1.0
    # إبرة المؤشر تقف عند 1 كحد أقصى بصرياً
    spi_c = max(0.0, min(spi, max_val))
    width, height = 170 * mm, 46 * mm
    cx, cy, r, inner_r = 60 * mm, 4 * mm, 34 * mm, 19 * mm
    drawing = Drawing(width, height)

    bands = [(0.0, 0.8, DANGER), (0.8, 0.9, WARNING), (0.9, max_val, SUCCESS)]
    for lo, hi, col in bands:
        a1 = 180 - (lo / max_val) * 180
        a2 = 180 - (hi / max_val) * 180
        drawing.add(Wedge(cx, cy, r, a2, a1, fillColor=col, strokeColor=WHITE, strokeWidth=1))
    
    drawing.add(Circle(cx, cy, inner_r, fillColor=WHITE, strokeColor=WHITE, strokeWidth=0))

    angle = 180 - (spi_c / max_val) * 180
    rad = math.radians(angle)
    needle_len = r * 0.9
    nx, ny = cx + needle_len * math.cos(rad), cy + needle_len * math.sin(rad)
    drawing.add(Line(cx, cy, nx, ny, strokeColor=NAVY_DARK, strokeWidth=2.5))
    drawing.add(Circle(cx, cy, 3.2, fillColor=NAVY_DARK, strokeColor=NAVY_DARK))

    value_color = SUCCESS if spi >= 0.9 else (WARNING if spi >= 0.8 else DANGER)
    
    # كتابة الرقم الحقيقي مهما زاد عن 1
    drawing.add(String(cx, cy + 12, f"{spi:.2f}", fontName="Arabic-Bold", fontSize=18, fillColor=value_color, textAnchor="middle"))
    drawing.add(String(cx, cy - 9, ar(t(lang, "spi_label")), fontName="Arabic", fontSize=7.5, fillColor=MUTED, textAnchor="middle"))
    return drawing

def _fetch_image_flowable(url, max_w, max_h):
    if not url or not str(url).strip() or url == "لا يوجد مرفق":
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        img_bytes = io.BytesIO(resp.content)
        img = Image(img_bytes)
        iw, ih = img.imageWidth, img.imageHeight
        if not iw or not ih: return None
        scale = min(max_w / iw, max_h / ih)
        img.drawWidth = iw * scale; img.drawHeight = ih * scale
        return img
    except Exception:
        return None

def _image_grid(urls, cols=2, cell_w=80 * mm, cell_h=55 * mm, empty_text="لا توجد صور مسجلة."):
    flowables = [_fetch_image_flowable(u, cell_w - 4 * mm, cell_h - 4 * mm) for u in urls if u and str(u).strip() and u != "لا يوجد مرفق"]
    flowables = [f for f in flowables if f is not None]
    if not flowables: return P(empty_text, size=9, color=MUTED, align=TA_RIGHT)

    rows = []
    for i in range(0, len(flowables), cols):
        chunk = flowables[i:i + cols]
        while len(chunk) < cols: chunk.append("")
        rows.append(chunk)
    tbl = Table(rows, colWidths=[cell_w] * cols, rowHeights=[cell_h] * len(rows))
    tbl.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), 0.5, BORDER), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return tbl

def _days_between(d1, d2):
    def parse(d):
        if isinstance(d, (date, datetime)): return d if isinstance(d, date) else d.date()
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    return (parse(d2) - parse(d1)).days

def build_project_pdf(project_name, records, generated_by=None, lang="ar"):
    lang = lang if lang in TXT else "ar"
    align = TA_LEFT if lang == "en" else TA_RIGHT

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=15 * mm, bottomMargin=15 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
        title=f"{t(lang, 'header_subtitle')} - {project_name}",
    )

    story = []
    latest = records[-1] if records else {}

    header_cells = []
    if os.path.exists(LOGO_PATH):
        try:
            logo_img = Image(LOGO_PATH, width=14 * mm, height=14 * mm)
            header_cells.append(logo_img)
        except Exception:
            pass
    title_block = [
        P(t(lang, "header_company"), size=16, bold=True, color=NAVY, align=TA_CENTER),
        P(t(lang, "header_subtitle"), size=12, color=MUTED, align=TA_CENTER),
    ]
    if header_cells:
        htbl = Table([[title_block, header_cells[0]]], colWidths=[150 * mm, 20 * mm])
        htbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "CENTER")]))
        story.append(htbl)
    else:
        story.extend(title_block)
    story.append(Spacer(1, 4 * mm))
    story.append(P(project_name or "-", size=18, bold=True, color=GOLD, align=TA_CENTER))
    story.append(Spacer(1, 2 * mm))
    gen_dt = datetime.now().strftime('%Y-%m-%d %I:%M %p')
    story.append(P(f"{t(lang, 'generated_at')}: {gen_dt}", size=8, color=MUTED, align=TA_CENTER))
    story.append(Spacer(1, 6 * mm))

    if not records:
        story.append(P(t(lang, "no_data"), size=12, color=MUTED, align=TA_CENTER))
        doc.build(story)
        buf.seek(0)
        return buf

    desc = latest.get("project_desc") or t(lang, "no_desc")
    story.append(KeepTogether([
        _section_title(t(lang, "sec_desc"), align=align),
        Spacer(1, 2 * mm),
        P(desc, size=9.5, color=colors.HexColor("#3c4657"), align=TA_RIGHT),
    ]))
    story.append(Spacer(1, 6 * mm))

    story.append(KeepTogether([
        _section_title(t(lang, "sec_overview"), align=align),
        Spacer(1, 2 * mm),
        _kv_table([
            (t(lang, "lbl_manager"), latest.get("manager_name") or "-"),
            (t(lang, "lbl_type"), latest.get("project_type") or "-"),
            (t(lang, "lbl_owner"), latest.get("project_owner") or "-"),
            (t(lang, "lbl_developer"), latest.get("project_developer") or "-"),
            (t(lang, "lbl_contractor"), latest.get("project_contractor") or "-"),
            (t(lang, "lbl_last_update"), safe_date(latest.get("current_data_date"))),
        ], align=align),
    ]))
    story.append(Spacer(1, 5 * mm))

    story.append(_kpi_row([
        (t(lang, "kpi_actual"), fmt_pct(latest.get("act_prog_cur"))),
        (t(lang, "kpi_planned"), fmt_pct(latest.get("plan_prog_cur"))),
        (t(lang, "kpi_contractor_val"), fmt_num(latest.get("contractor_val"))),
        (t(lang, "kpi_ncr_open"), fmt_num(latest.get("ncr_open"))),
    ]))
    story.append(Spacer(1, 6 * mm))

    act_cur = round(float(latest.get("act_prog_cur") or 0) * 100)
    act_prev = round(float(latest.get("act_prog_prev") or 0) * 100)
    plan_cur = round(float(latest.get("plan_prog_cur") or 0) * 100)
    plan_prev = round(float(latest.get("plan_prog_prev") or 0) * 100)

    story.append(KeepTogether([
        _section_title(t(lang, "sec_progress"), align=align),
        Spacer(1, 3 * mm),
        _cluster_bar_chart(
            categories=[t(lang, "cat_prev_period"), t(lang, "cat_cur_period")],
            series=[[act_prev, act_cur], [plan_prev, plan_cur]],
            series_names=[t(lang, "kpi_actual"), t(lang, "kpi_planned")],
            palette=[NAVY, GOLD], val_max=100, bar_label_fmt="%d%%",
        ),
        _legend_row([(NAVY, t(lang, "kpi_actual")), (GOLD, t(lang, "kpi_planned"))], align=align),
        Spacer(1, 2 * mm),
        _diff_pill_row(act_cur - act_prev, plan_cur - plan_prev, lang=lang),
    ]))
    story.append(Spacer(1, 6 * mm))

    spi_val = (act_cur / plan_cur) if plan_cur > 0 else (1.0 if act_cur > 0 else 0)
    story.append(KeepTogether([
        _section_title(t(lang, "sec_spi"), align=align),
        Spacer(1, 3 * mm),
        _spi_gauge(spi_val, lang=lang),
    ]))
    story.append(Spacer(1, 6 * mm))

    elapsed_pct, remaining_pct = 0, 100
    dur_total_lbl = dur_elapsed_lbl = dur_remaining_lbl = "-"
    days_unit = t(lang, "days_unit")
    try:
        total_days = _days_between(latest.get("start_contractual"), latest.get("end_contractual"))
        if total_days > 0:
            elapsed_days = _days_between(latest.get("start_contractual"), latest.get("current_data_date"))
            elapsed_days = max(0, min(elapsed_days, total_days))
            remaining_days = total_days - elapsed_days
            elapsed_pct = round((elapsed_days / total_days) * 100)
            remaining_pct = 100 - elapsed_pct
            dur_total_lbl = f"{total_days} {days_unit}"
            dur_elapsed_lbl = f"{elapsed_days} {days_unit} ({elapsed_pct}%)"
            dur_remaining_lbl = f"{remaining_days} {days_unit} ({remaining_pct}%)"
    except Exception:
        pass
    story.append(KeepTogether([
        _section_title(t(lang, "sec_duration"), align=align),
        Spacer(1, 3 * mm),
        _duration_donut(elapsed_pct, remaining_pct, lang=lang),
        Spacer(1, 2 * mm),
        _kv_table([
            (t(lang, "lbl_total_duration"), dur_total_lbl),
            (t(lang, "lbl_elapsed"), dur_elapsed_lbl),
            (t(lang, "lbl_remaining"), dur_remaining_lbl),
        ], align=align),
    ]))
    story.append(Spacer(1, 6 * mm))

    sd_sub, sd_app, sd_rev = num(latest.get("drawings_sub")), num(latest.get("drawings_app")), num(latest.get("drawings_rev"))
    sd_total = sd_sub + sd_app + sd_rev
    wir_sub, wir_app, wir_rev = num(latest.get("ir_sub")), num(latest.get("ir_app")), num(latest.get("ir_rev"))
    wir_total = wir_sub + wir_app + wir_rev
    ncr_open, ncr_closed = num(latest.get("ncr_open")), num(latest.get("ncr_closed"))
    ncr_total = ncr_open + ncr_closed

    qcats = [t(lang, "cat_submitted"), t(lang, "cat_approved"), t(lang, "cat_review")]
    qcolors = [INFO, SUCCESS, GOLD]
    story.append(KeepTogether([
        _section_title(t(lang, "sec_quality"), align=align),
        Spacer(1, 3 * mm),
        P(t(lang, "sec_sd"), size=9.5, bold=True, color=NAVY, align=align),
        _metric_with_total(
            t(lang, "cat_total"), sd_total,
            _distributed_bar_chart(qcats, [sd_sub, sd_app, sd_rev], qcolors, width=142 * mm, height=42 * mm),
        ),
    ]))
    story.append(Spacer(1, 3 * mm))
    story.append(KeepTogether([
        P(t(lang, "sec_wir"), size=9.5, bold=True, color=NAVY, align=align),
        _metric_with_total(
            t(lang, "cat_total"), wir_total,
            _distributed_bar_chart(qcats, [wir_sub, wir_app, wir_rev], qcolors, width=142 * mm, height=42 * mm),
        ),
    ]))
    story.append(Spacer(1, 3 * mm))
    story.append(KeepTogether([
        P(t(lang, "sec_ncr"), size=9.5, bold=True, color=NAVY, align=align),
        _metric_with_total(
            t(lang, "cat_total"), ncr_total,
            _distributed_bar_chart([t(lang, "cat_closed"), t(lang, "cat_open")], [ncr_closed, ncr_open],
                                    [SUCCESS, DANGER], width=142 * mm, height=42 * mm),
        ),
    ]))
    story.append(Spacer(1, 6 * mm))

    story.append(KeepTogether([
        _section_title(t(lang, "sec_eval"), align=align),
        Spacer(1, 3 * mm),
        _kv_table([
            (t(lang, "eval_labor"), f"{fmt_num(latest.get('eval_labor'))}/10"),
            (t(lang, "eval_equip"), f"{fmt_num(latest.get('eval_equip'))}/10"),
            (t(lang, "eval_financial"), f"{fmt_num(latest.get('eval_financial'))}/10"),
            (t(lang, "eval_hse"), f"{fmt_num(latest.get('eval_hse'))}/10"),
        ], align=align),
    ]))
    story.append(Spacer(1, 6 * mm))

    story.append(KeepTogether([
        _section_title(t(lang, "sec_financial"), align=align),
        Spacer(1, 2 * mm),
        _kv_table([
            (t(lang, "kpi_contractor_val"), fmt_num(latest.get("contractor_val"))),
            (t(lang, "lbl_mods_count"), fmt_num(latest.get("contractor_mods_count"))),
            (t(lang, "lbl_mods_val"), fmt_num(latest.get("contractor_mods_val"))),
            (t(lang, "lbl_inv_count"), fmt_num(latest.get("cont_inv_count"))),
            (t(lang, "lbl_inv_val"), fmt_num(latest.get("cont_inv_val"))),
            (t(lang, "lbl_start_contractual"), safe_date(latest.get("start_contractual"))),
            (t(lang, "lbl_end_contractual"), safe_date(latest.get("end_contractual"))),
            (t(lang, "lbl_start_actual"), safe_date(latest.get("start_actual"))),
            (t(lang, "lbl_end_expected"), safe_date(latest.get("end_expected"))),
        ], align=align),
    ]))
    story.append(Spacer(1, 6 * mm))

    photo_urls = [latest.get("file_link_1"), latest.get("file_link_2"), latest.get("file_link_3"), latest.get("file_link_4")]
    story.append(KeepTogether([
        _section_title(t(lang, "sec_photos"), align=align),
        Spacer(1, 3 * mm),
        _image_grid(photo_urls, cols=2, cell_w=80 * mm, cell_h=55 * mm, empty_text=t(lang, "no_photos")),
    ]))
    story.append(Spacer(1, 6 * mm))

    iso_img = _fetch_image_flowable(latest.get("isometric_link"), 165 * mm, 90 * mm)
    if iso_img:
        iso_wrap = Table([[iso_img]], colWidths=[170 * mm])
        iso_wrap.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        iso_content = iso_wrap
    else:
        iso_content = P(t(lang, "no_iso"), size=9, color=MUTED, align=align)
    story.append(KeepTogether([
        _section_title(t(lang, "sec_iso"), align=align),
        Spacer(1, 3 * mm),
        iso_content,
    ]))
    story.append(Spacer(1, 6 * mm))

    works_items = [
        (t(lang, "works_completed"), "works_completed"),
        (t(lang, "works_ongoing"), "works_ongoing"),
        (t(lang, "works_planned"), "works_planned"),
    ]
    if any(latest.get(key) and str(latest.get(key)).strip() for _, key in works_items):
        works_block = [_section_title(t(lang, "sec_works"), align=align), Spacer(1, 2 * mm)]
        for label, key in works_items:
            val = latest.get(key)
            if val and str(val).strip():
                works_block.append(P(label, size=9.5, bold=True, color=NAVY, align=align))
                works_block.append(P(str(val), size=9, color=colors.HexColor("#3c4657"), align=TA_RIGHT))
                works_block.append(Spacer(1, 3 * mm))
        story.append(KeepTogether(works_block))
        story.append(Spacer(1, 3 * mm))

    # ---------- المعوقات المجمعة مع التقسيم التلقائي ----------
    all_obstacles = {}
    for rec in records:
        raw_obs = rec.get("obstacles_data")
        try:
            obs_list = json.loads(raw_obs) if isinstance(raw_obs, str) else (raw_obs or [])
            if isinstance(obs_list, list):
                for o in obs_list:
                    if o and o.get("description"):
                        desc = o.get("description").strip()
                        all_obstacles[desc] = {
                            "description": desc,
                            "impact": o.get("impact") or "غير مؤثر",
                            "closed": str(o.get("closed")).lower() in ['true', '1']
                        }
        except Exception:
            pass

    final_obstacles = list(all_obstacles.values())
    final_obstacles.sort(key=lambda x: x["closed"])

    if final_obstacles:
        rows = [[P(t(lang, "col_desc"), size=9, bold=True, color=WHITE, align=align),
                 P(t(lang, "col_impact"), size=9, bold=True, color=WHITE, align=TA_CENTER),
                 P(t(lang, "col_status"), size=9, bold=True, color=WHITE, align=TA_CENTER)]]
        row_bgs = []
        for o in final_obstacles:
            impact = o.get("impact", "-")
            impact_label = t(lang, f"impact_{impact}") if f"impact_{impact}" in TXT.get(lang, {}) else impact
            status_label = t(lang, "status_resolved") if o.get("closed") else t(lang, "status_open")
            
            # تقسيم النص بناءً على الفواصل المختلفة
            desc_text = o.get("description", "-")
            parts = [p.strip() for p in re.split(r'[_\/*,،\-]+', desc_text) if p.strip()]
            
            # إنشاء فقرة لكل نقطة مفصولة داخل خلية الجدول
            desc_flowables = []
            if len(parts) > 1:
                for p in parts:
                    desc_flowables.append(P(f"• {p}", size=8.5, align=TA_RIGHT))
            else:
                desc_flowables.append(P(parts[0] if parts else desc_text, size=8.5, align=TA_RIGHT))

            rows.append([
                desc_flowables,
                P(impact_label, size=8.5, align=TA_CENTER),
                P(status_label, size=8.5, align=TA_CENTER),
            ])
            row_bgs.append(IMPACT_COLORS.get(impact, (MUTED, BG_MUTED))[1])
        
        tbl = Table(rows, colWidths=[95 * mm, 35 * mm, 35 * mm])
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for i, bg in enumerate(row_bgs, start=1):
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
        tbl.setStyle(TableStyle(style_cmds))
        obstacles_content = tbl
    else:
        obstacles_content = P(t(lang, "no_obstacles"), size=9, color=MUTED, align=align)

    # استخدام التسمية الجديدة "المعوقات الحالية (مجمعة)" إذا كانت متوفرة في القاموس
    sec_obs_title = t(lang, "sec_obstacles")
    story.append(KeepTogether([
        _section_title(sec_obs_title, align=align),
        Spacer(1, 2 * mm),
        obstacles_content,
    ]))

    story.append(Spacer(1, 8 * mm))
    story.append(P(t(lang, "footer"), size=7.5, color=MUTED, align=TA_CENTER))

    doc.build(story)
    buf.seek(0)
    return buf
