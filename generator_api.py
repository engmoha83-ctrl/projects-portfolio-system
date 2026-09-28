# -*- coding: utf-8 -*-
"""
واجهة مولّد البرامج الزمنية — الجداول والمسارات.

تنقسم البيانات طبقتين، وهذا الفصل هو ما يجعل الكتالوج **يتراكم** مع الوقت
بدل أن يُكتب من جديد في كلّ مشروع:

* **مكتبة الشركة** (`gen_lib_*`) — تخصّصات وأنواع أعمال وقواعد ربط وشواهدها.
  مشتركة بين كلّ المشاريع.
* **تعريف المشروع** (`gen_places` وأخواتها) — شجرة أماكن هذا البرنامج،
  وسلسلة تسلسلها، ومصفوفة انطباقها.

والأنشطة المتولَّدة تُكتب في `sch_activities` نفسه الذي يقرأ منه المحرّك،
فلا شبكة ثانية ولا منطق مكرّر.
"""

import json
import traceback
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

import generator as G

router = APIRouter()
_get_conn = None
_templates = None


def setup(get_conn, tpl):
    global _get_conn, _templates
    _get_conn, _templates = get_conn, tpl


def _admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


def _deny():
    return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)


def _plain(v):
    """قيمة صالحة للـJSON: ‎Decimal‎ إلى ‎float‎ والتواريخ إلى نصّ."""
    from decimal import Decimal
    from datetime import date, datetime
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _batch(cur, sql, rows, template=None, size=500):
    """
    كتابة دفعات لا استعلامًا لكل صفّ.

    قاعدة البيانات على Supabase بعيدة، وألفا نشاط بألفي ذهاب وإياب تنتهي
    بانقضاء مهلة الطلب قبل أن تنتهي الكتابة.
    """
    if not rows:
        return
    from psycopg2.extras import execute_values
    for i in range(0, len(rows), size):
        execute_values(cur, sql, rows[i:i + size], template=template,
                       page_size=size)


def _fail(e):
    return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-800:]}, status_code=500)


# ─────────── مستويات التقسيم الافتراضية ───────────
# بذرةٌ تُزرع مرّة، والقائمة بعدها ملكُ المستخدم يضيف ويحذف.
# ‎token‎ هو رمزها في نمط كود النشاط.
DEFAULT_LEVELS = [
    ("zone", "Zone", None, "ZONE", 10),
    ("building", "Building", None, "BUILDING", 20),
    ("floor", "Floor", None, "FLOOR", 30),
    ("corridor", "Corridor", None, "CORRIDOR", 40),
    ("unit", "Unit", None, "UNIT", 50),
    ("room", "Room", None, "ROOM", 60),
]

# ─────────── أقسام MasterFormat ───────────
# معياريّة وثابتة لكلّ المشاريع: تُقرأ ولا تُحرَّر، ومنها يُختار تخصّص
# نوع العمل. وثباتها هو ما يجعل كتالوج الأعمال مقارَنًا بين المشاريع.
CSI_DIVISIONS = [
    ("00", "Procurement and Contracting Requirements", None, 0),
    ("01", "General Requirements", None, 1),
    ("02", "Existing Conditions", None, 2),
    ("03", "Concrete", None, 3),
    ("04", "Masonry", None, 4),
    ("05", "Metals", None, 5),
    ("06", "Wood, Plastics, and Composites", None, 6),
    ("07", "Thermal and Moisture Protection", None, 7),
    ("08", "Openings", None, 8),
    ("09", "Finishes", None, 9),
    ("10", "Specialties", None, 10),
    ("11", "Equipment", None, 11),
    ("12", "Furnishings", None, 12),
    ("13", "Special Construction", None, 13),
    ("14", "Conveying Equipment", None, 14),
    ("21", "Fire Suppression", None, 21),
    ("22", "Plumbing", None, 22),
    ("23", "Heating, Ventilating, and Air Conditioning", None, 23),
    ("25", "Integrated Automation", None, 25),
    ("26", "Electrical", None, 26),
    ("27", "Communications", None, 27),
    ("28", "Electronic Safety and Security", None, 28),
    ("31", "Earthwork", None, 31),
    ("32", "Exterior Improvements", None, 32),
    ("33", "Utilities", None, 33),
    ("34", "Transportation", None, 34),
    ("35", "Waterway and Marine Construction", None, 35),
    ("40", "Process Interconnections", None, 40),
    ("41", "Material Processing and Handling Equipment", None, 41),
    ("42", "Process Heating, Cooling, and Drying Equipment", None, 42),
    ("43", "Process Gas and Liquid Handling", None, 43),
    ("44", "Pollution and Waste Control Equipment", None, 44),
    ("46", "Water and Wastewater Equipment", None, 46),
    ("48", "Electrical Power Generation", None, 48),
]


# ═══════════════════════════ الجداول ═══════════════════════════

