# -*- coding: utf-8 -*-
"""
القواميس — مكتبة الشركة التي يُبنى منها كلّ برنامج زمني.

تُبنى مرّة وتُستعمل في كلّ مشروع وتكبر مع الوقت. وتقسيمة المشروع (المناطق
والمباني والأدوار) ليست منها: تلك لكلّ مشروع وحده، والأنشطة تُسكَّن فيها
بحسب تصنيفها من هنا.

| القاموس        | الجدول                                   |
|----------------|------------------------------------------|
| CSI Divisions  | ‎gen_lib_csi‎ — ثابتة، تُقرأ ولا تُحرَّر      |
| Disciplines    | ‎gen_lib_disciplines‎                       |
| Work types     | ‎gen_lib_work_types‎ + خطواته وأوزانها       |
| Logic rules    | ‎gen_lib_logic‎ — تُحرَّر من مسارات المولّد    |
| Engineering    | ‎gen_lib_extras‎ بنوع ‎eng‎                   |
| T&C            | ‎gen_lib_extras‎ بنوع ‎tc‎                    |
| Materials      | ‎gen_lib_materials‎ + ‎gen_lib_wt_materials‎   |
| Resources      | ‎gen_lib_resources‎ + معدّلات ‎gen_lib_rates‎  |

والحقل الذي يعني «كلّ نوع العمل لا خطوة بعينها» يُخزَّن نصًّا فارغًا لا
‎NULL‎: بوستجرس لا يعدّ ‎NULL‎ مساويًا لـ‎NULL‎ في القيود الفريدة، وقد ضاعف
ذلك قواعد الربط مرّة من تسع عشرة إلى ثمان وثلاثين.
"""

import json
import traceback
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

router = APIRouter()
_get_conn = None
_templates = None


def setup(get_conn, tpl):
    global _get_conn, _templates
    _get_conn, _templates = get_conn, tpl


def _admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


def _deny():
    return JSONResponse({"success": False, "error": "Not authorised"},
                        status_code=403)


def _fail(e):
    return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-800:]}, status_code=500)


def _plain(v):
    from decimal import Decimal
    from datetime import date, datetime
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _batch(cur, sql, rows, template=None, size=500):
    if not rows:
        return
    from psycopg2.extras import execute_values
    for i in range(0, len(rows), size):
        execute_values(cur, sql, rows[i:i + size], template=template,
                       page_size=size)


def _num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _code(v):
    return (str(v or "")).strip().upper()


def _words(v):
    """الكلمات المفتاحية: قائمة أو نصّ مفصول بفواصل، مُنظَّفة ومكرّرها محذوف."""
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    out, seen = [], set()
    for w in v or []:
        w = str(w).strip().lower()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


# ═══════════════════════════ البذرة ═══════════════════════════
# قيمٌ مبدئيّة تُزرع مرّة حين تكون الجداول فارغة، ومعلَّمة ‎seed‎ ليُعرف أنّها
# ليست أرقام الشركة. الغرض أن تُفتح الصفحة على مثال يُفهم منه كيف تُملأ، لا
# أن تُعتمد هذه الأرقام: معدّلات الأداء وأسعار الساعة تختلف من شركة لأخرى.

SEED_DISCIPLINES = [
    ("CIV", "Civil & Structural", "03", 10),
    ("ARC", "Architectural", "09", 20),
    ("MEP", "Mechanical, Electrical & Plumbing", "23", 30),
    ("ELE", "Electrical", "26", 40),
    ("PLB", "Plumbing", "22", 50),
    ("FPS", "Fire Protection", "21", 60),
    ("SIT", "Sitework", "31", 70),
    ("INF", "Infrastructure", "33", 80),
]

SEED_RESOURCES = [
    # code, name, kind, cost per hour (SAR)
    ("FRM", "Foreman", "labor", 45), ("CRP", "Carpenter", "labor", 22),
    ("STF", "Steel fixer", "labor", 22), ("MSN", "Mason", "labor", 22),
    ("PLR", "Plasterer", "labor", 22), ("TLR", "Tiler", "labor", 24),
    ("PNR", "Painter", "labor", 20), ("ELC", "Electrician", "labor", 26),
    ("PLM", "Plumber", "labor", 26), ("LAB", "Labourer", "labor", 14),
    ("EXV", "Excavator", "equipment", 180), ("LDR", "Loader", "equipment", 150),
    ("DMP", "Dump truck", "equipment", 120), ("CRN", "Mobile crane", "equipment", 350),
    ("PMP", "Concrete pump", "equipment", 300), ("CMP", "Compactor", "equipment", 90),
]

