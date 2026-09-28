# -*- coding: utf-8 -*-
"""
تفكيك جدول الكميّات إلى أنشطة — المنطق وحده، بلا قاعدة بيانات ولا واجهة.

ثلاث خطوات لكلّ بند، وكلّها قابلة للتعديل بعدها:

1. **التصنيف**: أيّ نوع عمل؟ يُقترح من كلمات القاموس في وصف البند وسياقه.
2. **التسكين**: في أيّ مكان من تقسيمة المشروع، وبأيّ نسبة؟ يُقترح من رقم
   المبنى والدور المستخرَجين من البند، ثمّ ينزل إلى المستوى الذي ينطبق عليه
   نوع العمل: بند هيكلٍ يُسكَّن في «المبنى ١» يتوزّع على أدواره.
3. **التقسيم**: نشاطٌ واحد، أو خطوات نوع العمل بأوزانها — اختيارٌ لكلّ بند.

ثمّ يتولّد نشاطٌ لكلّ ‎(بند، مكان، خطوة)‎ بكميّته وتكلفته وساعاته ومدّته، و
تُبنى العلاقات، وتُحسب الأوزان من تحت لفوق: وزن النشاط = تكلفته ÷ إجمالي
التكلفة، ووزن كلّ مكان وقسم CSI مجموع ما تحته.
"""

import math
import re
from collections import defaultdict

import generator as G

HPD = 8.0                               # ساعات يوم العمل
REL_TYPES = ("FS", "SS", "FF", "SF")
MAX_NAME = 120                          # حدّ اسم النشاط في بريمافيرا


