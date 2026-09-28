# -*- coding: utf-8 -*-
"""
جدول الكميّات داخل البرنامج الزمني: استيراده، وتفكيكه، والتوليد منه.

البند يُحفظ كما جاء، ومعه ثلاثة قرارات تُقترح آليًّا وتُعدَّل باليد:
نوع عمله، وأين يُسكَّن من تقسيمة المشروع وبأيّ نسبة، وهل يُقسَّم لخطوات. و
القرار المعدَّل باليد يُعلَّم ‎manual‎ فلا تكتب فوقه إعادةُ الاقتراح.

والتوليد يكتب في ‎sch_activities‎ نفسه الذي يقرأ منه المحرّك، بمفتاح توليد
‎(بند، مكان، خطوة)‎ لا يتغيّر، فتبقى التعديلات اليدوية المعلَّمة ‎overridden‎.
"""

import json
import os
import tempfile
import traceback
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse

import boq_parser
import breakdown as B
import dictionaries_api
import generator as G
import generator_api

router = APIRouter()
_get_conn = None


def setup(get_conn):
    global _get_conn
    _get_conn = get_conn


def _admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


def _deny():
    return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)


def _fail(e):
    return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()[-800:]}, status_code=500)


_batch = generator_api._batch
_plain = generator_api._plain

# المحلّل يكتب حالة الحساب بالعربية لأنّه أداة داخلية؛ الواجهة إنجليزية فتُخزَّن رموزًا.
_CHECK = {"مطابق": "match", "مختلف": "mismatch", "سعر مفقود": "rate_missing",
          "بلا سعر": "no_price", "عمود الإجمالي مشكوك فيه": "suspect_amount"}
_CTX = ("bill", "section", "group_l1", "group_l2", "group_l3", "category")
_TOK = ("building", "floor", "room", "csi_division", "code_work_type",
        "code_discipline", "materials", "dimensions", "diameter", "thickness",
        "capacity", "weight", "tag_code", "spec_ref")


# ═══════════════════════════ الجداول ═══════════════════════════