def ensure_schema(cur):
    """تُنشأ مع أوّل اتصال كبقيّة الجداول، فلا شيء يُعمل يدويًّا في Supabase."""

    # ---- مكتبة الشركة ----
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_disciplines (
                       id SERIAL PRIMARY KEY,
                       code TEXT UNIQUE NOT NULL,
                       name TEXT NOT NULL,
                       name_ar TEXT,
                       csi_division TEXT,
                       seq INTEGER DEFAULT 0)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_work_types (
                       id SERIAL PRIMARY KEY,
                       code TEXT NOT NULL,
                       name TEXT NOT NULL,
                       name_ar TEXT,
                       discipline TEXT,
                       csi_division TEXT,
                       unit TEXT,
                       default_days NUMERIC DEFAULT 1,
                       productivity NUMERIC,
                       calendar_hint TEXT,
                       applies_to JSONB NOT NULL DEFAULT '[]'::jsonb,
                       seq INTEGER DEFAULT 0,
                       origin TEXT DEFAULT 'manual',
                       created_at TIMESTAMP DEFAULT NOW(),
                       UNIQUE (code))""")

    # القواعد الفاعلة: ما يقرأ منه المولّد
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_logic (
                       id SERIAL PRIMARY KEY,
                       pred_wt TEXT NOT NULL,
                       succ_wt TEXT NOT NULL,
                       rel_type TEXT NOT NULL DEFAULT 'FS',
                       lag_days NUMERIC DEFAULT 0,
                       scope TEXT NOT NULL DEFAULT 'same',
                       project_type TEXT,
                       origin TEXT DEFAULT 'derived',
                       observations INTEGER DEFAULT 0,
                       agreement NUMERIC,
                       sources INTEGER DEFAULT 0,
                       lag_min NUMERIC, lag_max NUMERIC,
                       confidence NUMERIC,
                       note TEXT,
                       active BOOLEAN DEFAULT TRUE,
                       updated_at TIMESTAMP DEFAULT NOW(),
                       UNIQUE (pred_wt, succ_wt, scope, project_type))""")
    # القيد أعلاه لا يمنع التكرار حين ‎project_type‎ فارغة: بوستجرس لا يعدّ
    # ‎NULL‎ مساويًا لـ‎NULL‎ في الفهارس الفريدة، فكان ‎ON CONFLICT‎ لا يجد
    # هدفه وكلّ حفظ يُنشئ صفًّا جديدًا — تضاعفت المكتبة من تسع عشرة قاعدة
    # إلى ثمان وثلاثين بحفظة واحدة. الفهرس الجزئيّ يسدّ ذلك.
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS gen_logic_general
                   ON gen_lib_logic (pred_wt, succ_wt, scope)
                   WHERE project_type IS NULL""")

    # الشواهد: سجلّ يتراكم ولا يُمحى. إعادة الاشتقاق تقرؤه كلّه مجتمعًا،
    # فالملفّ الجديد يحسّن ولا يمحو، والاختلاف بين المشاريع يظهر ولا يضيع.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_logic_evidence (
                       id SERIAL PRIMARY KEY,
                       source TEXT NOT NULL,
                       project TEXT,
                       project_type TEXT,
                       pred_wt TEXT NOT NULL,
                       succ_wt TEXT NOT NULL,
                       rel_type TEXT NOT NULL,
                       scope TEXT NOT NULL,
                       count INTEGER DEFAULT 1,
                       lag_min NUMERIC, lag_median NUMERIC, lag_max NUMERIC,
                       confidence NUMERIC,
                       imported_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("""CREATE INDEX IF NOT EXISTS gen_evidence_pair
                   ON gen_lib_logic_evidence (pred_wt, succ_wt, scope)""")

    # مستويات التقسيم — قائمة تُضاف ويُحذف منها، لا ثوابت في الكود.
    # مشروعٌ يقسَّم إلى أجنحة ومراحل، وآخر إلى فلل وقطع أراضٍ، وحصرُها في
    # ستّة أسماء مكتوبة داخل الصفحة يجعل نصف المشاريع لا تُعبَّر عنها.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_levels (
                       id SERIAL PRIMARY KEY,
                       key TEXT UNIQUE NOT NULL,
                       label TEXT NOT NULL,
                       label_ar TEXT,
                       token TEXT,
                       seq INTEGER DEFAULT 0,
                       active BOOLEAN DEFAULT TRUE)""")

    # أقسام MasterFormat — معياريّة ثابتة لكلّ المشاريع، تُزرع مرّة.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_lib_csi (
                       division TEXT PRIMARY KEY,
                       name TEXT NOT NULL,
                       name_ar TEXT,
                       seq INTEGER DEFAULT 0)""")

    cur.execute("SELECT COUNT(*) FROM gen_lib_levels")
    if not cur.fetchone()[0]:
        _batch(cur, "INSERT INTO gen_lib_levels (key,label,label_ar,token,seq) "
                    "VALUES %s ON CONFLICT (key) DO NOTHING", DEFAULT_LEVELS)
    cur.execute("SELECT COUNT(*) FROM gen_lib_csi")
    if not cur.fetchone()[0]:
        _batch(cur, "INSERT INTO gen_lib_csi (division,name,name_ar,seq) "
                    "VALUES %s ON CONFLICT (division) DO NOTHING", CSI_DIVISIONS)

    # ---- تعريف المشروع ----
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_places (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL
                           REFERENCES schedules(id) ON DELETE CASCADE,
                       parent_id INTEGER REFERENCES gen_places(id) ON DELETE CASCADE,
                       level_key TEXT NOT NULL,
                       code TEXT NOT NULL,
                       name TEXT,
                       seq INTEGER DEFAULT 0,
                       repeat_count INTEGER DEFAULT 1,
                       repeat_pattern TEXT DEFAULT '{code}{n:02d}',
                       pos_x NUMERIC, pos_y NUMERIC)""")
    for col, ddl in (("pos_x", "NUMERIC"), ("pos_y", "NUMERIC")):
        cur.execute(f"ALTER TABLE gen_places ADD COLUMN IF NOT EXISTS {col} {ddl}")
    cur.execute("""CREATE INDEX IF NOT EXISTS gen_places_sched
                   ON gen_places (schedule_id, parent_id, seq)""")

    # سلسلة التسلسل الصريحة بين الأشقّاء. الزوج المعلَّم «بالتوازي» لا
    # يُنتج رابطًا. هذه بديل استنتاج التسلسل من مواضع الأشكال على الكانفس.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_place_rels (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL
                           REFERENCES schedules(id) ON DELETE CASCADE,
                       pred_place INTEGER REFERENCES gen_places(id) ON DELETE CASCADE,
                       succ_place INTEGER REFERENCES gen_places(id) ON DELETE CASCADE,
                       mode TEXT NOT NULL DEFAULT 'sequential',
                       rel_type TEXT DEFAULT 'FS',
                       lag_days NUMERIC DEFAULT 0)""")
    cur.execute("""CREATE INDEX IF NOT EXISTS gen_place_rels_sched
                   ON gen_place_rels (schedule_id)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS gen_scope (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL
                           REFERENCES schedules(id) ON DELETE CASCADE,
                       work_type TEXT NOT NULL,
                       level_key TEXT,
                       place_id INTEGER REFERENCES gen_places(id) ON DELETE CASCADE,
                       mode TEXT NOT NULL DEFAULT 'include')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS gen_settings (
                       schedule_id INTEGER PRIMARY KEY
                           REFERENCES schedules(id) ON DELETE CASCADE,
                       project_code TEXT DEFAULT '',
                       id_pattern TEXT DEFAULT
                           '{PRJ}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}',
                       separator TEXT DEFAULT '-',
                       project_type TEXT,
                       force_calendar INTEGER,
                       last_generated TIMESTAMP,
                       last_diff JSONB NOT NULL DEFAULT '{}'::jsonb)""")

    # أعمدة المولّد على جدول الأنشطة القائم. تُضاف بـALTER محميّ لأنّ الجدول
    # يُنشأ تلقائيًّا وقد امتلأ قبل وجود المولّد.
    cur.execute("ALTER TABLE gen_lib_work_types "
                "ADD COLUMN IF NOT EXISTS csi_division TEXT")
    for col, ddl in (
            ("gen_key", "TEXT"),
            ("overridden", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
            ("qty", "NUMERIC"),
            ("crew_size", "NUMERIC"),
            ("productivity", "NUMERIC"),
            ("driver", "TEXT")):
        cur.execute(f"ALTER TABLE sch_activities "
                    f"ADD COLUMN IF NOT EXISTS {col} {ddl}")
    # مفتاح التوليد هو الهوية، ففريدٌ داخل البرنامج الواحد. والأنشطة
    # المكتوبة باليد (بلا مفتاح) خارج هذا الشرط فلا يقيّدها.
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS sch_activities_genkey
                   ON sch_activities (schedule_id, gen_key)
                   WHERE gen_key IS NOT NULL""")
    cur.execute("""ALTER TABLE sch_relations
                   ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'manual'""")


