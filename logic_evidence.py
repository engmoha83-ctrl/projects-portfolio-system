# -*- coding: utf-8 -*-
"""
مستخرِج شواهد المنطق — يقرأ برامج زمنية حقيقية ويستخرج منها قواعد الربط.

المشكلة: الملفّ يعطي علاقةً بين **نشاطين بعينهما** (‎ARC-001-FFL → ARC-002-FFL‎)،
والمولّد يحتاج قاعدةً بين **نوعَي عمل** (مبانٍ → بياض، داخل المكان نفسه، FS+0).
فالتحويل يقتضي معرفة: أيّ نوع عمل يمثّله كلّ نشاط، وفي أيّ مكان يقع.

**الاسم يقول ماذا، والهيكل يقول أين.** هذه خلاصة قراءة خمسة ملفّات حقيقية،
وهي تخالف أوّل ما جُرِّب هنا. بُني هذا الملفّ أوّلًا على تفكيك كود النشاط،
فنجح في ملفّين وسقط في ثلاثة: أكواد أحد البرامج ‎A1000, A1010, A1020‎ لا تحمل
دلالةً أصلًا، وآخر يكوّد التسلسل لا العمل. فصار كلّ نشاط «نوع عمل» وحده،
و١٢٤٦٩ علاقة أنتجت ١٠٣٣٤ قاعدة — أي لم تُنتج شيئًا.

بينما الاسم يتكرّر حرفيًّا عبر الأماكن:

    ARC-001-FFL  Block Works-Hollow Blocks   …/ First Floor  / Division 04
    ARC-001-GFL  Block Works-Hollow Blocks   …/ Ground Floor / Division 04

فنوع العمل من الاسم بعد تطبيعه، والمكان من مستوى الهيكل الذي تتكرّر تحته
الأسماء نفسها — ويُستنتَج هذا المستوى من الملفّ لا يُفترض.
"""

import re
import statistics
from collections import Counter, defaultdict

SEP = re.compile(r"[-_.\s/]+")

# كلمات تدلّ على المكان لا على العمل: تُنزَع من مفتاح نوع العمل حتى لا يصير
# «بياض الدور الثاني» نوعًا مختلفًا عن «بياض الدور الثالث».
_PLACE_WORDS = {
    "floor", "floors", "level", "levels", "basement", "ground", "roof",
    "mezzanine", "podium", "zone", "zones", "area", "areas", "block",
    "building", "bldg", "tower", "wing", "phase", "sector", "plot", "villa",
    "unit", "units", "room", "rooms", "apartment", "apt", "typical", "upper",
    "lower", "north", "south", "east", "west", "external", "internal",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "st", "nd", "rd", "th", "no", "km",
    "دور", "طابق", "منطقة", "مبنى", "برج", "قطاع", "وحدة", "غرفة", "بدروم",
}
_STOP = {"the", "of", "and", "for", "to", "at", "in", "on", "a", "an", "all",
         "with", "as", "per", "by", "from", "its", "including", "incl",
         "works", "work", "new", "existing", "complete", "completion"}

_WORD = re.compile(r"[A-Za-z؀-ۿ]+")


def name_key(name, words=4):
    """
    مفتاح نوع العمل من اسم النشاط.

    تُنزَع الأرقام والترقيم وكلمات المكان وحشو اللغة، ويُبقى على أوّل أربع
    كلمات دالّة. «Block Works-Hollow Blocks» و«Block works – hollow block»
    مفتاحهما واحد، و«Plastering 3rd Floor» و«Plastering Ground Floor» كذلك.
    """
    ws = []
    for w in _WORD.findall(str(name or "")):
        lw = w.lower()
        if lw in _STOP or lw in _PLACE_WORDS or len(w) == 1:
            continue
        ws.append(w.upper())
        if len(ws) >= words:
            break
    return " ".join(ws)


def wbs_parts(wbs):
    return [p.strip() for p in re.split(r"\s*/\s*", str(wbs or "")) if p.strip()]


# ═════════════ استنتاج مستوى المكان في الهيكل من الملفّ نفسه ═════════════

def _place_candidate(acts, getter):
    """
    يقيس مرشَّحًا لمفتاح المكان بمعيار واحد: **تكرار الأعمال نفسها تحته**.

    فلو ظهر «أعمال المباني» تحت «الدور الأول» و«الأرضي» و«السطح»، فذلك
    المرشَّح أماكنُ. أمّا ما لكلّ قيمةٍ فيه أعمالُها وحدها (التخصّصات مثلًا)
    فليس مكانًا.
    """
    names_by_val = defaultdict(set)
    for a in acts:
        v = getter(a)
        nk = name_key(a.get("name"))
        if v and nk:
            names_by_val[v].add(nk)
    if len(names_by_val) < 2:
        return None
    seen = Counter()
    for vals in names_by_val.values():
        for nk in vals:
            seen[nk] += 1
    total = len(seen)
    repeated = sum(1 for n in seen.values() if n >= 2)
    return {"values": len(names_by_val), "distinct_names": total,
            "repeat_share": round(repeated / total, 3) if total else 0.0,
            "avg_names": round(statistics.mean(
                len(v) for v in names_by_val.values()), 1)}