SEED_MATERIALS = [
    # code, name, unit, unit cost, lead time (days), keywords
    ("CONC", "Ready-mix concrete", "m3", 250, 7, "ready mix,concrete"),
    ("RBR", "Reinforcement steel", "t", 2800, 21, "rebar,reinforcement,steel bars"),
    ("BLK", "Concrete block", "no", 3.5, 7, "block"),
    ("TILE", "Porcelain tile", "m2", 45, 45, "porcelain,ceramic,tile"),
    ("PNT", "Emulsion paint", "l", 18, 7, "paint,emulsion"),
    ("GYP", "Gypsum board", "m2", 30, 30, "gypsum,board"),
]

# ‎(نوع العمل: الوحدة، الكلمات، الخطوات، المعدّلات، المواد، الهندسة، T&C)‎
SEED_WT = {
    "SITE": ("m2", "site preparation,clearing,grubbing,leveling,asphalt,paving,landscaping",
             [], [("", "LDR", 0.01, 1, False), ("", "LAB", 0.10, 4, False)],
             [], [], []),
    "EXC": ("m3", "excavation,excavate,backfill,trench",
            [("EXC", "Excavation", 80, "FS", 0), ("BKF", "Backfill & compaction", 20, "SS", 2)],
            [("EXC", "EXV", 0.03, 1, True), ("EXC", "DMP", 0.05, 2, False),
             ("BKF", "CMP", 0.05, 1, True), ("BKF", "LAB", 0.10, 2, False)],
            [], [], []),
    "FND": ("m3", "footing,foundation,raft,pile cap,tie beam,blinding",
            [("FRM", "Formwork", 35, "FS", 0), ("RBR", "Reinforcement", 30, "SS", 1),
             ("CON", "Pour & cure", 35, "FS", 0)],
            [("FRM", "CRP", 3.0, 6, True), ("RBR", "STF", 2.5, 6, True),
             ("CON", "LAB", 1.5, 8, False), ("CON", "PMP", 0.05, 1, False)],
            [("CONC", 1.02), ("RBR", 0.10)],
            [("SD", "Shop drawings", 14, 10), ("SUB", "Submittal", 7, 20),
             ("APP", "Approval", 14, 30)], []),
    "STR": ("m3", "reinforced concrete,column,slab,beam,shear wall,staircase,concrete",
            [("FRM", "Formwork", 35, "FS", 0), ("RBR", "Reinforcement", 30, "SS", 1),
             ("CON", "Pour & cure", 35, "FS", 0)],
            [("FRM", "CRP", 3.0, 8, True), ("RBR", "STF", 2.5, 8, True),
             ("CON", "LAB", 1.5, 10, False), ("CON", "PMP", 0.05, 1, False)],
            [("CONC", 1.02), ("RBR", 0.12)],
            [("SD", "Shop drawings", 14, 10), ("SUB", "Submittal", 7, 20),
             ("APP", "Approval", 14, 30)], []),
    "BLK": ("m2", "block,blockwork,masonry",
            [], [("", "MSN", 0.8, 6, True), ("", "LAB", 0.6, 4, False)],
            [("BLK", 12.5)], [], []),
    "MEP": ("no", "pipe,piping,conduit,duct,cable,sleeve,first fix",
            [], [("", "PLM", 1.5, 4, True), ("", "ELC", 1.5, 4, False)],
            [], [("SD", "Shop drawings", 21, 10), ("SUB", "Material submittal", 7, 20),
                 ("APP", "Approval", 14, 30)],
            [("PT", "Pressure test", 2, "floor", 10),
             ("TC", "Testing & commissioning", 10, "building", 20)]),
    "PLS": ("m2", "plaster,plastering,render",
            [], [("", "PLR", 0.5, 6, True), ("", "LAB", 0.3, 3, False)], [], [], []),
    "CEI": ("m2", "ceiling,false ceiling,suspended ceiling",
            [], [("", "CRP", 0.6, 4, True)], [("GYP", 1.05)],
            [("SUB", "Material submittal", 7, 10), ("APP", "Approval", 14, 20)], []),
    "TIL": ("m2", "tile,tiling,porcelain,ceramic,marble,skirting",
            [("SCR", "Screed", 40, "FS", 0), ("TIL", "Tiling", 60, "SS", 2)],
            [("SCR", "MSN", 0.4, 4, True), ("TIL", "TLR", 0.9, 6, True),
             ("TIL", "LAB", 0.4, 3, False)],
            [("TILE", 1.05)],
            [("SUB", "Material submittal", 7, 10), ("APP", "Approval", 14, 20)], []),
    "DOR": ("no", "door,doors,frame,ironmongery",
            [], [("", "CRP", 4.0, 2, True)], [],
            [("SD", "Shop drawings", 14, 10), ("APP", "Approval", 14, 20)], []),
    "PNT": ("m2", "paint,painting,emulsion,primer",
            [("PRP", "Putty & primer", 50, "FS", 0), ("FIN", "Final coats", 50, "SS", 3)],
            [("PRP", "PNR", 0.15, 6, True), ("FIN", "PNR", 0.12, 6, True)],
            [("PNT", 0.3)], [], []),
}