# ═══════════════════════════ تطبيع النصّ ═══════════════════════════

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm(s):
    """نصٌّ للمطابقة: صغير، عربيّه موحَّد الهمزات والتاء، وفواصله مسافات."""
    s = str(s or "").lower().translate(_AR_DIGITS)
    s = re.sub(r"[ً-ْـ]", "", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي")
    s = re.sub(r"[^0-9a-z؀-ۿ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _variants(kw):
    """صيغ الكلمة المفتاحية: الجمع الإنجليزيّ البسيط وأداة التعريف العربية."""
    out = {kw}
    if re.fullmatch(r"[a-z0-9 ]+", kw):
        last = kw.split(" ")[-1]
        stem = kw[: -len(last)]
        out |= {stem + last + "s", stem + last + "es"}
        if last.endswith("y"):
            out.add(stem + last[:-1] + "ies")
    elif not kw.startswith("ال"):
        out.add("ال" + kw)
    return out


# ═══════════════════════════ ١. التصنيف ═══════════════════════════

def classify(item, work_types):
    """
    يقترح نوع العمل لبند.

    يعيد ‎(الكود أو None، الدرجة، المرشّحون)‎. الكلمة المتعدّدة الألفاظ أثقل من
    المفردة لأنّها أدقّ («reinforced concrete» أوضح من «concrete»)، ووصف البند
    أثقل من عنوان قسمه. ورمز نوع العمل في كود البند — إن وُجد — يرجّح
    بقوّة. والتعادل لا يُحسم تخمينًا: يُترك البند بلا تصنيف ومعه مرشّحوه.
    """
    desc = " " + norm(item.get("item_name")) + " "
    ctx = " " + norm(" ".join(str(item.get(k) or "") for k in
                              ("section", "bill", "group_l1", "group_l2",
                               "group_l3", "category"))) + " "
    code_wt = str(item.get("code_work_type") or "").strip().upper()
    csi = str(item.get("csi_division") or "").strip()

    scores = []
    for w in work_types:
        s = 0.0
        for kw in w.get("keywords") or []:
            k = norm(kw)
            if not k:
                continue
            weight = len(k.split(" "))
            for v in _variants(k):
                if f" {v} " in desc:
                    s += 1.0 * weight
                    break
                if f" {v} " in ctx:
                    s += 0.5 * weight
                    break
        if code_wt and code_wt == w["code"]:
            s += 3
        if s and csi and csi == (w.get("csi_division") or ""):
            s += 0.5
        if s > 0:
            scores.append((s, w["code"]))
    scores.sort(key=lambda x: -x[0])
    cands = [{"code": c, "score": round(s, 2)} for s, c in scores[:4]]
    if not scores:
        return None, 0.0, []
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None, scores[0][0], cands
    return scores[0][1], scores[0][0], cands


# ═══════════════════════════ ٢. التسكين ═══════════════════════════

_FLOOR_WORDS = {"ground": 0, "g": 0, "gf": 0, "ارضي": 0, "الارضي": 0,
                "basement": -1, "بدروم": -1, "البدروم": -1, "b": -1,
                "roof": "roof", "سطح": "roof", "السطح": "roof", "rf": "roof"}


def _ident(s):
    """‎'B-02'‎ ← ‎('b', 2)‎ · ‎'Build.1'‎ ← ‎('build', 1)‎ · ‎'A'‎ ← ‎('a', None)‎"""
    t = norm(s).replace(" ", "")
    m = re.fullmatch(r"([a-z؀-ۿ]*)(\d+)([a-z]?)", t)
    if m:
        return (m.group(1) + m.group(3)), int(m.group(2))
    return t, None


def floor_key(s):
    """مفتاح دورٍ موحَّد: ‎0‎ للأرضي، سالبٌ للبدروم، ‎'roof'‎ للسطح، وإلّا رقمه."""
    t = norm(s)
    if not t:
        return None
    flat = t.replace(" ", "")
    if flat in _FLOOR_WORDS:
        return _FLOOR_WORDS[flat]
    for w, k in _FLOOR_WORDS.items():
        if len(w) > 2 and w in t.split(" "):
            return k
    m = re.search(r"\bb(?:asement)?\s*(\d+)\b", t)
    if m:
        return -int(m.group(1))
    m = re.search(r"(\d+)", t)
    if m:
        return int(m.group(1))
    return None


def _building_match(token, place):
    it, n = _ident(token)
    for field in (place.code, place.name):
        pl, pn = _ident(field)
        if n is not None and pn is not None and n == pn:
            if it in ("", "b", "bldg", "building", "block", "blk") or it == pl or not pl:
                return True
            if pl in ("b", "bldg", "build", "building", "block", "blk"):
                return it in ("", "b", "bldg", "building")
        if n is None and it and it == pl:
            return True
    return False


def _floor_match(token, place):
    k = floor_key(token)
    if k is None:
        return False
    for field in (place.name, place.code):
        if floor_key(field) == k:
            return True
    return False


def locate(item, tree, level_of):
    """
    يقترح المكان من رقم المبنى والدور المستخرَجين من البند.

    ‎level_of‎ يعطي لكلّ مستوى دوره: ‎'building'‎ أو ‎'floor'‎ أو لا شيء. يعيد
    قائمة أماكن (فارغة إن لم يُعرف شيء أو التبس). والالتباس لا يُخمَّن: دورٌ
    بلا مبنى في مشروعٍ فيه عدّة مبانٍ يُترك لصاحبه.
    """
    b_tok = str(item.get("building") or "").strip()
    f_tok = str(item.get("floor") or "").strip()
    if not b_tok and not f_tok:
        return []
    nodes = list(tree.nodes.values())
    bl = [p for p in nodes if level_of(p.level) == "building"]
    fl = [p for p in nodes if level_of(p.level) == "floor"]
    bmatch = [p for p in bl if b_tok and _building_match(b_tok, p)]
    if b_tok and len(bmatch) != 1:
        return []
    if not f_tok:
        return [bmatch[0].id]
    fm = [p for p in fl if _floor_match(f_tok, p)]
    if bmatch:
        under = set(descendants(tree, bmatch[0].id))
        fm = [p for p in fm if p.id in under]
        return [fm[0].id] if len(fm) == 1 else [bmatch[0].id]
    return [fm[0].id] if len(fm) == 1 else []


def descendants(tree, pid):
    out, stack = [], list(tree.nodes[pid]._children) if pid in tree.nodes else []
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(tree.nodes[c]._children)
    return out


def spread(tree, pid, applies_to):
    """
    يُنزل تسكينًا إلى المستوى الذي ينطبق عليه نوع العمل.

    بندُ هيكلٍ (ينطبق على الأدوار) سُكِّن في «المنطقة ١» يتوزّع على أدوار
    مبانيها. وكلّ فرعٍ ينزل وحده: مبنى لم تُعرَّف أدواره بعد يأخذ حصّته هو
    نفسه بدل أن يسقط من التوزيع. والمكان الذي من مستوى النوع — أو تحته —
    يبقى حيث وُضع: اختيار صاحبه يُحترم.
    """
    if pid not in tree.nodes:
        return []
    if not applies_to:
        return [pid]

    def down(x):
        n = tree.nodes[x]
        if n.level in applies_to or not n._children:
            return [x]
        out = []
        for c in sorted(n._children, key=lambda c: tree.nodes[c].seq):
            out += down(c)
        return out

    p = tree.nodes[pid]
    if p.level in applies_to or not p._children:
        return [pid]
    # النوع ينطبق على مستوى فوق المكان المختار (بند مبنى سُكِّن في دور): يبقى
    if not any(tree.nodes[d].level in applies_to for d in descendants(tree, pid)):
        return [pid]
    return down(pid)


def _ancestors(tree, pid):
    out, cur = [], tree.nodes[pid].parent
    while cur and cur in tree.nodes and cur not in out:
        out.append(cur)
        cur = tree.nodes[cur].parent
    return out


def _order_key(tree, pid):
    chain = [pid] + _ancestors(tree, pid)
    return [tree.nodes[x].seq for x in reversed(chain)]


def equal_shares(places):
    """حصصٌ متساوية مجموعها ‎100‎ تمامًا — الفرق كلّه على الأخيرة."""
    if not places:
        return []
    n = len(places)
    base = round(100.0 / n, 4)
    out = [[p, base] for p in places]
    out[-1][1] = round(100.0 - base * (n - 1), 4)
    return [tuple(x) for x in out]


_UNIT_ALIASES = {"m2": "m2", "sqm": "m2", "sq m": "m2", "م2": "m2", "متر مربع": "m2",
                 "m3": "m3", "cum": "m3", "cu m": "m3", "م3": "m3", "متر مكعب": "m3",
                 "m": "m", "lm": "m", "rm": "m", "ml": "m", "م ط": "m", "متر طولي": "m",
                 "no": "no", "nos": "no", "nr": "no", "each": "no", "ea": "no", "pcs": "no",
                 "عدد": "no", "t": "t", "ton": "t", "tons": "t", "طن": "t",
                 "kg": "kg", "ls": "ls", "lump sum": "ls", "مقطوعية": "ls", "set": "set"}


def unit_key(u):
    """وحدةٌ موحَّدة للمقارنة: ‎'Sq.m'‎ و‎'m²'‎ و‎'م2'‎ كلّها ‎m2‎."""
    t = str(u or "").lower().replace("²", "2").replace("³", "3").replace(".", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return _UNIT_ALIASES.get(t, _UNIT_ALIASES.get(t.replace(" ", ""), t.replace(" ", "")))


# ═══════════════════════════ ٣. التوليد ═══════════════════════════

class Act:
    """نشاطٌ متولَّد من جدول الكميّات. ‎key‎ هويّته التي لا تتغيّر."""

    __slots__ = ("key", "kind", "item", "place", "work_type", "step", "name",
                 "qty", "unit", "cost", "res_cost", "weight", "hours",
                 "days", "materials", "code", "wbs", "discipline", "csi",
                 "calendar", "driving", "order", "items")

    def __init__(self, key, kind, **kw):
        self.key, self.kind = key, kind
        for f in self.__slots__[2:]:
            setattr(self, f, kw.get(f))
        self.hours = self.hours or {}
        self.materials = self.materials or []
        self.items = self.items or []
        self.cost = self.cost or 0.0
        self.res_cost = self.res_cost or 0.0
        self.weight = self.weight or 0.0

    def as_dict(self):
        return {f: getattr(self, f) for f in self.__slots__}


def _durations(qty, rates, resources, frac, step):
    """
    الساعات والمدّة من معدّلات الأداء.

    لكلّ مورد: الساعات = الكميّة × ساعاته للوحدة، ومدّته = الساعات ÷ (العدد ×
    ساعات اليوم). المورد المعلَّم «driving» يحكم مجموعته، وإلّا فالأبطأ.

    ‎step‎ مُعطى: نشاطُ خطوة — معدّلاتها، ومعدّلات النوع كلّه بنسبة وزنها.
    ‎step‎ فارغ: البند كلّه نشاطٌ واحد — خطواته متتابعة فتُجمع مُددها، ومعدّلات
    النوع كلّه تمشي معها بالتوازي.
    """
    groups = defaultdict(list)
    hours = defaultdict(float)
    for r in rates:
        st = r.get("step") or ""
        if step is not None and st not in ("", step):
            continue
        f = frac if (step is not None and st == "") else 1.0
        h = qty * float(r.get("hours_per_unit") or 0) * f
        if h <= 0:
            continue
        crew = max(float(r.get("crew_count") or 1), 1e-6)
        d = h / (crew * HPD)
        hours[r["resource"]] += h
        groups[st].append((d, bool(r.get("driving")), r["resource"]))

    def gov(g):
        drv = [x for x in g if x[1]]
        pick = max(drv or g, key=lambda x: x[0])
        return pick[0], pick[2]

    if not groups:
        return dict(hours), None, None
    whole = gov(groups[""]) if groups.get("") else (0.0, None)
    steps = [gov(g) for k, g in groups.items() if k]
    seq = sum(x[0] for x in steps)
    if seq >= whole[0]:
        drv = max(steps, key=lambda x: x[0])[1] if steps else whole[1]
        return dict(hours), seq, drv
    return dict(hours), whole[0], whole[1]


def _days(d):
    """المدّة بأيّام صحيحة لا تقلّ عن يوم: بريمافيرا لا تجدول نصف نشاط."""
    if d is None:
        return None
    return max(1, int(math.ceil(d - 1e-9)))


def auto_codes(tree):
    """يعطي كلّ مكانٍ بلا كود كودًا من حرف مستواه وترتيبه بين إخوته: ‎F3‎."""
    by_parent = defaultdict(list)
    for p in tree.nodes.values():
        by_parent[p.parent].append(p)
    for sibs in by_parent.values():
        n = defaultdict(int)
        for p in sorted(sibs, key=lambda x: x.seq):
            n[p.level] += 1
            if not (p.code or "").strip():
                p.code = f"{(p.token or p.level or 'P')[:1].upper()}{n[p.level]}"


def build(items, allocs, tree, dicts, settings=None):
    """
    يولّد الأنشطة والعلاقات والأوزان من البنود المفكَّكة.

    ‎items‎: بنود مخزَّنة (‎id, item_name, qty, unit, amount, work_type,
    split, include‎). ‎allocs‎: ‎{item_id: [(place_id, share%)]}‎ — الأماكن كما
    اختارها صاحبها، وتُنزَل إلى مستوى نوع العمل هنا. ‎tree‎:
    شجرة أماكن ‎generator.PlaceTree‎ غير موسَّعة. ‎dicts‎: القواميس من
    ‎dictionaries_api.load_all‎.

    يعيد ‎(acts, rels, diag, rollup)‎.
    """
    settings = settings or {}
    project = settings.get("project", "")
    pattern = settings.get("pattern") or "{PRJ}-{AREA}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}"
    sep = settings.get("sep") or "-"
    calendar = settings.get("force_calendar")

    wt_by = {w["code"]: w for w in dicts["work_types"]}
    res_by = {r["code"]: r for r in dicts["resources"]}
    mat_by = {m["code"]: m for m in dicts["materials"]}
    csi_of_disc = {d["code"]: d.get("csi_division") for d in dicts["disciplines"]}

    xt = tree.expand()
    auto_codes(xt)
    # التسكين على عقدة متكرّرة يتوزّع على نسخها
    copies = defaultdict(list)
    for nid in xt.nodes:
        copies[nid.split("#")[0]].append(nid)

    diag = defaultdict(int)
    diag["unclassified"] = []
    diag["unallocated"] = []
    diag["unit_mismatch"] = []
    acts, by_item = [], defaultdict(list)       # by_item[(item, place)] = [acts…] بالترتيب
    pieces = []                                 # قطعة لكلّ (بند، مكان، خطوة) قبل التجميع

    for it in items:
        if not it.get("include", True):
            diag["excluded"] += 1
            continue
        wt = wt_by.get(it.get("work_type") or "")
        if not wt:
            diag["unclassified"].append(it["id"])
            continue
        al = allocs.get(it["id"]) or []
        if not al:
            diag["unallocated"].append(it["id"])
            continue
        qty = float(it.get("qty") or 0)
        amount = float(it.get("amount") or 0)
        # وحدةٌ تخالف وحدة نوع العمل علامةُ تصنيفٍ خاطئ: معدّلات ‎m2‎ على بندٍ
        # بالعدد تُنتج مددًا لا معنى لها.
        if unit_key(it.get("unit")) and unit_key(wt.get("unit")) and \
                unit_key(it.get("unit")) != unit_key(wt.get("unit")):
            diag["unit_mismatch"].append(it["id"])
        if not amount:
            diag["no_price"] += 1
        steps = wt.get("steps") or []
        split = bool(it.get("split")) and bool(steps)
        parts = ([(s["code"], s["name"], float(s["weight"] or 0) / 100.0) for s in steps]
                 if split else [(None, None, 1.0)])
        disc = wt.get("discipline") or ""
        csi = wt.get("csi_division") or csi_of_disc.get(disc) or ""

        # التسكين محفوظٌ كما اختاره صاحبه («المبنى ١»)، ويُنزَل هنا إلى مستوى
        # نوع العمل — فدورٌ يُضاف للمبنى بعد التسكين يأخذ حصّته وحده. ثمّ تُقسم
        # كلّ حصّة على النسخ المتكرّرة.
        placed = []
        for pid, share in al:
            targets = spread(tree, str(pid), wt.get("applies_to") or [])
            for t in targets:
                cs = copies.get(t) or []
                for c in cs:
                    placed.append((c, float(share) / len(targets) / len(cs)))
        if not placed:
            diag["unallocated"].append(it["id"])
            continue
        for pid, share in placed:
            q = qty * share / 100.0
            for st_code, st_name, frac in parts:
                hours, dur, drv = _durations(q, wt.get("rates") or [], res_by,
                                             frac, st_code)
                if dur is None:
                    base = float(wt.get("days") or 1)
                    dur = base * (frac if split else 1.0)
                    diag["default_duration"] += 1
                rc = sum(h * float((res_by.get(r) or {}).get("unit_cost") or 0)
                         for r, h in hours.items())
                mats = []
                for m in wt.get("materials") or []:
                    mt = mat_by.get(m["material"])
                    if not mt:
                        continue
                    mq = q * float(m.get("qty_per_unit") or 0) * frac
                    mc = mq * float(mt.get("unit_cost") or 0)
                    rc += mc
                    mats.append({"material": m["material"], "qty": round(mq, 4),
                                 "cost": round(mc, 2)})
                pieces.append({
                    "item": it, "wt": wt, "place": pid, "step": st_code,
                    "step_name": st_name, "split": split, "qty": q,
                    "unit": it.get("unit") or wt.get("unit") or "",
                    "cost": amount * share / 100.0 * frac, "res_cost": rc,
                    "hours": hours, "dur": dur, "driving": drv, "mats": mats,
                    "disc": disc, "csi": csi})

    # ── من القطع إلى أنشطة: لكلّ بند، أو مجمَّعةً لكلّ نوع عمل في كلّ مكان ──
    # الاختيار على نوع العمل في القاموس. المجمَّع يحمل بنوده (للـMapping sheet)،
    # ومدّته مجموع مدد بنوده لأنّ طاقمًا واحدًا ينجزها، وكميّته مجموعها إن
    # اتّحدت الوحدة — والوحدات المختلفة لا تُجمع.
    def make_name(label, body, pl):
        name = f"{label}{body}"
        if len(name) > MAX_NAME - len(pl.name) - 3:
            name = name[:MAX_NAME - len(pl.name) - 4].rstrip() + "…"
        return f"{name} - {pl.name}"[:MAX_NAME]

    groups = defaultdict(list)
    for pc in pieces:
        per_item = bool(pc["wt"].get("per_item"))
        if per_item:
            gk = ("i", pc["item"]["id"], pc["place"], pc["step"])
        else:
            gk = ("g", pc["wt"]["code"], pc["split"], pc["place"], pc["step"])
        groups[gk].append(pc)
    for gk, ps in groups.items():
        p0 = ps[0]
        wt, pid, st_code = p0["wt"], p0["place"], p0["step"]
        pl = xt.nodes[pid]
        label = f"{p0['step_name']} - " if p0["step_name"] else ""
        hours = defaultdict(float)
        for pc in ps:
            for r, h in pc["hours"].items():
                hours[r] += h
        units = {unit_key(pc["unit"]) for pc in ps}
        qty = sum(pc["qty"] for pc in ps) if len(units) == 1 else None
        drv = max(ps, key=lambda pc: pc["dur"])["driving"]
        mats = defaultdict(lambda: [0.0, 0.0])
        for pc in ps:
            for m in pc["mats"]:
                mats[m["material"]][0] += m["qty"]
                mats[m["material"]][1] += m["cost"]
        common = dict(place=pid, work_type=wt["code"], step=st_code,
                      unit=p0["unit"] if qty is not None else "mixed",
                      cost=sum(pc["cost"] for pc in ps),
                      res_cost=sum(pc["res_cost"] for pc in ps),
                      hours={k: round(v, 3) for k, v in hours.items()},
                      days=_days(sum(pc["dur"] for pc in ps)),
                      materials=[{"material": k, "qty": round(v[0], 4), "cost": round(v[1], 2)}
                                 for k, v in mats.items()],
                      discipline=p0["disc"], csi=p0["csi"],
                      calendar=wt.get("calendar_hint") or calendar, driving=drv,
                      qty=round(qty, 4) if qty is not None else None,
                      wbs=" / ".join(x.name for x in xt.ancestry(pid)),
                      items=[{"item": pc["item"]["id"], "qty": round(pc["qty"], 4),
                              "cost": round(pc["cost"], 2)} for pc in ps])
        if gk[0] == "i":
            it = p0["item"]
            a = Act(f"b{it['id']}|{pid}|{st_code or '-'}", "boq", item=it["id"],
                    name=make_name(label, str(it.get("item_name") or wt["name"]).strip(), pl),
                    **common)
            chain = (it["id"], pid)
        else:
            a = Act(f"g{wt['code']}|{'s' if p0['split'] else 'w'}|{pid}|{st_code or '-'}",
                    "boq", item=None, name=make_name(label, wt["name"], pl), **common)
            chain = (("g", wt["code"], p0["split"]), pid)
        acts.append(a)
        by_item[chain].append(a)

    # ── الأوزان: من أصغر نشاط ──
    total = sum(a.cost for a in acts)
    basis = "cost"
    if total <= 0:
        # جدول كميّات بلا أسعار: الوزن من تكلفة القاموس بدل أن يصير كلّه صفرًا
        total, basis = sum(a.res_cost for a in acts), "dictionary"
    for a in acts:
        v = a.cost if basis == "cost" else a.res_cost
        a.weight = (v / total * 100.0) if total else 0.0

    # ── الأكواد ──
    counters = defaultdict(int)
    has_step = "{STEP}" in pattern
    for a in acts:
        pl = xt.nodes[a.place]
        wt_code = a.work_type + ("" if has_step or not a.step else a.step)
        wtp = G.WorkType(wt_code, wt_code, discipline=a.discipline)
        pat = pattern.replace("{STEP}", a.step or "")
        prefix = G.make_code(pat.replace("{NNN}", ""), pl, wtp, xt, project, 1, sep)
        counters[prefix] += 1
        a.code = G.make_code(pat, pl, wtp, xt, project, counters[prefix], sep)

    rels = []

    def link(p, s, t="FS", lag=0.0, why=""):
        if p is s:
            return
        rels.append({"pred": p.key, "succ": s.key, "type": t,
                     "lag": float(lag or 0), "origin": why})

    # ١. سلسلة الخطوات داخل البند الواحد في المكان الواحد
    step_rel = {}
    for w in dicts["work_types"]:
        for s in w.get("steps") or []:
            step_rel[(w["code"], s["code"])] = (s.get("rel_type") or "FS",
                                                float(s.get("lag_days") or 0))
    for group in by_item.values():
        for prev, cur in zip(group, group[1:]):
            t, lag = step_rel.get((cur.work_type, cur.step), ("FS", 0.0))
            link(prev, cur, t, lag, "step")

    # ٢. قواعد أنواع الأعمال، محلولةً على شجرة الأماكن
    first_of = defaultdict(list)       # (place, wt) → أوّل خطوة من كلّ بند
    last_of = defaultdict(list)        # (place, wt) → آخر خطوة من كلّ بند
    for (item_id, pid), group in by_item.items():
        first_of[(pid, group[0].work_type)].append(group[0])
        last_of[(pid, group[-1].work_type)].append(group[-1])
    unresolved = defaultdict(int)
    for r in dicts.get("rules") or []:
        made = 0
        # جريان الطاقم (النوع نفسه إلى المكان التالي) يتبع **البند نفسه**: بند
        # الأبواب في الدور الأول يسبق بند الأبواب ذاته في الثاني. ربطُ كلّ
        # بنود الدور بكلّ بنود التالي كان يصنع ‎n²‎ علاقة لكلّ زوج أدوار —
        # سبعة عشر ألفًا لسبعمئة نشاط في أوّل تجربة على جدول حقيقيّ.
        same_type = r["pred_wt"] == r["succ_wt"] and r["scope"] != "same"
        for (pid, wtc), preds in list(last_of.items()):
            if wtc != r["pred_wt"]:
                continue
            for target in G._targets(xt, pid, r["scope"]):
                succs = first_of.get((target, r["succ_wt"])) or []
                for p in preds:
                    for s in succs:
                        if same_type and s.item != p.item:
                            continue
                        link(p, s, r["rel_type"], r.get("lag_days") or 0, "rule")
                        made += 1
        if not made and any(k[1] == r["pred_wt"] for k in last_of):
            unresolved[f"{r['pred_wt']}->{r['succ_wt']} {r['scope']}"] += 1

    # ── ما ليس له سابق/لاحق من نوعه: بدايات الأنواع ونهاياتها ──
    act_by_key = {a.key: a for a in acts}

    # جوارٌ يُبنى مرّة بعد علاقات البنود، فالبحث عن البدايات والنهايات
    # لا يمسح كلّ العلاقات عند كلّ سؤال — مشروعٌ بآلاف الأنشطة يُسأل مئات المرّات.
    succ_of, pred_of = defaultdict(set), defaultdict(set)
    for r in rels:
        succ_of[r["pred"]].add(r["succ"])
        pred_of[r["succ"]].add(r["pred"])
    boq_by_wt = defaultdict(list)
    for a in acts:
        boq_by_wt[a.work_type].append(a)

    def _edge(group, adj):
        keys = {a.key for a in group}
        return [a for a in group if not (adj[a.key] & keys)]

    def starts_of(wtc, within=None):
        g = [a for a in boq_by_wt[wtc] if within is None or a.place in within]
        return _edge(g, pred_of)

    def ends_of(wtc, within=None):
        g = [a for a in boq_by_wt[wtc] if within is None or a.place in within]
        return _edge(g, succ_of)

    present = []
    for a in acts:
        if a.work_type not in present:
            present.append(a.work_type)

    # ٣. الهندسة: مرّةً للمشروع لكلّ نوع عمل موجود، وآخرها قبل بداياته
    extras = defaultdict(list)
    for x in dicts.get("extras") or []:
        extras[(x["kind"], x["work_type"])].append(x)
    eng_last = {}
    for wtc in present:
        chain = sorted(extras.get(("eng", wtc), []), key=lambda x: x.get("seq") or 0)
        prev = None
        for x in chain:
            a = Act(f"eng|{wtc}|{x['code']}", "eng", work_type=wtc,
                    name=f"{x['name']} - {wt_by[wtc]['name']}"[:MAX_NAME],
                    days=_days(float(x.get("duration_days") or 1)),
                    code=sep.join(v for v in (project, "ENG", wtc, x["code"]) if v),
                    wbs="Engineering", discipline=wt_by[wtc].get("discipline"),
                    csi=wt_by[wtc].get("csi_division"), calendar=calendar)
            acts.append(a)
            act_by_key[a.key] = a
            if prev:
                link(prev, a, x.get("rel_type") or "FS", x.get("lag_days") or 0, "engineering")
            prev = a
        if prev:
            eng_last[wtc] = prev

    # ٤. التوريد: مادّةٌ لها مدّة توريد تسبق أوّل استعمال لها
    procured = defaultdict(list)
    for wtc in present:
        for m in wt_by[wtc].get("materials") or []:
            mt = mat_by.get(m["material"])
            if mt and float(mt.get("lead_time_days") or 0) > 0:
                procured[m["material"]].append(wtc)
    via_proc = set()
    for mc, users in procured.items():
        mt = mat_by[mc]
        a = Act(f"prc|{mc}", "proc", name=f"Procure {mt['name']}"[:MAX_NAME],
                days=_days(float(mt["lead_time_days"])),
                code=sep.join(v for v in (project, "PRC", mc) if v),
                wbs="Procurement", calendar=calendar)
        acts.append(a)
        act_by_key[a.key] = a
        for wtc in users:
            if wtc in eng_last:
                link(eng_last[wtc], a, "FS", 0, "procurement")
            for s in starts_of(wtc):
                link(a, s, "FS", 0, "procurement")
            via_proc.add(wtc)
    for wtc, e in eng_last.items():
        if wtc not in via_proc:
            for s in starts_of(wtc):
                link(e, s, "FS", 0, "engineering")

    # ٥. الاختبار والتشغيل: بعد انتهاء نوع العمل داخل مكانه
    for wtc in present:
        for x in sorted(extras.get(("tc", wtc), []), key=lambda x: x.get("seq") or 0):
            lvl = x.get("level_key") or ""
            if lvl:
                hosts = [p.id for p in xt.nodes.values() if p.level == lvl]
            else:
                hosts = [None]
            for h in hosts:
                within = None if h is None else set([h] + descendants(xt, h))
                ends = ends_of(wtc, within)
                if not ends:
                    continue
                pl = xt.nodes[h] if h else None
                a = Act(f"tc|{wtc}|{x['code']}|{h or '-'}", "tc", work_type=wtc, place=h,
                        name=(f"{x['name']} - {wt_by[wtc]['name']}"
                              + (f" - {pl.name}" if pl else ""))[:MAX_NAME],
                        days=_days(float(x.get("duration_days") or 1)),
                        wbs=(" / ".join(n.name for n in xt.ancestry(h)) if h
                             else "Testing & commissioning"),
                        discipline=wt_by[wtc].get("discipline"),
                        csi=wt_by[wtc].get("csi_division"), calendar=calendar)
                a.code = sep.join(v for v in (
                    project, *(n.code for n in (xt.ancestry(h) if h else [])),
                    "TC", wtc, x["code"]) if v)
                acts.append(a)
                act_by_key[a.key] = a
                for e in ends:
                    link(e, a, "FS", 0, "tc")

    # ٦. روابط الخريطة الصريحة بين المناطق: ملخّصة على بدايات ونهايات كلّ منطقة
    for p_id, s_id, t, lag in settings.get("constraints") or []:
        pa = [x for x in xt.nodes if x == str(p_id) or x.startswith(str(p_id) + "#")]
        sa = [x for x in xt.nodes if x == str(s_id) or x.startswith(str(s_id) + "#")]
        for a_ in pa:
            for b_ in sa:
                A = set([a_] + descendants(xt, a_))
                B = set([b_] + descendants(xt, b_))
                inA = [a for a in acts if a.kind == "boq" and a.place in A]
                inB = [a for a in acts if a.kind == "boq" and a.place in B]
                if not inA or not inB:
                    continue
                firstA, lastA = _edge(inA, pred_of), _edge(inA, succ_of)
                firstB, lastB = _edge(inB, pred_of), _edge(inB, succ_of)
                src = lastA if t in ("FS", "FF") else firstA
                dst = firstB if t in ("FS", "SS") else lastB
                for x in src:
                    for y in dst:
                        link(x, y, t, lag, "map")

    # تكرارٌ لا يضيف شيئًا: الزوج نفسه بالنوع نفسه يُحفظ مرّة
    seen, uniq = set(), []
    for r in rels:
        sig = (r["pred"], r["succ"], r["type"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(r)
    rels = uniq

    rollup = weights_rollup(acts, xt, wt_by, csi_of_disc)
    d = dict(diag)
    d.update({"activities": len(acts), "relations": len(rels),
              "boq_activities": sum(1 for a in acts if a.kind == "boq"),
              "engineering": sum(1 for a in acts if a.kind == "eng"),
              "procurement": sum(1 for a in acts if a.kind == "proc"),
              "tc": sum(1 for a in acts if a.kind == "tc"),
              "total_cost": round(sum(a.cost for a in acts), 2),
              "weight_basis": basis, "unresolved_rules": dict(unresolved)})
    return acts, rels, d, rollup


def weights_rollup(acts, xt, wt_by, csi_of_disc):
    """
    الأوزان مجموعةً من تحت لفوق: لكلّ مكان (بكلّ أسلافه)، ولكلّ قسم CSI، و
    تخصّص، ونوع عمل. ولا وزن يُكتب باليد: يتغيّر وحده حين يتغيّر ما تحته.
    """
    place = defaultdict(float)
    csi = defaultdict(float)
    disc = defaultdict(float)
    wtype = defaultdict(float)
    for a in acts:
        if not a.weight:
            continue
        if a.place and a.place in xt.nodes:
            for n in xt.ancestry(a.place):
                place[n.id.split("#")[0]] += a.weight
        csi[a.csi or ""] += a.weight
        disc[a.discipline or ""] += a.weight
        wtype[a.work_type or ""] += a.weight
    r = lambda d: {k: round(v, 4) for k, v in d.items()}
    return {"place": r(place), "csi": r(csi), "discipline": r(disc), "work_type": r(wtype)}
