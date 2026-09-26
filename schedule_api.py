"""البرنامج الزمني: قاعدة البيانات، والقوالب، والواجهة البرمجية.

    الحساب كلّه في ‎schedule_engine.py‎ وقراءة ملفات بريمافيرا في ‎xer_io.py‎.
    هذا الملف يصل بينهما وبين الموقع: يحفظ الشبكة ويسترجعها، ويبني برنامجًا
    من قالب جاهز أو من ملف XER، ويشغّل المحرّك ويخزّن نتائجه.

    فُصل عن ‎app.py‎ لأنه سيكبر: التحديثات والسيناريوهات وتحليل التأخير
    والتصدير كلها ستُضاف هنا، وما كان لها أن تُضاف إلى ملف تجاوز خمسة آلاف
    سطر أصلًا.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse

import schedule_engine as se
import xer_io

router = APIRouter()

_get_conn = None
_templates = None


def setup(get_conn, tpl):
    """يُستدعى مرة واحدة من app.py ليمرّر الاتصال ومحرّك القوالب."""
    global _get_conn, _templates
    _get_conn, _templates = get_conn, tpl


def _admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


def _deny():
    return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)


def _fail(e):
    import traceback
    return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-800:]}, status_code=500)


# ═══════════════════════════ قاعدة البيانات ═══════════════════════════

def ensure_schema(cur):
    """جداول البرنامج الزمني. تُنشأ مرة واحدة مع أول اتصال، كبقيّة الجداول."""
    cur.execute("""CREATE TABLE IF NOT EXISTS schedules (
                       id SERIAL PRIMARY KEY,
                       project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                       name TEXT NOT NULL,
                       kind TEXT DEFAULT 'current',
                       source TEXT DEFAULT 'manual',
                       start_date TIMESTAMP,
                       data_date TIMESTAMP,
                       must_finish TIMESTAMP,
                       lag_calendar TEXT DEFAULT 'predecessor',
                       oos TEXT DEFAULT 'retained_logic',
                       baseline_of INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
                       stats JSONB NOT NULL DEFAULT '{}'::jsonb,
                       notes TEXT,
                       calc_at TIMESTAMP,
                       created_by TEXT, created_at TIMESTAMP DEFAULT NOW(),
                       updated_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("CREATE INDEX IF NOT EXISTS schedules_project ON schedules (project_id, kind)")

    cur.execute("""CREATE TABLE IF NOT EXISTS sch_calendars (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                       name TEXT NOT NULL,
                       week JSONB NOT NULL DEFAULT '{}'::jsonb,
                       exc JSONB NOT NULL DEFAULT '{}'::jsonb,
                       is_default BOOLEAN DEFAULT FALSE,
                       seq INTEGER DEFAULT 100)""")
    cur.execute("CREATE INDEX IF NOT EXISTS sch_cal_sched ON sch_calendars (schedule_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS sch_wbs (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                       code TEXT NOT NULL, name TEXT NOT NULL,
                       parent TEXT, seq INTEGER DEFAULT 100)""")
    cur.execute("CREATE INDEX IF NOT EXISTS sch_wbs_sched ON sch_wbs (schedule_id, seq)")

    cur.execute("""CREATE TABLE IF NOT EXISTS sch_activities (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                       code TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                       wbs TEXT, atype TEXT DEFAULT 'task',
                       duration DOUBLE PRECISION DEFAULT 0,
                       remaining DOUBLE PRECISION,
                       calendar_id INTEGER REFERENCES sch_calendars(id) ON DELETE SET NULL,
                       status TEXT DEFAULT 'not_started',
                       act_start TIMESTAMP, act_finish TIMESTAMP,
                       cstr TEXT, cstr_date TIMESTAMP,
                       seq INTEGER DEFAULT 100, notes TEXT,
                       es TIMESTAMP, ef TIMESTAMP, ls TIMESTAMP, lf TIMESTAMP,
                       tf DOUBLE PRECISION, ff DOUBLE PRECISION,
                       critical BOOLEAN DEFAULT FALSE, lp BOOLEAN DEFAULT FALSE,
                       bl_es TIMESTAMP, bl_ef TIMESTAMP)""")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS sch_act_code
                   ON sch_activities (schedule_id, code)""")
    cur.execute("CREATE INDEX IF NOT EXISTS sch_act_sched ON sch_activities (schedule_id, seq, id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS sch_relations (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                       pred TEXT NOT NULL, succ TEXT NOT NULL,
                       rtype TEXT DEFAULT 'FS',
                       lag DOUBLE PRECISION DEFAULT 0)""")
    cur.execute("CREATE INDEX IF NOT EXISTS sch_rel_sched ON sch_relations (schedule_id)")


# ═══════════════════════════ التقويمات ═══════════════════════════
# أسبوع العمل بترقيم بريمافيرا: ١=الأحد … ٧=السبت.

def _wk(days, periods):
    return {str(d): [list(p) for p in periods] for d in days}


DAY_8 = [(480, 720), (780, 1020)]          # ٠٨:٠٠–١٢:٠٠ ثم ١٣:٠٠–١٧:٠٠
DAY_10 = [(420, 720), (780, 1080)]         # ٠٧:٠٠–١٢:٠٠ ثم ١٣:٠٠–١٨:٠٠

DEFAULT_CALENDARS = [
    # الافتراضي في مواقع المملكة ستة أيام، والجمعة عطلة
    {"name": "6 days (Sat–Thu) 8h", "week": _wk([7, 1, 2, 3, 4, 5], DAY_8),
     "exc": {}, "is_default": True, "seq": 1},
    {"name": "5 days (Sun–Thu) 8h", "week": _wk([1, 2, 3, 4, 5], DAY_8),
     "exc": {}, "is_default": False, "seq": 2},
    {"name": "6 days (Sat–Thu) 10h", "week": _wk([7, 1, 2, 3, 4, 5], DAY_10),
     "exc": {}, "is_default": False, "seq": 3},
    # للأنشطة التي لا تتوقّف: معالجة الخرسانة، وفترات الانتظار التعاقدية
    {"name": "Continuous 24/7", "week": _wk(range(1, 8), [(0, 1440)]),
     "exc": {}, "is_default": False, "seq": 4},
]


def _cal_from_row(row):
    """يبني تقويم المحرّك من صفّ قاعدة البيانات."""
    cid, name, week, exc = row[0], row[1], row[2] or {}, row[3] or {}
    w = {int(k): [tuple(p) for p in v] for k, v in week.items()}
    e = {k: [tuple(p) for p in v] for k, v in exc.items()}
    return se.Calendar(w, e, name, str(cid))


# ═══════════════════════════ القوالب الجاهزة ═══════════════════════════
# كل نشاط: (الكود، الاسم، النوع، المدّة بالأيام، بند الهيكل، السوابق)
# والسوابق بصيغة بريمافيرا المعتادة: "A1010, A1020SS+10, A1030FS-5"

MS, FMS = "start_milestone", "finish_milestone"

TOWER = {
    "key": "tower",
    "name": "High-rise / Multi-storey Building",
    "name_ar": "برج أو مبنى متعدّد الأدوار",
    "note": "هيكل خرساني بأدوار متكرّرة، واجهة زجاجية، تشطيبات كاملة",
    "wbs": [("1", "Preliminaries"), ("2", "Substructure"), ("3", "Superstructure"),
            ("4", "Envelope"), ("5", "MEP"), ("6", "Finishes"),
            ("7", "External Works"), ("8", "Testing & Handover")],
    "acts": [
        ("A1000", "Notice to Proceed", MS, 0, "1", ""),
        ("A1010", "Mobilization & site establishment", "task", 20, "1", "A1000"),
        ("A1020", "Temporary facilities & utilities", "task", 15, "1", "A1010"),
        ("A1030", "Shop drawings — structural", "task", 45, "1", "A1000"),
        ("A1040", "Material submittals & approvals", "task", 60, "1", "A1000"),
        ("A1050", "Survey & setting out", "task", 10, "1", "A1010"),

        ("A2000", "Shoring & piling", "task", 40, "2", "A1050, A1030, A1020"),
        ("A2010", "Bulk excavation", "task", 30, "2", "A2000SS+15, A2000FF+5"),
        ("A2020", "Dewatering", "task", 45, "2", "A2010SS"),
        ("A2030", "Blinding concrete (PCC)", "task", 12, "2", "A2010"),
        ("A2040", "Raft foundation", "task", 30, "2", "A2030"),
        ("A2050", "Waterproofing — substructure", "task", 18, "2", "A2040"),
        ("A2060", "Retaining walls", "task", 35, "2", "A2050"),
        ("A2070", "Backfilling", "task", 15, "2", "A2060, A2020FF"),
        ("A2080", "Basement slabs", "task", 45, "2", "A2060SS+15"),

        ("A3000", "Ground floor slab", "task", 25, "3", "A2080"),
        ("A3010", "Typical floors — columns & walls", "task", 90, "3", "A3000"),
        ("A3020", "Typical floors — slabs", "task", 100, "3", "A3010SS+10, A3010FF+10"),
        ("A3030", "Staircases & cores", "task", 60, "3", "A3010SS+20"),
        ("A3040", "Roof slab", "task", 20, "3", "A3020"),
        ("A3050", "Structure complete", FMS, 0, "3", "A3040, A3030"),

        ("A4000", "External blockwork", "task", 70, "4", "A3020SS+40"),
        ("A4010", "Curtain wall — fabrication", "task", 90, "4", "A1040"),
        ("A4020", "Curtain wall — installation", "task", 80, "4", "A4010, A3020SS+60"),
        ("A4030", "Roof waterproofing & insulation", "task", 25, "4", "A3040"),
        ("A4040", "External plastering & cladding", "task", 50, "4", "A4000"),

        ("A5000", "MEP first fix", "task", 110, "5", "A3020SS+30"),
        ("A5010", "Lift installation", "task", 90, "5", "A3030"),
        ("A5020", "HVAC equipment installation", "task", 45, "5", "A5000SS+60"),
        ("A5030", "Fire fighting & alarm systems", "task", 60, "5", "A5000SS+40"),
        ("A5040", "MEP second fix", "task", 70, "5", "A5000, A6000"),

        ("A6000", "Internal blockwork & plastering", "task", 90, "6", "A3020SS+50"),
        ("A6010", "Screed & wet area waterproofing", "task", 40, "6", "A6000"),
        ("A6020", "Tiling & flooring", "task", 70, "6", "A6010"),
        ("A6030", "False ceilings", "task", 55, "6", "A5040SS+20"),
        ("A6040", "Painting", "task", 60, "6", "A6020SS+20, A6020FF+10, A6030SS+20, A6030FF+5"),
        ("A6050", "Doors & ironmongery", "task", 40, "6", "A6040SS+20"),
        ("A6060", "Joinery & fit-out", "task", 50, "6", "A6040"),

        ("A7000", "External utilities & drainage", "task", 45, "7", "A2070"),
        ("A7010", "Hardscape & parking", "task", 40, "7", "A7000"),
        ("A7020", "Landscaping & irrigation", "task", 35, "7", "A7010"),

        ("A8000", "Testing & commissioning", "task", 45, "8", "A5040, A5020, A5030, A5010"),
        ("A8010", "Authority approvals & certificates", "task", 30, "8", "A8000, A3050"),
        ("A8020", "Snagging & de-snagging", "task", 30, "8",
         "A8000, A6050, A6060, A4020, A4030, A4040"),
        ("A8030", "Final cleaning", "task", 15, "8", "A8020"),
        ("A8040", "Handover", FMS, 0, "8", "A8030, A8010, A7020"),
    ],
}

VILLAS = {
    "key": "villas",
    "name": "Villas Compound",
    "name_ar": "مجمّع فلل",
    "note": "وحدات متكرّرة مع بنية تحتية داخلية وأسوار وتنسيق موقع",
    "wbs": [("1", "Preliminaries"), ("2", "Site Infrastructure"), ("3", "Villas — Structure"),
            ("4", "Villas — Finishes"), ("5", "MEP"), ("6", "External Works"),
            ("7", "Testing & Handover")],
    "acts": [
        ("A1000", "Notice to Proceed", MS, 0, "1", ""),
        ("A1010", "Mobilization & site establishment", "task", 15, "1", "A1000"),
        ("A1020", "Shop drawings", "task", 30, "1", "A1000"),
        ("A1030", "Material submittals & approvals", "task", 45, "1", "A1000"),
        ("A1040", "Survey & setting out", "task", 8, "1", "A1010"),

        ("A2000", "Site clearance & grading", "task", 15, "2", "A1040"),
        ("A2010", "Storm & foul drainage network", "task", 40, "2", "A2000"),
        ("A2020", "Water supply network", "task", 35, "2", "A2010SS+10, A2010FF+5"),
        ("A2030", "Electrical network & substation", "task", 45, "2", "A2000"),
        ("A2040", "Internal roads — subbase", "task", 30, "2", "A2020, A2030"),

        ("A3000", "Excavation & foundations", "task", 45, "3", "A1040, A1020"),
        ("A3010", "Ground floor structure", "task", 60, "3", "A3000SS+15, A3000FF+10"),
        ("A3020", "First floor structure", "task", 60, "3", "A3010SS+20"),
        ("A3030", "Roof structure & parapets", "task", 40, "3", "A3020SS+20, A3020FF+10"),
        ("A3040", "Structure complete", FMS, 0, "3", "A3030"),

        ("A4000", "Blockwork", "task", 70, "4", "A3020SS+20, A1030"),
        ("A4010", "External plastering", "task", 50, "4", "A4000SS+20"),
        ("A4020", "Internal plastering", "task", 60, "4", "A4000SS+20, A4000FF+10"),
        ("A4030", "Tiling & flooring", "task", 70, "4", "A4020, A5000"),
        ("A4040", "Doors & windows", "task", 45, "4", "A4010"),
        ("A4050", "Painting", "task", 60, "4", "A4030SS+20"),
        ("A4060", "Joinery & kitchens", "task", 40, "4", "A4050SS+20, A4050FF+5"),

        ("A5000", "MEP first fix", "task", 80, "5", "A3020SS+20"),
        ("A5010", "MEP second fix", "task", 50, "5", "A4030"),
        ("A5020", "AC units & final fix", "task", 30, "5", "A5010SS+10, A5010FF"),

        ("A6000", "Boundary walls & gates", "task", 40, "6", "A2040"),
        ("A6010", "Internal roads — asphalt", "task", 25, "6", "A2040, A3030"),
        ("A6020", "Swimming pools", "task", 45, "6", "A3010"),
        ("A6030", "Landscaping & irrigation", "task", 35, "6", "A6000, A6010"),

        ("A7000", "Testing & commissioning", "task", 25, "7", "A5020, A6020"),
        ("A7010", "Municipality approvals", "task", 30, "7", "A7000, A3040"),
        ("A7020", "Snagging & de-snagging", "task", 25, "7", "A7000, A4060, A4040"),
        ("A7030", "Handover", FMS, 0, "7", "A7020, A7010, A6030"),
    ],
}

INFRA = {
    "key": "infra",
    "name": "Roads & Infrastructure",
    "name_ar": "طرق وبنية تحتية",
    "note": "أعمال ترابية وشبكات رطبة وجافّة وطبقات أسفلت",
    "wbs": [("1", "Preliminaries"), ("2", "Earthworks"), ("3", "Wet Utilities"),
            ("4", "Dry Utilities"), ("5", "Pavement"), ("6", "Finishing Works"),
            ("7", "Testing & Handover")],
    "acts": [
        ("A1000", "Notice to Proceed", MS, 0, "1", ""),
        ("A1010", "Mobilization", "task", 20, "1", "A1000"),
        ("A1020", "Survey & setting out", "task", 20, "1", "A1010"),
        ("A1030", "Design & shop drawings", "task", 45, "1", "A1000"),
        ("A1040", "Material approvals", "task", 40, "1", "A1000"),
        ("A1050", "Traffic diversion plan approval", "task", 30, "1", "A1000"),

        ("A2000", "Site clearance & demolition", "task", 30, "2", "A1020, A1050"),
        ("A2010", "Existing utility relocation", "task", 60, "2", "A2000"),
        ("A2020", "Cut & fill", "task", 70, "2", "A2010SS+20, A2010FF+10"),
        ("A2030", "Subgrade preparation", "task", 45, "2", "A2020SS+30, A2020FF+10"),

        ("A3000", "Storm drainage network", "task", 80, "3", "A2020SS+20, A1030"),
        ("A3010", "Sewer network", "task", 70, "3", "A3000SS+20, A3000FF+10"),
        ("A3020", "Water network", "task", 60, "3", "A3010SS+20, A3010FF+10"),
        ("A3030", "Manholes & chambers", "task", 50, "3", "A3000SS+20"),

        ("A4000", "Electrical ducts & chambers", "task", 55, "4", "A2030SS+10"),
        ("A4010", "Telecom ducts", "task", 45, "4", "A4000SS+15"),
        ("A4020", "Street lighting", "task", 50, "4", "A4000"),

        ("A5000", "Subbase course", "task", 50, "5",
         "A2030, A3020, A3030, A4010, A1040"),
        ("A5010", "Base course", "task", 45, "5", "A5000SS+15, A5000FF+5"),
        ("A5020", "Asphalt — binder course", "task", 35, "5", "A5010SS+15"),
        ("A5030", "Asphalt — wearing course", "task", 30, "5", "A5020SS+20, A5020FF+5"),

        ("A6000", "Kerbs & sidewalks", "task", 50, "6", "A5010"),
        ("A6010", "Road marking & signage", "task", 25, "6", "A5030"),
        ("A6020", "Guardrails & safety works", "task", 20, "6", "A5030"),
        ("A6030", "Landscaping & irrigation", "task", 40, "6", "A6000"),

        ("A7000", "Testing & inspection", "task", 25, "7", "A6010, A6020, A4020"),
        ("A7010", "Authority approvals", "task", 30, "7", "A7000"),
        ("A7020", "Snagging", "task", 20, "7", "A7000"),
        ("A7030", "Handover", FMS, 0, "7", "A7020, A7010, A6030"),
    ],
}

TEMPLATES = {t["key"]: t for t in (TOWER, VILLAS, INFRA)}


def templates_meta():
    return [{"key": t["key"], "name": t["name"], "name_ar": t["name_ar"],
             "note": t["note"], "activities": len(t["acts"]),
             "wbs": len(t["wbs"])} for t in TEMPLATES.values()]


# ═══════════════════════ قراءة السوابق وكتابتها ═══════════════════════
# الصيغة التي يكتبها المجدولون بلا تفكير: "A1010, A1020SS+10, A1030FS-5".
# إدخال العلاقات في جدول منفصل أدقّ نظريًا وأبطأ عمليًا بكثير.

_PRED = re.compile(r"^\s*(.+?)\s*(FS|SS|FF|SF)?\s*([+-]\s*\d+(?:\.\d+)?)?\s*$", re.I)


def parse_preds(text, known=None):
    """يحوّل نصّ السوابق إلى ‎[(كود، نوع، تخلّف بالأيام)]‎، ويتجاهل ما لا يُفهم."""
    out = []
    for tok in re.split(r"[,;\n]+", str(text or "")):
        tok = tok.strip()
        if not tok:
            continue
        if known and tok in known:            # كود كامل قد ينتهي بحروف تشبه النوع
            out.append((tok, "FS", 0.0))
            continue
        m = _PRED.match(tok)
        if not m:
            continue
        code, typ, lag = m.group(1).strip(), (m.group(2) or "FS").upper(), m.group(3)
        if not code:
            continue
        out.append((code, typ, float(re.sub(r"\s+", "", lag)) if lag else 0.0))
    return out


def preds_text(rels, hpd):
    """يعيد بناء النصّ من العلاقات المخزَّنة — التخلّف بالأيام لا بالساعات."""
    bits = []
    for pred, typ, lag in rels:
        s = pred + ("" if typ == "FS" else typ)
        d = (lag or 0) / (hpd or 8)
        if abs(d) > 1e-6:
            if typ == "FS":
                s += "FS"
            s += ("+" if d > 0 else "-") + _trim(abs(d))
        bits.append(s)
    return ", ".join(bits)


def _trim(x):
    return str(int(round(x))) if abs(x - round(x)) < 1e-6 else f"{x:g}"


# ═══════════════════════════ التحميل والحفظ ═══════════════════════════

def _load(cursor, sched_id):
    """يقرأ برنامجًا كاملًا من قاعدة البيانات."""
    cursor.execute("""SELECT id, project_id, name, kind, source, start_date, data_date,
                             must_finish, lag_calendar, oos, baseline_of, stats, notes,
                             calc_at, updated_at
                      FROM schedules WHERE id = %s""", (sched_id,))
    r = cursor.fetchone()
    if not r:
        return None
    s = {"id": r[0], "project_id": r[1], "name": r[2], "kind": r[3], "source": r[4],
         "start_date": r[5], "data_date": r[6], "must_finish": r[7],
         "lag_calendar": r[8], "oos": r[9], "baseline_of": r[10],
         "stats": r[11] or {}, "notes": r[12], "calc_at": r[13], "updated_at": r[14]}

    cursor.execute("""SELECT id, name, week, exc, is_default, seq FROM sch_calendars
                      WHERE schedule_id = %s ORDER BY seq, id""", (sched_id,))
    s["calendars"] = [{"id": c[0], "name": c[1], "week": c[2], "exc": c[3],
                       "is_default": c[4], "seq": c[5]} for c in cursor.fetchall()]

    cursor.execute("""SELECT code, name, parent, seq FROM sch_wbs
                      WHERE schedule_id = %s ORDER BY seq, code""", (sched_id,))
    s["wbs"] = [{"code": w[0], "name": w[1], "parent": w[2], "seq": w[3]}
                for w in cursor.fetchall()]

    cursor.execute("""SELECT pred, succ, rtype, lag FROM sch_relations
                      WHERE schedule_id = %s""", (sched_id,))
    rels = {}
    for pred, succ, typ, lag in cursor.fetchall():
        rels.setdefault(succ, []).append((pred, typ, lag))
    s["_rels"] = rels

    cursor.execute("""SELECT code, name, wbs, atype, duration, remaining, calendar_id,
                             status, act_start, act_finish, cstr, cstr_date, seq, notes,
                             es, ef, ls, lf, tf, ff, critical, lp, bl_es, bl_ef
                      FROM sch_activities WHERE schedule_id = %s ORDER BY seq, id""",
                   (sched_id,))
    s["activities"] = [dict(zip(
        ("code", "name", "wbs", "atype", "duration", "remaining", "calendar_id",
         "status", "act_start", "act_finish", "cstr", "cstr_date", "seq", "notes",
         "es", "ef", "ls", "lf", "tf", "ff", "critical", "lp", "bl_es", "bl_ef"), a))
        for a in cursor.fetchall()]
    return s


def _hpd(s):
    """ساعات يوم العمل في التقويم الافتراضي — وحدة العرض في كل الصفحة."""
    for c in s.get("calendars", []):
        if c["is_default"]:
            return _cal_from_row((c["id"], c["name"], c["week"], c["exc"])).hours_per_day
    return 8.0


def payload(s):
    """يحوّل ما قُرئ من القاعدة إلى الشكل الذي تفهمه الصفحة."""
    hpd = _hpd(s)
    acts = []
    for a in s["activities"]:
        d = dict(a)
        d["duration_d"] = round((a["duration"] or 0) / hpd, 2)
        d["remaining_d"] = None if a["remaining"] is None else round(a["remaining"] / hpd, 2)
        d["tf_d"] = None if a["tf"] is None else round(a["tf"] / hpd, 2)
        d["ff_d"] = None if a["ff"] is None else round(a["ff"] / hpd, 2)
        d["preds"] = preds_text(s["_rels"].get(a["code"], []), hpd)
        acts.append(d)
    return {"schedule": {k: v for k, v in s.items()
                         if k not in ("activities", "wbs", "_rels")},
            "wbs": s["wbs"], "activities": acts, "hours_per_day": round(hpd, 4)}


def _dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, 8)
    return xer_io.dt(v)


def save_network(cursor, sched_id, wbs, acts, hpd):
    """يكتب الأنشطة والعلاقات والهيكل. استبدال كامل، لا تعديل جزئي.

       الاستبدال أبسط وأضمن: جدول الأنشطة محرَّر كورقة إكسل، والصفوف تُضاف
       وتُحذف ويتغيّر ترتيبها في التحرير الواحد، فمحاولة مطابقة ما تغيّر
       صفًّا صفًّا تكلف أكثر مما تنفع وتترك علاقات معلّقة عند أول خطأ.
    """
    cursor.execute("DELETE FROM sch_wbs WHERE schedule_id = %s", (sched_id,))
    for i, w in enumerate(wbs or []):
        code = str(w.get("code") or "").strip()
        if not code:
            continue
        cursor.execute("""INSERT INTO sch_wbs (schedule_id, code, name, parent, seq)
                          VALUES (%s, %s, %s, %s, %s)""",
                       (sched_id, code[:40], str(w.get("name") or code)[:200],
                        (w.get("parent") or None), i))

    seen, rows = set(), []
    for i, a in enumerate(acts or []):
        code = str(a.get("code") or "").strip()[:40]
        if not code or code in seen:
            continue                       # الكود مفتاح النشاط، فلا يتكرّر
        seen.add(code)
        dur = float(a.get("duration_d") or 0) * hpd
        rem = a.get("remaining_d")
        atype = a.get("atype") or "task"
        if atype in (MS, FMS):
            dur = 0.0
        status = a.get("status") or "not_started"
        rows.append((code, str(a.get("name") or "")[:300],
                     str(a["wbs"])[:40] if a.get("wbs") else None, atype, dur,
                     None if rem in (None, "") else float(rem) * hpd,
                     a.get("calendar_id") or None, status,
                     _dt(a.get("act_start")), _dt(a.get("act_finish")),
                     (a.get("cstr") or None), _dt(a.get("cstr_date")), i,
                     (a.get("notes") or None), a.get("preds") or ""))

    codes = {r[0] for r in rows}
    if codes:
        cursor.execute("DELETE FROM sch_activities WHERE schedule_id = %s AND NOT (code = ANY(%s))",
                       (sched_id, list(codes)))
    else:
        cursor.execute("DELETE FROM sch_activities WHERE schedule_id = %s", (sched_id,))

    for r in rows:
        cursor.execute("""INSERT INTO sch_activities
              (schedule_id, code, name, wbs, atype, duration, remaining, calendar_id,
               status, act_start, act_finish, cstr, cstr_date, seq, notes)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (schedule_id, code) DO UPDATE SET
                name=EXCLUDED.name, wbs=EXCLUDED.wbs, atype=EXCLUDED.atype,
                duration=EXCLUDED.duration, remaining=EXCLUDED.remaining,
                calendar_id=EXCLUDED.calendar_id, status=EXCLUDED.status,
                act_start=EXCLUDED.act_start, act_finish=EXCLUDED.act_finish,
                cstr=EXCLUDED.cstr, cstr_date=EXCLUDED.cstr_date,
                seq=EXCLUDED.seq, notes=EXCLUDED.notes""",
                       (sched_id,) + r[:14])

    cursor.execute("DELETE FROM sch_relations WHERE schedule_id = %s", (sched_id,))
    missing = []
    for r in rows:
        succ, txt = r[0], r[14]
        for pred, typ, lag_d in parse_preds(txt, codes):
            if pred not in codes:
                missing.append(f"{succ} ← {pred}")
                continue
            if pred == succ:
                continue
            cursor.execute("""INSERT INTO sch_relations (schedule_id, pred, succ, rtype, lag)
                              VALUES (%s,%s,%s,%s,%s)""",
                           (sched_id, pred, succ, typ, lag_d * hpd))
    return missing


# ═══════════════════════════ تشغيل المحرّك ═══════════════════════════

def build_engine(s):
    """يبني شبكة المحرّك من البرنامج المقروء."""
    sch = se.Schedule(start=s["start_date"], data_date=s["data_date"],
                      must_finish=s["must_finish"], name=s["name"],
                      lag_calendar=s["lag_calendar"] or "predecessor",
                      out_of_sequence=s["oos"] or "retained_logic")
    for c in s["calendars"]:
        sch.add_calendar(_cal_from_row((c["id"], c["name"], c["week"], c["exc"])),
                         default=c["is_default"])
    if not sch.calendars:
        sch.add_calendar(se.Calendar.standard(days=(7, 1, 2, 3, 4, 5)), default=True)
    for a in s["activities"]:
        sch.add(se.Activity(
            id=a["code"], name=a["name"], duration=a["duration"] or 0,
            remaining=a["remaining"], calendar=a["calendar_id"] and str(a["calendar_id"]),
            type=a["atype"] or "task", wbs=a["wbs"], code=a["code"],
            status=a["status"] or "not_started",
            actual_start=a["act_start"], actual_finish=a["act_finish"],
            constraint=a["cstr"] or None, constraint_date=a["cstr_date"]))
    for succ, rels in s["_rels"].items():
        for pred, typ, lag in rels:
            if pred in sch.activities and succ in sch.activities:
                sch.link(pred, succ, typ, lag or 0)
    return sch


def calculate(cursor, sched_id):
    """يجدول ويخزّن النتائج، ويعيد ملخّصًا وما لاحظه على الشبكة."""
    s = _load(cursor, sched_id)
    if not s:
        return None
    if not s["activities"]:
        return {"ok": False, "error": "لا أنشطة في هذا البرنامج بعد"}
    sch = build_engine(s)
    if sch.start is None and sch.data_date is None:
        sch.start = datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
    try:
        sch.run()
    except se.CycleError as e:
        return {"ok": False, "error": "حلقة منطقية مغلقة تمنع الحساب",
                "cycle": e.cycles[0] if e.cycles else []}
    except se.ScheduleError as e:
        return {"ok": False, "error": str(e)}

    sch.longest_path_set()
    hpd = _hpd(s)
    for a in sch.activities.values():
        cursor.execute("""UPDATE sch_activities SET es=%s, ef=%s, ls=%s, lf=%s,
                                 tf=%s, ff=%s, critical=%s, lp=%s
                          WHERE schedule_id=%s AND code=%s""",
                       (a.es, a.ef, a.ls, a.lf, a.total_float, a.free_float,
                        bool(a.critical), bool(a.longest_path), sched_id, a.id))

    opens = sch.open_ends()
    stats = {
        "activities": len(sch.activities),
        "relations": len(sch.relations),
        "start": sch.activities and min(
            (a.es for a in sch.activities.values() if a.es), default=None),
        "finish": sch.finish,
        "critical": len(sch.critical_path()),
        "longest_path": sum(1 for a in sch.activities.values() if a.longest_path),
        "open_ends": len(opens),
        "negative_float": sum(1 for a in sch.activities.values()
                              if a.total_float is not None and a.total_float < -1e-6),
        "constrained": sum(1 for a in sch.activities.values() if a.constraint),
        "complete": sum(1 for a in sch.activities.values() if a.status == se.COMPLETE),
        "in_progress": sum(1 for a in sch.activities.values() if a.status == se.IN_PROGRESS),
        "hours_per_day": round(hpd, 4),
    }
    for k in ("start", "finish"):
        if stats[k]:
            stats[k] = stats[k].isoformat()
    cursor.execute("UPDATE schedules SET stats=%s, calc_at=NOW(), updated_at=NOW() WHERE id=%s",
                   (json.dumps(stats), sched_id))
    return {"ok": True, "stats": stats,
            "warnings": sch.warnings,
            "open_ends": [{"code": a.id, "name": a.name, "why": why} for a, why in opens[:60]]}


# ═══════════════════════════ نقاط الواجهة ═══════════════════════════

@router.get("/api/schedule-templates")
async def api_templates(request: Request):
    if not _admin(request):
        return _deny()
    return {"success": True, "templates": templates_meta()}


@router.get("/api/schedules")
async def api_list(request: Request, project_id: int = 0):
    """برامج مشروع واحد، أو كلّها حين لا يُذكر مشروع."""
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        cur = conn.cursor()
        q = """SELECT s.id, s.project_id, s.name, s.kind, s.source, s.data_date,
                      s.stats, s.calc_at, s.updated_at, p.name_en, p.code,
                      (SELECT COUNT(*) FROM sch_activities a WHERE a.schedule_id = s.id)
               FROM schedules s LEFT JOIN projects p ON p.id = s.project_id"""
        args = ()
        if project_id:
            q += " WHERE s.project_id = %s"
            args = (project_id,)
        q += " ORDER BY s.updated_at DESC"
        cur.execute(q, args)
        rows = [{"id": r[0], "project_id": r[1], "name": r[2], "kind": r[3],
                 "source": r[4], "data_date": r[5], "stats": r[6] or {},
                 "calc_at": r[7], "updated_at": r[8], "project": r[9] or r[10],
                 "activities": r[11]} for r in cur.fetchall()]
        conn.close()
        return {"success": True, "schedules": rows, "templates": templates_meta()}
    except Exception as e:
        return _fail(e)


@router.post("/api/schedules")
async def api_create(request: Request):
    """برنامج جديد: فارغ، أو من قالب جاهز. والاستيراد له نقطته الخاصّة."""
    if not _admin(request):
        return _deny()
    try:
        b = await request.json()
        pid = int(b.get("project_id") or 0) or None
        tkey = (b.get("template") or "").strip()
        name = (b.get("name") or "").strip()
        start = _dt(b.get("start_date")) or datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
        tpl = TEMPLATES.get(tkey)
        if not name:
            name = tpl["name"] if tpl else "New schedule"

        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO schedules (project_id, name, kind, source, start_date,
                                              data_date, created_by)
                       VALUES (%s,%s,'current',%s,%s,%s,%s) RETURNING id""",
                    (pid, name[:200], ("template:" + tkey) if tpl else "manual",
                     start, start, request.cookies.get("super_admin_auth")))
        sid = cur.fetchone()[0]
        for c in DEFAULT_CALENDARS:
            cur.execute("""INSERT INTO sch_calendars (schedule_id, name, week, exc, is_default, seq)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (sid, c["name"], json.dumps(c["week"]), json.dumps(c["exc"]),
                         c["is_default"], c["seq"]))
        if tpl:
            hpd = 8.0
            wbs = [{"code": c, "name": n} for c, n in tpl["wbs"]]
            acts = [{"code": c, "name": n, "atype": t, "duration_d": d,
                     "wbs": w, "preds": p} for c, n, t, d, w, p in tpl["acts"]]
            save_network(cur, sid, wbs, acts, hpd)
            calculate(cur, sid)
        conn.commit()
        conn.close()
        return {"success": True, "id": sid}
    except Exception as e:
        return _fail(e)


@router.post("/api/schedules/import")
async def api_import(request: Request, file: UploadFile = File(...),
                     project_id: int = 0, proj: str = ""):
    """استيراد ملف XER. الملف قد يحوي أكثر من مشروع، فيُختار الأكبر أو المطلوب."""
    if not _admin(request):
        return _deny()
    try:
        raw = await file.read()
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        db = xer_io.parse(text or raw.decode("latin-1", "replace"))
        found = xer_io.projects(db)
        if not found:
            return JSONResponse({"success": False, "error": "الملف لا يحتوي على مشروع"},
                                status_code=400)
        pick = proj or max(found, key=lambda p: p["tasks"])["id"]
        sch = xer_io.build(db, pick)
        answers = xer_io.p6_answers(db, pick)

        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO schedules (project_id, name, kind, source, start_date,
                                              data_date, must_finish, created_by)
                       VALUES (%s,%s,'current','xer',%s,%s,%s,%s) RETURNING id""",
                    (int(project_id) or None, (sch.name or file.filename)[:200],
                     sch.start, sch.data_date, sch.must_finish,
                     request.cookies.get("super_admin_auth")))
        sid = cur.fetchone()[0]

        # تقويمات الملف نفسه، لا التقويمات الافتراضية — وإلّا اختلّت كل التواريخ
        calmap = {}
        for i, (key, c) in enumerate(sch.calendars.items()):
            cur.execute("""INSERT INTO sch_calendars (schedule_id, name, week, exc, is_default, seq)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (sid, c.name or f"Calendar {i+1}",
                         json.dumps({str(d): [list(p) for p in c.week[d]] for d in range(1, 8)}),
                         json.dumps({k: [list(p) for p in v] for k, v in c.exc.items()}),
                         c is sch.default_calendar, i))
            calmap[key] = cur.fetchone()[0]

        wbs_seen, wbs = set(), []
        for a in sch.activities.values():
            if a.wbs and a.wbs not in wbs_seen:
                wbs_seen.add(a.wbs)
                wbs.append({"code": a.wbs[:40], "name": a.wbs})
        for w in wbs:
            cur.execute("INSERT INTO sch_wbs (schedule_id, code, name, seq) VALUES (%s,%s,%s,%s)",
                        (sid, w["code"], w["name"], 0))

        for i, a in enumerate(sch.activities.values()):
            cur.execute("""INSERT INTO sch_activities
                  (schedule_id, code, name, wbs, atype, duration, remaining, calendar_id,
                   status, act_start, act_finish, cstr, cstr_date, seq)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (schedule_id, code) DO NOTHING""",
                        (sid, (a.code or a.id)[:40], a.name[:300],
                         (a.wbs or "")[:40] or None, a.type, a.duration, a.remaining,
                         calmap.get(str(a.calendar)), a.status, a.actual_start,
                         a.actual_finish, a.constraint, a.constraint_date, i))
        idcode = {a.id: (a.code or a.id)[:40] for a in sch.activities.values()}
        for r in sch.relations:
            cur.execute("""INSERT INTO sch_relations (schedule_id, pred, succ, rtype, lag)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (sid, idcode[r.pred], idcode[r.succ], r.type, r.lag))

        res = calculate(cur, sid)
        # المعايرة: نجدول الشبكة كما قرأناها ونضعها بجانب ما حسبه بريمافيرا
        # نفسه داخل الملف. الرقم الذي يعود هو أصدق ما يُقال عن المحرّك.
        try:
            sch.run()
            cmp_res = xer_io.compare_with_p6(sch, answers)
        except Exception as ex:
            cmp_res = {"rate": None, "total": 0, "matched": 0, "by_field": {},
                       "mismatches": [], "error": f"{type(ex).__name__}: {ex}"}
        conn.commit()
        conn.close()
        return {"success": True, "id": sid, "calc": res,
                "projects_in_file": [{"id": p["id"], "code": p["code"], "tasks": p["tasks"]}
                                     for p in found],
                "p6": {"rate": cmp_res["rate"], "total": cmp_res["total"],
                       "matched": cmp_res["matched"], "by_field": cmp_res["by_field"],
                       "worst": [{"code": m["code"], "name": m["name"],
                                  "fields": sorted(m["diff"]), "worst_hr": m["worst"]}
                                 for m in cmp_res.get("mismatches", [])[:15]],
                       "error": cmp_res.get("error")}}
    except Exception as e:
        return _fail(e)