# أنواع الأعمال نفسها تُزرع فقط حين يكون الكتالوج فارغًا تمامًا.
SEED_WT_BASE = [
    # code, name, discipline, csi, unit, default days, applies to
    ("SITE", "Site works", "SIT", "31", "m2", 10, ["zone"]),
    ("EXC", "Excavation", "SIT", "31", "m3", 15, ["building"]),
    ("FND", "Foundations", "CIV", "03", "m3", 25, ["building"]),
    ("STR", "Concrete structure", "CIV", "03", "m3", 12, ["floor"]),
    ("BLK", "Blockwork", "ARC", "04", "m2", 8, ["floor"]),
    ("MEP", "MEP first fix", "MEP", "23", "no", 10, ["floor"]),
    ("PLS", "Plastering", "ARC", "09", "m2", 7, ["floor"]),
    ("CEI", "Suspended ceiling", "ARC", "09", "m2", 6, ["floor"]),
    ("TIL", "Tiling", "ARC", "09", "m2", 6, ["floor"]),
    ("DOR", "Doors", "ARC", "08", "no", 4, ["floor"]),
    ("PNT", "Final paint", "ARC", "09", "m2", 5, ["floor"]),
]


# قواعد مبدئيّة لأنواع الأعمال المزروعة، تُزرع حين تكون المكتبة بلا قاعدة:
# بدونها يخرج أوّل توليد أنشطةً بلا علاقة واحدة.
SEED_RULES = [
    ("SITE", "EXC", "children"), ("EXC", "FND", "same"), ("FND", "STR", "children"),
    ("STR", "BLK", "same"), ("BLK", "MEP", "same"), ("MEP", "PLS", "same"),
    ("PLS", "CEI", "same"), ("CEI", "TIL", "same"), ("TIL", "DOR", "same"),
    ("DOR", "PNT", "same"), ("STR", "BLK", "prev"),
    ("STR", "STR", "next"), ("BLK", "BLK", "next"), ("MEP", "MEP", "next"),
    ("PLS", "PLS", "next"), ("CEI", "CEI", "next"), ("TIL", "TIL", "next"),
    ("DOR", "DOR", "next"), ("PNT", "PNT", "next"),
]