# ═══════════════════════════ القراءة ═══════════════════════════

def load_definition(cur, sched_id):
    """يقرأ تعريف المشروع ويبنيه شجرةً وكتالوجًا وقواعد."""
    cur.execute("""SELECT p.id, p.parent_id, p.level_key, p.code, p.name, p.seq,
                          p.repeat_count, p.repeat_pattern,
                          COALESCE(l.token, UPPER(p.level_key))
                   FROM gen_places p
                   LEFT JOIN gen_lib_levels l ON l.key = p.level_key
                   WHERE p.schedule_id=%s ORDER BY p.seq, p.id""",
                (sched_id,))
    tree = G.PlaceTree()
    for r in cur.fetchall():
        tree.add(G.Place(str(r[0]), r[2], r[3], r[4] or r[3],
                         parent=str(r[1]) if r[1] else None, seq=r[5] or 0,
                         repeat=max(1, r[6] or 1),
                         repeat_pattern=r[7] or "{code}{n:02d}",
                         token=r[8]))

    # الأسهم المرسومة على اللوحة: «بالتوازي» تقطع الرابط، وغيرها تصنع
    # سلسلة التسلسل التي يقرأ منها ‎next/prev‎.
    cur.execute("""SELECT pred_place, succ_place, mode, rel_type, lag_days
                   FROM gen_place_rels WHERE schedule_id=%s""", (sched_id,))
    for pred, succ, mode, rt, lag in cur.fetchall():
        if not pred or not succ:
            continue
        a, b = str(pred), str(succ)
        if mode == "parallel":
            tree.mark_parallel(a, b)
        else:
            tree.link_places(a, b, (rt or "FS").upper(), float(lag or 0))

    cur.execute("""SELECT work_type, level_key, place_id, mode
                   FROM gen_scope WHERE schedule_id=%s""", (sched_id,))
    inc, exc = {}, {}
    for wt, lvl, pid, mode in cur.fetchall():
        if mode == "include" and lvl:
            inc.setdefault(wt, set()).add(lvl)
        elif mode == "exclude":
            exc.setdefault(wt, set()).add(str(pid) if pid else lvl)

    cur.execute("""SELECT code, name, name_ar, discipline, unit,
                          default_days, productivity, calendar_hint, applies_to
                   FROM gen_lib_work_types ORDER BY seq, id""")
    wts = []
    for r in cur.fetchall():
        code = r[0]
        applies = set(r[8] or []) | inc.get(code, set())
        wts.append(G.WorkType(code, code, r[1] or code, r[3] or "", r[4] or "",
                              float(r[5] or 1), r[6],
                              r[7], tuple(applies), tuple(exc.get(code, ()))))

    cur.execute("""SELECT pred_wt, succ_wt, rel_type, lag_days, scope, origin,
                          COALESCE(note,'')
                   FROM gen_lib_logic WHERE active
                     AND (project_type IS NULL OR project_type = COALESCE(
                          (SELECT project_type FROM gen_settings
                           WHERE schedule_id=%s), project_type))
                   ORDER BY observations DESC NULLS LAST""", (sched_id,))
    rules = []
    for r in cur.fetchall():
        try:
            rules.append(G.Rule(r[0], r[1], r[2], float(r[3] or 0), r[4],
                                r[5] or "derived", r[6]))
        except ValueError:
            continue        # قاعدة بنوع أو نطاق غير معروف تُتخطّى ولا تُسقط الكلّ

    cur.execute("""SELECT project_code, id_pattern, separator, force_calendar
                   FROM gen_settings WHERE schedule_id=%s""", (sched_id,))
    s = cur.fetchone() or ("", "{PRJ}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}",
                           "-", None)
    return tree, wts, rules, {"project": s[0] or "", "pattern": s[1],
                              "sep": s[2] or "-", "force_calendar": s[3]}