def learn_place_level(acts):
    """
    يجد مفتاح المكان، سواءً كان مستوًى في الهيكل أو رمزًا في كود النشاط.

    المرشَّحان يُقاسان **بالمعيار نفسه** ويُختار الأقوى، لأنّ الملفّات تختلف:
    في برنامج يقع المكان في الهيكل («الدور الأول»)، وفي آخر في آخر الكود
    (‎MEC-006-B1A‎) والهيكل كلّه تخصّصات. وفرضُ أحدهما يُسقط نصف الملفّات.
    """
    cands = []
    maxd = max((len(wbs_parts(a.get("wbs"))) for a in acts), default=0)
    for d in range(maxd):
        st = _place_candidate(
            acts, lambda a, d=d: (wbs_parts(a.get("wbs"))[d]
                                  if len(wbs_parts(a.get("wbs"))) > d else ""))
        if st:
            cands.append(dict(st, kind="wbs", index=d))

    width = Counter(len(tokens(a.get("code"))) for a in acts
                    if a.get("code")).most_common(1)
    if width and width[0][0] >= 2:
        w = width[0][0]
        for i in range(w):
            st = _place_candidate(
                acts, lambda a, i=i, w=w: (tokens(a.get("code"))[i]
                                           if len(tokens(a.get("code"))) == w
                                           else ""))
            if st:
                cands.append(dict(st, kind="code", index=i))

    if not cands:
        return {"kind": None, "index": None, "confidence": 0.0,
                "candidates": []}
    best = max(cands, key=lambda s: (s["repeat_share"], s["values"]))
    # لا يُعتدّ بمكانٍ إلّا إذا تكرّر ربع الأعمال فأكثر عبر قيَمه
    if best["repeat_share"] < 0.25 or best["values"] < 2:
        return {"kind": None, "index": None, "confidence": 0.0,
                "candidates": cands}
    return {"kind": best["kind"], "index": best["index"],
            "confidence": round(min(1.0, 0.4 + best["repeat_share"]), 2),
            "values": best["values"], "repeat_share": best["repeat_share"],
            "candidates": cands}


def tokens(code):
    return [t for t in SEP.split(str(code or "").strip().upper()) if t]


def classify(act, shape):
    """يعيد (مفتاح نوع العمل، مفتاح المكان) لنشاط واحد."""
    wt = name_key(act.get("name"))
    kind, i = shape.get("kind"), shape.get("index")
    pl = ""
    if kind == "wbs":
        parts = wbs_parts(act.get("wbs"))
        pl = parts[i] if len(parts) > i else ""
    elif kind == "code":
        t = tokens(act.get("code"))
        pl = t[i] if len(t) > i else ""
    if not pl:
        parts = wbs_parts(act.get("wbs"))
        pl = parts[-1] if parts else ""
    return wt, pl


# ═══════════════════════════ استخراج الشواهد ═══════════════════════════

def place_order(acts):
    """
    ترتيب الأماكن **زمنيًّا** — بأبكر نشاط يبدأ في كلّ مكان.

    ومنه وحده تُقرأ دلالة «التالي» و«السابق»: الطاقم ينهي دورًا فينتقل إلى
    الذي يليه **في الزمن**، لا الذي يليه في ترتيب صفوف الملفّ. وقد كان
    الترتيب يُؤخذ من ترتيب ظهور الأنشطة، وهو ترتيب اعتباطيّ جعل «التالي»
    تعني «ما صادف أن جاء بعده في القائمة» — ١٤٤٦ علاقة وُسمت به بلا معنى.

    وإذا لم تتوفّر تواريخ، يُعاد ترتيب الظهور ويُعلَّم النطاق ‎other‎ بدلًا
    من ادّعاء جوارٍ لا دليل عليه.
    """
    first = {}
    dated = False
    for i, a in enumerate(acts):
        p = a.get("_place")
        if not p:
            continue
        h = a.get("order_hint")
        if h is not None:
            dated = True
        key = (h, i) if h is not None else (None, i)
        cur = first.get(p)
        if cur is None or _hint_lt(key, cur):
            first[p] = key
    ordered = sorted(first, key=lambda p: (first[p][0] is None, first[p]))
    return {p: i for i, p in enumerate(ordered)}, dated


def _hint_lt(a, b):
    if a[0] is None:
        return b[0] is None and a[1] < b[1]
    return b[0] is None or a[0] < b[0] or (a[0] == b[0] and a[1] < b[1])


