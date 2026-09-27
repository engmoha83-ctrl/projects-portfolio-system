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
from fastapi.responses import JSONResponse

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


def _fail(e):
    return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-800:]}, status_code=500)


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
                       unit TEXT,
                       default_days NUMERIC DEFAULT 1,
                       productivity NUMERIC,
                       calendar_hint TEXT,
                       applies_to JSONB NOT NULL DEFAULT '[]'::jsonb,
                       seq INTEGER DEFAULT 0,
                       origin TEXT DEFAULT 'يدوية',
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
                       origin TEXT DEFAULT 'مشتقّة',
                       observations INTEGER DEFAULT 0,
                       agreement NUMERIC,
                       sources INTEGER DEFAULT 0,
                       lag_min NUMERIC, lag_max NUMERIC,
                       confidence NUMERIC,
                       note TEXT,
                       active BOOLEAN DEFAULT TRUE,
                       updated_at TIMESTAMP DEFAULT NOW(),
                       UNIQUE (pred_wt, succ_wt, scope, project_type))""")

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
                       repeat_pattern TEXT DEFAULT '{code}{n:02d}')""")
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
    for col, ddl in (
            ("gen_key", "TEXT"),
            ("overridden", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
            ("qty", "NUMERIC"),
            ("crew_size", "NUMERIC"),
            ("productivity", "NUMERIC"),
            ("driver", "TEXT")):
        cur.execute(f"ALTER TABLE sch_activities "
                    f"ADD COLUMN IF NOT EXISTS {col} {ddl}")
    cur.execute("""CREATE INDEX IF NOT EXISTS sch_activities_genkey
                   ON sch_activities (schedule_id, gen_key)""")
    cur.execute("""ALTER TABLE sch_relations
                   ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'manual'""")


# ═══════════════════════════ القراءة ═══════════════════════════

def load_definition(cur, sched_id):
    """يقرأ تعريف المشروع ويبنيه شجرةً وكتالوجًا وقواعد."""
    cur.execute("""SELECT id, parent_id, level_key, code, name, seq,
                          repeat_count, repeat_pattern
                   FROM gen_places WHERE schedule_id=%s ORDER BY seq, id""",
                (sched_id,))
    tree = G.PlaceTree()
    for r in cur.fetchall():
        tree.add(G.Place(str(r[0]), r[2], r[3], r[4] or r[3],
                         parent=str(r[1]) if r[1] else None, seq=r[5] or 0,
                         repeat=max(1, r[6] or 1),
                         repeat_pattern=r[7] or "{code}{n:02d}"))

    cur.execute("""SELECT pred_place, succ_place, mode FROM gen_place_rels
                   WHERE schedule_id=%s""", (sched_id,))
    for pred, succ, mode in cur.fetchall():
        if mode == "parallel" and pred and succ:
            tree.mark_parallel(str(pred), str(succ))

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
        wts.append(G.WorkType(code, code, r[2] or r[1], r[3] or "", r[4] or "",
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
                                r[5] or "مشتقّة", r[6]))
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
            if not tree.nodes:
                return JSONResponse({"success": False,
                                     "error": "لا أماكن معرَّفة في هذا البرنامج"})
            if not wts:
                return JSONResponse({"success": False,
                                     "error": "كتالوج أنواع الأعمال فارغ"})
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

            keep = {json.dumps(list(a.gen_key)) for a in fresh}
            if delete_missing and diff["removed"]:
                cur.execute("""DELETE FROM sch_activities
                               WHERE schedule_id=%s AND gen_key IS NOT NULL
                                 AND NOT (gen_key = ANY(%s))""",
                            (sched_id, list(keep)))

            cal = st["force_calendar"]
            for a in fresh:
                gk = json.dumps(list(a.gen_key))
                # الإدراج يتصادم على (البرنامج، الكود) — وهو فهرس فريد قائم
                cur.execute("""INSERT INTO sch_activities
                                 (schedule_id, gen_key, code, name,
                                  duration, calendar_id, wbs, atype, status)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,'task','not_started')
                               ON CONFLICT (schedule_id, code) DO NOTHING""",
                            (sched_id, gk, a.code, a.name, a.days * 8.0,
                             cal, a.wbs))
                # الحقول المعلَّمة يدويًّا تُستثنى من الكتابة، لا الصفّ كلّه:
                # من عدّل الاسم يبقى اسمه وتتحدّث مدّته.
                cur.execute("""UPDATE sch_activities SET
                                 code = CASE WHEN overridden @> '["code"]'::jsonb
                                             THEN code ELSE %s END,
                                 name = CASE WHEN overridden @> '["name"]'::jsonb
                                             THEN name ELSE %s END,
                                 duration = CASE WHEN overridden @> '["days"]'::jsonb
                                             THEN duration ELSE %s END,
                                 wbs = CASE WHEN overridden @> '["wbs"]'::jsonb
                                             THEN wbs ELSE %s END
                               WHERE schedule_id=%s AND gen_key=%s""",
                            (a.code, a.name, a.days * 8.0, a.wbs, sched_id, gk))

            cur.execute("""DELETE FROM sch_relations
                           WHERE schedule_id=%s
                             AND COALESCE(origin,'manual') <> 'manual'""",
                        (sched_id,))
            for r in rels:
                cur.execute("""INSERT INTO sch_relations
                                 (schedule_id, pred, succ, rtype, lag, origin)
                               SELECT %s, p.code, s.code, %s, %s, 'generated'
                               FROM sch_activities p, sch_activities s
                               WHERE p.schedule_id=%s AND s.schedule_id=%s
                                 AND p.gen_key=%s AND s.gen_key=%s""",
                            (sched_id, r["type"], r["lag"] * 8.0,
                             sched_id, sched_id,
                             json.dumps(list(r["pred"])),
                             json.dumps(list(r["succ"]))))

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
                if cur_row and cur_row[0] == "يدوية":
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
                               VALUES (%s,%s,%s,%s,%s,'مشتقّة',%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (pred_wt, succ_wt, scope, project_type)
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
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return JSONResponse({"success": True, "rules": rows,
                             "count": len(rows)}, )
    except Exception as e:
        return _fail(e)