def load_existing(cur, sched_id):
    """الأنشطة المتولَّدة الموجودة — عليها تقوم المطابقة."""
    cur.execute("""SELECT gen_key, code, name, duration, calendar_id, wbs,
                          COALESCE(overridden,'[]'::jsonb)
                   FROM sch_activities
                   WHERE schedule_id=%s AND gen_key IS NOT NULL""", (sched_id,))
    out = []
    for r in cur.fetchall():
        try:
            pid, wid = json.loads(r[0])
        except Exception:
            continue
        ov = r[6] if isinstance(r[6], list) else json.loads(r[6] or "[]")
        out.append(G.GenActivity((pid, wid), r[1], r[2],
                                 pid, wid, float(r[3] or 0) / 8.0,
                                 r[4], r[5] or "", ov))
    return out


# ═══════════════════════════ المسارات ═══════════════════════════

@router.post("/api/gen/{sched_id}/preview")
async def preview(sched_id: int, request: Request):
    """
    يولّد ويعرض **الفرق** ولا يكتب شيئًا.

    لا شيء يُنفَّذ قبل أن يُرى: كم يُضاف، وكم يُحذف، وكم يُحدَّث، وأيّ
    تعديلات يدوية محميّة.
    """
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            tree, wts, rules, st = load_definition(cur, sched_id)
            # رمزٌ تترجمه الصفحة، لا نصٌّ عربيّ يُحشر في واجهة إنجليزية.
            if not tree.nodes:
                return JSONResponse({"success": False, "code": "no_places",
                                     "error": "No places defined"})
            if not wts:
                return JSONResponse({"success": False, "code": "no_work_types",
                                     "error": "Work-type catalogue is empty"})
            fresh, rels, diag = G.generate(
                tree, wts, rules, project=st["project"],
                pattern=st["pattern"], default_calendar=st["force_calendar"])
            existing = load_existing(cur, sched_id)
            diff = G.reconcile(existing, fresh)
        return JSONResponse({
            "success": True, "diag": diag, "summary": diff["summary"],
            "relations": len(rels),
            "added": [{"code": a.code, "name": a.name, "days": a.days}
                      for a in diff["added"][:200]],
            "removed": [{"code": a.code, "name": a.name}
                        for a in diff["removed"][:200]],
            "updated": diff["updated"][:200],
        })
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/{sched_id}/apply")
async def apply(sched_id: int, request: Request):
    """
    ينفّذ التوليد. الحقول المعلَّمة `overridden` لا تُمسّ، والعلاقات المضافة
    باليد (`origin='manual'`) تبقى بينما تُبنى المولَّدة من جديد.
    """
    if not _admin(request):
        return _deny()
    body = await request.json() if await request.body() else {}
    delete_missing = bool(body.get("delete_missing", True))
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            tree, wts, rules, st = load_definition(cur, sched_id)
            fresh, rels, diag = G.generate(
                tree, wts, rules, project=st["project"],
                pattern=st["pattern"], default_calendar=st["force_calendar"])
            existing = load_existing(cur, sched_id)
            diff = G.reconcile(existing, fresh)

            # الكتابة دفعات لا استعلامًا لكل صفّ: قاعدة البيانات بعيدة،
            # وألفا نشاط بألفي استعلام تنتهي بانقضاء مهلة الطلب.
            cal = st["force_calendar"]
            sid = str(int(sched_id))

            # **الهوية مفتاح التوليد لا الكود.** الكود يتغيّر: رقمه المتسلسل
            # يزيح كلّما أُضيف مكان قبله. وكان الإدراج يتصادم على الكود، فإذا
            # تغيّر كودُ نشاطٍ أُدرج صفٌّ ثانٍ لمفتاح التوليد نفسه بدل أن
            # يُحدَّث الأوّل — ظهر ذلك مفتاحًا مكرّرًا بعد إعادة توليد زادت
            # أدوارًا. لذلك: تُحرَّر الأكواد أوّلًا، ثمّ يُحدَّث الموجود
            # بمفتاحه، ثمّ يُدرَج ما لا مفتاح له.
            cur.execute("""UPDATE sch_activities
                           SET code = 'TMP#' || id
                           WHERE schedule_id=%s AND gen_key IS NOT NULL""",
                        (sched_id,))

            # الحقول المعلَّمة يدويًّا تُستثنى من الكتابة، لا الصفّ كلّه:
            # من عدّل الاسم يبقى اسمه وتتحدّث مدّته.
            upd = [(json.dumps(list(a.gen_key)), a.code, a.name,
                    a.days * 8.0, a.wbs) for a in fresh]
            UPD_T = "(%s::text,%s::text,%s::text,%s::double precision,%s::text)"
            _batch(cur, """UPDATE sch_activities t SET
                             code = v.code,
                             name = CASE WHEN t.overridden @> '["name"]'::jsonb
                                         THEN t.name ELSE v.name END,
                             duration = CASE WHEN t.overridden @> '["days"]'::jsonb
                                         THEN t.duration ELSE v.dur END,
                             wbs = CASE WHEN t.overridden @> '["wbs"]'::jsonb
                                         THEN t.wbs ELSE v.wbs END
                           FROM (VALUES %s) AS v(gk, code, name, dur, wbs)
                           WHERE t.schedule_id=""" + sid +
                   """ AND t.gen_key = v.gk""", upd, template=UPD_T)

            rows = [(sched_id, json.dumps(list(a.gen_key)), a.code, a.name,
                     a.days * 8.0, cal, a.wbs, "task", "not_started")
                    for a in fresh]
            _batch(cur, """INSERT INTO sch_activities
                             (schedule_id, gen_key, code, name, duration,
                              calendar_id, wbs, atype, status)
                           VALUES %s
                           ON CONFLICT (schedule_id, gen_key)
                             WHERE gen_key IS NOT NULL DO NOTHING""",
                   rows)

            # أيّ صفّ بقي بكودٍ مؤقّت فمفتاحُه لم يَعُد في التوليد. لا يُترك
            # بكودٍ مشوَّه: يُحذف إن سُمح بالحذف، وإلّا يُردّ كودُه المولَّد.
            cur.execute("""DELETE FROM sch_activities
                           WHERE schedule_id=%s AND code LIKE 'TMP#%%'
                             AND gen_key IS NOT NULL AND %s""",
                        (sched_id, delete_missing))
            cur.execute("""UPDATE sch_activities
                           SET code = 'ORPHAN-' || id
                           WHERE schedule_id=%s AND code LIKE 'TMP#%%'""",
                        (sched_id,))

            cur.execute("""DELETE FROM sch_relations
                           WHERE schedule_id=%s
                             AND COALESCE(origin,'manual') <> 'manual'""",
                        (sched_id,))
            rel_rows = [(json.dumps(list(r["pred"])), json.dumps(list(r["succ"])),
                         r["type"], r["lag"] * 8.0) for r in rels]
            REL_T = "(%s::text,%s::text,%s::text,%s::double precision)"
            _batch(cur, """INSERT INTO sch_relations
                             (schedule_id, pred, succ, rtype, lag, origin)
                           SELECT """ + str(int(sched_id)) + """, p.code, s.code,
                                  v.rt, v.lag, 'generated'
                           FROM (VALUES %s) AS v(pgk, sgk, rt, lag)
                           JOIN sch_activities p
                             ON p.schedule_id=""" + str(int(sched_id)) +
                   """ AND p.gen_key = v.pgk
                           JOIN sch_activities s
                             ON s.schedule_id=""" + str(int(sched_id)) +
                   """ AND s.gen_key = v.sgk""", rel_rows, template=REL_T)

            cur.execute("""INSERT INTO gen_settings (schedule_id, last_generated,
                                                     last_diff)
                           VALUES (%s, NOW(), %s)
                           ON CONFLICT (schedule_id) DO UPDATE
                             SET last_generated=NOW(), last_diff=EXCLUDED.last_diff""",
                        (sched_id, json.dumps(diff["summary"])))
        return JSONResponse({"success": True, "diag": diag,
                             "summary": diff["summary"]})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/evidence/import")