def scope_of(pp, sp, order, dated=True):
    if not pp or not sp:
        return "unknown"
    if pp == sp:
        return "same"
    if not dated:
        return "other"          # بلا ترتيب زمنيّ لا يُدّعى جوار
    i, j = order.get(pp), order.get(sp)
    if i is None or j is None:
        return "other"
    if j == i + 1:
        return "next"
    if j == i - 1:
        return "prev"
    return "other"


def extract(acts, rels, source, project="", hours_per_day=8.0):
    """
    يحوّل شبكةً حقيقية إلى شواهد ربط بين أنواع الأعمال.

    ‎acts‎: ‎[{"id","code","name","wbs"}]‎ · ‎rels‎: ‎[{"pred","succ","type","lag_hr"}]‎
    """
    shape = learn_place_level(acts)
    by_id = {}
    for a in acts:
        wt, pl = classify(a, shape)
        a = dict(a)
        a["_wt"], a["_place"] = wt, pl
        by_id[a["id"]] = a

    order, dated = place_order(list(by_id.values()))
    ev, lags, skipped = Counter(), defaultdict(list), Counter()

    for r in rels:
        p, s = by_id.get(r["pred"]), by_id.get(r["succ"])
        if not p or not s:
            skipped["طرف خارج الملفّ"] += 1
            continue
        if not p["_wt"] or not s["_wt"]:
            skipped["اسم لا يُشتقّ منه نوع عمل"] += 1
            continue
        if p["_wt"] == s["_wt"] and p["_place"] == s["_place"]:
            skipped["الطرفان نفس العمل ونفس المكان"] += 1
            continue
        scope = scope_of(p["_place"], s["_place"], order, dated)
        key = (p["_wt"], s["_wt"], (r.get("type") or "FS").upper(), scope)
        ev[key] += 1
        lags[key].append(round(float(r.get("lag_hr") or 0.0)
                               / (hours_per_day or 8.0), 3))

    out = []
    for (pw, sw, rt, scope), n in ev.most_common():
        L = sorted(lags[(pw, sw, rt, scope)])
        out.append({
            "source": source, "project": project,
            "pred_work_type": pw, "succ_work_type": sw,
            "rel_type": rt, "scope": scope, "count": n,
            "lag_min": L[0], "lag_median": L[len(L) // 2], "lag_max": L[-1],
            "confidence": shape.get("confidence", 0.0),
        })
    diag = {
        "source": source, "project": project,
        "activities": len(by_id), "relations": len(rels),
        "evidence_rows": len(out), "skipped": dict(skipped),
        "place_key": f'{shape.get("kind")}[{shape.get("index")}]', "places": len(order),
        "work_types": len({a["_wt"] for a in by_id.values() if a["_wt"]}),
        "confidence": shape.get("confidence", 0.0),
        "place_order_dated": dated,
        "compression": round(len(rels) / len(out), 2) if out else 0.0,
    }
    return out, diag


# ═════════════════════════ اشتقاق القواعد ═════════════════════════

def derive_rules(evidence, min_count=2, min_sources=1):
    """
    يشتقّ القواعد الفاعلة من الشواهد المتراكمة.

    القاعدة تُختار بنوع العلاقة **الأكثر شهادةً**، ويبقى التخلُّف توزيعًا لا
    رقمًا: أدنى ووسيط وأعلى، وعدد المشاهدات، وعدد الملفّات، ونسبة الاتّفاق،
    والأنواع المنافسة إن وُجدت — ليُقرأ مدى الخلاف لا رقمٌ يبدو قاطعًا وهو
    شهادةٌ واحدة.
    """
    g = defaultdict(list)
    for e in evidence:
        g[(e["pred_work_type"], e["succ_work_type"], e["scope"])].append(e)

    rules = []
    for (pw, sw, scope), rows in g.items():
        total = sum(r["count"] for r in rows)
        srcs = {r["source"] for r in rows}
        if total < min_count or len(srcs) < min_sources:
            continue
        by_type = Counter()
        for r in rows:
            by_type[r["rel_type"]] += r["count"]
        rt, rt_n = by_type.most_common(1)[0]
        picked = [r for r in rows if r["rel_type"] == rt]
        rules.append({
            "pred_work_type": pw, "succ_work_type": sw, "scope": scope,
            "rel_type": rt,
            "lag_days": round(statistics.median(
                [r["lag_median"] for r in picked]), 2),
            "lag_min": min(r["lag_min"] for r in picked),
            "lag_max": max(r["lag_max"] for r in picked),
            "observations": total, "agreeing": rt_n,
            "agreement": round(rt_n / total * 100, 1),
            "sources": len(srcs), "source_list": ", ".join(sorted(srcs)),
            "rival_types": ", ".join(f"{k}:{v}" for k, v in
                                     by_type.most_common() if k != rt),
            "origin": "مشتقّة",
            "confidence": round(sum(r["confidence"] * r["count"]
                                    for r in rows) / total, 2),
        })
    rules.sort(key=lambda r: (-r["observations"], -r["sources"]))
    return rules