@router.get("/api/schedules/{sched_id}")
async def api_get(sched_id: int, request: Request):
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        cur = conn.cursor()
        s = _load(cur, sched_id)
        conn.close()
        if not s:
            return JSONResponse({"success": False, "error": "البرنامج غير موجود"}, status_code=404)
        out = payload(s)
        out["success"] = True
        return out
    except Exception as e:
        return _fail(e)


@router.post("/api/schedules/{sched_id}")
async def api_save(sched_id: int, request: Request):
    """حفظ الشبكة، ثم إعادة الحساب مباشرةً ما لم يُطلب غير ذلك."""
    if not _admin(request):
        return _deny()
    try:
        b = await request.json()
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM schedules WHERE id = %s", (sched_id,))
        if not cur.fetchone():
            conn.close()
            return JSONResponse({"success": False, "error": "البرنامج غير موجود"}, status_code=404)

        sets, args = [], []
        for col, key in (("name", "name"), ("kind", "kind"), ("notes", "notes"),
                         ("lag_calendar", "lag_calendar"), ("oos", "oos")):
            if key in b:
                sets.append(f"{col} = %s")
                args.append(str(b[key])[:200] if b[key] is not None else None)
        for col in ("start_date", "data_date", "must_finish"):
            if col in b:
                sets.append(f"{col} = %s")
                args.append(_dt(b[col]))
        if sets:
            cur.execute(f"UPDATE schedules SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s",
                        args + [sched_id])

        if "calendars" in b:
            _save_calendars(cur, sched_id, b["calendars"])

        missing = []
        if "activities" in b:
            s0 = _load(cur, sched_id)
            missing = save_network(cur, sched_id, b.get("wbs") or s0["wbs"],
                                   b["activities"], _hpd(s0))

        res = None if b.get("no_calc") else calculate(cur, sched_id)
        conn.commit()
        s = _load(cur, sched_id)
        conn.close()
        out = payload(s)
        out.update({"success": True, "calc": res, "missing_preds": missing[:40]})
        return out
    except Exception as e:
        return _fail(e)