def _seed(cur):
    """تُزرع كلّ مجموعة فقط إن كان جدولها فارغًا، فلا تُكتب فوق بيانات الشركة."""
    def empty(t):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        return not cur.fetchone()[0]

    if empty("gen_lib_disciplines"):
        _batch(cur, "INSERT INTO gen_lib_disciplines (code,name,csi_division,seq) "
                    "VALUES %s ON CONFLICT (code) DO NOTHING", SEED_DISCIPLINES)
    if empty("gen_lib_work_types"):
        _batch(cur, "INSERT INTO gen_lib_work_types (code,name,discipline,csi_division,"
                    "unit,default_days,applies_to,seq,origin) VALUES %s "
                    "ON CONFLICT (code) DO NOTHING",
               [(c, n, d, csi, u, days, json.dumps(ap), i * 10, "seed")
                for i, (c, n, d, csi, u, days, ap) in enumerate(SEED_WT_BASE)],
               template="(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)")
    if empty("gen_lib_logic"):
        _batch(cur, "INSERT INTO gen_lib_logic (pred_wt,succ_wt,rel_type,lag_days,scope,"
                    "origin,active) VALUES %s ON CONFLICT DO NOTHING",
               [(p, q, "FS", 0, sc, "manual", True) for p, q, sc in SEED_RULES])
    if empty("gen_lib_resources"):
        _batch(cur, "INSERT INTO gen_lib_resources (code,name,kind,unit_cost,seq,origin) "
                    "VALUES %s ON CONFLICT (code) DO NOTHING",
               [(c, n, k, u, i * 10, "seed")
                for i, (c, n, k, u) in enumerate(SEED_RESOURCES)])
    if empty("gen_lib_materials"):
        _batch(cur, "INSERT INTO gen_lib_materials "
                    "(code,name,unit,unit_cost,lead_time_days,keywords,seq,origin) "
                    "VALUES %s ON CONFLICT (code) DO NOTHING",
               [(c, n, u, p, lt, json.dumps(_words(k)), i * 10, "seed")
                for i, (c, n, u, p, lt, k) in enumerate(SEED_MATERIALS)],
               template="(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)")

    # نوع العمل بلا قسم CSI يرث قسم تخصّصه، فلا يبقى وزنه خارج كلّ الأقسام.
    cur.execute("""UPDATE gen_lib_work_types w SET csi_division = d.csi_division
                   FROM gen_lib_disciplines d
                   WHERE w.discipline = d.code AND d.csi_division IS NOT NULL
                     AND COALESCE(w.csi_division, '') = ''""")

    # الخطوات والمعدّلات والمواد والهندسة لأنواع الأعمال الموجودة فقط، ولا
    # يُزرع شيء منها لنوع عملٍ عرّفت له الشركة شيئًا من قبل.
    cur.execute("SELECT code, unit, keywords FROM gen_lib_work_types")
    have = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for wt, (unit, kw, steps, rates, mats, eng, tc) in SEED_WT.items():
        if wt not in have:
            continue
        cur_unit, cur_kw = have[wt]
        if not cur_unit:
            cur.execute("UPDATE gen_lib_work_types SET unit=%s WHERE code=%s", (unit, wt))
        if not cur_kw:
            cur.execute("UPDATE gen_lib_work_types SET keywords=%s::jsonb WHERE code=%s",
                        (json.dumps(_words(kw)), wt))
        cur.execute("SELECT COUNT(*) FROM gen_lib_wt_steps WHERE work_type=%s", (wt,))
        if not cur.fetchone()[0] and steps:
            _batch(cur, "INSERT INTO gen_lib_wt_steps "
                        "(work_type,code,name,weight,rel_type,lag_days,seq) VALUES %s",
                   [(wt, c, n, w, rt, lg, i * 10)
                    for i, (c, n, w, rt, lg) in enumerate(steps)])
        cur.execute("SELECT COUNT(*) FROM gen_lib_rates WHERE work_type=%s", (wt,))
        if not cur.fetchone()[0] and rates:
            _batch(cur, "INSERT INTO gen_lib_rates "
                        "(work_type,step,resource,hours_per_unit,crew_count,driving) "
                        "VALUES %s", [(wt,) + r for r in rates])
        cur.execute("SELECT COUNT(*) FROM gen_lib_wt_materials WHERE work_type=%s", (wt,))
        if not cur.fetchone()[0] and mats:
            _batch(cur, "INSERT INTO gen_lib_wt_materials (work_type,material,qty_per_unit) "
                        "VALUES %s", [(wt,) + m for m in mats])
        cur.execute("SELECT COUNT(*) FROM gen_lib_extras WHERE work_type=%s", (wt,))
        if not cur.fetchone()[0] and (eng or tc):
            rows = [("eng", wt, c, n, d, None, s, "FS", 0) for c, n, d, s in eng]
            rows += [("tc", wt, c, n, d, lvl, s, "FS", 0) for c, n, d, lvl, s in tc]
            _batch(cur, "INSERT INTO gen_lib_extras "
                        "(kind,work_type,code,name,duration_days,level_key,seq,"
                        "rel_type,lag_days) VALUES %s", rows)


# ═══════════════════════════ الجداول ═══════════════════════════

