# -*- coding: utf-8 -*-
"""
مولّد البرامج الزمنية — البرنامج دالّة في هيكل المشروع.

يُعرَّف شيئان: **شجرة أماكن** (مناطق، مبانٍ، أدوار، وحدات) و**كتالوج أنواع
أعمال**. ثمّ يُضرب الأول في الثاني فتتولّد الأنشطة عند التقاطع، بأكوادها
وعلاقاتها ومددها. وتغييرُ الهيكل يعيد التوليد في ثوانٍ بدل شهر عمل.

ثلاثة قرارات تحكم هذا الملفّ، وهي في ورقة التصميم:

* **محوران منفصلان**: المكان والتخصّص لا يُدمجان في شجرة واحدة. الشجرة
  الواحدة تكرّر قائمة التخصّصات تحت كلّ غرفة، فيصير تعديل نوع عمل واحد
  تعديلَ آلاف العقد. والتسطيح للتصدير عمليةُ إخراج لا بنيةُ تخزين.
* **التسلسل صريح**: «التالي» يُقرأ من سلسلة مكتوبة، لا يُستنتج من صورة.
* **إعادة التوليد مطابقة**: كلّ نشاط يحمل مفتاح توليده، فيُضاف الجديد
  ويُشال الملغى وتبقى التعديلات اليدوية.
"""

import re
from collections import defaultdict

# نطاقات المكان في قواعد الربط
SAME, NEXT, PREV, PARENT, CHILDREN = "same", "next", "prev", "parent", "children"
SCOPES = (SAME, NEXT, PREV, PARENT, CHILDREN)

REL_TYPES = ("FS", "SS", "FF", "SF")
MAX_CODE = 40                      # حدّ ‎task_code‎ في بريمافيرا


# ═══════════════════════════ نماذج البيانات ═══════════════════════════

class Place:
    """عقدة في شجرة الأماكن. تحمل نوع مستواها بنفسها، فالشجرة لا تُلزَم بانتظام."""

    __slots__ = ("id", "parent", "level", "code", "name", "seq",
                 "repeat", "repeat_pattern", "token", "x", "y", "_children")

    def __init__(self, id, level, code, name="", parent=None, seq=0,
                 repeat=1, repeat_pattern="{code}{n:02d}", token=None,
                 x=None, y=None):
        self.id, self.parent, self.level = id, parent, level
        self.code, self.name, self.seq = code, name or code, seq
        self.repeat, self.repeat_pattern = repeat, repeat_pattern
        self.token = (token or level or "").upper()
        self.x, self.y = x, y
        self._children = []

    def __repr__(self):
        return f"<Place {self.id} {self.level}:{self.code}>"


class WorkType:
    """نوع عمل في كتالوج الشركة. التقويم يُسنَد هنا لا إلى كلّ نشاط."""

    __slots__ = ("id", "code", "name", "discipline", "unit", "days",
                 "productivity", "calendar", "applies_to", "excludes")

    def __init__(self, id, code, name="", discipline="", unit="", days=1.0,
                 productivity=None, calendar=None, applies_to=(), excludes=()):
        self.id, self.code, self.name = id, code, name or code
        self.discipline, self.unit = discipline, unit
        self.days, self.productivity, self.calendar = days, productivity, calendar
        # الانطباق يُعرَّف على **المستوى** لا على العقدة، وتُستثنى المخصوصة.
        # لولا ذلك لصار التعريف مئات الضغطات قبل أوّل توليد.
        self.applies_to = tuple(applies_to)
        self.excludes = tuple(excludes)

    def __repr__(self):
        return f"<WorkType {self.code}>"