async def import_evidence(request: Request):
    """
    يستورد شواهد منطق ويعيد اشتقاق القواعد منها **كلّها مجتمعة**.

    الملفّ الجديد يضيف شواهد ولا يمحو سابقتها، والقاعدة المعلَّمة «يدوية»
    لا تُمسّ — تُعرض فقط حين تخالفها الشواهد الجديدة.
    """
    if not _admin(request):
        return _deny()
    body = await request.json()
    rows = body.get("evidence") or []
    try:
        import logic_evidence as LE
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            for e in rows:
                cur.execute("""INSERT INTO gen_lib_logic_evidence
                                 (source, project, project_type, pred_wt,
                                  succ_wt, rel_type, scope, count,
                                  lag_min, lag_median, lag_max, confidence)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (e.get("source", ""), e.get("project", ""),
                             body.get("project_type"),
                             e["pred_work_type"], e["succ_work_type"],
                             e["rel_type"], e["scope"], e.get("count", 1),
                             e.get("lag_min"), e.get("lag_median"),
                             e.get("lag_max"), e.get("confidence")))

            cur.execute("""SELECT source, project, pred_wt, succ_wt, rel_type,
                                  scope, count, lag_min, lag_median, lag_max,
                                  confidence
                           FROM gen_lib_logic_evidence""")
            allev = [{"source": r[0], "project": r[1], "pred_work_type": r[2],
                      "succ_work_type": r[3], "rel_type": r[4], "scope": r[5],
                      "count": r[6] or 1, "lag_min": float(r[7] or 0),
                      "lag_median": float(r[8] or 0), "lag_max": float(r[9] or 0),
                      "confidence": float(r[10] or 0)} for r in cur.fetchall()]
            rules = LE.derive_rules(allev, min_count=2)

            conflicts = []
            for r in rules:
                cur.execute("""SELECT origin, rel_type, lag_days
                               FROM gen_lib_logic
                               WHERE pred_wt=%s AND succ_wt=%s AND scope=%s
                                 AND project_type IS NULL""",
                            (r["pred_work_type"], r["succ_work_type"],
                             r["scope"]))
                cur_row = cur.fetchone()
                if cur_row and cur_row[0] == "manual":
                    if cur_row[1] != r["rel_type"]:
                        conflicts.append({
                            "pair": f"{r['pred_work_type']} → {r['succ_work_type']}",
                            "scope": r["scope"], "manual": cur_row[1],
                            "evidence": r["rel_type"],
                            "observations": r["observations"]})
                    continue        # اليدوية لا تُمسّ
                cur.execute("""INSERT INTO gen_lib_logic
                                 (pred_wt, succ_wt, rel_type, lag_days, scope,
                                  origin, observations, agreement, sources,
                                  lag_min, lag_max, confidence, updated_at)
                               VALUES (%s,%s,%s,%s,%s,'derived',%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (pred_wt, succ_wt, scope)
                                 WHERE project_type IS NULL
                               DO UPDATE SET rel_type=EXCLUDED.rel_type,
                                 lag_days=EXCLUDED.lag_days,
                                 observations=EXCLUDED.observations,
                                 agreement=EXCLUDED.agreement,
                                 sources=EXCLUDED.sources,
                                 lag_min=EXCLUDED.lag_min,
                                 lag_max=EXCLUDED.lag_max,
                                 confidence=EXCLUDED.confidence,
                                 updated_at=NOW()""",
                            (r["pred_work_type"], r["succ_work_type"],
                             r["rel_type"], r["lag_days"], r["scope"],
                             r["observations"], r["agreement"], r["sources"],
                             r["lag_min"], r["lag_max"], r["confidence"]))
        return JSONResponse({"success": True, "imported": len(rows),
                             "evidence_total": len(allev),
                             "rules": len(rules),
                             "conflicts_with_manual": conflicts})
    except Exception as e:
        return _fail(e)


@router.get("/api/gen/rules")
async def list_rules(request: Request):
    """القواعد الفاعلة، الأقوى شهادةً أولًا، ومعها ما يُقرأ به وزنها."""
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT pred_wt, succ_wt, rel_type, lag_days, scope,
                                  origin, observations, agreement, sources,
                                  lag_min, lag_max, confidence, active, note
                           FROM gen_lib_logic
                           ORDER BY observations DESC NULLS LAST, pred_wt""")
            cols = ["pred_wt", "succ_wt", "rel_type", "lag_days", "scope",
                    "origin", "observations", "agreement", "sources",
                    "lag_min", "lag_max", "confidence", "active", "note"]
            # أعمدة NUMERIC تعود من بوستجرس كـDecimal، وهو لا يُحوَّل إلى JSON
            rows = [{k: _plain(v) for k, v in zip(cols, r)}
                    for r in cur.fetchall()]
        return JSONResponse({"success": True, "rules": rows,
                             "count": len(rows)}, )
    except Exception as e:
        return _fail(e)


# ═══════════════════════ تعريف المشروع: قراءة وكتابة ═══════════════════════

@router.get("/api/gen/{sched_id}/definition")
async def get_definition(sched_id: int, request: Request):
    """كلّ ما تحتاجه الصفحة: الأماكن والكتالوج والانطباق والإعدادات."""
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT id, parent_id, level_key, code, name, seq,
                                  repeat_count, repeat_pattern, pos_x, pos_y
                           FROM gen_places WHERE schedule_id=%s
                           ORDER BY seq, id""", (sched_id,))
            places = [{"id": r[0], "parent": r[1], "level": r[2], "code": r[3],
                       "name": r[4], "seq": r[5], "repeat": r[6] or 1,
                       "pattern": r[7], "x": _plain(r[8]), "y": _plain(r[9])}
                      for r in cur.fetchall()]

            cur.execute("""SELECT pred_place, succ_place, mode, rel_type, lag_days
                           FROM gen_place_rels WHERE schedule_id=%s""",
                        (sched_id,))
            prels = [{"pred": r[0], "succ": r[1], "mode": r[2],
                      "rel_type": r[3] or "FS", "lag_days": _plain(r[4]) or 0}
                     for r in cur.fetchall()]

            cur.execute("""SELECT code, name, name_ar, discipline, unit,
                                  default_days, applies_to, seq, csi_division
                           FROM gen_lib_work_types ORDER BY seq, id""")
            wts = [{"code": r[0], "name": r[1], "name_ar": r[2],
                    "discipline": r[3], "unit": r[4],
                    "days": float(r[5] or 1), "applies_to": r[6] or [],
                    "seq": r[7], "csi_division": r[8]} for r in cur.fetchall()]

            cur.execute("""SELECT work_type, level_key, place_id, mode
                           FROM gen_scope WHERE schedule_id=%s""", (sched_id,))
            scope = [{"work_type": r[0], "level": r[1], "place": r[2],
                      "mode": r[3]} for r in cur.fetchall()]

            cur.execute("""SELECT project_code, id_pattern, separator,
                                  project_type, force_calendar, last_generated,
                                  last_diff
                           FROM gen_settings WHERE schedule_id=%s""",
                        (sched_id,))
            s = cur.fetchone()
            settings = {
                "project_code": s[0] if s else "",
                "id_pattern": s[1] if s else
                "{PRJ}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}",
                "separator": s[2] if s else "-",
                "project_type": s[3] if s else None,
                "force_calendar": s[4] if s else None,
                "last_generated": str(s[5]) if s and s[5] else None,
                "last_diff": s[6] if s else {}}

            cur.execute("""SELECT COUNT(*) FROM sch_activities
                           WHERE schedule_id=%s AND gen_key IS NOT NULL""",
                        (sched_id,))
            generated = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM gen_lib_logic WHERE active")
            nrules = cur.fetchone()[0]
        return JSONResponse({"success": True, "places": places,
                             "place_rels": prels, "work_types": wts,
                             "scope": scope, "settings": settings,
                             "generated": generated, "rules": nrules})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/{sched_id}/places")
async def save_places(sched_id: int, request: Request):
    """
    حفظ شجرة الأماكن **استبدالًا كاملًا** لا تعديلًا جزئيًّا.

    الشجرة تُحرَّر كورقة: تُضاف عقد وتُحذف ويتغيّر ترتيبها في التحرير الواحد،
    فمطابقة ما تغيّر عقدةً عقدةً تكلّف أكثر ممّا تنفع وتترك روابط معلَّقة عند
    أوّل خطأ. والأنشطة لا تضيع بذلك: مفتاح التوليد يحمل معرّف المكان، والمعرّف
    يُعاد كما هو لكلّ عقدة باقية.
    """
    if not _admin(request):
        return _deny()
    body = await request.json()
    nodes = body.get("places") or []
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            # الصفّ الجديد يأتي بمعرّف مؤقّت من الصفحة (‎new1‎)، وليس رقمًا.
            # كان يُمرَّر إلى ‎int()‎ فيرفع الاستثناء قبل أن تُكتب سطرًا واحدًا،
            # فلم يكن يُحفظ مكانٌ واحد أبدًا — وكلّ ما بعده كان يشكو من
            # «لا أماكن معرَّفة» وهي على الشاشة أمام المستخدم.
            keep = [int(n["id"]) for n in nodes
                    if str(n.get("id") or "").isdigit()]
            if keep:
                cur.execute("""DELETE FROM gen_places WHERE schedule_id=%s
                               AND NOT (id = ANY(%s))""", (sched_id, keep))
            else:
                cur.execute("DELETE FROM gen_places WHERE schedule_id=%s",
                            (sched_id,))
            idmap = {}
            for n in nodes:               # الآباء قبل الأبناء
                pid = n.get("id")
                parent = n.get("parent")
                parent = idmap.get(parent, parent)
                args = (sched_id, parent, n.get("level") or "zone",
                        (n.get("code") or "").strip(),
                        (n.get("name") or "").strip() or n.get("code"),
                        int(n.get("seq") or 0),
                        max(1, int(n.get("repeat") or 1)),
                        n.get("pattern") or "{code}{n:02d}",
                        n.get("x"), n.get("y"))
                if pid and not str(pid).startswith("new"):
                    cur.execute("""UPDATE gen_places SET parent_id=%s,
                                     level_key=%s, code=%s, name=%s, seq=%s,
                                     repeat_count=%s, repeat_pattern=%s,
                                     pos_x=%s, pos_y=%s
                                   WHERE id=%s AND schedule_id=%s""",
                                args[1:] + (int(pid), sched_id))
                    idmap[pid] = int(pid)
                else:
                    cur.execute("""INSERT INTO gen_places
                                     (schedule_id,parent_id,level_key,code,
                                      name,seq,repeat_count,repeat_pattern,
                                      pos_x,pos_y)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   RETURNING id""", args)
                    idmap[pid] = cur.fetchone()[0]

            cur.execute("DELETE FROM gen_place_rels WHERE schedule_id=%s",
                        (sched_id,))
            for r in body.get("place_rels") or []:
                pr = idmap.get(r.get("pred"), r.get("pred"))
                sc = idmap.get(r.get("succ"), r.get("succ"))
                if not pr or not sc or str(pr) == str(sc):
                    continue
                cur.execute("""INSERT INTO gen_place_rels
                                 (schedule_id,pred_place,succ_place,mode,
                                  rel_type,lag_days)
                               VALUES (%s,%s,%s,%s,%s,%s)""",
                            (sched_id, pr, sc, r.get("mode") or "sequential",
                             (r.get("rel_type") or "FS").upper(),
                             float(r.get("lag_days") or 0)))
        return JSONResponse({"success": True, "ids": {str(k): v
                                                      for k, v in idmap.items()}})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/work-types")
async def save_work_types(request: Request):
    """كتالوج الشركة — مشترك بين المشاريع، فيُكتب مرّة ويُعاد استعماله."""
    if not _admin(request):
        return _deny()
    body = await request.json()
    rows = body.get("work_types") or []
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            codes = [(w.get("code") or "").strip().upper() for w in rows
                     if (w.get("code") or "").strip()]
            if body.get("replace") and codes:
                cur.execute("""DELETE FROM gen_lib_work_types
                               WHERE NOT (code = ANY(%s))""", (codes,))
            data = [(c, (w.get("name") or c).strip(),
                     (w.get("name_ar") or "").strip() or None,
                     (w.get("discipline") or "").strip().upper(),
                     (w.get("unit") or "").strip(),
                     float(w.get("days") or 1),
                     json.dumps(w.get("applies_to") or []),
                     int(w.get("seq") or i),
                     (w.get("csi_division") or "").strip() or None)
                    for i, (w, c) in enumerate(zip(rows, codes))]
            _batch(cur, """INSERT INTO gen_lib_work_types
                             (code,name,name_ar,discipline,unit,default_days,
                              applies_to,seq,csi_division)
                           VALUES %s
                           ON CONFLICT (code) DO UPDATE SET
                             csi_division=EXCLUDED.csi_division,
                             name=EXCLUDED.name, name_ar=EXCLUDED.name_ar,
                             discipline=EXCLUDED.discipline,
                             unit=EXCLUDED.unit,
                             default_days=EXCLUDED.default_days,
                             applies_to=EXCLUDED.applies_to,
                             seq=EXCLUDED.seq""", data,
                   template="(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)")
        return JSONResponse({"success": True, "saved": len(data)})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/{sched_id}/scope")
async def save_scope(sched_id: int, request: Request):
    """مصفوفة الانطباق — استثناءات على المستوى أو على عقدة بعينها."""
    if not _admin(request):
        return _deny()
    body = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM gen_scope WHERE schedule_id=%s", (sched_id,))
            rows = [(sched_id, r["work_type"], r.get("level"),
                     r.get("place"), r.get("mode") or "include")
                    for r in (body.get("scope") or []) if r.get("work_type")]
            _batch(cur, """INSERT INTO gen_scope
                             (schedule_id,work_type,level_key,place_id,mode)
                           VALUES %s""", rows)
        return JSONResponse({"success": True, "saved": len(rows)})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/{sched_id}/settings")
async def save_settings(sched_id: int, request: Request):
    """نمط الكود ورمز المشروع ونوعه والتقويم الموحَّد إن فُرض."""
    if not _admin(request):
        return _deny()
    b = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO gen_settings
                             (schedule_id, project_code, id_pattern, separator,
                              project_type, force_calendar)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (schedule_id) DO UPDATE SET
                             project_code=EXCLUDED.project_code,
                             id_pattern=EXCLUDED.id_pattern,
                             separator=EXCLUDED.separator,
                             project_type=EXCLUDED.project_type,
                             force_calendar=EXCLUDED.force_calendar""",
                        (sched_id, (b.get("project_code") or "").strip().upper(),
                         b.get("id_pattern") or
                         "{PRJ}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}",
                         b.get("separator") or "-",
                         b.get("project_type") or None,
                         b.get("force_calendar") or None))
        return JSONResponse({"success": True})
    except Exception as e:
        return _fail(e)