def ensure_schema(cur):
    """يُنادى بعد ‎generator_api.ensure_schema‎: يعتمد على جداول المكتبة هناك."""
    cur.execute("ALTER TABLE gen_lib_work_types "
                "ADD COLUMN IF NOT EXISTS keywords JSONB NOT NULL DEFAULT '[]'::jsonb")
    cur.execute("ALTER TABLE gen_lib_disciplines "
                "ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'manual'")

    # خطوات نوع العمل وأوزانها. البند يُقسَّم بها حين يختار المستخدم ذلك،
    # والعلاقة المخزّنة على الخطوة هي علاقتها بالخطوة التي قبلها.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_wt_steps (
                       id SERIAL PRIMARY KEY,
                       work_type TEXT NOT NULL,
                       code TEXT NOT NULL,
                       name TEXT NOT NULL,
                       weight NUMERIC NOT NULL DEFAULT 0,
                       rel_type TEXT NOT NULL DEFAULT 'FS',
                       lag_days NUMERIC NOT NULL DEFAULT 0,
                       seq INTEGER DEFAULT 0,
                       UNIQUE (work_type, code))""")

    # الموارد: عمالة ومعدات بتكلفة الساعة.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_resources (
                       id SERIAL PRIMARY KEY,
                       code TEXT UNIQUE NOT NULL,
                       name TEXT NOT NULL,
                       kind TEXT NOT NULL DEFAULT 'labor',
                       unit_cost NUMERIC NOT NULL DEFAULT 0,
                       seq INTEGER DEFAULT 0,
                       origin TEXT DEFAULT 'manual')""")

    # معدّل الأداء: ساعات المورد لكلّ وحدة من نوع العمل، وعدده في الطاقم.
    # ‎step‎ الفارغ يعني المعدّل لنوع العمل كلّه.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_rates (
                       id SERIAL PRIMARY KEY,
                       work_type TEXT NOT NULL,
                       step TEXT NOT NULL DEFAULT '',
                       resource TEXT NOT NULL,
                       hours_per_unit NUMERIC NOT NULL DEFAULT 0,
                       crew_count NUMERIC NOT NULL DEFAULT 1,
                       driving BOOLEAN NOT NULL DEFAULT FALSE,
                       UNIQUE (work_type, step, resource))""")

    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_materials (
                       id SERIAL PRIMARY KEY,
                       code TEXT UNIQUE NOT NULL,
                       name TEXT NOT NULL,
                       unit TEXT,
                       unit_cost NUMERIC NOT NULL DEFAULT 0,
                       lead_time_days NUMERIC NOT NULL DEFAULT 0,
                       keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                       seq INTEGER DEFAULT 0,
                       origin TEXT DEFAULT 'manual')""")

    # استهلاك المادّة لكلّ وحدة من نوع العمل: ‎1.02 m3‎ خرسانة لكلّ ‎m3‎ هيكل.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_wt_materials (
                       id SERIAL PRIMARY KEY,
                       work_type TEXT NOT NULL,
                       material TEXT NOT NULL,
                       qty_per_unit NUMERIC NOT NULL DEFAULT 0,
                       UNIQUE (work_type, material))""")

    # الهندسة و‎T&C‎ في جدول واحد لأنّ شكلهما واحد: أنشطة ملحقة بنوع عمل.
    # الهندسة تسبق أوّل تنفيذ له مرّةً للمشروع، و‎T&C‎ تليه وتتكرّر على
    # المستوى المذكور في ‎level_key‎ (فارغٌ = مرّةً للمشروع).
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_extras (
                       id SERIAL PRIMARY KEY,
                       kind TEXT NOT NULL,
                       work_type TEXT NOT NULL,
                       code TEXT NOT NULL,
                       name TEXT NOT NULL,
                       duration_days NUMERIC NOT NULL DEFAULT 1,
                       level_key TEXT,
                       rel_type TEXT NOT NULL DEFAULT 'FS',
                       lag_days NUMERIC NOT NULL DEFAULT 0,
                       seq INTEGER DEFAULT 0,
                       UNIQUE (kind, work_type, code))""")

    _seed(cur)


# ═══════════════════════════ القراءة ═══════════════════════════

