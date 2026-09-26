"""محرّك البرنامج الزمني — التقويمات وحساب المسار الحرج (CPM).

    لماذا وحدة مستقلّة عن app.py؟
    لأن هذا الجزء رياضيّ بحت: يدخله شبكة أنشطة ويخرج منه تواريخ وفوائض،
    ولا يعرف شيئًا عن قاعدة البيانات ولا عن الويب. فصله يعني أنه يُختبَر
    وحده باختبارات محسوبة باليد، وأن أي خطأ في التواريخ نعرف مكانه فورًا.

    الوحدة المعتمَدة للمدد هي الساعة — كما يخزّنها بريمافيرا تمامًا
    (‎target_drtn_hr_cnt‎) — ويُترك التحويل إلى أيام لطبقة العرض، لأن
    «اليوم» يختلف من تقويم لآخر (٨ ساعات هنا، ١٠ هناك) فلا يصلح وحدةً
    داخلية.

    الاصطلاح في المواعيد: البداية هي أول لحظة عمل، والنهاية هي اللحظة
    التي ينتهي عندها آخر دقيقة عمل. فنشاط مدّته ٨ ساعات يبدأ الأحد ٠٨:٠٠
    في تقويم ‎٠٨:٠٠–١٦:٠٠‎ ينتهي الأحد ١٦:٠٠. وهذا هو اصطلاح P6 نفسه،
    ولهذا فاللحظة ١٦:٠٠ صالحة كنهاية ولا تصلح كبداية.
"""

from __future__ import annotations

from datetime import datetime, timedelta, date

MINUTE = 1.0 / 60.0
_EPS = 1e-6                    # تسامح الفاصلة العائمة بالدقائق
_MAX_DAYS = 20000              # حارس ضد تقويم لا يعمل فيه أحد أبدًا


# ───────────────────────────── التقويم ──────────────────────────────