class Rule:
    """
    قاعدة ربط واحدة. الصيغة الواحدة تعبّر عن أنواع المنطق الثلاثة:

    * داخل المكان الواحد — ‎scope=same‎
    * جريان الطاقم بين الأماكن — السابق = اللاحق و‎scope=next‎
    * مركّب بين مكانين ونوعين — ‎scope=prev‎ أو ‎next‎
    """

    __slots__ = ("pred", "succ", "type", "lag", "scope", "origin", "note")

    def __init__(self, pred, succ, type="FS", lag=0.0, scope=SAME,
                 origin="derived", note=""):
        if type not in REL_TYPES:
            raise ValueError(f"نوع علاقة غير معروف: {type}")
        if scope not in SCOPES:
            raise ValueError(f"نطاق مكان غير معروف: {scope}")
        self.pred, self.succ, self.type = pred, succ, type
        self.lag, self.scope = float(lag), scope
        self.origin, self.note = origin, note

    def __repr__(self):
        return (f"<Rule {self.pred}->{self.succ} {self.type}"
                f"{self.lag:+g} {self.scope}>")


class GenActivity:
    """
    نشاط متولَّد.

    ‎gen_key‎ هو ‎(place_id, work_type_id)‎ — داخليّ لا يتغيّر أبدًا، وعليه
    تقوم المطابقة. و‎code‎ نصٌّ معروض يُعاد بناؤه من النمط ويقبل التعديل.
    لو صار الكود هو المفتاح، لقطع تغييرُ كود منطقةٍ واحدة المطابقةَ على كلّ
    أنشطتها فتُقرأ جديدةً وتُحذف الأصلية ويضيع تقدّمها.
    """

    __slots__ = ("gen_key", "code", "name", "place", "work_type", "days",
                 "calendar", "wbs", "overridden")

    def __init__(self, gen_key, code, name, place, work_type, days,
                 calendar=None, wbs="", overridden=()):
        self.gen_key, self.code, self.name = gen_key, code, name
        self.place, self.work_type = place, work_type
        self.days, self.calendar, self.wbs = days, calendar, wbs
        self.overridden = set(overridden)

    def __repr__(self):
        return f"<GenActivity {self.code}>"


# ═══════════════════════════ شجرة الأماكن ═══════════════════════════