def load_all(cur):
    """كلّ القواميس في بنية واحدة — تقرؤها الصفحة ويقرؤها المولّد."""
    def rows(sql, args=()):
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]

    csi = rows("SELECT division, name FROM gen_lib_csi ORDER BY seq")
    disciplines = rows("""SELECT code, name, csi_division FROM gen_lib_disciplines
                          ORDER BY seq, code""")
    levels = rows("""SELECT key, label, token FROM gen_lib_levels
                     WHERE active ORDER BY seq, id""")
    work_types = rows("""SELECT code, name, discipline, csi_division, unit,
                                default_days AS days, applies_to, keywords,
                                calendar_hint, seq, origin
                         FROM gen_lib_work_types ORDER BY seq, code""")
    steps = rows("""SELECT work_type, code, name, weight, rel_type, lag_days, seq
                    FROM gen_lib_wt_steps ORDER BY work_type, seq, id""")
    rates = rows("""SELECT work_type, step, resource, hours_per_unit, crew_count,
                           driving FROM gen_lib_rates ORDER BY work_type, step, id""")
    wt_mats = rows("""SELECT work_type, material, qty_per_unit
                      FROM gen_lib_wt_materials ORDER BY work_type, id""")
    extras = rows("""SELECT kind, work_type, code, name, duration_days, level_key,
                            rel_type, lag_days, seq
                     FROM gen_lib_extras ORDER BY kind, work_type, seq, id""")
    materials = rows("""SELECT code, name, unit, unit_cost, lead_time_days,
                               keywords, seq, origin
                        FROM gen_lib_materials ORDER BY seq, code""")
    resources = rows("""SELECT code, name, kind, unit_cost, seq, origin
                        FROM gen_lib_resources ORDER BY kind, seq, code""")
    rules = rows("""SELECT pred_wt, succ_wt, rel_type, lag_days, scope, origin,
                           observations, agreement, sources
                    FROM gen_lib_logic
                    WHERE project_type IS NULL AND active
                    ORDER BY pred_wt, succ_wt, scope""")

    by = {}
    for w in work_types:
        w["steps"], w["rates"], w["materials"] = [], [], []
        by[w["code"]] = w
    for s in steps:
        if s["work_type"] in by:
            by[s["work_type"]]["steps"].append(s)
    for r in rates:
        if r["work_type"] in by:
            by[r["work_type"]]["rates"].append(r)
    for m in wt_mats:
        if m["work_type"] in by:
            by[m["work_type"]]["materials"].append(m)
    return {"csi": csi, "disciplines": disciplines, "levels": levels,
            "work_types": work_types, "extras": extras,
            "materials": materials, "resources": resources, "rules": rules}


# ═══════════════════════════ الكتابة ═══════════════════════════

REL_TYPES = ("FS", "SS", "FF", "SF")