@router.get("/admin-generator/{sched_id}", response_class=HTMLResponse)
async def page_generator(sched_id: int, request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    return _templates.TemplateResponse(request, "generator.html", {
        "admin_user": admin_user, "active_page": "schedule",
        "sched_id": sched_id})


@router.post("/api/gen/rules")
async def save_rules(request: Request):
    """
    كتابة قواعد الربط باليد.

    من غير قاعدة واحدة يخرج التوليد أنشطةً مفكوكة بلا علاقة بينها — عددٌ
    صحيح وشبكةٌ لا وجود لها. والقاعدة المكتوبة هنا تُعلَّم **يدوية**، فلا
    تمسّها إعادة الاشتقاق حين تُستورد ملفّات شواهد جديدة.
    """
    if not _admin(request):
        return _deny()
    body = await request.json()
    rows = body.get("rules") or []
    SCOPES = ("same", "next", "prev", "parent", "children")
    TYPES = ("FS", "SS", "FF", "SF")
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            clean = []
            for r in rows:
                p = (r.get("pred_wt") or "").strip().upper()
                s = (r.get("succ_wt") or "").strip().upper()
                sc = (r.get("scope") or "same").strip().lower()
                t = (r.get("rel_type") or "FS").strip().upper()
                if not p or not s or sc not in SCOPES or t not in TYPES:
                    continue
                clean.append((p, s, t, float(r.get("lag_days") or 0), sc))
            if body.get("replace"):
                cur.execute("""DELETE FROM gen_lib_logic
                               WHERE origin='manual' AND project_type IS NULL""")
            _batch(cur, """INSERT INTO gen_lib_logic
                             (pred_wt, succ_wt, rel_type, lag_days, scope,
                              origin, active, updated_at)
                           VALUES %s
                           ON CONFLICT (pred_wt, succ_wt, scope)
                             WHERE project_type IS NULL
                           DO UPDATE SET rel_type=EXCLUDED.rel_type,
                             lag_days=EXCLUDED.lag_days, active=TRUE,
                             origin='manual', updated_at=NOW()""",
                   [(p, s, t, lag, sc, "manual", True) for p, s, t, lag, sc in clean],
                   template="(%s,%s,%s,%s,%s,%s,%s,NOW())")
        return JSONResponse({"success": True, "saved": len(clean)})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/rules/delete")
async def delete_rule(request: Request):
    """حذف قاعدة بعينها."""
    if not _admin(request):
        return _deny()
    b = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM gen_lib_logic
                           WHERE pred_wt=%s AND succ_wt=%s AND scope=%s
                             AND project_type IS NULL""",
                        ((b.get("pred_wt") or "").upper(),
                         (b.get("succ_wt") or "").upper(),
                         (b.get("scope") or "same")))
        return JSONResponse({"success": True})
    except Exception as e:
        return _fail(e)


# ═══════════════════ المستويات وأقسام CSI ═══════════════════

@router.get("/api/gen/levels")
async def list_levels(request: Request):
    """مستويات التقسيم وأقسام MasterFormat — الأولى تُحرَّر والثانية تُقرأ."""
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT key, label, label_ar, token, seq, active
                           FROM gen_lib_levels ORDER BY seq, id""")
            levels = [{"key": r[0], "label": r[1], "label_ar": r[2],
                       "token": r[3] or r[0].upper(), "seq": r[4],
                       "active": r[5]} for r in cur.fetchall()]
            cur.execute("""SELECT division, name, name_ar FROM gen_lib_csi
                           ORDER BY seq""")
            csi = [{"division": r[0], "name": r[1], "name_ar": r[2]}
                   for r in cur.fetchall()]
        return JSONResponse({"success": True, "levels": levels, "csi": csi})
    except Exception as e:
        return _fail(e)