def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_boq_items (
                       id SERIAL PRIMARY KEY,
                       schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                       seq INTEGER DEFAULT 0,
                       source_file TEXT, source_sheet TEXT, source_row INTEGER,
                       price_source TEXT,
                       item_no TEXT, item_name TEXT NOT NULL DEFAULT '',
                       unit TEXT, qty NUMERIC, unit_rate NUMERIC, amount NUMERIC,
                       amount_check TEXT,
                       context JSONB NOT NULL DEFAULT '{}'::jsonb,
                       tokens JSONB NOT NULL DEFAULT '{}'::jsonb,
                       work_type TEXT,
                       wt_origin TEXT DEFAULT '',
                       wt_score NUMERIC,
                       candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
                       split BOOLEAN NOT NULL DEFAULT FALSE,
                       include BOOLEAN NOT NULL DEFAULT TRUE,
                       alloc_origin TEXT DEFAULT '',
                       created_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("CREATE INDEX IF NOT EXISTS gen_boq_sched ON gen_boq_items (schedule_id, seq, id)")
    # التسكين كما اختاره صاحبه؛ يُنزَل إلى مستوى نوع العمل عند التوليد.
    cur.execute("""CREATE TABLE IF NOT EXISTS gen_boq_alloc (
                       item_id INTEGER NOT NULL REFERENCES gen_boq_items(id) ON DELETE CASCADE,
                       place_id INTEGER NOT NULL REFERENCES gen_places(id) ON DELETE CASCADE,
                       share NUMERIC NOT NULL DEFAULT 100,
                       PRIMARY KEY (item_id, place_id))""")
    # أعمدة الأنشطة المتولَّدة من البنود: الكميّة والتكلفة والوزن والساعات.
    for col, ddl in (("gen_kind", "TEXT"), ("boq_item", "INTEGER"),
                     ("place_key", "TEXT"), ("work_type", "TEXT"), ("step", "TEXT"),
                     ("unit", "TEXT"), ("cost", "NUMERIC"), ("res_cost", "NUMERIC"),
                     ("weight", "NUMERIC"), ("labor_hours", "NUMERIC"),
                     ("equip_hours", "NUMERIC"),
                     ("resources", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
                     ("materials", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                     ("discipline", "TEXT"), ("csi_division", "TEXT")):
        cur.execute(f"ALTER TABLE sch_activities ADD COLUMN IF NOT EXISTS {col} {ddl}")
    cur.execute("""ALTER TABLE gen_settings
                   ADD COLUMN IF NOT EXISTS last_rollup JSONB NOT NULL DEFAULT '{}'::jsonb""")


# ═══════════════════════════ القراءة ═══════════════════════════

def load_items(cur, sid):
    cur.execute("""SELECT id, seq, source_file, source_sheet, source_row, price_source,
                          item_no, item_name, unit, qty, unit_rate, amount, amount_check,
                          context, tokens, work_type, wt_origin, wt_score, candidates,
                          split, include, alloc_origin
                   FROM gen_boq_items WHERE schedule_id=%s ORDER BY seq, id""", (sid,))
    cols = [d[0] for d in cur.description]
    items = [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
    cur.execute("""SELECT a.item_id, a.place_id, a.share FROM gen_boq_alloc a
                   JOIN gen_boq_items i ON i.id = a.item_id
                   WHERE i.schedule_id=%s ORDER BY a.item_id, a.place_id""", (sid,))
    allocs = {}
    for iid, pid, sh in cur.fetchall():
        allocs.setdefault(iid, []).append((str(pid), float(sh)))
    return items, allocs


def _flat(item):
    """البند بحقوله مسطّحةً كما يتوقّعها التصنيف والتسكين."""
    out = dict(item)
    out.update(item.get("context") or {})
    out.update(item.get("tokens") or {})
    return out


def _level_role(cur):
    """دور كلّ مستوى في التسكين: أيّها «مبنى» وأيّها «دور»."""
    cur.execute("SELECT key, label, token FROM gen_lib_levels")
    role = {}
    for k, l, t in cur.fetchall():
        s = f"{k} {l} {t}".lower()
        if any(w in s for w in ("building", "block", "bldg", "villa", "tower")):
            role[k] = "building"
        elif any(w in s for w in ("floor", "level", "storey", "story")):
            role[k] = "floor"
    return lambda lvl: role.get(lvl)


def auto_fill(cur, sid, ids=None, what=("classify", "allocate"), force=False):
    """
    يقترح نوع العمل والتسكين للبنود. ما عُدِّل باليد لا يُمسّ إلا بـ‎force‎.

    يعيد عدد ما صُنِّف وما سُكِّن.
    """
    tree, _w, _r, _s = generator_api.load_definition(cur, sid)
    tree._index()
    dicts = dictionaries_api.load_all(cur)
    wts = dicts["work_types"]
    role = _level_role(cur)
    items, allocs = load_items(cur, sid)
    if ids is not None:
        want = {int(i) for i in ids}
        items = [i for i in items if i["id"] in want]

    cls, loc = [], []
    for it in items:
        f = _flat(it)
        if "classify" in what and (force or it["wt_origin"] != "manual"):
            code, score, cands = B.classify(f, wts)
            cls.append((it["id"], code, "auto" if code else "", score,
                        json.dumps(cands)))
        if "allocate" in what and (force or it["alloc_origin"] != "manual"):
            places = B.locate(f, tree, role)
            loc.append((it["id"], places))
    if cls:
        _batch(cur, """UPDATE gen_boq_items t SET work_type=v.wt, wt_origin=v.o,
                         wt_score=v.s, candidates=v.c::jsonb
                       FROM (VALUES %s) AS v(id, wt, o, s, c) WHERE t.id=v.id""",
               cls, template="(%s::int,%s::text,%s::text,%s::numeric,%s::text)")
    if loc:
        cur.execute("DELETE FROM gen_boq_alloc WHERE item_id = ANY(%s)",
                    ([i for i, _ in loc],))
        rows = []
        for iid, places in loc:
            for pid, sh in B.equal_shares(places):
                rows.append((iid, int(pid), sh))
        _batch(cur, "INSERT INTO gen_boq_alloc (item_id, place_id, share) VALUES %s", rows)
        _batch(cur, """UPDATE gen_boq_items t SET alloc_origin=v.o
                       FROM (VALUES %s) AS v(id, o) WHERE t.id=v.id""",
               [(iid, "auto" if places else "") for iid, places in loc],
               template="(%s::int,%s::text)")
    return {"classified": sum(1 for c in cls if c[1]),
            "located": sum(1 for _, p in loc if p)}


# ═══════════════════════════ المسارات: الاستيراد ═══════════════════════════

@router.post("/api/boq/{sid}/parse")
async def boq_parse(sid: int, request: Request, file: UploadFile = File(...)):
    """
    يقرأ ملفّ جدول الكميّات ولا يحفظ شيئًا: يعيد البنود وجهات التسعير لتُراجَع.

    ملفّ فيه أكثر من جهة مسعِّرة (عروض متعدّدة) يُعاد بكلّ جهاته، ويختار
    المستخدم أيّها يُعتمد قبل الحفظ.
    """
    if not _admin(request):
        return _deny()
    data = await file.read()
    suffix = os.path.splitext(file.filename or "")[1].lower() or ".xlsx"
    if suffix not in (".xlsx", ".xlsm"):
        return JSONResponse({"success": False,
                             "error": "Upload the BOQ as an Excel file (.xlsx)."})
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
            tf.write(data)
            tf.flush()
            items, groups, unparsed, diags = boq_parser.parse_workbook(
                tf.name, file.filename)
    except Exception as e:
        return JSONResponse({"success": False,
                             "error": f"This file could not be read as a BOQ ({type(e).__name__})."})
    out, sources = [], {}
    for r in items:
        src = r.get("price_source") or ""
        sources[src] = sources.get(src, 0) + 1
        out.append({
            "source_file": r.get("source_file"), "source_sheet": r.get("source_sheet"),
            "source_row": r.get("source_row"), "price_source": src,
            "item_no": str(r.get("item_no") or ""),
            "item_name": str(r.get("item_name") or ""),
            "unit": r.get("unit") or r.get("unit_raw") or "",
            "qty": r.get("qty"), "unit_rate": r.get("unit_rate"),
            "amount": r.get("amount"),
            "amount_check": _CHECK.get(r.get("amount_check"), r.get("amount_check") or ""),
            "context": {k: r.get(k) for k in _CTX if r.get(k)},
            "tokens": {k: r.get(k) for k in _TOK if r.get(k)}})
    return JSONResponse({"success": True, "file": file.filename,
                         "items": out,
                         "sources": [{"name": k, "rows": v} for k, v in sources.items()],
                         "unparsed": len(unparsed),
                         "sheets": len(diags)})


@router.post("/api/boq/{sid}/items")
async def boq_save_items(sid: int, request: Request):
    """يحفظ البنود المختارة (بعد المراجعة)، ثمّ يقترح نوع العمل والتسكين لها."""
    if not _admin(request):
        return _deny()
    b = await request.json()
    rows = b.get("items") or []
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            if b.get("mode") == "replace":
                cur.execute("DELETE FROM gen_boq_items WHERE schedule_id=%s", (sid,))
            cur.execute("SELECT COALESCE(MAX(seq),0) FROM gen_boq_items WHERE schedule_id=%s",
                        (sid,))
            base = cur.fetchone()[0] or 0

            def f(v):
                try:
                    return None if v in (None, "") else float(v)
                except (TypeError, ValueError):
                    return None
            data = [(sid, base + (i + 1) * 10, r.get("source_file"), r.get("source_sheet"),
                     r.get("source_row"), r.get("price_source") or "",
                     str(r.get("item_no") or ""), str(r.get("item_name") or "")[:2000],
                     r.get("unit") or "", f(r.get("qty")), f(r.get("unit_rate")),
                     f(r.get("amount")), r.get("amount_check") or "",
                     json.dumps(r.get("context") or {}), json.dumps(r.get("tokens") or {}))
                    for i, r in enumerate(rows)]
            ids = []
            from psycopg2.extras import execute_values
            for k in range(0, len(data), 500):
                ids += [x[0] for x in execute_values(cur, """INSERT INTO gen_boq_items
                    (schedule_id, seq, source_file, source_sheet, source_row, price_source,
                     item_no, item_name, unit, qty, unit_rate, amount, amount_check,
                     context, tokens) VALUES %s RETURNING id""", data[k:k + 500],
                    template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                    fetch=True)]
            res = auto_fill(cur, sid, ids)
        return JSONResponse({"success": True, "saved": len(ids), **res})
    except Exception as e:
        return _fail(e)


# ═══════════════════════════ المسارات: التفكيك ═══════════════════════════

@router.get("/api/boq/{sid}")
async def boq_get(sid: int, request: Request):
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            items, allocs = load_items(cur, sid)
            for it in items:
                it["alloc"] = allocs.get(it["id"], [])
        return JSONResponse({"success": True, "items": items})
    except Exception as e:
        return _fail(e)


@router.post("/api/boq/{sid}/edit")
async def boq_edit(sid: int, request: Request):
    """
    تعديلٌ جماعيّ: نوع العمل، والتقسيم، والإدراج، والتسكين — لبند أو لمئات.

    ‎{ids, set: {work_type?, split?, include?}, alloc?: [[place_id, share]…]}‎.
    نوع العمل والتسكين المرسَلان يُعلَّمان يدويَّين.
    """
    if not _admin(request):
        return _deny()
    b = await request.json()
    ids = [int(i) for i in b.get("ids") or []]
    st = b.get("set") or {}
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM gen_boq_items WHERE schedule_id=%s AND id = ANY(%s)",
                        (sid, ids))
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return JSONResponse({"success": True, "updated": 0})
            if "work_type" in st:
                wt = (st["work_type"] or "").strip().upper() or None
                cur.execute("""UPDATE gen_boq_items SET work_type=%s,
                                 wt_origin=CASE WHEN %s IS NULL THEN '' ELSE 'manual' END
                               WHERE id = ANY(%s)""", (wt, wt, ids))
            if "split" in st:
                cur.execute("UPDATE gen_boq_items SET split=%s WHERE id = ANY(%s)",
                            (bool(st["split"]), ids))
            if "include" in st:
                cur.execute("UPDATE gen_boq_items SET include=%s WHERE id = ANY(%s)",
                            (bool(st["include"]), ids))
            if "alloc" in b:
                cur.execute("SELECT id FROM gen_places WHERE schedule_id=%s", (sid,))
                ok = {r[0] for r in cur.fetchall()}
                al = [(int(p), float(s)) for p, s in (b.get("alloc") or [])
                      if int(p) in ok and float(s) > 0]
                cur.execute("DELETE FROM gen_boq_alloc WHERE item_id = ANY(%s)", (ids,))
                _batch(cur, "INSERT INTO gen_boq_alloc (item_id, place_id, share) VALUES %s",
                       [(i, p, s) for i in ids for p, s in al])
                cur.execute("UPDATE gen_boq_items SET alloc_origin=%s WHERE id = ANY(%s)",
                            ("manual" if al else "", ids))
        return JSONResponse({"success": True, "updated": len(ids)})
    except Exception as e:
        return _fail(e)


@router.post("/api/boq/{sid}/auto")
async def boq_auto(sid: int, request: Request):
    """يعيد الاقتراح الآليّ. ‎force‎ يكتب فوق اليدويّ أيضًا — بطلب صريح فقط."""
    if not _admin(request):
        return _deny()
    b = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            res = auto_fill(cur, sid, b.get("ids"),
                            tuple(b.get("what") or ("classify", "allocate")),
                            bool(b.get("force")))
        return JSONResponse({"success": True, **res})
    except Exception as e:
        return _fail(e)


@router.post("/api/boq/{sid}/delete")
async def boq_delete(sid: int, request: Request):
    if not _admin(request):
        return _deny()
    b = await request.json()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            if b.get("all"):
                cur.execute("DELETE FROM gen_boq_items WHERE schedule_id=%s", (sid,))
            else:
                cur.execute("DELETE FROM gen_boq_items WHERE schedule_id=%s AND id = ANY(%s)",
                            (sid, [int(i) for i in b.get("ids") or []]))
            n = cur.rowcount
        return JSONResponse({"success": True, "deleted": n})
    except Exception as e:
        return _fail(e)


# ═══════════════════════════ المسارات: التوليد ═══════════════════════════

def _generate(cur, sid):
    tree, _w, _r, st = generator_api.load_definition(cur, sid)
    tree._index()
    dicts = dictionaries_api.load_all(cur)
    items, allocs = load_items(cur, sid)
    # روابط الخريطة الصريحة (قيدٌ بين منطقتين) تُمرَّر للتوليد؛ روابط الترتيب
    # وحدها تبقى في الشجرة يقرأ منها نطاقا ‎next/prev‎.
    cur.execute("""SELECT pred_place, succ_place, rel_type, lag_days FROM gen_place_rels
                   WHERE schedule_id=%s AND mode='constraint'""", (sid,))
    cons = [(str(p), str(s), (t or "FS").upper(), float(l or 0))
            for p, s, t, l in cur.fetchall() if p and s]
    acts, rels, diag, roll = B.build(items, allocs, tree, dicts,
                                     {**st, "constraints": cons})
    return acts, rels, diag, roll, st


def _existing(cur, sid):
    cur.execute("""SELECT gen_key, code, name, duration, wbs,
                          COALESCE(overridden,'[]'::jsonb)
                   FROM sch_activities WHERE schedule_id=%s AND gen_key IS NOT NULL""",
                (sid,))
    return {r[0]: {"code": r[1], "name": r[2], "days": (r[3] or 0) / 8.0,
                   "wbs": r[4] or "", "overridden": r[5] or []}
            for r in cur.fetchall()}


def _diff(old, acts):
    new = {a.key: a for a in acts}
    added = [a for k, a in new.items() if k not in old]
    removed = [dict(v, key=k) for k, v in old.items() if k not in new]
    updated = []
    for k, a in new.items():
        o = old.get(k)
        if not o:
            continue
        ch = {}
        for f, nv in (("code", a.code), ("name", a.name), ("days", float(a.days or 0)),
                      ("wbs", a.wbs or "")):
            if f in o["overridden"]:
                continue
            if (o[f] or "") != (nv or "") if f != "days" else abs(o[f] - nv) > 1e-6:
                ch[f] = [o[f], nv]
        if ch:
            updated.append({"code": o["code"], "changes": ch,
                            "protected": o["overridden"]})
    return added, removed, updated


@router.post("/api/boq/{sid}/preview")
async def boq_preview(sid: int, request: Request):
    """يولّد ويعرض الفرق والأوزان ولا يكتب شيئًا."""
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            acts, rels, diag, roll, _st = _generate(cur, sid)
            added, removed, updated = _diff(_existing(cur, sid), acts)
        kinds = {}
        for a in acts:
            kinds[a.kind] = kinds.get(a.kind, 0) + 1
        return JSONResponse({"success": True, "diag": diag, "rollup": roll,
                             "summary": {"added": len(added), "removed": len(removed),
                                         "updated": len(updated),
                                         "kept": len(acts) - len(added) - len(updated)},
                             "relations": len(rels), "kinds": kinds,
                             "sample": [{"code": a.code, "name": a.name, "days": a.days,
                                         "qty": a.qty, "unit": a.unit, "cost": round(a.cost, 2),
                                         "weight": round(a.weight, 4), "kind": a.kind,
                                         "driving": a.driving}
                                        for a in acts[:300]],
                             "removed": [{"code": r["code"], "name": r["name"]}
                                         for r in removed[:200]],
                             "updated": updated[:200]})
    except Exception as e:
        return _fail(e)


@router.post("/api/boq/{sid}/apply")
async def boq_apply(sid: int, request: Request):
    """
    يكتب الأنشطة والعلاقات. الحقول المعلَّمة ‎overridden‎ لا تُمسّ، والعلاقات
    اليدوية تبقى، والأنشطة المتولَّدة التي لم تَعُد في التوليد تُحذف.
    """
    if not _admin(request):
        return _deny()
    try:
        conn = _get_conn()
        with conn, conn.cursor() as cur:
            acts, rels, diag, roll, st = _generate(cur, sid)
            if not acts:
                return JSONResponse({"success": False, "code": "nothing",
                                     "error": "Nothing to generate — classify and place "
                                              "at least one BOQ item first."})
            old = _existing(cur, sid)
            added, removed, updated = _diff(old, acts)
            cur.execute("SELECT code, kind FROM gen_lib_resources")
            kind_of = dict(cur.fetchall())
            ssid = str(int(sid))

            # الأكواد تُحرَّر أوّلًا ثمّ يُحدَّث الموجود بمفتاحه ثمّ يُدرَج الجديد:
            # الكود يتغيّر ومفتاح التوليد لا يتغيّر.
            cur.execute("""UPDATE sch_activities SET code = 'TMP#' || id
                           WHERE schedule_id=%s AND gen_key IS NOT NULL""", (sid,))

            def row(a):
                lab = sum(h for r, h in a.hours.items() if kind_of.get(r) != "equipment")
                eq = sum(h for r, h in a.hours.items() if kind_of.get(r) == "equipment")
                return (a.key, a.code, a.name, float(a.days or 1) * 8.0, a.wbs or "",
                        a.kind, a.item, a.place, a.work_type, a.step, a.unit,
                        a.qty, round(a.cost, 2), round(a.res_cost, 2), round(a.weight, 6),
                        round(lab, 3), round(eq, 3), json.dumps(a.hours),
                        json.dumps(a.materials), a.discipline, a.csi, a.driving)
            data = [row(a) for a in acts]
            T = ("(%s::text,%s::text,%s::text,%s::double precision,%s::text,%s::text,"
                 "%s::int,%s::text,%s::text,%s::text,%s::text,%s::numeric,%s::numeric,"
                 "%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::jsonb,%s::jsonb,"
                 "%s::text,%s::text,%s::text)")
            COLS = ("gk, code, name, dur, wbs, kind, item, place, wt, step, unit, qty, "
                    "cost, rcost, weight, lab, eq, res, mats, disc, csi, drv")
            _batch(cur, """UPDATE sch_activities t SET
                             code = v.code,
                             name = CASE WHEN t.overridden @> '["name"]'::jsonb
                                         THEN t.name ELSE v.name END,
                             duration = CASE WHEN t.overridden @> '["days"]'::jsonb
                                         THEN t.duration ELSE v.dur END,
                             wbs = CASE WHEN t.overridden @> '["wbs"]'::jsonb
                                         THEN t.wbs ELSE v.wbs END,
                             gen_kind=v.kind, boq_item=v.item, place_key=v.place,
                             work_type=v.wt, step=v.step, unit=v.unit, qty=v.qty,
                             cost=v.cost, res_cost=v.rcost, weight=v.weight,
                             labor_hours=v.lab, equip_hours=v.eq, resources=v.res,
                             materials=v.mats, discipline=v.disc, csi_division=v.csi,
                             driver=v.drv
                           FROM (VALUES %s) AS v(""" + COLS + """)
                           WHERE t.schedule_id=""" + ssid + " AND t.gen_key = v.gk",
                   data, template=T)
            cal = st.get("force_calendar")
            _batch(cur, """INSERT INTO sch_activities
                             (schedule_id, gen_key, code, name, duration, wbs, gen_kind,
                              boq_item, place_key, work_type, step, unit, qty, cost,
                              res_cost, weight, labor_hours, equip_hours, resources,
                              materials, discipline, csi_division, driver,
                              calendar_id, atype, status)
                           SELECT """ + ssid + """, v.gk, v.code, v.name, v.dur, v.wbs,
                                  v.kind, v.item, v.place, v.wt, v.step, v.unit, v.qty,
                                  v.cost, v.rcost, v.weight, v.lab, v.eq, v.res, v.mats,
                                  v.disc, v.csi, v.drv, """ + (str(int(cal)) if cal else "NULL") + """,
                                  'task', 'not_started'
                           FROM (VALUES %s) AS v(""" + COLS + """)
                           ON CONFLICT (schedule_id, gen_key)
                             WHERE gen_key IS NOT NULL DO NOTHING""",
                   data, template=T)
            cur.execute("""DELETE FROM sch_activities
                           WHERE schedule_id=%s AND code LIKE 'TMP#%%'
                             AND gen_key IS NOT NULL""", (sid,))

            cur.execute("""DELETE FROM sch_relations WHERE schedule_id=%s
                             AND COALESCE(origin,'manual') <> 'manual'""", (sid,))
            REL_T = "(%s::text,%s::text,%s::text,%s::double precision,%s::text)"
            _batch(cur, """INSERT INTO sch_relations
                             (schedule_id, pred, succ, rtype, lag, origin)
                           SELECT """ + ssid + """, p.code, s.code, v.rt, v.lag, v.o
                           FROM (VALUES %s) AS v(pgk, sgk, rt, lag, o)
                           JOIN sch_activities p ON p.schedule_id=""" + ssid +
                   """ AND p.gen_key = v.pgk
                           JOIN sch_activities s ON s.schedule_id=""" + ssid +
                   """ AND s.gen_key = v.sgk""",
                   [(r["pred"], r["succ"], r["type"], r["lag"] * 8.0,
                     "gen:" + (r["origin"] or "rule")) for r in rels], template=REL_T)

            summary = {"added": len(added), "removed": len(removed),
                       "updated": len(updated),
                       "kept": len(acts) - len(added) - len(updated)}
            cur.execute("""INSERT INTO gen_settings (schedule_id, last_generated, last_diff,
                                                     last_rollup)
                           VALUES (%s, NOW(), %s, %s)
                           ON CONFLICT (schedule_id) DO UPDATE
                             SET last_generated=NOW(), last_diff=EXCLUDED.last_diff,
                                 last_rollup=EXCLUDED.last_rollup""",
                        (sid, json.dumps(summary), json.dumps(roll)))
        return JSONResponse({"success": True, "diag": diag, "summary": summary,
                             "relations": len(rels), "rollup": roll})
    except Exception as e:
        return _fail(e)