def save_all(cur, d):
    """
    يستبدل القواميس المرسَلة بما فيها، في معاملة واحدة.

    كلّ مفتاح غائب عن ‎d‎ يُترك كما هو، فتحفظ الصفحة قاموسًا واحدًا دون أن
    تلمس الباقي. والتحقّق هنا لا في الصفحة وحدها: ما يدخل المكتبة يدخل كلّ
    مشروع بعدها.
    """
    problems = []

    if "disciplines" in d:
        rows, seen = [], set()
        for i, x in enumerate(d["disciplines"]):
            c = _code(x.get("code"))
            if not c or c in seen:
                continue
            seen.add(c)
            rows.append((c, (x.get("name") or c).strip(),
                         (x.get("csi_division") or "").strip() or None, i * 10))
        cur.execute("DELETE FROM gen_lib_disciplines WHERE NOT (code = ANY(%s))",
                    ([r[0] for r in rows],))
        _batch(cur, """INSERT INTO gen_lib_disciplines (code,name,csi_division,seq)
                       VALUES %s ON CONFLICT (code) DO UPDATE SET
                         name=EXCLUDED.name, csi_division=EXCLUDED.csi_division,
                         seq=EXCLUDED.seq""", rows)

    if "resources" in d:
        rows, seen = [], set()
        for i, x in enumerate(d["resources"]):
            c = _code(x.get("code"))
            if not c or c in seen:
                continue
            seen.add(c)
            kind = x.get("kind") if x.get("kind") in ("labor", "equipment") else "labor"
            rows.append((c, (x.get("name") or c).strip(), kind,
                         _num(x.get("unit_cost")), i * 10))
        cur.execute("DELETE FROM gen_lib_resources WHERE NOT (code = ANY(%s))",
                    ([r[0] for r in rows],))
        _batch(cur, """INSERT INTO gen_lib_resources (code,name,kind,unit_cost,seq)
                       VALUES %s ON CONFLICT (code) DO UPDATE SET
                         name=EXCLUDED.name, kind=EXCLUDED.kind,
                         unit_cost=EXCLUDED.unit_cost, seq=EXCLUDED.seq,
                         origin=CASE WHEN gen_lib_resources.name=EXCLUDED.name
                                      AND gen_lib_resources.kind=EXCLUDED.kind
                                      AND gen_lib_resources.unit_cost=EXCLUDED.unit_cost
                                     THEN gen_lib_resources.origin
                                     ELSE 'manual' END""", rows)

    if "materials" in d:
        rows, seen = [], set()
        for i, x in enumerate(d["materials"]):
            c = _code(x.get("code"))
            if not c or c in seen:
                continue
            seen.add(c)
            rows.append((c, (x.get("name") or c).strip(), (x.get("unit") or "").strip(),
                         _num(x.get("unit_cost")), _num(x.get("lead_time_days")),
                         json.dumps(_words(x.get("keywords"))), i * 10))
        cur.execute("DELETE FROM gen_lib_materials WHERE NOT (code = ANY(%s))",
                    ([r[0] for r in rows],))
        _batch(cur, """INSERT INTO gen_lib_materials
                         (code,name,unit,unit_cost,lead_time_days,keywords,seq)
                       VALUES %s ON CONFLICT (code) DO UPDATE SET
                         name=EXCLUDED.name, unit=EXCLUDED.unit,
                         unit_cost=EXCLUDED.unit_cost,
                         lead_time_days=EXCLUDED.lead_time_days,
                         keywords=EXCLUDED.keywords, seq=EXCLUDED.seq,
                         origin=CASE WHEN gen_lib_materials.unit_cost=EXCLUDED.unit_cost
                                      AND gen_lib_materials.lead_time_days=EXCLUDED.lead_time_days
                                      AND gen_lib_materials.name=EXCLUDED.name
                                     THEN gen_lib_materials.origin
                                     ELSE 'manual' END""", rows,
               template="(%s,%s,%s,%s,%s,%s::jsonb,%s)")

    if "work_types" in d:
        wts, seen = [], set()
        for i, x in enumerate(d["work_types"]):
            c = _code(x.get("code"))
            if not c or c in seen:
                continue
            seen.add(c)
            wts.append((c, x, i))
        codes = [c for c, _, _ in wts]
        cur.execute("DELETE FROM gen_lib_work_types WHERE NOT (code = ANY(%s))", (codes,))
        _batch(cur, """INSERT INTO gen_lib_work_types
                         (code,name,discipline,csi_division,unit,default_days,
                          applies_to,keywords,calendar_hint,seq)
                       VALUES %s ON CONFLICT (code) DO UPDATE SET
                         name=EXCLUDED.name, discipline=EXCLUDED.discipline,
                         csi_division=EXCLUDED.csi_division, unit=EXCLUDED.unit,
                         default_days=EXCLUDED.default_days,
                         applies_to=EXCLUDED.applies_to,
                         keywords=EXCLUDED.keywords,
                         calendar_hint=EXCLUDED.calendar_hint, seq=EXCLUDED.seq""",
               [(c, (x.get("name") or c).strip(), _code(x.get("discipline")),
                 (x.get("csi_division") or "").strip() or None,
                 (x.get("unit") or "").strip(), _num(x.get("days"), 1.0),
                 json.dumps(x.get("applies_to") or []),
                 json.dumps(_words(x.get("keywords"))),
                 (x.get("calendar_hint") or "").strip() or None, i * 10)
                for c, x, i in wts],
               template="(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)")

        # الكتالوج يُرسَل بكامله، فخطواته ومعدّلاته وموادّه تُستبدل كلّها.
        for t in ("gen_lib_wt_steps", "gen_lib_rates", "gen_lib_wt_materials"):
            cur.execute(f"DELETE FROM {t}")
        steps, rates, mats = [], [], []
        for c, x, _ in wts:
            st_codes, total = set(), 0.0
            for j, s in enumerate(x.get("steps") or []):
                sc = _code(s.get("code"))
                if not sc or sc in st_codes:
                    continue
                st_codes.add(sc)
                w = _num(s.get("weight"))
                total += w
                rt = _code(s.get("rel_type")) or "FS"
                steps.append((c, sc, (s.get("name") or sc).strip(), w,
                              rt if rt in REL_TYPES else "FS",
                              _num(s.get("lag_days")), j * 10))
            # الأوزان تقسم تكلفة البند، فمجموعها غير المئة يُنتج تكلفة لا
            # تساوي البند. يُحفظ ويُبلَّغ عنه ولا يُرفض: قد يكون العمل جاريًا.
            if st_codes and abs(total - 100) > 0.01:
                problems.append({"work_type": c, "issue": "step_weights",
                                 "total": round(total, 2)})
            seen_r = set()
            for r in x.get("rates") or []:
                res = _code(r.get("resource"))
                st = _code(r.get("step"))
                if not res or (st, res) in seen_r:
                    continue
                if st and st not in st_codes:
                    st = ""
                seen_r.add((st, res))
                rates.append((c, st, res, _num(r.get("hours_per_unit")),
                              max(_num(r.get("crew_count"), 1.0), 0.0001),
                              bool(r.get("driving"))))
            seen_m = set()
            for m in x.get("materials") or []:
                mc = _code(m.get("material"))
                if not mc or mc in seen_m:
                    continue
                seen_m.add(mc)
                mats.append((c, mc, _num(m.get("qty_per_unit"))))
        _batch(cur, """INSERT INTO gen_lib_wt_steps
                         (work_type,code,name,weight,rel_type,lag_days,seq)
                       VALUES %s""", steps)
        _batch(cur, """INSERT INTO gen_lib_rates
                         (work_type,step,resource,hours_per_unit,crew_count,driving)
                       VALUES %s""", rates)
        _batch(cur, """INSERT INTO gen_lib_wt_materials
                         (work_type,material,qty_per_unit) VALUES %s""", mats)

    for kind in ("eng", "tc"):
        key = "engineering" if kind == "eng" else "tc"
        if key not in d:
            continue
        cur.execute("DELETE FROM gen_lib_extras WHERE kind=%s", (kind,))
        rows, seen = [], set()
        for i, x in enumerate(d[key]):
            wt, c = _code(x.get("work_type")), _code(x.get("code"))
            if not wt or not c or (wt, c) in seen:
                continue
            seen.add((wt, c))
            rt = _code(x.get("rel_type")) or "FS"
            rows.append((kind, wt, c, (x.get("name") or c).strip(),
                         max(_num(x.get("duration_days"), 1.0), 0.0),
                         (x.get("level_key") or "").strip() or None,
                         rt if rt in REL_TYPES else "FS",
                         _num(x.get("lag_days")), i * 10))
        _batch(cur, """INSERT INTO gen_lib_extras
                         (kind,work_type,code,name,duration_days,level_key,
                          rel_type,lag_days,seq) VALUES %s""", rows)

    if "rules" in d:
        # القاعدة المشتقّة تحتفظ بأصلها وشواهدها ما لم تُعدَّل؛ والمعدَّلة
        # تُرسَل بأصل ‎manual‎ فلا تمسّها إعادة الاشتقاق بعد ذلك.
        scopes = ("same", "next", "prev", "parent", "children")
        rows, seen = [], set()
        for x in d["rules"]:
            p, q = _code(x.get("pred_wt")), _code(x.get("succ_wt"))
            sc = (x.get("scope") or "same").strip().lower()
            rt = _code(x.get("rel_type")) or "FS"
            if not p or not q or sc not in scopes or rt not in REL_TYPES:
                continue
            if (p, q, sc) in seen:
                problems.append({"rule": f"{p}->{q} {sc}", "issue": "duplicate"})
                continue
            seen.add((p, q, sc))
            origin = "derived" if x.get("origin") == "derived" else "manual"
            rows.append((p, q, rt, _num(x.get("lag_days")), sc, origin))
        keep = [f"{p}|{q}|{sc}" for p, q, _, _, sc, _ in rows]
        cur.execute("""DELETE FROM gen_lib_logic WHERE project_type IS NULL
                       AND NOT ((pred_wt || '|' || succ_wt || '|' || scope) = ANY(%s))""",
                    (keep,))
        _batch(cur, """INSERT INTO gen_lib_logic
                         (pred_wt, succ_wt, rel_type, lag_days, scope, origin,
                          active, updated_at)
                       VALUES %s
                       ON CONFLICT (pred_wt, succ_wt, scope)
                         WHERE project_type IS NULL
                       DO UPDATE SET rel_type=EXCLUDED.rel_type,
                         lag_days=EXCLUDED.lag_days, active=TRUE,
                         origin=EXCLUDED.origin, updated_at=NOW()""",
               rows, template="(%s,%s,%s,%s,%s,%s,TRUE,NOW())")

    return problems


# ═══════════════════════════ المسارات ═══════════════════════════

@router.get("/api/dict")
async def get_dicts(request: Request):
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            return JSONResponse({"success": True, **load_all(cur)})
    except Exception as e:
        return _fail(e)


@router.post("/api/dict")
async def post_dicts(request: Request):
    if not _admin(request):
        return _deny()
    body = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            problems = save_all(cur, body)
        return JSONResponse({"success": True, "problems": problems})
    except Exception as e:
        return _fail(e)


@router.get("/admin-dictionaries", response_class=HTMLResponse)
async def page_dictionaries(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    return _templates.TemplateResponse(request, "dictionaries.html", {
        "admin_user": admin_user, "active_page": "dictionaries"})