def _save_calendars(cur, sched_id, cals):
    keep = []
    for i, c in enumerate(cals or []):
        name = str(c.get("name") or "Calendar")[:120]
        week = {str(int(k)): [[int(p[0]), int(p[1])] for p in v]
                for k, v in (c.get("week") or {}).items() if str(k).isdigit()}
        exc = {str(k)[:10]: [[int(p[0]), int(p[1])] for p in v]
               for k, v in (c.get("exc") or {}).items()}
        if c.get("id"):
            cur.execute("""UPDATE sch_calendars SET name=%s, week=%s, exc=%s,
                           is_default=%s, seq=%s WHERE id=%s AND schedule_id=%s""",
                        (name, json.dumps(week), json.dumps(exc),
                         bool(c.get("is_default")), i, int(c["id"]), sched_id))
            keep.append(int(c["id"]))
        else:
            cur.execute("""INSERT INTO sch_calendars (schedule_id, name, week, exc, is_default, seq)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (sched_id, name, json.dumps(week), json.dumps(exc),
                         bool(c.get("is_default")), i))
            keep.append(cur.fetchone()[0])
    if keep:
        cur.execute("DELETE FROM sch_calendars WHERE schedule_id=%s AND NOT (id = ANY(%s))",
                    (sched_id, keep))
        cur.execute("""UPDATE sch_calendars SET is_default = TRUE WHERE id = (
                         SELECT id FROM sch_calendars WHERE schedule_id=%s ORDER BY seq, id LIMIT 1)
                       AND NOT EXISTS (SELECT 1 FROM sch_calendars
                                       WHERE schedule_id=%s AND is_default)""",
                    (sched_id, sched_id))


@router.post("/api/schedules/{sched_id}/calculate")
async def api_calculate(sched_id: int, request: Request):
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        cur = conn.cursor()
        res = calculate(cur, sched_id)
        if res is None:
            conn.close()
            return JSONResponse({"success": False, "error": "البرنامج غير موجود"}, status_code=404)
        conn.commit()
        s = _load(cur, sched_id)
        conn.close()
        out = payload(s)
        out.update({"success": True, "calc": res})
        return out
    except Exception as e:
        return _fail(e)


@router.delete("/api/schedules/{sched_id}")
async def api_delete(sched_id: int, request: Request):
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM schedules WHERE id = %s", (sched_id,))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return _fail(e)


@router.get("/admin-schedule/{sched_id}", response_class=HTMLResponse)
async def page_schedule(sched_id: int, request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    return _templates.TemplateResponse(request, "schedule.html", {
        "admin_user": admin_user, "active_page": "schedule", "sched_id": sched_id})