class Calendar:
    """تقويم عمل: أيام الأسبوع وفتراتها، والاستثناءات (إجازات أو أيام خاصّة).

       week: قاموس رقم اليوم بترقيم P6 ‏(1=الأحد … 7=السبت) ← قائمة فترات
             كل فترة ‎(دقيقة البداية, دقيقة النهاية)‎ من منتصف الليل.
       exc:  قاموس 'YYYY-MM-DD' ← قائمة فترات. القائمة الفارغة تعني إجازة.

       الفترة نصف مفتوحة ‎[a, b)‎: اللحظة b ليست لحظة عمل، وهي مع ذلك
       نهاية صالحة. هذا التفريق هو ما يجعل «نهاية اليوم» و«بداية اليوم
       التالي» لحظتين مختلفتين، وهو ما يفعله P6.
    """

    __slots__ = ("name", "week", "exc", "id", "_daymin", "_wpd", "_pcache", "_mcache")

    def __init__(self, week=None, exc=None, name="", cal_id=None):
        self.name = name
        self.id = cal_id
        self.week = {d: sorted(tuple(p) for p in (week or {}).get(d, [])) for d in range(1, 8)}
        self.exc = {k: sorted(tuple(p) for p in v) for k, v in (exc or {}).items()}
        # الجدولة تسأل عن فترات اليوم نفسه آلاف المرّات، والأيام محدودة،
        # فالحفظ هنا يوفّر معظم زمن الحساب على الشبكات الكبيرة
        self._pcache, self._mcache = {}, {}
        self._daymin = {}                       # دقائق العمل في كل يوم أسبوع
        for d in range(1, 8):
            self._daymin[d] = sum(b - a for a, b in self.week[d])
        wk = sum(self._daymin.values())
        days = sum(1 for d in range(1, 8) if self._daymin[d] > 0)
        self._wpd = (wk / days / 60.0) if days else 8.0

    # ---- تعريفات جاهزة ----

    @staticmethod
    def standard(days=(1, 2, 3, 4, 5), start="08:00", end="16:00", name="Standard", exc=None):
        """تقويم بسيط: أيام محدّدة بترقيم P6 وفترة واحدة متصلة."""
        a, b = _hhmm(start), _hhmm(end)
        return Calendar({d: [(a, b)] for d in days}, exc or {}, name)

    @staticmethod
    def continuous(name="24h"):
        """٢٤ ساعة × ٧ أيام — تقويم الأنشطة المستمرّة وبعض قيود العقد."""
        return Calendar({d: [(0, 1440)] for d in range(1, 8)}, {}, name)

    # ---- أساسيات ----

    @property
    def hours_per_day(self):
        """متوسّط ساعات يوم العمل — للعرض فقط، لا يدخل في أي حساب."""
        return self._wpd

    def periods(self, d: date):
        """فترات العمل في يوم بعينه، مع مراعاة الاستثناءات."""
        p = self._pcache.get(d)
        if p is None:
            if self.exc:
                p = self.exc.get(d.isoformat())
                if p is None:
                    p = self.week[d.isoweekday() % 7 + 1]   # الاثنين(1)→2 … الأحد(7)→1
            else:
                p = self.week[d.isoweekday() % 7 + 1]
            self._pcache[d] = p
        return p

    def day_minutes(self, d: date):
        m = self._mcache.get(d)
        if m is None:
            m = self._mcache[d] = sum(b - a for a, b in self.periods(d))
        return m

    def is_working(self, dt: datetime) -> bool:
        """هل هذه اللحظة داخل فترة عمل؟ (النهاية غير داخلة)"""
        m = dt.hour * 60 + dt.minute + dt.second / 60.0
        return any(a - _EPS <= m < b - _EPS for a, b in self.periods(dt.date()))

    # ---- المحاذاة ----

    def snap_start(self, dt: datetime) -> datetime:
        """أقرب لحظة عمل من عند dt أو بعدها — تصلح بدايةً لنشاط."""
        d, m = dt.date(), dt.hour * 60 + dt.minute + dt.second / 60.0
        for _ in range(_MAX_DAYS):
            for a, b in self.periods(d):
                if m < b - _EPS:
                    return _at(d, max(a, m))
            d, m = d + timedelta(days=1), 0.0
        raise CalendarError(f"التقويم {self.name!r} لا يحتوي أيام عمل")

    def snap_finish(self, dt: datetime) -> datetime:
        """أقرب لحظة عمل عند dt أو قبلها — تصلح نهايةً لنشاط."""
        d, m = dt.date(), dt.hour * 60 + dt.minute + dt.second / 60.0
        for _ in range(_MAX_DAYS):
            for a, b in reversed(self.periods(d)):
                if m > a + _EPS:
                    return _at(d, min(b, m))
            d = d - timedelta(days=1)
            m = 1440.0
        raise CalendarError(f"التقويم {self.name!r} لا يحتوي أيام عمل")

    # ---- الحساب ----

    def add(self, dt: datetime, hours: float) -> datetime:
        """يتقدّم من dt بمقدار hours من ساعات العمل ويعيد لحظة الانتهاء.

           المدّة الصفرية تعيد لحظة البداية بعد محاذاتها. والمدّة السالبة
           تنعكس إلى sub، لأن التخلّف الزمني (lag) قد يكون سالبًا.
        """
        if hours < 0:
            return self.sub(dt, -hours)
        t = self.snap_start(dt)
        rem = hours * 60.0
        if rem <= _EPS:
            return t
        d, m = t.date(), t.hour * 60 + t.minute + t.second / 60.0
        for _ in range(_MAX_DAYS):
            for a, b in self.periods(d):
                lo = max(a, m)
                if lo >= b - _EPS:
                    continue
                avail = b - lo
                if avail >= rem - _EPS:
                    return _at(d, lo + rem)
                rem -= avail
            d, m = d + timedelta(days=1), 0.0
        raise CalendarError(f"مدّة أطول من {_MAX_DAYS} يوم في التقويم {self.name!r}")

    def sub(self, dt: datetime, hours: float) -> datetime:
        """يتراجع من dt بمقدار hours من ساعات العمل — للمرور العكسي."""
        if hours < 0:
            return self.add(dt, -hours)
        t = self.snap_finish(dt)
        rem = hours * 60.0
        if rem <= _EPS:
            return t
        d, m = t.date(), t.hour * 60 + t.minute + t.second / 60.0
        for _ in range(_MAX_DAYS):
            for a, b in reversed(self.periods(d)):
                hi = min(b, m)
                if hi <= a + _EPS:
                    continue
                avail = hi - a
                if avail >= rem - _EPS:
                    return _at(d, hi - rem)
                rem -= avail
            d, m = d - timedelta(days=1), 1440.0
        raise CalendarError(f"مدّة أطول من {_MAX_DAYS} يوم في التقويم {self.name!r}")

    def work_between(self, a: datetime, b: datetime) -> float:
        """ساعات العمل بين لحظتين. سالبة إذا جاءت b قبل a."""
        if b < a:
            return -self.work_between(b, a)
        if a.date() == b.date():
            return self._in_day(a.date(), _min(a), _min(b)) / 60.0
        tot = self._in_day(a.date(), _min(a), 1440.0)
        d = a.date() + timedelta(days=1)
        guard = 0
        while d < b.date() and guard < _MAX_DAYS:
            tot += self.day_minutes(d)
            d += timedelta(days=1)
            guard += 1
        tot += self._in_day(b.date(), 0.0, _min(b))
        return tot / 60.0

    def _in_day(self, d: date, lo: float, hi: float) -> float:
        return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in self.periods(d))


