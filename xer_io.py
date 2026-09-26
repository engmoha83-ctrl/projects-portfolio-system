"""قراءة ملفات بريمافيرا P6 بصيغة XER وتحويلها إلى شبكة يفهمها المحرّك.

    صيغة XER نصّية مفصولة بمسافات جدولة:
        %T اسم الجدول · %F أسماء أعمدته · %R صفّ بيانات · %E النهاية
    وما لا يبدأ بـ‎%‎ فهو تتمّة قيمة امتدّت على أسطر (ملاحظة مثلًا).

    الفائدة الكبرى في هذا الملف أنه لا يحمل المدخلات فحسب، بل يحمل أيضًا
    إجابات بريمافيرا نفسها: ‎early_start_date‎ و‎late_start_date‎
    و‎total_float_hr_cnt‎ لكل نشاط. فنستطيع أن نجدول الشبكة بمحرّكنا ثم
    نقارن نشاطًا نشاطًا بما حسبه P6 — تحقّقٌ لا يحتاج إلى تصديقنا لأنفسنا.
    هذه هي وظيفة ‎compare_with_p6‎ في آخر الملف.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from schedule_engine import (
    Activity, Calendar, Schedule, Relation,
    TASK, START_MS, FIN_MS, LOE, WBS_SUM,
    NOT_STARTED, IN_PROGRESS, COMPLETE,
    SNET, SNLT, FNET, FNLT, MSO, MFO, ALAP, MANDATORY_START, MANDATORY_FIN,
    FS, SS, FF, SF,
)

# ───────────────────── قواميس ترجمة رموز P6 ─────────────────────

TASK_TYPE = {
    "TT_Task": TASK,
    "TT_Rsrc": TASK,
    "TT_Mile": START_MS,
    "TT_FinMile": FIN_MS,
    "TT_LOE": LOE,
    "TT_WBS": WBS_SUM,
}

STATUS = {"TK_NotStart": NOT_STARTED, "TK_Active": IN_PROGRESS, "TK_Complete": COMPLETE}

CONSTRAINT = {
    "CS_MSO": MSO, "CS_MEO": MFO,
    "CS_MSOA": SNET, "CS_MSOB": SNLT,
    "CS_MEOA": FNET, "CS_MEOB": FNLT,
    "CS_ALAP": ALAP,
    "CS_MANDSTART": MANDATORY_START, "CS_MANDFIN": MANDATORY_FIN,
}

REL_TYPE = {"PR_FS": FS, "PR_SS": SS, "PR_FF": FF, "PR_SF": SF}

# الاتجاه المعاكس — للتصدير
TASK_TYPE_OUT = {v: k for k, v in reversed(list(TASK_TYPE.items()))}
STATUS_OUT = {v: k for k, v in STATUS.items()}
CONSTRAINT_OUT = {v: k for k, v in CONSTRAINT.items()}
REL_TYPE_OUT = {v: k for k, v in REL_TYPE.items()}


# ───────────────────────── القارئ الخام ─────────────────────────

def parse(text: str) -> dict:
    """يقرأ نصّ XER ويعيد {اسم الجدول: [صفوف كقواميس]}."""
    out, tbl, fields = {}, None, None
    for ln in text.splitlines():
        if not ln or ln[0] != "%":
            continue
        p = ln.split("\t")
        k = p[0]
        if k == "%T":
            tbl = (p[1] if len(p) > 1 else "").strip()
            fields = None
            out.setdefault(tbl, [])
        elif k == "%F":
            fields = [x.strip() for x in p[1:]]
        elif k == "%R" and tbl and fields:
            vals = p[1:]
            out[tbl].append({f: (vals[i] if i < len(vals) else "") for i, f in enumerate(fields)})
    return out


def read_file(path, encoding=None):
    """يقرأ ملفًا من القرص. ملفات P6 غالبًا cp1252 وأحيانًا utf‑8 بعلامة."""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ([encoding] if encoding else []) + ["utf-8-sig", "cp1252", "latin-1"]:
        try:
            return parse(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse(raw.decode("latin-1", "replace"))


# ───────────────────────── تحويل القيم ─────────────────────────

def num(v, default=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?")


def dt(v):
    m = _DT.match(str(v or "").strip())
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))


# ──────────────────────── تقويمات P6 ────────────────────────
# ‎clndr_data‎ سلسلة واحدة فيها أيام الأسبوع بفتراتها ثم الاستثناءات.
# اليوم مرقَّم ١=الأحد … ٧=السبت، والاستثناء تاريخه رقم يوم من 1899‑12‑30
# (تقويم إكسل)، واستثناء بلا فترات يعني إجازة.

_SEG = re.compile(r"s\|(\d\d:\d\d)\|f\|(\d\d:\d\d)")
_WDAY = re.compile(r"\(0\|\|([1-7])\(\)\(")
_EXC = re.compile(r"\(0\|\|\d+\(d\|(\d+)\)\(")
_EPOCH = datetime(1899, 12, 30)


def _mins(s):
    return int(s[:2]) * 60 + int(s[3:5])


def _segments(chunk):
    out = []
    for a, b in _SEG.findall(chunk or ""):
        lo, hi = _mins(a), (1440 if b == "00:00" else _mins(b))
        if hi > lo:
            out.append((lo, hi))
    return sorted(out)


def parse_calendar(data, name="", cal_id=None):
    data = str(data or "")
    iw = data.find("DaysOfWeek")
    ie = data.find("Exceptions")
    if iw < 0:
        return None
    week_part = data[iw:ie if ie >= 0 else len(data)]
    week = {}
    parts = _WDAY.split(week_part)
    for i in range(1, len(parts), 2):
        week[int(parts[i])] = _segments(parts[i + 1])
    exc = {}
    if ie >= 0:
        ep = _EXC.split(data[ie:])
        for i in range(1, len(ep), 2):
            day = (_EPOCH + timedelta(days=int(ep[i]))).date()
            exc[day.isoformat()] = _segments(ep[i + 1])
    return Calendar(week, exc, name or "Calendar", cal_id)


def calendars(db):
    """كل تقويمات الملف، بمفتاح clndr_id."""
    out = {}
    for c in db.get("CALENDAR", []):
        cal = parse_calendar(c.get("clndr_data"), c.get("clndr_name", ""), c.get("clndr_id"))
        if cal is None:
            hrs = num(c.get("day_hr_cnt"), 8) or 8
            cal = Calendar.standard(end=f"{8 + int(hrs):02d}:00",
                                    name=c.get("clndr_name", ""))
            cal.id = c.get("clndr_id")
        out[str(c.get("clndr_id"))] = cal
    return out


# ───────────────────────── بناء الشبكة ─────────────────────────

def projects(db):
    """قائمة المشاريع داخل الملف — الملف قد يحوي أكثر من واحد."""
    return [{
        "id": p.get("proj_id"),
        "code": p.get("proj_short_name", ""),
        "name": p.get("proj_short_name", ""),
        "data_date": dt(p.get("last_recalc_date")),
        "start": dt(p.get("plan_start_date")),
        "must_finish": dt(p.get("scd_end_date")),
        "calendar": p.get("clndr_id"),
        "tasks": sum(1 for t in db.get("TASK", []) if t.get("proj_id") == p.get("proj_id")),
    } for p in db.get("PROJECT", [])]


def wbs_tree(db, proj_id):
    """هيكل تجزئة العمل: مفتاح wbs_id ← {الاسم، الأب، المسار الكامل}."""
    rows = [w for w in db.get("PROJWBS", []) if w.get("proj_id") == proj_id]
    nodes = {w["wbs_id"]: {"id": w["wbs_id"], "parent": w.get("parent_wbs_id") or None,
                           "code": w.get("wbs_short_name", ""),
                           "name": w.get("wbs_name", ""),
                           "seq": num(w.get("seq_num")), "path": None} for w in rows}

    def path(wid, depth=0):
        n = nodes.get(wid)
        if n is None or depth > 50:
            return []
        if n["path"] is not None:
            return n["path"]
        up = path(n["parent"], depth + 1) if n["parent"] in nodes else []
        n["path"] = up + [n["name"]]
        return n["path"]

    for wid in list(nodes):
        path(wid)
    return nodes


def build(db, proj_id=None, use_remaining=True):
    """يبني Schedule من مشروع داخل الملف، جاهزًا للجدولة.

       use_remaining: المدّة المعتمَدة في الجدولة هي المتبقّية (كما يفعل P6
       عند التحديث). أوقفها لتجدول البرنامج بمدده الأصلية — وهو ما نحتاجه
       عند إعادة بناء الخطّة الأساسية.
    """
    if proj_id is None:
        ps = projects(db)
        if not ps:
            raise ValueError("لا يحتوي الملف على أي مشروع")
        proj_id = max(ps, key=lambda p: p["tasks"])["id"]
    pr = next((p for p in db.get("PROJECT", []) if p.get("proj_id") == proj_id), {})

    cals = calendars(db)
    s = Schedule(
        start=dt(pr.get("plan_start_date")),
        data_date=dt(pr.get("last_recalc_date")),
        must_finish=dt(pr.get("scd_end_date")),
        name=pr.get("proj_short_name", ""),
    )
    for key, c in cals.items():
        s.add_calendar(c, default=(str(pr.get("clndr_id")) == key))
    if s.default_calendar is None:
        s.add_calendar(Calendar.standard(), default=True)

    wbs = wbs_tree(db, proj_id)
    for t in db.get("TASK", []):
        if t.get("proj_id") != proj_id:
            continue
        status = STATUS.get(t.get("status_code"), NOT_STARTED)
        target = num(t.get("target_drtn_hr_cnt"))
        remain = num(t.get("remain_drtn_hr_cnt"))
        node = wbs.get(t.get("wbs_id"))
        a = Activity(
            id=t["task_id"],
            name=t.get("task_name", ""),
            code=t.get("task_code", ""),
            duration=target,
            remaining=(remain if use_remaining else target) if status != COMPLETE else 0.0,
            calendar=t.get("clndr_id"),
            type=TASK_TYPE.get(t.get("task_type"), TASK),
            wbs=" / ".join(node["path"]) if node else None,
            status=status,
            actual_start=dt(t.get("act_start_date")),
            actual_finish=dt(t.get("act_end_date")),
            constraint=CONSTRAINT.get(t.get("cstr_type")),
            constraint_date=dt(t.get("cstr_date")),
            constraint2=CONSTRAINT.get(t.get("cstr_type2")),
            constraint2_date=dt(t.get("cstr_date2")),
            expected_finish=dt(t.get("expect_end_date")),
            suspend=dt(t.get("suspend_date")),
            resume=dt(t.get("resume_date")),
        )
        s.add(a)

    ids = s.activities
    for r in db.get("TASKPRED", []):
        if r.get("proj_id") != proj_id and r.get("pred_proj_id") != proj_id:
            continue
        succ, pred = r.get("task_id"), r.get("pred_task_id")
        if succ not in ids or pred not in ids:
            continue            # علاقة خارجية إلى مشروع آخر — تُهمَل هنا
        s.link(pred, succ, REL_TYPE.get(r.get("pred_type"), FS), num(r.get("lag_hr_cnt")))
    return s


def p6_answers(db, proj_id):
    """ما حسبه بريمافيرا نفسه لكل نشاط — مرجع المقارنة."""
    out = {}
    for t in db.get("TASK", []):
        if t.get("proj_id") != proj_id:
            continue
        out[t["task_id"]] = {
            "code": t.get("task_code", ""),
            "name": t.get("task_name", ""),
            "es": dt(t.get("act_start_date")) or dt(t.get("early_start_date")),
            "ef": dt(t.get("act_end_date")) or dt(t.get("early_end_date")),
            "ls": dt(t.get("late_start_date")),
            "lf": dt(t.get("late_end_date")),
            "tf": num(t.get("total_float_hr_cnt"), None) if t.get("total_float_hr_cnt") else None,
            "ff": num(t.get("free_float_hr_cnt"), None) if t.get("free_float_hr_cnt") else None,
        }
    return out


def costs(db, proj_id):
    """التكلفة المخطَّطة والفعلية لكل نشاط — من الموارد ومن بنود التكلفة."""
    out = {}
    for r in db.get("TASKRSRC", []):
        if r.get("proj_id") != proj_id:
            continue
        o = out.setdefault(r["task_id"], {"budget": 0.0, "actual": 0.0, "remaining": 0.0})
        o["budget"] += num(r.get("target_cost"))
        o["actual"] += num(r.get("act_reg_cost")) + num(r.get("act_ot_cost"))
        o["remaining"] += num(r.get("remain_cost"))
    for r in db.get("PROJCOST", []):
        if r.get("proj_id") != proj_id:
            continue
        o = out.setdefault(r["task_id"], {"budget": 0.0, "actual": 0.0, "remaining": 0.0})
        o["budget"] += num(r.get("target_cost"))
        o["actual"] += num(r.get("act_cost"))
        o["remaining"] += num(r.get("remain_cost"))
    return out


# ────────────────────── التحقّق أمام بريمافيرا ──────────────────────

def compare_with_p6(sched: Schedule, answers: dict, tol_minutes=60, tol_float_hr=0.5):
    """يقارن نتائجنا بما خزّنه P6، ويعيد تقريرًا بالفروق.

       لا معنى لقول «المحرّك يعمل» ما لم يُقَس على ملف حقيقي. وهذه الدالة
       هي المقياس: تعيد نسبة التطابق وقائمة الأنشطة المختلفة، مرتّبةً
       بحجم الفرق، فيُقرأ أكبر خطأ أولًا.
    """
    rows, tol = [], timedelta(minutes=tol_minutes)
    counts = {"es": 0, "ef": 0, "ls": 0, "lf": 0, "tf": 0}
    total = 0
    for aid, a in sched.activities.items():
        p = answers.get(aid)
        if not p:
            continue
        total += 1
        diff = {}
        for f in ("es", "ef", "ls", "lf"):
            mine, theirs = getattr(a, f), p.get(f)
            if mine and theirs and abs(mine - theirs) > tol:
                diff[f] = (mine, theirs, round((mine - theirs).total_seconds() / 3600, 2))
                counts[f] += 1
        if a.total_float is not None and p.get("tf") is not None:
            d = a.total_float - p["tf"]
            if abs(d) > tol_float_hr:
                diff["tf"] = (a.total_float, p["tf"], round(d, 2))
                counts["tf"] += 1
        if diff:
            worst = max(abs(v[2]) for v in diff.values())
            rows.append({"id": aid, "code": p.get("code"), "name": p.get("name"),
                         "type": a.type, "status": a.status, "worst": worst, "diff": diff})
    rows.sort(key=lambda r: -r["worst"])
    matched = total - len(rows)
    return {
        "total": total,
        "matched": matched,
        "rate": round(matched / total * 100, 2) if total else 0.0,
        "by_field": counts,
        "mismatches": rows,
    }


def report(cmp_result, limit=25):
    """تقرير نصّي مختصر عن المقارنة — للطرفية أو لصفحة الفحص."""
    r = cmp_result
    out = [f"طابق {r['matched']} من {r['total']} نشاطًا ({r['rate']}%)",
           "  الفروق حسب الحقل: " + ", ".join(f"{k}={v}" for k, v in r["by_field"].items() if v)]
    for m in r["mismatches"][:limit]:
        bits = ", ".join(f"{f}: نحن {v[0]:%Y-%m-%d %H:%M} / P6 {v[1]:%Y-%m-%d %H:%M} ({v[2]:+g}س)"
                         if hasattr(v[0], "year") else f"{f}: نحن {v[0]} / P6 {v[1]} ({v[2]:+g})"
                         for f, v in m["diff"].items())
        out.append(f"  {m['code'] or m['id']} · {m['name'][:40]} — {bits}")
    if len(r["mismatches"]) > limit:
        out.append(f"  … و{len(r['mismatches']) - limit} نشاطًا آخر")
    return "\n".join(out)