class PlaceTree:
    """شجرة الأماكن، ومنها تُقرأ سلسلة التسلسل ودلالة «التالي»."""

    def __init__(self, places=()):
        self.nodes = {}
        self.parallel = set()        # أزواج تمشي معًا فلا رابط بينها
        # الروابط المرسومة صراحةً بين الأماكن: ‎{sid: [(succ, type, lag)]}‎.
        # وهي **مصدر الحقيقة** للتسلسل حين تُوجد؛ وترتيب الأشقّاء بديلٌ
        # يُلجأ إليه حين لا يُرسم شيء، لا العكس.
        self.links = {}
        self.back = {}
        for p in places:
            self.add(p)

    def link_places(self, pred, succ, rel_type="FS", lag=0.0):
        self.links.setdefault(pred, []).append((succ, rel_type, float(lag)))
        self.back.setdefault(succ, []).append((pred, rel_type, float(lag)))

    def add(self, place):
        self.nodes[place.id] = place
        return place

    def mark_parallel(self, a, b):
        """الزوج المعلَّم «بالتوازي» لا يُنتج رابطًا، فتمشي المنطقتان معًا."""
        self.parallel.add((a, b))
        self.parallel.add((b, a))

    # ---- التوسيع ----

    def expand(self):
        """
        يوسّع المجموعات المتكرّرة إلى عقد مستقلّة.

        عشرون دورًا تُعرَّف مرّةً بعدد ونمط تسمية، **وتُنشأ العشرون فورًا**
        لا عند التوليد: الدور السابع عشر قد يختلف، فلا بدّ أن يكون له وجودٌ
        مستقلّ يُعدَّل ويُستثنى منه نوع عمل.
        """
        # الترتيب يُوسَّع لكلّ العقد، لا للمتكرّرة وحدها. لو ضُرب ترتيب
        # المتكرّرة في ألف وتُرك غيرها، لسبق السطحُ (٤) الدورَ الأول (٣٠٠١)،
        # فيُصبّ سقف المبنى قبل أدواره — وهو ما ظهر في أوّل تشغيل.
        STEP = 1000
        out = {}
        for p in sorted(self.nodes.values(),
                        key=lambda x: (_depth(x, self.nodes), x.seq)):
            if p.repeat <= 1:
                q = Place(p.id, p.level, p.code, p.name, p.parent,
                          seq=p.seq * STEP, token=p.token, x=p.x, y=p.y)
                out[p.id] = q
                continue
            for n in range(1, p.repeat + 1):
                code = p.repeat_pattern.format(code=p.code, n=n)
                nid = f"{p.id}#{n}"
                out[nid] = Place(nid, p.level, code,
                                 name=f"{p.name} {n}", parent=p.parent,
                                 seq=p.seq * STEP + n, token=p.token,
                                 x=p.x, y=p.y)
        tree = PlaceTree()
        tree.nodes = out
        tree.parallel = set(self.parallel)
        # الروابط تُنقل، ورابطُ عقدةٍ متكرّرة يسري على نسخها كلّها
        for pred, lst in self.links.items():
            preds = [k for k in out if k == pred or k.startswith(pred + "#")]
            for succ, t, lag in lst:
                succs = [k for k in out if k == succ or k.startswith(succ + "#")]
                for a in preds:
                    for b in succs:
                        tree.link_places(a, b, t, lag)
        tree._index()
        return tree

    def _index(self):
        for p in self.nodes.values():
            p._children = []
        for p in sorted(self.nodes.values(), key=lambda x: x.seq):
            if p.parent and p.parent in self.nodes:
                self.nodes[p.parent]._children.append(p.id)

    # ---- الجوار ----

    def siblings(self, pid):
        """الأشقّاء من المستوى نفسه، مرتّبين — وهم سلسلة التسلسل."""
        p = self.nodes[pid]
        if p.parent and p.parent in self.nodes:
            ids = self.nodes[p.parent]._children
        else:
            ids = [q.id for q in self.nodes.values() if not q.parent]
        sib = [i for i in ids if self.nodes[i].level == p.level]
        return sorted(sib, key=lambda i: self.nodes[i].seq)

    def neighbours(self, pid, step):
        """
        الأماكن التالية (‎+1‎) أو السابقة (‎-1‎).

        السهم المرسوم على اللوحة يسبق كلّ شيء: إن وُجد رابطٌ صريح من هذا
        المكان فهو الجواب، ومنطقةٌ تغذّي منطقتين تعطي اثنتين لا واحدة.
        وحين لا يُرسم شيء يُقرأ ترتيب الأشقّاء، فلا يبقى التعريف معطّلًا
        إلى أن تُرسم كلّ الأسهم.
        """
        table = self.links if step > 0 else self.back
        if pid in table:
            return [t for t, _rt, _lag in table[pid]
                    if (pid, t) not in self.parallel]
        sib = self.siblings(pid)
        try:
            i = sib.index(pid)
        except ValueError:
            return []
        j = i + step
        if not 0 <= j < len(sib):
            return []
        other = sib[j]
        return [] if (pid, other) in self.parallel else [other]

    def neighbour(self, pid, step):
        n = self.neighbours(pid, step)
        return n[0] if n else None

    def children_of(self, pid):
        return list(self.nodes[pid]._children)

    def ancestry(self, pid):
        """من الجذر إلى العقدة — منها تُقرأ رموز الكود."""
        chain, seen = [], set()
        cur = pid
        while cur and cur in self.nodes and cur not in seen:
            seen.add(cur)
            chain.append(self.nodes[cur])
            cur = self.nodes[cur].parent
        return list(reversed(chain))

    def ordered(self):
        return sorted(self.nodes.values(),
                      key=lambda p: (_depth(p, self.nodes), p.seq))


def _depth(p, nodes, guard=0):
    d, cur = 0, p.parent
    seen = set()
    while cur and cur in nodes and cur not in seen and d < 50:
        seen.add(cur)
        d += 1
        cur = nodes[cur].parent
    return d


# ═══════════════════════════ الانطباق ═══════════════════════════