class CalendarError(Exception):
    pass


def _hhmm(s):
    h, m = str(s).split(":")[:2]
    v = int(h) * 60 + int(m)
    return 1440 if v == 0 and str(s).startswith("24") else v


def _at(d: date, minutes: float) -> datetime:
    """يبني لحظة من يوم وعدد دقائق — مع استيعاب ٢٤:٠٠ كنهاية لليوم."""
    dd, m = d, round(minutes, 6)
    if m >= 1440 - _EPS:
        dd, m = d + timedelta(days=1), 0.0
    sec = int(round(m * 60))
    return datetime(dd.year, dd.month, dd.day) + timedelta(seconds=sec)


def _min(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60.0


# ───────────────────────── عناصر الشبكة ──────────────────────────

TASK = "task"                  # نشاط عادي
START_MS = "start_milestone"   # معلم بداية
FIN_MS = "finish_milestone"    # معلم نهاية
LOE = "loe"                    # مستوى جهد — يمتدّ مع سوابقه ولواحقه
WBS_SUM = "wbs_summary"        # ملخّص هيكل تجزئة العمل
HAMMOCK = "hammock"

MILESTONES = (START_MS, FIN_MS)
SPANNING = (LOE, WBS_SUM, HAMMOCK)   # لا تُجدوَل بمدّتها بل بمدى ما يحيط بها

NOT_STARTED, IN_PROGRESS, COMPLETE = "not_started", "in_progress", "complete"

# القيود: أسماء P6 مختصرة ومعناها
SNET = "start_on_or_after"      # CS_MSOA
SNLT = "start_on_or_before"     # CS_MSOB
FNET = "finish_on_or_after"     # CS_MEOA
FNLT = "finish_on_or_before"    # CS_MEOB
MSO = "must_start_on"           # CS_MSO
MFO = "must_finish_on"          # CS_MEO
ALAP = "as_late_as_possible"    # CS_ALAP
MANDATORY_START = "mandatory_start"    # CS_MANDSTART — يكسر المنطق
MANDATORY_FIN = "mandatory_finish"     # CS_MANDFIN

FS, SS, FF, SF = "FS", "SS", "FF", "SF"


class Activity:
    """نشاط واحد. المدخلات هي ما يعرفه المستخدم؛ والمخرجات يملؤها المحرّك."""

    __slots__ = (
        "id", "name", "duration", "calendar", "type", "wbs", "code",
        "constraint", "constraint_date", "constraint2", "constraint2_date",
        "status", "actual_start", "actual_finish", "remaining",
        "expected_finish", "suspend", "resume",
        # مخرجات الجدولة
        "es", "ef", "ls", "lf", "total_float", "free_float",
        "critical", "driving_pred", "longest_path",
    )

    def __init__(self, id, name="", duration=0.0, calendar=None, type=TASK,
                 wbs=None, code=None, constraint=None, constraint_date=None,
                 constraint2=None, constraint2_date=None, status=NOT_STARTED,
                 actual_start=None, actual_finish=None, remaining=None,
                 expected_finish=None, suspend=None, resume=None):
        self.id = str(id)
        self.name = name
        self.duration = float(duration or 0.0)
        self.calendar = calendar
        self.type = type
        self.wbs = wbs
        self.code = code
        self.constraint = constraint
        self.constraint_date = constraint_date
        self.constraint2 = constraint2
        self.constraint2_date = constraint2_date
        self.status = status
        self.actual_start = actual_start
        self.actual_finish = actual_finish
        # المتبقّي: ما لم يُذكر فهو المدّة كاملة قبل البدء، وصفر بعد الاكتمال
        self.remaining = float(remaining) if remaining is not None else None
        self.expected_finish = expected_finish
        self.suspend = suspend
        self.resume = resume
        self.es = self.ef = self.ls = self.lf = None
        self.total_float = self.free_float = None
        self.critical = False
        self.driving_pred = None
        self.longest_path = False

    @property
    def is_milestone(self):
        return self.type in MILESTONES

    def remaining_hours(self):
        if self.status == COMPLETE:
            return 0.0
        if self.remaining is not None:
            return max(0.0, self.remaining)
        return max(0.0, self.duration)

    def __repr__(self):
        return f"<Activity {self.id} {self.name!r} {self.duration}h>"


class Relation:
    """علاقة منطقية بين نشاطين. التخلّف lag بالساعات وقد يكون سالبًا."""

    __slots__ = ("pred", "succ", "type", "lag", "driving")

    def __init__(self, pred, succ, type=FS, lag=0.0):
        self.pred = str(pred)
        self.succ = str(succ)
        self.type = type
        self.lag = float(lag or 0.0)
        self.driving = False

    def __repr__(self):
        s = f"{self.pred}→{self.succ} {self.type}"
        return f"<Relation {s}{self.lag:+g}h>" if self.lag else f"<Relation {s}>"


class ScheduleError(Exception):
    pass


class CycleError(ScheduleError):
    def __init__(self, cycles):
        self.cycles = cycles
        head = " ← ".join(cycles[0]) if cycles else ""
        super().__init__(f"حلقة منطقية مغلقة في الشبكة: {head}")


# ────────────────────────── الشبكة والجدولة ──────────────────────────

class Schedule:
    """شبكة أنشطة كاملة: أنشطة وعلاقات وتقويمات وتاريخ بيانات.

       الاستعمال:
           s = Schedule(start=datetime(2026, 1, 4))
           s.add_calendar(Calendar.standard(), default=True)
           s.add(Activity("A", duration=40))
           s.link("A", "B")
           s.run()
    """

    def __init__(self, start=None, data_date=None, must_finish=None,
                 name="", lag_calendar="predecessor",
                 out_of_sequence="retained_logic"):
        self.name = name
        self.start = start
        self.data_date = data_date
        self.must_finish = must_finish
        self.activities = {}
        self.relations = []
        self.calendars = {}
        self.default_calendar = None
        self.lag_calendar = lag_calendar            # predecessor | successor | 24h | project
        self.out_of_sequence = out_of_sequence      # retained_logic | progress_override
        self.warnings = []
        self._succ = {}
        self._pred = {}

    # ---- البناء ----

    def add_calendar(self, cal: Calendar, default=False):
        key = cal.id if cal.id is not None else (cal.name or f"cal{len(self.calendars)}")
        self.calendars[str(key)] = cal
        if default or self.default_calendar is None:
            self.default_calendar = cal
        return cal

    def cal(self, act_or_key):
        """تقويم نشاط أو مفتاح — ويرجع إلى الافتراضي عند الغياب."""
        key = act_or_key.calendar if isinstance(act_or_key, Activity) else act_or_key
        if key is None:
            return self._default()
        c = self.calendars.get(str(key))
        if c is None:
            self.warnings.append(f"تقويم غير معروف: {key} — استُعمل الافتراضي")
            return self._default()
        return c

    def _default(self):
        if self.default_calendar is None:
            self.add_calendar(Calendar.standard(), default=True)
        return self.default_calendar

    def add(self, act: Activity, **kw):
        if not isinstance(act, Activity):
            act = Activity(act, **kw)
        if act.id in self.activities:
            raise ScheduleError(f"نشاط مكرّر: {act.id}")
        self.activities[act.id] = act
        return act

    def link(self, pred, succ, type=FS, lag=0.0):
        r = Relation(pred, succ, type, lag)
        self.relations.append(r)
        return r

    # ---- الترتيب المنطقي ----

    def _index(self):
        self._succ = {a: [] for a in self.activities}
        self._pred = {a: [] for a in self.activities}
        keep = []
        for r in self.relations:
            if r.pred not in self.activities or r.succ not in self.activities:
                self.warnings.append(f"علاقة معلّقة تشير إلى نشاط غير موجود: {r.pred}→{r.succ}")
                continue
            if r.pred == r.succ:
                self.warnings.append(f"نشاط مرتبط بنفسه: {r.pred}")
                continue
            keep.append(r)
            self._succ[r.pred].append(r)
            self._pred[r.succ].append(r)
        self.relations = keep

    def topo_order(self):
        """ترتيب طوبولوجي، ويرفع CycleError مع مسار الحلقة إن وُجدت.

           رسالة «حلقة موجودة» وحدها لا تنفع مستخدمًا أمام ألفي نشاط،
           فنعيد مسار الحلقة نفسه ليعرف أي رابط يقطع.
        """
        indeg = {a: len(self._pred[a]) for a in self.activities}
        queue = [a for a, n in indeg.items() if n == 0]
        order = []
        while queue:
            a = queue.pop()
            order.append(a)
            for r in self._succ[a]:
                indeg[r.succ] -= 1
                if indeg[r.succ] == 0:
                    queue.append(r.succ)
        if len(order) < len(self.activities):
            raise CycleError(self._find_cycles(set(self.activities) - set(order)))
        return order

    def _find_cycles(self, stuck):
        """يستخرج حلقة فعليّة واحدة على الأقل من الأنشطة العالقة."""
        cycles, seen = [], set()
        for root in sorted(stuck):
            if root in seen:
                continue
            path, on_path = [], set()

            def walk(n):
                if n in on_path:
                    i = path.index(n)
                    cycles.append(path[i:] + [n])
                    return True
                if n in seen or n not in stuck:
                    return False
                seen.add(n)
                path.append(n)
                on_path.add(n)
                for r in self._succ[n]:
                    if walk(r.succ):
                        return True
                path.pop()
                on_path.discard(n)
                return False

            walk(root)
            if cycles:
                break
        return cycles or [sorted(stuck)]

    # ---- الجدولة ----

    def run(self):
        """يجري المرور الأمامي والعكسي ويحسب الفوائض. يعيد self."""
        self._index()
        order = self.topo_order()
        start = self._project_start()
        self._forward(order, start)
        self._backward(order)
        self._floats()
        self._spanning(order)
        return self

    def _project_start(self):
        if self.start:
            return self.start
        if self.data_date:
            return self.data_date
        dates = [a.actual_start for a in self.activities.values() if a.actual_start]
        dates += [a.constraint_date for a in self.activities.values()
                  if a.constraint in (SNET, MSO, MANDATORY_START) and a.constraint_date]
        if dates:
            return min(dates)
        raise ScheduleError("لا تاريخ بداية للمشروع ولا تاريخ بيانات ولا أي تاريخ فعلي")

    # ---- المرور الأمامي ----

    def _forward(self, order, start):
        dd = self.data_date
        for aid in order:
            a = self.activities[aid]
            cal = self.cal(a)

            if a.status == COMPLETE and a.actual_start and a.actual_finish:
                a.es, a.ef = a.actual_start, a.actual_finish
                continue

            # أبكر ما يمليه المنطق على هذا النشاط
            es_cands, ef_cands = [], []
            for r in self._pred[aid]:
                p = self.activities[r.pred]
                if p.es is None:
                    continue
                if self.out_of_sequence == "progress_override" and a.status == IN_PROGRESS:
                    continue            # تجاوز التقدّم: المنطق يسقط عن نشاط بدأ
                if r.type == FS:
                    es_cands.append((self._shift(p, a, p.ef, r.lag), r))
                elif r.type == SS:
                    es_cands.append((self._shift(p, a, p.es, r.lag), r))
                elif r.type == FF:
                    ef_cands.append((self._shift(p, a, p.ef, r.lag), r))
                elif r.type == SF:
                    ef_cands.append((self._shift(p, a, p.es, r.lag), r))
                else:
                    self.warnings.append(f"نوع علاقة غير معروف: {r.type}")

            dur = a.remaining_hours()

            if a.status == IN_PROGRESS and a.actual_start:
                a.es = a.actual_start
                # العمل المتبقّي لا يبدأ قبل تاريخ البيانات، ولا قبل ما يمليه
                # منطقٌ لم يكتمل بعد (المنطق المحتفَظ به)
                resume = a.resume or dd or a.actual_start
                for t, _r in es_cands:
                    if t > resume:
                        resume = t
                base = cal.snap_start(resume)
                a.ef = cal.add(base, dur) if not a.is_milestone else base
                if a.expected_finish:
                    a.ef = max(a.ef, cal.snap_finish(a.expected_finish))
            else:
                es = start if not es_cands else max(t for t, _ in es_cands)
                if dd and dd > es:
                    es = dd            # لا شيء لم يبدأ يمكن أن يبدأ في الماضي
                es = cal.snap_start(es)
                es, forced = self._apply_start_constraint(a, cal, es)
                if a.type == FIN_MS and es_cands and not forced:
                    # معلم النهاية يقع عند اللحظة التي يمليها سابقه، لا في صباح الغد
                    es = cal.snap_finish(max(t for t, _ in es_cands))
                a.es = es
                a.ef = es if a.is_milestone or dur <= 0 else cal.add(es, dur)

            # علاقات FF/SF قد تدفع النهاية وحدها، فتُسحَب البداية خلفها
            if ef_cands and a.status != COMPLETE:
                need = cal.snap_finish(max(t for t, _ in ef_cands))
                if need > a.ef:
                    a.ef = need
                    if not a.is_milestone and a.status != IN_PROGRESS:
                        a.es = cal.sub(a.ef, dur) if dur > 0 else cal.snap_start(a.ef)
                    elif a.is_milestone:
                        a.es = a.ef

            a.ef = self._apply_finish_constraint(a, cal, a.ef)
            if not a.is_milestone and a.status == NOT_STARTED and a.ef < a.es:
                a.es = a.ef
            # السابق «الدافع» هو من يقع موعده المحاذى على بداية اللاحق نفسها.
            # المقارنة تكون بعد المحاذاة لا قبلها، وإلّا لم يُعَدّ سابقٌ ينتهي
            # الخميس عصرًا دافعًا للاحق يبدأ الأحد صباحًا، وهو دافعه فعلًا.
            for t, r in es_cands:
                if _close(cal.snap_start(t), a.es) or _close(cal.snap_finish(t), a.es):
                    r.driving = True
                    a.driving_pred = a.driving_pred or r.pred
            for t, r in ef_cands:
                if _close(cal.snap_finish(t), a.ef):
                    r.driving = True
                    a.driving_pred = a.driving_pred or r.pred

    def _apply_start_constraint(self, a, cal, es):
        c, cd = a.constraint, a.constraint_date
        if not c or not cd:
            return es, False
        if c in (SNET,) and cd > es:
            return cal.snap_start(cd), True
        if c in (MSO, MANDATORY_START):
            return cal.snap_start(cd), True
        return es, False

    def _apply_finish_constraint(self, a, cal, ef):
        c, cd = a.constraint, a.constraint_date
        if c in (FNET,) and cd and cd > ef:
            ef = cal.snap_finish(cd)
        elif c in (MFO, MANDATORY_FIN) and cd:
            ef = cal.snap_finish(cd)
        c2, cd2 = a.constraint2, a.constraint2_date
        if c2 == FNET and cd2 and cd2 > ef:
            ef = cal.snap_finish(cd2)
        if c2 == SNET and cd2 and cd2 > a.es:
            a.es = cal.snap_start(cd2)
            if a.ef is not None and a.es > a.ef:
                ef = cal.add(a.es, a.remaining_hours())
        return ef

    # ---- المرور العكسي ----

    def _backward(self, order):
        finish = self.must_finish or max(
            (a.ef for a in self.activities.values() if a.ef), default=None)
        if finish is None:
            raise ScheduleError("تعذّر تحديد نهاية المشروع")
        for aid in reversed(order):
            a = self.activities[aid]
            cal = self.cal(a)

            if a.status == COMPLETE:
                a.ls, a.lf = a.es, a.ef
                continue

            lf_cands, ls_cands = [], []
            for r in self._succ[aid]:
                s = self.activities[r.succ]
                if s.lf is None:
                    continue
                if s.status == COMPLETE:
                    continue        # لاحق منتهٍ لا يقيّد سابقًا لم ينته
                if r.type == FS:
                    lf_cands.append(self._unshift(a, s, s.ls, r.lag))
                elif r.type == SS:
                    ls_cands.append(self._unshift(a, s, s.ls, r.lag))
                elif r.type == FF:
                    lf_cands.append(self._unshift(a, s, s.lf, r.lag))
                elif r.type == SF:
                    ls_cands.append(self._unshift(a, s, s.lf, r.lag))

            dur = a.remaining_hours()
            lf = min(lf_cands) if lf_cands else cal.snap_finish(finish)
            if ls_cands:
                lf = min(lf, cal.add(min(ls_cands), dur) if not a.is_milestone else min(ls_cands))
            lf = self._late_constraint(a, cal, lf)
            if a.is_milestone or dur <= 0:
                # المعلم لحظة واحدة لا فترة، وموعده المتأخّر يأتي جاهزًا من
                # لاحقه. ولو حاذيناه كنهايةِ فترة عمل لدُفع معلم البداية إلى
                # مساء اليوم السابق فظهر له فائض سالب من العدم.
                a.lf = lf if (cal.is_working(lf) or _close(cal.snap_finish(lf), lf)) \
                    else cal.snap_finish(lf)
                a.ls = a.lf
            else:
                a.lf = cal.snap_finish(lf)
                a.ls = cal.sub(a.lf, dur)
            if a.status == IN_PROGRESS and a.actual_start:
                a.ls = min(a.ls, a.actual_start)

    def _late_constraint(self, a, cal, lf):
        c, cd = a.constraint, a.constraint_date
        if c in (FNLT, MFO, MANDATORY_FIN) and cd:
            lf = min(lf, cal.snap_finish(cd))
        elif c in (SNLT, MSO, MANDATORY_START) and cd:
            lf = min(lf, cal.add(cal.snap_start(cd), a.remaining_hours()))
        c2, cd2 = a.constraint2, a.constraint2_date
        if c2 == FNLT and cd2:
            lf = min(lf, cal.snap_finish(cd2))
        if c2 == SNLT and cd2:
            lf = min(lf, cal.add(cal.snap_start(cd2), a.remaining_hours()))
        return lf

    # ---- الفوائض ----

    def _floats(self):
        for a in self.activities.values():
            cal = self.cal(a)
            if a.status == COMPLETE:
                a.total_float = a.free_float = None
                a.critical = False
                continue
            a.total_float = round(cal.work_between(a.ef, a.lf), 4)
            slack = []
            for r in self._succ[a.id]:
                s = self.activities[r.succ]
                if s.es is None or s.status == COMPLETE:
                    continue
                if r.type == FS:
                    slack.append(cal.work_between(self._shift(a, s, a.ef, r.lag), s.es))
                elif r.type == SS:
                    slack.append(cal.work_between(self._shift(a, s, a.es, r.lag), s.es))
                elif r.type == FF:
                    slack.append(cal.work_between(self._shift(a, s, a.ef, r.lag), s.ef))
                elif r.type == SF:
                    slack.append(cal.work_between(self._shift(a, s, a.es, r.lag), s.ef))
            ff = min(slack) if slack else a.total_float
            a.free_float = round(min(ff, a.total_float), 4)
            a.critical = a.total_float <= _EPS

    # ---- الأنشطة الممتدّة ----

    def _spanning(self, order):
        """LOE والملخّصات لا مدّة لها: تمتدّ من أبكر سابقٍ إلى آخر لاحق.

           تُحسَب بعد الجميع لأنها تابعة لا متبوعة، ولا تُعَدّ حرجة أبدًا
           مهما بدا فائضها صفرًا — وهذا ما يفعله P6، وإلا امتلأ المسار
           الحرج ببنود إدارية لا تؤخّر أحدًا.
        """
        for aid in order:
            a = self.activities[aid]
            if a.type not in SPANNING:
                continue
            ps = [self.activities[r.pred] for r in self._pred[aid]]
            ss = [self.activities[r.succ] for r in self._succ[aid]]
            starts = [x.es for x in ps + ss if x.es] or [a.es]
            ends = [x.ef for x in ps + ss if x.ef] or [a.ef]
            if starts and ends:
                a.es, a.ef = min(starts), max(ends)
                a.ls, a.lf = a.es, a.ef
            a.critical = False
            a.total_float = a.free_float = None

    # ---- أدوات ----

    def _shift(self, pred, succ, t, lag):
        """يزيح لحظةً بمقدار التخلّف الزمني على تقويم التخلّف.

           والتخلّف الصفري لا يُزاح أصلًا: تُمرَّر اللحظة كما هي ليحاذيها
           تقويم اللاحق نفسه. لو أدخلناها في تقويم السابق لأخطأنا كلما
           اختلف التقويمان — نشاط في تقويم ٢٤ ساعة يلي نشاطًا ينتهي
           الرابعة عصرًا يجب أن يبدأ الرابعة عصرًا، لا صباح الغد.
        """
        if not lag:
            return t
        return self._lag_cal(pred, succ).add(t, lag)

    def _unshift(self, pred, succ, t, lag):
        if not lag:
            return t
        return self._lag_cal(pred, succ).sub(t, lag)

    def _lag_cal(self, pred, succ):
        m = self.lag_calendar
        if m == "successor":
            return self.cal(succ)
        if m == "24h":
            return Calendar.continuous()
        if m == "project":
            return self._default()
        return self.cal(pred)

    # ---- المخرجات ----

    @property
    def finish(self):
        return max((a.ef for a in self.activities.values() if a.ef), default=None)

    def critical_path(self):
        """الأنشطة الحرجة مرتّبة زمنيًا."""
        cr = [a for a in self.activities.values() if a.critical and a.type not in SPANNING]
        return sorted(cr, key=lambda a: (a.es, a.ef))

    def longest_path_set(self):
        """المسار الأطول: تتبّع عكسي من آخر نشاط عبر السوابق الدافعة.

           فرقٌ يهمّ في المطالبات: «الحرج» فائضه صفر وقد يكون كذلك بسبب
           قيدٍ مفروض، أما «الأطول» فهو السلسلة التي تحدّد النهاية فعلًا.
        """
        ends = [a for a in self.activities.values() if a.ef and a.type not in SPANNING]
        if not ends:
            return []
        last = max(a.ef for a in ends)
        # قد ينتهي أكثر من نشاط في اللحظة نفسها — نشاط ومعلمه مثلًا — فنبدأ
        # التتبّع من كلّها، وإلّا سقط من المسار ما وقع خلف نظيره في الترتيب
        out, stack, seen = [], [a.id for a in ends if a.ef == last], set()
        while stack:
            aid = stack.pop()
            if aid in seen:
                continue
            seen.add(aid)
            a = self.activities[aid]
            a.longest_path = True
            out.append(a)
            for r in self._pred[aid]:
                if r.driving:
                    stack.append(r.pred)
        return sorted(out, key=lambda a: (a.es, a.ef))

    def open_ends(self):
        """أنشطة بلا سابق أو بلا لاحق — أول ما يراجعه أي مدقّق جدول."""
        if not self._succ:
            self._index()
        miss = []
        for aid, a in self.activities.items():
            if a.type in SPANNING:
                continue
            # الارتباط بمستوى جهد أو ببند ملخّص ليس منطقًا: هذه بنود تابعة
            # لما حولها، فنشاط كل لواحقه منها هو نشاط بلا لاحق حقيقي
            real = lambda k: self.activities[k].type not in SPANNING
            no_p = not any(r.type in (FS, SS) and real(r.pred) for r in self._pred.get(aid, []))
            no_s = not any(r.type in (FS, FF) and real(r.succ) for r in self._succ.get(aid, []))
            if no_p or no_s:
                miss.append((a, "بلا سابق" if no_p and not no_s
                             else "بلا لاحق" if no_s and not no_p else "بلا سابق ولا لاحق"))
        return miss

    def to_rows(self, hours_per_day=None):
        """صفوف جاهزة للعرض أو للتخزين."""
        rows = []
        for a in sorted(self.activities.values(), key=lambda x: (x.es or datetime.max, x.id)):
            hpd = hours_per_day or self.cal(a).hours_per_day
            rows.append({
                "id": a.id, "name": a.name, "wbs": a.wbs, "type": a.type,
                "duration_hr": a.duration, "duration_d": _d(a.duration, hpd),
                "remaining_hr": a.remaining_hours(),
                "status": a.status,
                "es": a.es, "ef": a.ef, "ls": a.ls, "lf": a.lf,
                "total_float_hr": a.total_float, "total_float_d": _d(a.total_float, hpd),
                "free_float_hr": a.free_float, "free_float_d": _d(a.free_float, hpd),
                "critical": a.critical, "longest_path": a.longest_path,
            })
        return rows


def _d(hours, hpd):
    return None if hours is None else round(hours / hpd, 2)


def _close(a, b):
    return a is not None and b is not None and abs((a - b).total_seconds()) < 60