@router.post("/api/gen/levels")
async def save_levels(request: Request):
    """
    حفظ مستويات التقسيم.

    المستوى المستعمَل في أماكن قائمة لا يُحذف صامتًا: تُعاد أسماء الأماكن
    التي تمنع حذفه، فيقرّر المستخدم. حذفٌ صامت هنا يُيتّم عقدًا في الشجرة.
    """
    if not _admin(request):
        return _deny()
    body = await request.json()
    rows = body.get("levels") or []
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            keys = []
            for i, l in enumerate(rows):
                k = (l.get("key") or l.get("label") or "").strip().lower()
                k = "".join(c if c.isalnum() else "_" for c in k).strip("_")
                if not k:
                    continue
                keys.append(k)
                cur.execute("""INSERT INTO gen_lib_levels
                                 (key,label,label_ar,token,seq,active)
                               VALUES (%s,%s,%s,%s,%s,TRUE)
                               ON CONFLICT (key) DO UPDATE SET
                                 label=EXCLUDED.label,
                                 label_ar=EXCLUDED.label_ar,
                                 token=EXCLUDED.token, seq=EXCLUDED.seq,
                                 active=TRUE""",
                            (k, (l.get("label") or k).strip(),
                             (l.get("label_ar") or "").strip() or None,
                             (l.get("token") or k).strip().upper(),
                             int(l.get("seq") or (i + 1) * 10)))
            blocked = []
            if keys:
                cur.execute("""SELECT DISTINCT level_key FROM gen_places
                               WHERE NOT (level_key = ANY(%s))""", (keys,))
                blocked = [r[0] for r in cur.fetchall()]
                cur.execute("""DELETE FROM gen_lib_levels
                               WHERE NOT (key = ANY(%s))
                                 AND key NOT IN (SELECT DISTINCT level_key
                                                 FROM gen_places)""", (keys,))
        return JSONResponse({"success": True, "saved": len(keys),
                             "in_use": blocked})
    except Exception as e:
        return _fail(e)