def applicable(tree, work_types):
    """
    يحسب أزواج ‎(مكان، نوع عمل)‎ التي ستتولّد منها الأنشطة.

    ليس كلّ نوع عمل ينطبق على كلّ مكان: البدروم بلا واجهة، والسطح بلا غرف.
    """
    pairs = []
    for p in tree.ordered():
        for wt in work_types:
            if wt.applies_to and p.level not in wt.applies_to:
                continue
            if p.id in wt.excludes or p.code in wt.excludes:
                continue
            pairs.append((p, wt))
    return pairs


# ═══════════════════════════ أكواد الأنشطة ═══════════════════════════

_TOKEN = re.compile(r"\{([A-Z]+)\}")


def make_code(pattern, place, work_type, tree, project="", seq=1, sep="-"):
    """
    يبني كود النشاط من نمط قابل للتعريف.

    النمط المستعمل فعلًا في مشاريع الشركة — مستخرَجًا من ١٦٦ كودًا في جداول
    كميّاتها — على شاكلة ‎{PRJ}-{ZONE}-{DISC}-{WT}-{NNN}‎، وعدد رموزه يختلف:
    المباني بلا رمز منطقة. فالرمز الفارغ **يُحذف** ولا يترك فاصلين متتاليين.
    """
    by_level = {(a.token or a.level).upper(): a.code
                for a in tree.ancestry(place.id)}
    vals = dict(by_level)
    vals["PRJ"] = project
    vals["DISC"] = work_type.discipline
    vals["WT"] = work_type.code
    vals["NNN"] = f"{seq:03d}"

    parts = []
    for tok in _TOKEN.findall(pattern):
        v = str(vals.get(tok, "") or "").strip()
        if v:
            parts.append(re.sub(r"\s+", "", v).upper())
    code = sep.join(parts)
    return code[:MAX_CODE]


def unique_codes(acts):
    """حارس تفرُّد: كودان متطابقان يفسدان الاستيراد في بريمافيرا صامتًا."""
    seen, clashes = {}, []
    for a in acts:
        if a.code not in seen:
            seen[a.code] = a
            continue
        n = 2
        base = a.code[:MAX_CODE - 3]
        while f"{base}_{n}" in seen:
            n += 1
        clashes.append((a.code, f"{base}_{n}"))
        a.code = f"{base}_{n}"
        seen[a.code] = a
    return clashes


# ═══════════════════════════ التوليد ═══════════════════════════

def generate(tree, work_types, rules, project="", pattern=None,
             hours_per_day=8.0, default_calendar=None):
    """
    يولّد الأنشطة والعلاقات من التعريف.

    يعيد ‎(الأنشطة، العلاقات، تشخيص)‎ — والأنشطة بمفاتيح توليدها، والعلاقات
    أزواجَ مفاتيح، فلا شيء هنا معلَّق على كود قابل للتغيير.
    """
    tree = tree.expand()
    pattern = pattern or "{PRJ}-{ZONE}-{BUILDING}-{FLOOR}-{DISC}-{WT}-{NNN}"
    pairs = applicable(tree, work_types)

    acts, by_key, seq_of = [], {}, defaultdict(int)
    for place, wt in pairs:
        seq_of[wt.id] += 1
        key = (place.id, wt.id)
        wbs = " / ".join(a.name for a in tree.ancestry(place.id))
        a = GenActivity(
            gen_key=key,
            code=make_code(pattern, place, wt, tree, project, seq_of[wt.id]),
            name=f"{wt.name} — {place.name}",
            place=place.id, work_type=wt.id,
            days=wt.days,
            calendar=wt.calendar or default_calendar,
            wbs=wbs)
        acts.append(a)
        by_key[key] = a

    clashes = unique_codes(acts)
    rels, unresolved = build_relations(tree, rules, by_key)
    diag = {
        "places": len(tree.nodes), "work_types": len(work_types),
        "pairs": len(pairs), "activities": len(acts), "relations": len(rels),
        "rules": len(rules), "code_clashes": len(clashes),
        "unresolved_rules": unresolved,
    }
    return acts, rels, diag


