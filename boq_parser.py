# -*- coding: utf-8 -*-
"""
محلّل جداول الكميّات — يقرأ ملفّ إكسل بأيّ شكل ويُخرج صفوفًا موحّدة.

المخرَج لكل بند: الاسم، الكمية، الوحدة، السعر الإفرادي، الإجمالي (= كمية × سعر)،
ومعها أعمدة التفكيك المستخرجة من الكود ومن صفوف المجموعات ومن نصّ البند نفسه.

مبدأ الملفّ: **لا يمرّ صفّ صامتًا**. كل صفّ يُصنَّف، وما لم يُفهم يُجمَع في قائمة
مستقلّة بدل أن يُهمَل — لأنّ بندًا ضائعًا في جدول كميّات معناه فلوس ضائعة.
"""

import re
import unicodedata
from collections import Counter

# ═══════════════════════════════ تطبيع النصّ ═══════════════════════════════

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm(v):
    """يحوّل أيّ قيمة إلى نصّ نظيف: بلا مسافات زائدة ولا محارف تحكّم."""
    if v is None:
        return ""
    s = str(v)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("‏", "").replace("‎", "").replace("\xa0", " ")
    s = re.sub(r"[\r\n\t]+", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def norm_key(v):
    """صيغة للمقارنة: صغيرة، بلا تشكيل، بلا مسافات ولا نقاط."""
    s = norm(v).lower().translate(_AR_DIGITS)
    s = re.sub(r"[ً-ْـ]", "", s)   # تشكيل وتطويل
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه")
    return re.sub(r"[\s._\-:>/\\()]+", "", s)


def num(v):
    """يقرأ رقمًا، ويعيد None لو لم يكن رقمًا. يصحّح فساد الفاصلة العائمة."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f or f in (float("inf"), float("-inf")) else _tidy(f)
    s = norm(v).translate(_AR_DIGITS)
    if not s or s.startswith("#"):          # ‎#REF!‎ وأخواتها
        return None
    s = s.replace(",", "").replace("٫", ".")
    s = re.sub(r"(SAR|ر\.س|ريال)", "", s, flags=re.I).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?%", s):
        return _tidy(float(s[:-1]) / 100.0)
    try:
        return _tidy(float(s))
    except ValueError:
        return None


def _tidy(f):
    """‎7.3999999999999995 → 7.4‎ و‎244999.99999999997 → 245000‎."""
    r = round(f, 6)
    return float(int(r)) if abs(r - round(r)) < 1e-9 else r


def is_ar(s):
    return bool(re.search(r"[؀-ۿ]", s or ""))


def detat(s):
    """يحذف التطويل. «إجمــالي» و«إجمالي» كلمة واحدة، والنمط لا يراهما كذلك."""
    return re.sub(r"ـ+", "", s or "")


# ═══════════════════════════════ الوحدات ═══════════════════════════════
# المفتاح مُطبَّع بـ norm_key، فـ«م . ط» و«م.ط» و«م ط» تصير كلّها «مط».

_UNITS = {
    "m3":  ["م3", "م٣", "m3", "cum", "cbm", "متر مكعب", "مترمكعب", "m³", "cu.m"],
    "m2":  ["م2", "م٢", "m2", "sqm", "sq.m", "squaremeters", "squaremeter",
            "متر مربع", "مترمربع", "m²", "sm"],
    "m":   ["مط", "م.ط", "m", "lm", "l.m", "linearmeters", "linearmeter",
            "متر طولي", "مترطولي", "rm", "ml"],
    "no":  ["عدد", "no", "nos", "ea", "each", "pcs", "pc", "item", "items",
            "collection", "groups", "group", "set", "sets", "قطعه", "وحده",
            # ترجمات آلية ظهرت في ملفّات حقيقية
            "number", "thenumber", "fortheunit", "piece", "pieces", "roll",
            "rolls", "نظام", "مجموعه", "مجموعة", "طقم", "لفه"],
    "ls":  ["ls", "l.s", "sum", "lumpsum", "atask", "task", "مقطوعيه", "جمله",
            "اجمالي", "allow", "themission", "mission", "job", "المهمه"],
    "day": ["day", "days", "يوم", "ايام"],
    "kg":  ["kg", "kgs", "كجم", "كيلو", "كيلوجرام"],
    "ton": ["ton", "tons", "tonne", "طن", "t"],
    "lit": ["l", "lit", "liter", "litre", "لتر"],
    "hr":  ["hr", "hour", "hours", "ساعه", "ساعات"],
    "mth": ["month", "months", "شهر", "اشهر"],
}
_UNIT_LOOKUP = {norm_key(a): c for c, al in _UNITS.items() for a in al}


def unit_norm(raw):
    """يعيد (الوحدة القياسية، هل عُرفت). الوحدة المجهولة تُعاد كما هي مع False."""
    k = norm_key(raw)
    if not k or k in ("unit", "uom", "الوحده", "الوحدة"):
        return "", True          # صدى الترويسة داخل الصفوف، لا وحدة حقيقية
    if k in _UNIT_LOOKUP:
        return _UNIT_LOOKUP[k], True
    return norm(raw), False


# ═══════════════════════════ كود النشاط ═══════════════════════════
# ‎CHR-52K-RD-SITE-01‎ ، ‎CHR-CSSD-CIVL-03‎ — عدد الرموز يختلف.

_CODE_RE = re.compile(r"^[A-Z]{2,6}(?:-[A-Z0-9]{1,10}){2,5}$")

DISCIPLINES = {
    "RD": "Roads", "MC": "Mechanical", "EL": "Electrical", "CIVL": "Civil",
    "SITE": "Site Works", "ST": "Structural", "AR": "Architectural",
    "HV": "HVAC", "PL": "Plumbing", "FF": "Fire Fighting", "LC": "Low Current",
}
WORK_TYPES = {
    "SITE": "Site Works", "ASPH": "Asphalt", "LNDS": "Landscape",
    "CONS": "Construction", "MISC": "Miscellaneous", "PLUM": "Plumbing",
    "DRNG": "Drainage", "IRR": "Irrigation", "FUEL": "Fuel System",
    "FF": "Fire Fighting", "MED": "Medical Gas", "WSUP": "Water Supply",
    "LS": "Lump Sum", "CABL": "Cabling", "ELROOM": "Electrical Room",
    "COMM": "Communications", "ALTPWR": "Alternative Power", "CIVL": "Civil",
}


def parse_code(s):
    """يفكّك كود النشاط إلى رموزه. يعيد {} لو لم يكن كودًا."""
    s = norm(s).upper()
    if not _CODE_RE.match(s):
        return {}
    p = s.split("-")
    out = {"activity_code": s, "code_project": p[0], "code_seq": p[-1]}
    mid = p[1:-1]
    # الرمز الأخير قبل الرقم نوع عمل، والذي قبله تخصّص، وما تبقّى منطقة أو مبنى
    if mid:
        out["code_work_type"] = mid[-1]
        out["work_type_name"] = WORK_TYPES.get(mid[-1], "")
    if len(mid) >= 2:
        out["code_discipline"] = mid[-2]
        out["discipline_name"] = DISCIPLINES.get(mid[-2], "")
    if len(mid) >= 3:
        out["code_area"] = "-".join(mid[:-2])
    elif len(mid) == 2:
        out["code_area"] = ""
    return out


# ═════════════════════ استخراج الرموز من نصّ البند ═════════════════════

MATERIALS = {
    "concrete": ["concrete", "rcc", "r.c.c", "خرسانة", "خرسانه"],
    "asphalt": ["asphalt", "asphaltic", "bituminous", "tack coat", "prime coat",
                "اسفلت", "أسفلت", "بيتومين"],
    "steel": ["steel", "rebar", "reinforcement", "حديد", "تسليح"],
    "stainless_steel": ["stainless steel", "s.s", "استانلس"],
    "upvc": ["upvc", "u.p.v.c"],
    "pvc": ["pvc"],
    "hdpe": ["hdpe", "h.d.p.e", "polyethylene"],
    "ppr": ["ppr", "p.p.r"],
    "grp": ["grp", "g.r.p", "fiberglass", "فيبر"],
    "ductile_iron": ["ductile iron", "d.i pipe"],
    "cast_iron": ["cast iron", "c.i"],
    "copper": ["copper", "نحاس"],
    "aluminium": ["aluminium", "aluminum", "ألمنيوم", "المنيوم"],
    "galvanized": ["galvanized", "galvanised", "g.i", "مجلفن"],
    "block": ["building block", "blockwork", "block", "بلوك", "طابوق"],
    "gypsum": ["gypsum", "gypboard", "جبس", "جبسوم"],
    "tile": ["tile", "tiles", "tiling", "بلاط", "سيراميك", "ceramic",
             "porcelain", "بورسلين"],
    "marble": ["marble", "رخام"],
    "granite": ["granite", "جرانيت"],
    "corian": ["corian"],
    "interlock": ["interlock", "انترلوك", "إنترلوك"],
    "wood": ["wood", "woodwork", "plywood", "خشب", "أخشاب"],
    "glass": ["glass", "glazing", "زجاج"],
    "paint": ["paint", "painting", "دهان", "دهانات"],
    "insulation": ["insulation", "insulated", "عزل"],
    "cable": ["cable", "cables", "كابل", "كوابل"],
    "conduit": ["conduit", "مواسير كهرباء"],
    "duct": ["duct", "ductwork", "دكت"],
    "pipe": ["pipe", "piping", "ماسورة", "مواسير", "أنابيب"],
    "cement": ["cement", "أسمنت", "اسمنت"],
    "sand": ["sand", "رمل"],
    "aggregate": ["aggregate", "ركام", "بيسكورس"],
    "soil_fill": ["fill", "backfill", "ردم", "ردميات"],
}

_TOK = [
    # (اسم العمود، النمط، المجموعة المأخوذة)
    ("dimensions", re.compile(
        r"\b\d{2,5}(?:\.\d+)?\s*[x×*]\s*\d{2,5}(?:\.\d+)?"
        r"(?:\s*[x×*]\s*\d{2,5}(?:\.\d+)?)?\s*(?:mm|cm|m)?\b", re.I), 0),
    ("diameter", re.compile(
        r"\b(\d{1,4}(?:\.\d+)?(?:\s*\d/\d)?\s*(?:inch|inches|\"|''|mm|بوصة|بوصه))"
        r"(?=\s*(?:dia\.?|diameter|قطر)?\b)", re.I), 1),
    ("diameter", re.compile(
        r"(?:dia\.?|diameter|قطر)\s*[:=]?\s*(\d{1,4}(?:\.\d+)?)\s*"
        r"(?:mm|cm|inch|\")?", re.I), 0),
    ("thickness", re.compile(
        r"(?:thickness|thick|سمك)\s*(?:of\s*)?(?:not\s*less\s*than\s*)?"
        r"[:=]?\s*\(?\s*(\d{1,4}(?:\.\d+)?)\s*(cm|mm|m)\)?", re.I), 0),
    ("thickness", re.compile(
        r"\b(\d{1,3}(?:\.\d+)?)\s*(cm|mm)\s+thick\b", re.I), 0),
    ("capacity", re.compile(
        r"\b(\d{1,6}(?:\.\d+)?)\s*(hp|kw|kva|kv|tr|ton|m3|m³|liter|litre|"
        r"ltr|amp|a)\b", re.I), 0),
    ("tag_code", re.compile(r"\b([A-Z]{1,4}-\d{1,3})\b"), 1),
    ("spec_ref", re.compile(
        r"\b(?:M\.?C\.?|R\.?C\.?|A-)\s?\d(?:\.\d)?\b", re.I), 0),
]

# علامات العنوان الصريحة فقط. وُسِّعت مرّة فابتلعت أوصافًا عربية تبدأ بـ«أعمال»
# فصارت عناوين، وضاع ارتباطها ببنودها. الضيق هنا أأمن من السعة.
_GROUP_HINTS = re.compile(
    r"^\s*(bill\s*\(?\s*(no\.?)?\s*\d|section\s*[-:]?\s*\d|part\s*[-:]?\s*\d|"
    r"area\s*no|القسم\s+\S+|الباب\s+\S+|جزء\s*\d|بند\s*رقم)", re.I)

_TOTAL_HINTS = re.compile(
    r"(total|sub-?total|to\s+collection|carried|brought\s+forward|vat|"
    r"إجمالي|اجمالي|مجموع|الإجمالي|الضريبة|القيمة\s*المضافة|>>)", re.I)

# أعمدة التقدّم والأوامر التغييرية ليست أسعارًا، ولا تُضمّ إلى كتل التسعير
_PROGRESS_HINTS = re.compile(
    r"(performed|previous|current|todate|to\s*date|progress|executed|"
    r"invoic|certif|\bvo\b|variation|منفذ|سابق|حالي|المنجز|تراكمي)", re.I)

_BUILDING_RE = re.compile(
    r"\b(?:building|bldg|block|مبنى|مبني|عمارة)\s*[:#]?\s*"
    r"([A-Z]?-?\d{1,3}|[A-Z]\b)", re.I)
_FLOOR_RE = re.compile(
    r"\b(?:floor|level|storey|story|دور|طابق|منسوب)\s*[:#]?\s*"
    r"(\d{1,3}|ground|g|basement|b\d?|roof|أرضي|ارضي|بدروم|سطح)\b", re.I)
_ROOM_RE = re.compile(r"\b(?:room|ward|unit|غرفة|غرفه|جناح)\s*[:#]?\s*([A-Z]?-?\d{1,4})\b", re.I)
_WEIGHT_RE = re.compile(r"\b(\d{1,6}(?:\.\d+)?)\s*(kg|kgs|كجم|ton|tons|طن)\b", re.I)
_CSI_RE = re.compile(r"\bsection\s*[-:]?\s*(\d{2})\b", re.I)


def extract_tokens(text):
    """يستخرج من نصّ البند كلّ ما ينفع في التفكيك."""
    t = norm(text)
    if not t:
        return {}
    out = {}
    low = t.lower()

    mats = {name for name, keys in MATERIALS.items()
            if any(k in low for k in keys)}
    # الأخصّ يبتلع الأعمّ: ‎uPVC‎ يطابق ‎pvc‎ أيضًا، فيُذكر الاثنان وهما واحد
    for specific, generic in (("upvc", "pvc"), ("stainless_steel", "steel"),
                              ("ductile_iron", "steel"), ("cast_iron", "steel"),
                              ("ppr", "pipe"), ("hdpe", "pipe"),
                              ("upvc", "pipe"), ("conduit", "pipe")):
        if specific in mats:
            mats.discard(generic)
    if mats:
        out["materials"] = ", ".join(sorted(mats))

    for col, rx, grp in _TOK:
        if out.get(col):
            continue
        m = rx.search(t)
        if m:
            out[col] = norm(m.group(grp) if grp else m.group(0))

    for col, rx in (("building", _BUILDING_RE), ("floor", _FLOOR_RE),
                    ("room", _ROOM_RE)):
        m = rx.search(t)
        if m:
            out[col] = norm(m.group(1))

    m = _WEIGHT_RE.search(t)
    if m:
        out["weight"] = norm(m.group(0))
    m = _CSI_RE.search(t)
    if m:
        out["csi_division"] = m.group(1)
    return out


# ═══════════════════════════ كشف الترويسة والأعمدة ═══════════════════════════

_ROLE_KEYS = {
    "item_no":  ["البند", "item", "itemno", "s.n", "sn", "no", "رقم", "سريال"],
    "desc":     ["الوصف", "description", "descriptionitem", "بيانالاعمال",
                 "البيان", "الاعمال", "particulars"],
    "unit":     ["الوحدة", "unit", "uom", "الوحده"],
    "qty":      ["الكمية", "quantity", "qty", "totalquantity", "بالارقام",
                 "الكميه", "qty."],
    "rate":     ["السعرالافرادي", "unitrate", "rate", "price", "pricesr",
                 "السعر", "الفئة", "unitprice"],
    "amount":   ["السعرالاجمالي", "amount", "totalamt", "total", "amountsar",
                 "الاجمالي", "amountaspercontract"],
    "duration": ["duration", "days", "المدة", "مدة"],
    "category": ["category", "التصنيف", "القسم", "bill"],
    "notes":    ["notes", "note", "ملاحظات", "remarks"],
}
_ROLE_LOOKUP = {norm_key(a): r for r, al in _ROLE_KEYS.items() for a in al}


def _role_of(label):
    k = norm_key(label)
    if not k:
        return None
    if k in _ROLE_LOOKUP:
        return _ROLE_LOOKUP[k]
    for key, role in _ROLE_LOOKUP.items():
        if len(key) >= 4 and key in k:
            return role
    return None


def detect_header(rows, max_scan=20):
    """
    يجد صفّ الترويسة ويبني خريطة الأعمدة.

    يتعامل مع ترويسة سطرين (عربي فوق إنجليزي) بدمج نصّ العمودين،
    ومع تكرار زوج (سعر، إجمالي) لأكثر من مقاول.
    يعيد (رقم آخر صفّ ترويسة، خريطة الأدوار، قائمة كتل الأسعار).
    """
    best, best_score = None, 0
    for i in range(min(max_scan, len(rows))):
        for span in (1, 2):                       # سطر واحد أو سطران
            if i + span > len(rows):
                continue
            merged = []
            width = max(len(rows[i + k]) for k in range(span))
            for c in range(width):
                parts = [norm(rows[i + k][c]) for k in range(span)
                         if c < len(rows[i + k])]
                merged.append(" ".join(p for p in parts if p))
            roles = []
            for m in merged:
                r = _role_of(m)
                # عمود تقدّم أو أمر تغييري: ليس سعرًا ولا كمّية تعاقدية
                if r in ("rate", "amount", "qty") and _PROGRESS_HINTS.search(m):
                    r = None
                roles.append(r)
            found = {r for r in roles if r}
            score = len(found) + (2 if {"qty", "desc"} <= found else 0) \
                                + (1 if "unit" in found else 0)
            if score > best_score:
                best_score, best = score, (i, span, merged, roles)
    if not best or best_score < 4:
        return None, {}, []

    i, span, merged, roles = best
    colmap, price_blocks = {}, []
    rate_cols = [c for c, r in enumerate(roles) if r == "rate"]
    amt_cols = [c for c, r in enumerate(roles) if r == "amount"]

    for c, r in enumerate(roles):
        if r in ("rate", "amount"):
            continue
        colmap.setdefault(r, c) if r else None

    # أزواج (سعر، إجمالي) بالترتيب — واحد لكل جهة مسعِّرة
    for n, rc in enumerate(rate_cols):
        ac = next((a for a in amt_cols if a > rc), None)
        price_blocks.append({"rate_col": rc, "amount_col": ac, "name": ""})
    if not rate_cols and amt_cols:
        price_blocks.append({"rate_col": None, "amount_col": amt_cols[0],
                             "name": ""})

    # اسم كل جهة مسعِّرة من الصفوف القليلة التي فوق الترويسة مباشرة.
    # المسح البعيد يلتقط عناوين المشروع وتواريخ الوثائق فتصير «مقاولين».
    if len(price_blocks) > 1:
        for blk in price_blocks:
            rc = blk["rate_col"]
            if rc is None:
                continue
            for up in range(i - 1, max(-1, i - 5), -1):
                row = rows[up]
                for c in (rc, rc - 1, rc + 1):
                    if 0 <= c < len(row):
                        v = norm(row[c])
                        if (v and not _role_of(v) and 2 < len(v) < 30
                                and num(v) is None
                                and not re.search(r"\d{2}[-/]\d{2}", v)):
                            blk["name"] = v
                            break
                if blk["name"]:
                    break

    return i + span - 1, colmap, price_blocks


def detect_code_col(rows, start, colmap):
    """عمود كود النشاط بلا ترويسة — يُكتشف بالمحتوى لا بالاسم."""
    taken = set(colmap.values())
    width = max((len(r) for r in rows[start:]), default=0)
    best, best_hits = None, 0
    for c in range(width):
        if c in taken:
            continue
        hits = sum(1 for r in rows[start:]
                   if c < len(r) and _CODE_RE.match(norm(r[c]).upper()))
        if hits > best_hits:
            best, best_hits = c, hits
    return best if best_hits >= 3 else None


# ═══════════════════════════ تصنيف الصفوف ═══════════════════════════

ITEM, GROUP, TOTAL, CONT, BLANK, NOTE = (
    "item", "group", "total", "cont", "blank", "note")

# بنود فرعية بترقيم روماني صغير: ‎i‎ ، ‎ii‎ ، ‎iii‎ — مواصفات لا بنود مسعّرة
_ROMAN_RE = re.compile(r"^\(?[ivx]{1,5}\)?[.)]?$", re.I)
_NOTE_RE = re.compile(r"^\s*(note[ds]?\s*[:：]|ملاحظ[ةه]\s*[:：]|n\.b\.)", re.I)


def classify(desc, qty, rate, amount, code, item_no, prev_kind, prev_en=False):
    """
    يقرّر نوع الصفّ. لا يُترك صفّ بلا تصنيف.

    ‎prev_en‎: هل وصف البند السابق بغير العربية — فالوصف العربي بعده استكمالٌ له
    لا عنوان مجموعة. هذا هو شكل الجداول ثنائية اللغة: سطر إنجليزي ثم ترجمته.
    """
    desc_m = detat(desc)          # صيغة المطابقة: بلا تطويل
    has_desc = bool(desc)
    # الصفر ليس رقمًا هنا. عمود الإجمالي في الجداول متعدّدة المسعِّرين يحمل أصفارًا
    # في كلّ صفّ — حتى صفوف العناوين — فلو عُدَّ الصفر رقمًا صار كلُّ عنوانٍ بندًا.
    # البند الذي سعره صفر حقًّا تبقى له كمّية، وهي التي تُثبته.
    has_num = (qty is not None
               or (rate is not None and rate != 0)
               or (amount is not None and amount != 0))
    if not has_desc and not has_num and not code:
        return BLANK
    if _TOTAL_HINTS.search(desc_m) and qty is None:
        return TOTAL
    if item_no and _TOTAL_HINTS.search(detat(item_no)) and qty is None:
        return TOTAL               # ‎TOTAL‎ و‎VAT 15%‎ تُكتب في عمود البند
    # عنوان قسم يحمل مجموعه في خانة الإجمالي — يُكتب هكذا في بعض الجداول.
    # البند الحقيقي له كمّية أو كود دائمًا، فغيابهما مع عنوانٍ صريح يحسم الأمر.
    if _GROUP_HINTS.search(desc_m) and qty is None and not code:
        return GROUP
    if code or qty is not None:
        return ITEM
    if has_desc and not has_num:
        if _GROUP_HINTS.search(desc_m):
            return GROUP
        if prev_kind in (ITEM, CONT) and not item_no and is_ar(desc) and prev_en:
            return CONT            # الوصف العربي تحت البند الإنجليزي
        if prev_kind in (ITEM, CONT, NOTE) and (
                _NOTE_RE.search(desc_m) or _ROMAN_RE.match(item_no or "")
                or (len(desc) > 55 and not desc.isupper())):
            return NOTE            # مواصفة أو ملاحظة ملحقة بالبند السابق
        if desc.isupper() and len(desc) > 3:
            return GROUP
        return GROUP
    if amount is not None and not has_desc:
        return BLANK
    return ITEM if has_num else GROUP


# ═══════════════════════════ التحليل ═══════════════════════════

OUT_COLS = [
    "source_file", "source_sheet", "source_row", "price_source",
    "item_no", "activity_code", "item_name", "item_name_ar",
    "unit_raw", "unit", "qty", "unit_rate", "amount", "amount_in_file",
    "amount_check", "duration_days",
    "code_project", "code_area", "code_discipline", "discipline_name",
    "code_work_type", "work_type_name", "code_seq",
    "bill", "section", "group_l1", "group_l2", "group_l3", "category",
    "csi_division", "building", "floor", "room",
    "materials", "dimensions", "diameter", "thickness", "capacity",
    "weight", "tag_code", "spec_ref", "notes", "spec_notes", "row_kind",
]


def parse_sheet(rows, file_name, sheet_name):
    """يحلّل ورقة واحدة. يعيد (البنود، صفوف المجموعات، ما لم يُفهم، تشخيص)."""
    hdr, colmap, blocks = detect_header(rows)
    diag = {"file": file_name, "sheet": sheet_name, "header_row": None,
            "columns": {}, "price_sources": [], "rows": len(rows),
            "items": 0, "groups": 0, "totals": 0, "unparsed": 0,
            "amount_mismatch": 0, "unknown_units": Counter()}
    if hdr is None:
        diag["error"] = "لم تُعرَف ترويسة الجدول"
        return [], [], [], diag

    start = hdr + 1
    code_col = detect_code_col(rows, start, colmap)
    diag.update(header_row=hdr + 1, columns=dict(colmap),
                code_col=code_col,
                price_sources=[b["name"] or f"#{i+1}"
                               for i, b in enumerate(blocks)])

    def cell(row, key):
        c = colmap.get(key)
        return row[c] if c is not None and c < len(row) else None

    items, groups, unparsed = [], [], []
    stack = ["", "", ""]          # مستويات المجموعات المتداخلة
    bill = section = ""
    prev_kind, last, prev_en = BLANK, None, False
    sibling = []                  # صفوف البند الأخير، واحد لكلّ جهة مسعِّرة
    pending_no = ""               # رقم بند كُتب في سطر العنوان فوق الكمّية

    for idx in range(start, len(rows)):
        row = rows[idx]
        rno = idx + 1
        desc = norm(cell(row, "desc"))
        item_no = norm(cell(row, "item_no"))
        qty = num(cell(row, "qty"))
        code = norm(row[code_col]).upper() if (
            code_col is not None and code_col < len(row)) else ""
        if code and not _CODE_RE.match(code):
            code = ""
        rate = amt = None
        for b in blocks:
            if b["rate_col"] is not None and b["rate_col"] < len(row):
                rate = rate if rate is not None else num(row[b["rate_col"]])
            if b["amount_col"] is not None and b["amount_col"] < len(row):
                amt = amt if amt is not None else num(row[b["amount_col"]])
            if rate is not None or amt is not None:
                break
        if not desc and item_no and not qty and len(item_no) > 12:
            desc, item_no = item_no, ""       # الوصف انزاح لعمود البند

        # صدى الترويسة: السطر الثاني من ترويسة ثنائية اللغة لم يُضمّ إليها
        if desc and qty is None and _role_of(desc) and idx <= start + 2:
            prev_kind = BLANK
            continue

        kind = classify(desc, qty, rate, amt, code, item_no, prev_kind, prev_en)

        if kind == BLANK:
            prev_kind = kind
            continue

        if kind == TOTAL:
            diag["totals"] += 1
            prev_kind = kind
            continue

        if kind == CONT and last is not None:
            for rec in sibling:
                rec["item_name_ar"] = norm(
                    (rec.get("item_name_ar", "") + " " + desc).strip())
            prev_kind = kind
            continue

        if kind == NOTE:
            diag["notes"] = diag.get("notes", 0) + 1
            for rec in sibling:
                rec["spec_notes"] = norm(
                    (rec.get("spec_notes", "") + " ⏎ " + desc).strip(" ⏎"))
            prev_kind = kind
            continue

        if kind == GROUP:
            diag["groups"] += 1
            low = desc.lower()
            if re.search(r"bill\s*\(?\s*\d|bill\s*no", low):
                bill, stack = desc, ["", "", ""]
            elif re.search(r"^section|^القسم|^part\s*\d", low) or _CSI_RE.search(desc):
                section, stack = desc, ["", "", ""]
            else:
                lvl = 0 if not stack[0] else (1 if not stack[1] else 2)
                if desc.isupper():
                    lvl, stack = 0, ["", "", ""]
                stack[lvl] = desc
                for k in range(lvl + 1, 3):
                    stack[k] = ""
            groups.append({"source_file": file_name, "source_sheet": sheet_name,
                           "source_row": rno, "text": desc,
                           "bill": bill, "section": section,
                           "l1": stack[0], "l2": stack[1], "l3": stack[2]})
            if item_no and not _ROMAN_RE.match(item_no):
                pending_no = item_no       # ‎7.7 External Fence‎ فوق صفّ الكمّية
            prev_kind = kind
            continue

        # ── بند ──
        if not desc and not code:
            unparsed.append({"source_file": file_name, "source_sheet": sheet_name,
                             "source_row": rno, "reason": "بند بلا وصف ولا كود",
                             "raw": " | ".join(norm(v) for v in row)[:300]})
            diag["unparsed"] += 1
            prev_kind = prev_kind
            continue

        u_raw = norm(cell(row, "unit"))
        u, known = unit_norm(u_raw)
        if u_raw and not known:
            diag["unknown_units"][u_raw] += 1

        base = {c: "" for c in OUT_COLS}
        base.update(
            source_file=file_name, source_sheet=sheet_name, source_row=rno,
            row_kind=ITEM,
            item_no=item_no or pending_no, item_name=desc,
            unit_raw=u_raw, unit=u,
            qty=qty, duration_days=num(cell(row, "duration")),
            category=norm(cell(row, "category")), notes=norm(cell(row, "notes")),
            bill=bill, section=section,
            group_l1=stack[0], group_l2=stack[1], group_l3=stack[2])
        base.update({k: v for k, v in parse_code(code).items() if k in base})
        for k, v in extract_tokens(desc + " " + base["category"]).items():
            if k in base and not base[k]:
                base[k] = v
        if not base["csi_division"]:
            m = _CSI_RE.search(base["category"] or section)
            if m:
                base["csi_division"] = m.group(1)

        # صفّ لكل جهة مسعِّرة — هذا هو الشكل الجاهز للـUnpivot
        made = False
        sibling = []
        pending_no = ""
        for n, b in enumerate(blocks):
            r_ = num(row[b["rate_col"]]) if (
                b["rate_col"] is not None and b["rate_col"] < len(row)) else None
            a_ = num(row[b["amount_col"]]) if (
                b["amount_col"] is not None and b["amount_col"] < len(row)) else None
            if r_ is None and a_ is None:
                continue
            rec = dict(base)
            rec["price_source"] = b["name"] or (f"#{n+1}" if len(blocks) > 1 else "")
            rec["unit_rate"] = r_
            rec["amount_in_file"] = a_
            if qty is not None and r_ is not None:
                rec["amount"] = _tidy(qty * r_)
                if a_ is not None and abs(rec["amount"] - a_) > max(
                        0.5, abs(a_) * 1e-4):
                    rec["amount_check"] = "مختلف"
                    diag["amount_mismatch"] += 1
                else:
                    rec["amount_check"] = "مطابق"
            elif a_ is not None:
                rec["amount"] = a_
                rec["amount_check"] = "سعر مفقود"
                if qty:
                    rec["unit_rate"] = _tidy(a_ / qty) if qty else None
            items.append(rec)
            sibling.append(rec)
            made = True
        if not made:
            rec = dict(base)
            rec["amount_check"] = "بلا سعر"
            items.append(rec)
            sibling.append(rec)

        diag["items"] += 1
        last, prev_kind, prev_en = items[-1], ITEM, not is_ar(desc)

    _audit_blocks(items, blocks, diag)
    return items, groups, unparsed, diag


def _audit_blocks(items, blocks, diag):
    """
    يمتحن عمود الإجمالي حسابيًّا بدل تصديق عنوانه.

    في ملفّ حقيقي كان العمود المعنون ‎Amount As Per Contract‎ يحمل **كمّية**
    لا مبلغًا (تأكّد بأنّ الكمّية التعاقدية + كمّية الأمر التغييري = الكمّية
    الكلّية بالضبط). العنوان يكذب، والحساب لا يكذب.
    """
    by_src = {}
    for r in items:
        by_src.setdefault(r["price_source"], []).append(r)
    diag["blocks"] = []
    for src, rows in by_src.items():
        checked = [r for r in rows if r["amount_check"] in ("مطابق", "مختلف")]
        ok = sum(1 for r in checked if r["amount_check"] == "مطابق")
        zero_rate = sum(1 for r in rows
                        if (r["unit_rate"] in (0, 0.0))
                        and r["amount_in_file"] not in (None, "", 0, 0.0))
        ratio = (ok / len(checked)) if checked else None
        suspect = ratio is not None and len(checked) >= 5 and ratio < 0.5
        if suspect:
            for r in rows:
                if r["amount_check"] == "مختلف":
                    r["amount_check"] = "عمود الإجمالي مشكوك فيه"
        diag["blocks"].append({
            "price_source": src or "(واحد)", "rows": len(rows),
            "checked": len(checked), "match": ok,
            "match_pct": None if ratio is None else round(ratio * 100, 1),
            "zero_rate_with_amount": zero_rate, "suspect_amount_col": suspect})


def parse_workbook(path, file_name=None):
    """يحلّل ملفّ إكسل كاملًا بكل أوراقه."""
    import openpyxl
    import os
    file_name = file_name or os.path.basename(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    items, groups, unparsed, diags = [], [], [], []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if not rows:
            continue
        it, gr, up, dg = parse_sheet(rows, file_name, ws.title)
        items += it
        groups += gr
        unparsed += up
        diags.append(dg)
    wb.close()
    return items, groups, unparsed, diags