def build_relations(tree, rules, by_key):
    """
    يحلّ نطاق المكان على سلسلة الأماكن فتتولّد العلاقات.

    ‎same‎ داخل المكان · ‎next/prev‎ على الأشقّاء المرتّبين · ‎parent/children‎
    رأسيًّا. والزوج المعلَّم «بالتوازي» لا يُنتج رابطًا.
    """
    out, seen = [], set()
    unresolved = defaultdict(int)
    for r in rules:
        made = 0
        for (pid, wid), act in by_key.items():
            if wid != r.pred:
                continue
            for target in _targets(tree, pid, r.scope):
                succ = by_key.get((target, r.succ))
                if not succ:
                    continue
                if act.gen_key == succ.gen_key:
                    continue
                sig = (act.gen_key, succ.gen_key, r.type, r.lag)
                if sig in seen:
                    continue
                seen.add(sig)
                out.append({"pred": act.gen_key, "succ": succ.gen_key,
                            "type": r.type, "lag": r.lag,
                            "scope": r.scope, "origin": r.origin})
                made += 1
        if not made:
            unresolved[f"{r.pred}->{r.succ} {r.type} {r.scope}"] += 1
    return out, dict(unresolved)


def _targets(tree, pid, scope):
    if scope == SAME:
        return [pid]
    if scope == NEXT:
        return tree.neighbours(pid, +1)
    if scope == PREV:
        return tree.neighbours(pid, -1)
    if scope == PARENT:
        p = tree.nodes[pid].parent
        return [p] if p and p in tree.nodes else []
    if scope == CHILDREN:
        return tree.children_of(pid)
    return []


# ═══════════════════════════ إعادة التوليد ═══════════════════════════

def reconcile(existing, fresh):
    """
    يطابق التوليد الجديد بالموجود على **مفتاح التوليد**، ويعيد الفرق.

    من يولّد ألفي نشاط ويعدّل ثلاثمئة باليد ثمّ تُمحى تعديلاته عند أوّل
    إعادة توليد، لا يثق في الأداة مرّة ثانية أبدًا. فالمطابقة هنا:

    * ما في الجديد وليس في القديم → **يُضاف**
    * ما في القديم وليس في الجديد → **يُعرض للحذف** لا يُحذف صامتًا
    * ما في الاثنين → **يبقى**، وتُكتب الحقول غير المعلَّمة ‎overridden‎ فقط

    ولا شيء يُنفَّذ قبل أن يُعرض الفرق.
    """
    old = {a.gen_key: a for a in existing}
    new = {a.gen_key: a for a in fresh}
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    updated, kept = [], []
    for k in new:
        if k not in old:
            continue
        o, n = old[k], new[k]
        changes = {}
        for f in ("code", "name", "days", "calendar", "wbs"):
            if f in o.overridden:
                continue
            ov, nv = getattr(o, f), getattr(n, f)
            if ov != nv:
                changes[f] = (ov, nv)
        if changes:
            updated.append({"key": k, "code": o.code, "changes": changes,
                            "protected": sorted(o.overridden)})
        else:
            kept.append(o)
    return {"added": added, "removed": removed, "updated": updated,
            "kept": kept,
            "summary": {"added": len(added), "removed": len(removed),
                        "updated": len(updated), "kept": len(kept)}}


def apply_reconcile(existing, fresh, diff, delete=True):
    """ينفّذ الفرق بعد عرضه. التعديلات اليدوية المعلَّمة لا تُمسّ."""
    old = {a.gen_key: a for a in existing}
    new = {a.gen_key: a for a in fresh}
    out = []
    for k, n in new.items():
        o = old.get(k)
        if o is None:
            out.append(n)
            continue
        for f in ("code", "name", "days", "calendar", "wbs"):
            if f not in o.overridden:
                setattr(o, f, getattr(n, f))
        out.append(o)
    if not delete:
        out += [o for k, o in old.items() if k not in new]
    return out
