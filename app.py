import os
import json
import io
import time
import re
import secrets
import string
from urllib.parse import quote
from datetime import datetime, timedelta, date
from decimal import Decimal
from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import psycopg2
import psycopg2.extras
import cloudinary
import cloudinary.uploader
import requests
import bcrypt
from dotenv import load_dotenv
import pdf_report

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_URL = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_SCHEMA_READY = False


def get_db_connection():
    """اتصال جديد بقاعدة البيانات. أول اتصال ناجح في كل تشغيل للسيرفر يضيف الأعمدة الجديدة لو
    ناقصة (ADD COLUMN IF NOT EXISTS آمن ويتنفّذ مرة واحدة فقط)، فمفيش حاجة تتعمل يدوياً في Supabase."""
    global _SCHEMA_READY
    conn = psycopg2.connect(DB_URL)
    if not _SCHEMA_READY:
        try:
            cur = conn.cursor()
            # تاريخ مصدر النسبة الفعلية (YYYY-MM-DD): تحديث مدير المشروع / Data Date في XER /
            # نهاية الفترة المالية / يوم الإدخال اليدوي — عشان المنحنى الأسبوعي يقف عند آخر رقم حقيقي
            cur.execute("ALTER TABLE cashflow_rows ADD COLUMN IF NOT EXISTS act_date TEXT")
            # التوزيع اليومي لخط الأساس من برنامج XER (يوم عمل ← مبلغ مخطط). الجدول الشهري يفضل هو
            # المرجع للمجاميع، واليومي بيحدد شكل التوزيع جوه كل شهر بس.
            cur.execute("""CREATE TABLE IF NOT EXISTS cashflow_plan_daily (
                               project_name TEXT NOT NULL, d TEXT NOT NULL, amount NUMERIC,
                               PRIMARY KEY (project_name, d))""")
            # الجداول المخصصة: تعريف الأعمدة والصفوف في JSONB، فإضافة عمود ما تحتاجش هجرة
            cur.execute("""CREATE TABLE IF NOT EXISTS sheets (
                               id SERIAL PRIMARY KEY, name TEXT NOT NULL, name_en TEXT,
                               columns JSONB NOT NULL DEFAULT '[]'::jsonb,
                               settings JSONB NOT NULL DEFAULT '{}'::jsonb,
                               created_by TEXT, created_at TIMESTAMP DEFAULT NOW(),
                               updated_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sheet_rows (
                               id SERIAL PRIMARY KEY,
                               sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
                               seq INTEGER NOT NULL DEFAULT 0,
                               data JSONB NOT NULL DEFAULT '{}'::jsonb,
                               updated_by TEXT, updated_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS sheet_rows_sheet_seq ON sheet_rows (sheet_id, seq, id)")
            # ===== طبقة الحقائق: كل المديولات بتكتب أرقامها هنا، والداشبورد بيقرا من هنا بس =====
            cur.execute("""CREATE TABLE IF NOT EXISTS fact_metrics (
                               key TEXT PRIMARY KEY,
                               label TEXT NOT NULL, label_en TEXT,
                               unit TEXT DEFAULT '', kind TEXT DEFAULT 'number',
                               agg TEXT DEFAULT 'last', direction TEXT DEFAULT 'neutral',
                               sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                               note TEXT, active BOOLEAN DEFAULT TRUE, seq INTEGER DEFAULT 100)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS project_facts (
                               id SERIAL PRIMARY KEY,
                               project_name TEXT NOT NULL,
                               metric TEXT NOT NULL,
                               period DATE NOT NULL,
                               value DOUBLE PRECISION,
                               text_value TEXT,
                               source TEXT NOT NULL,
                               source_ref TEXT,
                               is_baseline BOOLEAN DEFAULT FALSE,
                               updated_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS project_facts_key
                           ON project_facts (project_name, metric, period, source,
                                             COALESCE(source_ref, ''), is_baseline)""")
            cur.execute("CREATE INDEX IF NOT EXISTS project_facts_lookup ON project_facts (metric, project_name, period)")
            conn.commit()
            _SCHEMA_READY = True
        except Exception:
            conn.rollback()
    return conn


def _today_ksa():
    """تاريخ اليوم بتوقيت السعودية (UTC+3) كنص YYYY-MM-DD."""
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")


def _clean_date(v):
    """يقبل YYYY-MM-DD (أو أطول) ويرجّعه بالشكل ده، وأي شيء غير صالح ← None."""
    txt = str(v or "")[:10]
    return txt if re.match(r"^\d{4}-\d{2}-\d{2}$", txt) else None


def _week_of(date_txt):
    """رقم الأسبوع داخل الشهر (1-7←1، 8-14←2، 15-21←3، الباقي←4) — نفس القاعدة في كل النظام."""
    try:
        day = int(str(date_txt)[8:10])
    except (TypeError, ValueError):
        return None
    return min(WEEKS_PER_MONTH, max(1, (day + 6) // 7))


def _cell_fields(c):
    """يجهّز الحقول والقيم لحفظ خانة من التدفق النقدي. لو اتبعتت نسبة فعلية من غير تاريخ
    (إدخال يدوي أو لصق)، التاريخ بيتحط تلقائياً: النهارده لو الشهر هو الشهر الجاري، وإلا فاضي
    (يعني نهاية الشهر). ولو النسبة اتمسحت، التاريخ يتمسح معاها."""
    fields, values = [], []
    for k in ("plan_amount", "plan_pct", "act_pct", "act_amount", "note", "act_date"):
        if k in c:
            fields.append(k)
            v = c[k]
            if k == "act_date":
                v = _clean_date(v)
            elif k != "note":
                v = None if (v is None or v == "") else float(v)
            values.append(v)
    if "act_pct" in c and "act_date" not in c:
        ym = (c.get("ym") or "")[:7]
        act = values[fields.index("act_pct")]
        today = _today_ksa()
        fields.append("act_date")
        values.append(today if (act is not None and ym == today[:7]) else None)
    return fields, values

def is_hashed_password(value: str) -> bool:
    return bool(value) and value.startswith(("$2a$", "$2b$", "$2y$"))

def verify_password(plain_password: str, stored_password: str) -> bool:
    if not stored_password:
        return False
    if is_hashed_password(stored_password):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), stored_password.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    return plain_password == stored_password

def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def generate_temp_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def log_audit(actor: str, action: str, details: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO audit_log (actor, action, details, created_at) VALUES (%s, %s, %s, NOW())", (actor, action, details))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Audit Log Error:", e)

def create_notification(message: str, link: str = "#"):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO notifications (message, link, is_read, created_at) VALUES (%s, %s, FALSE, NOW())", (message, link))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Notification Error:", e)

PROJECT_DASHBOARD_QUERY = '''
    SELECT current_data_date, manager_name, project_type, project_desc, project_owner, project_developer, project_contractor,
           consultant_val, contractor_val, cons_inv_val, cont_inv_val, cons_inv_count, cont_inv_count,
           start_contractual, end_contractual, start_actual, end_expected,
           act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev, works_completed, works_ongoing, works_planned, obstacles_data,
           eval_labor, eval_equip, eval_financial, eval_hse,
           drawings_sub, drawings_app, drawings_rev, ir_sub, ir_app, ir_rev, ncr_open, ncr_closed,
           consultant_mods_count, contractor_mods_count, contractor_mods_val,
           file_link_1, file_link_2, file_link_3, file_link_4, isometric_link
    FROM project_updates WHERE project_name = %s ORDER BY current_data_date ASC
'''

def fetch_project_records(project_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(PROJECT_DASHBOARD_QUERY, (project_name,))
    cols = [desc[0] for desc in cursor.description]
    records = [dict(zip(cols, row)) for row in cursor.fetchall()]
    conn.close()
    for r in records:
        for k, v in r.items():
            if isinstance(v, (date, datetime)):
                r[k] = str(v)
    return records

ARABIC_COLUMNS = {
    'id': 'م', 'username': 'اسم المستخدم', 'manager_name': 'مدير المشروع', 'project_name': 'اسم المشروع',
    'project_desc': 'وصف المشروع', 'project_type': 'نوع المشروع', 'current_data_date': 'تاريخ البيانات',
    'project_owner': 'المالك', 'project_developer': 'المطور', 'project_contractor': 'المقاول',
    'consultant_val': 'قيمة عقد الاستشاري', 'contractor_val': 'قيمة عقد المقاول',
    'consultant_mods_count': 'تعديلات الاستشاري (عدد)', 'consultant_mods_val': 'تعديلات الاستشاري (قيمة)', 
    'consultant_mods_time': 'تعديلات الاستشاري (مدة)', 'consultant_mods_end_date': 'تاريخ نهاية الاستشاري (معدل)',
    'contractor_mods_count': 'تعديلات المقاول (عدد)', 'contractor_mods_val': 'تعديلات المقاول (قيمة)', 
    'contractor_mods_time': 'تعديلات المقاول (مدة)', 'contractor_mods_end_date': 'تاريخ نهاية المقاول (معدل)',
    'cons_inv_count': 'فواتير الاستشاري (عدد)', 'cons_inv_val': 'فواتير الاستشاري (قيمة)', 'cons_inv_date': 'تاريخ فواتير الاستشاري',
    'cont_inv_count': 'فواتير المقاول (عدد)', 'cont_inv_val': 'فواتير المقاول (قيمة)', 'cont_inv_date': 'تاريخ فواتير المقاول',
    'start_contractual': 'بداية العقد', 'end_contractual': 'نهاية العقد', 'start_actual': 'البداية الفعلية', 'end_expected': 'النهاية المتوقعة',
    'act_prog_cur': 'الإنجاز الفعلي (حالي)', 'act_prog_prev': 'الإنجاز الفعلي (سابق)',
    'plan_prog_cur': 'الإنجاز المخطط (حالي)', 'plan_prog_prev': 'الإنجاز المخطط (سابق)',
    'works_completed': 'الأعمال المنجزة', 'works_ongoing': 'الأعمال الجارية', 'works_planned': 'الأعمال المخططة', 'obstacles_data': 'المعوقات',
    'eval_labor': 'تقييم العمالة', 'eval_equip': 'تقييم المعدات', 'eval_financial': 'التقييم المالي', 'eval_hse': 'تقييم السلامة',
    'drawings_sub': 'مخططات (مقدمة)', 'drawings_app': 'مخططات (معتمدة)', 'drawings_rev': 'مخططات (قيد المراجعة)',
    'ir_sub': 'طلبات IR (مقدمة)', 'ir_app': 'طلبات IR (معتمدة)', 'ir_rev': 'طلبات IR (قيد المراجعة)',
    'ncr_open': 'مخالفات NCR (مفتوحة)', 'ncr_closed': 'مخالفات NCR (مغلقة)',
    'file_link_1': 'مرفق 1', 'file_link_2': 'مرفق 2', 'file_link_3': 'مرفق 3', 'file_link_4': 'مرفق 4',
    'master_plan_link': 'المخطط العام', 'isometric_link': 'أيزومتريك', 'submission_date': 'تاريخ الإرسال', 'submission_time': 'وقت الإرسال'
}

ENGLISH_COLUMNS = {
    'id': '#', 'username': 'Username', 'manager_name': 'Project Manager', 'project_name': 'Project',
    'project_desc': 'Project Description', 'project_type': 'Project Type', 'current_data_date': 'Data Date',
    'project_owner': 'Owner', 'project_developer': 'Developer', 'project_contractor': 'Contractor',
    'consultant_val': 'Consultant Contract Value', 'contractor_val': 'Contractor Contract Value',
    'consultant_mods_count': 'Consultant Variations (Count)', 'consultant_mods_val': 'Consultant Variations (Value)',
    'consultant_mods_time': 'Consultant Variations (Time)', 'consultant_mods_end_date': 'Consultant Revised End Date',
    'contractor_mods_count': 'Contractor Variations (Count)', 'contractor_mods_val': 'Contractor Variations (Value)',
    'contractor_mods_time': 'Contractor Variations (Time)', 'contractor_mods_end_date': 'Contractor Revised End Date',
    'cons_inv_count': 'Consultant Invoices (Count)', 'cons_inv_val': 'Consultant Invoices (Value)',
    'cons_inv_date': 'Consultant Invoice Date',
    'cont_inv_count': 'Contractor Invoices (Count)', 'cont_inv_val': 'Contractor Invoices (Value)',
    'cont_inv_date': 'Contractor Invoice Date',
    'start_contractual': 'Contractual Start', 'end_contractual': 'Contractual End',
    'start_actual': 'Actual Start', 'end_expected': 'Expected Finish',
    'act_prog_cur': 'Actual Progress (Current)', 'act_prog_prev': 'Actual Progress (Previous)',
    'plan_prog_cur': 'Planned Progress (Current)', 'plan_prog_prev': 'Planned Progress (Previous)',
    'works_completed': 'Completed Works', 'works_ongoing': 'Ongoing Works', 'works_planned': 'Planned Works',
    'obstacles_data': 'Obstacles',
    'eval_labor': 'Manpower Rating', 'eval_equip': 'Equipment Rating',
    'eval_financial': 'Financial Rating', 'eval_hse': 'HSE Rating',
    'drawings_sub': 'Drawings (Submitted)', 'drawings_app': 'Drawings (Approved)', 'drawings_rev': 'Drawings (Under Review)',
    'ir_sub': 'Inspection Requests (Submitted)', 'ir_app': 'Inspection Requests (Approved)',
    'ir_rev': 'Inspection Requests (Under Review)',
    'ncr_open': 'NCRs (Open)', 'ncr_closed': 'NCRs (Closed)',
    'file_link_1': 'Attachment 1', 'file_link_2': 'Attachment 2', 'file_link_3': 'Attachment 3',
    'file_link_4': 'Attachment 4', 'master_plan_link': 'Master Plan', 'isometric_link': 'Isometric',
    'submission_date': 'Submission Date', 'submission_time': 'Submission Time'
}


def send_telegram_alert(action_type, manager, project):
    try:
        ksa_time = datetime.utcnow() + timedelta(hours=3)
        time_str = ksa_time.strftime("%Y-%m-%d | %I:%M %p")
        if action_type == "login":
            msg = f"🟢 *تسجيل دخول جديد*\n\n👤 المدير: {manager}\n🏢 المشروع: {project}\n🕒 الوقت: {time_str}"
        elif action_type == "submit":
            msg = f"✅ *تم إرسال تحديث أسبوعي*\n\n👤 المدير: {manager}\n🏢 المشروع: {project}\n🕒 الوقت: {time_str}"
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

cloudinary.config(cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"), api_key=os.getenv("CLOUDINARY_API_KEY"), api_secret=os.getenv("CLOUDINARY_API_SECRET"))
def upload_to_cloudinary(file: UploadFile):
    try:
        if file.content_type and file.content_type.startswith("image/"):
            return cloudinary.uploader.upload(file.file, resource_type="image", quality="auto", fetch_format="auto", width=1920, crop="limit").get("secure_url")
        return cloudinary.uploader.upload(file.file, resource_type="auto").get("secure_url")
    except:
        return None

# ==================== الجلسة: تمديد تلقائي + منع تخزين صفحات الإدارة ====================
ADMIN_SESSION_SECONDS = 86400          # 24 ساعة من آخر نشاط (وليس من وقت الدخول)

@app.middleware("http")
async def session_refresh(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    admin = request.cookies.get("super_admin_auth")
    # كل طلب ناجح يجدّد الجلسة، فالشغل المفتوح لا ينتهي فجأة أثناء الاستخدام
    if admin and response.status_code < 400 and path not in ("/admin-logout", "/logout"):
        response.set_cookie(key="super_admin_auth", value=admin, httponly=True,
                            max_age=ADMIN_SESSION_SECONDS, samesite="lax")
    # صفحات الإدارة لا تُخزَّن في المتصفح، فلا تظهر نسخة قديمة بعد انتهاء الجلسة
    if (admin or request.cookies.get("auth_user")) and \
       "text/html" in (response.headers.get("content-type") or ""):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
async def main_landing_page(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})

@app.post("/login")
async def do_login(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT manager_name, project_name, password FROM users WHERE username=%s", (username,))
    user = cursor.fetchone()

    if user and verify_password(password, user[2]):
        if not is_hashed_password(user[2]):
            try:
                cursor.execute("UPDATE users SET password=%s WHERE username=%s", (hash_password(password), username))
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()
        background_tasks.add_task(send_telegram_alert, "login", user[0], user[1])
        background_tasks.add_task(log_audit, username, "تسجيل دخول", "تم تسجيل دخول مدير المشروع")
        response = RedirectResponse(url="/update-portal", status_code=303)
        response.set_cookie(key="auth_user", value=username, httponly=True, samesite="lax")
        return response

    conn.close()
    return templates.TemplateResponse(request, "login.html", {"error": "اسم المستخدم أو كلمة المرور غير صحيحة"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("auth_user")
    return response

@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})

@app.post("/admin")
async def do_admin_login(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...)):
    ADMIN_ACCOUNTS = {
        "admin_mohamed": os.getenv("ADMIN_MOHAMED_PWD"),
        "admin_assistant": os.getenv("ADMIN_ASSISTANT_PWD")
    }
    if username in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[username] == password:
        background_tasks.add_task(log_audit, username, "تسجيل دخول إداري", f"دخول حساب {username}")
        response = RedirectResponse(url="/admin-hub", status_code=303)
        response.set_cookie(key="super_admin_auth", value=username, httponly=True,
                            max_age=ADMIN_SESSION_SECONDS, samesite="lax")
        return response
    return templates.TemplateResponse(request, "admin_login.html", {"error": "بيانات الدخول غير صحيحة"})

@app.get("/admin-logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("super_admin_auth")
    return response

@app.get("/admin-hub", response_class=HTMLResponse)
async def admin_hub_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_hub.html", {"admin_user": admin_user})

@app.get("/admin-dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates ORDER BY submission_date DESC, id DESC")
    original_columns = [desc[0] for desc in cursor.description]
    raw_rows = cursor.fetchall()
    conn.close()
    translated_columns = [ARABIC_COLUMNS.get(col, col) for col in original_columns]
    rows = []
    for row in raw_rows:
        row_list = list(row)
        for i, col in enumerate(original_columns):
            if col == 'obstacles_data' and row_list[i]: row_list[i] = json.dumps(row_list[i], ensure_ascii=False)
        rows.append(row_list)
    return templates.TemplateResponse(request, "admin_dashboard.html", {"original_columns": original_columns, "translated_columns": translated_columns, "rows": rows, "admin_user": admin_user, "active_page": "dashboard"})

@app.get("/api/project-pdf")
async def get_project_pdf(request: Request, background_tasks: BackgroundTasks, project: str = None, lang: str = "ar"):
    admin_user = request.cookies.get("super_admin_auth")
    auth_user = request.cookies.get("auth_user")
    lang = "en" if lang == "en" else "ar"

    if auth_user:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT project_name FROM users WHERE username=%s", (auth_user,))
        user = cursor.fetchone()
        conn.close()
        if not user: return HTMLResponse(content="<h3>غير مصرح</h3>", status_code=401)
        target_project = user[0] 
        actor = auth_user
    elif admin_user:
        target_project = project
        if not target_project: return HTMLResponse(content="<h3>يجب تحديد اسم المشروع</h3>", status_code=400)
        actor = admin_user
    else:
        return HTMLResponse(content="<h3>غير مصرح، يرجى تسجيل الدخول.</h3>", status_code=401)

    records = fetch_project_records(target_project)
    pdf_buffer = pdf_report.build_project_pdf(target_project, records, lang=lang)
    safe_name = "".join(c for c in target_project if c.isalnum() or c in (" ", "-", "_")).strip() or "project"
    filename = f"Report_{safe_name}.pdf" if lang == "en" else f"تقرير_{safe_name}.pdf"
    
    background_tasks.add_task(log_audit, actor, "تصدير PDF", f"تصدير تقرير مشروع {target_project}")
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

@app.post("/api/update-cell")
async def update_cell(request: Request, background_tasks: BackgroundTasks):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return {"success": False, "error": "غير مصرح"}
    data = await request.json()
    row_id, column, value = data.get("id"), data.get("column"), data.get("value")
    allowed_columns = set(ARABIC_COLUMNS.keys()) - {"id"}
    if column not in allowed_columns: return {"success": False, "error": "اسم عمود غير مسموح به"}
    if column == 'obstacles_data' and str(value).strip() == "": value = "[]"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE project_updates SET {column} = %s WHERE id = %s", (value, row_id))
        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, admin_user, "تعديل خلية", f"تعديل {column} للسجل {row_id}")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/admin-directory", response_class=HTMLResponse)
async def admin_directory_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id, d.username, d.manager_name, d.project_name, d.phone, d.email, d.location_link, d.profile_image,
               (SELECT COUNT(*) FROM project_updates p WHERE p.username = d.username) as updates_count
        FROM pm_directory d ORDER BY d.id ASC
    """)
    pms = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(request, "admin_directory.html", {"pms": pms, "admin_user": admin_user, "active_page": "directory"})

@app.post("/admin-update-pm")
async def admin_update_pm(request: Request, background_tasks: BackgroundTasks, pm_id: int = Form(...), phone: str = Form(""), email: str = Form(""), location_link: str = Form(""), profile_image: UploadFile = File(None)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    update_query, params = "UPDATE pm_directory SET phone=%s, email=%s, location_link=%s", [phone, email, location_link]
    if profile_image and profile_image.filename:
        image_url = upload_to_cloudinary(profile_image)
        if image_url:
            update_query += ", profile_image=%s"
            params.append(image_url)
    update_query += " WHERE id=%s"
    params.append(pm_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(update_query, tuple(params))
    conn.commit()
    conn.close()
    background_tasks.add_task(log_audit, admin_user, "تحديث دليل المديرين", f"تعديل بيانات المدير رقم {pm_id}")
    return RedirectResponse(url="/admin-directory", status_code=303)

@app.get("/admin-users", response_class=HTMLResponse)
async def admin_users_page(request: Request, notice: str = None):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, manager_name, project_name FROM users ORDER BY id ASC")
    users = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(request, "admin_users.html", {"users": users, "admin_user": admin_user, "notice": notice, "active_page": "users"})

@app.post("/admin-add-user")
async def admin_add_user(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(""), manager_name: str = Form(...), project_name: str = Form(...)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    final_password = password.strip() if password.strip() else generate_temp_password()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password, manager_name, project_name) VALUES (%s, %s, %s, %s)", (username.strip(), hash_password(final_password), manager_name.strip(), project_name.strip()))
        conn.commit()
        notice = f"تم إنشاء الحساب بنجاح. اسم المستخدم: {username.strip()} — كلمة المرور: {final_password} (يرجى تسليمها للمدير وحفظها فورًا، لن تظهر مرة أخرى)"
        background_tasks.add_task(create_notification, f"تم إنشاء حساب جديد: {username.strip()}")
        background_tasks.add_task(log_audit, admin_user, "إنشاء مستخدم", f"إنشاء حساب لـ {username.strip()}")
    except Exception as e:
        conn.rollback()
        notice = f"خطأ: تعذر إنشاء الحساب ({str(e)})"
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin-users?notice={quote(notice)}", status_code=303)

@app.post("/admin-update-user")
async def admin_update_user(request: Request, background_tasks: BackgroundTasks, user_id: int = Form(...), manager_name: str = Form(...), project_name: str = Form(...)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET manager_name=%s, project_name=%s WHERE id=%s", (manager_name.strip(), project_name.strip(), user_id))
    conn.commit()
    conn.close()
    background_tasks.add_task(log_audit, admin_user, "تحديث مستخدم", f"تحديث حساب رقم {user_id}")
    return RedirectResponse(url="/admin-users?notice=تم تحديث بيانات الحساب بنجاح", status_code=303)

@app.post("/admin-reset-password")
async def admin_reset_password(request: Request, background_tasks: BackgroundTasks, user_id: int = Form(...), new_password: str = Form("")):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    final_password = new_password.strip() if new_password.strip() else generate_temp_password()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password=%s WHERE id=%s", (hash_password(final_password), user_id))
    conn.commit()
    conn.close()
    background_tasks.add_task(log_audit, admin_user, "إعادة تعيين كلمة مرور", f"لحساب رقم {user_id}")
    notice = f"تم إعادة تعيين كلمة المرور بنجاح. كلمة المرور الجديدة: {final_password} (يرجى تسليمها للمدير فورًا)"
    return RedirectResponse(url=f"/admin-users?notice={quote(notice)}", status_code=303)

@app.post("/admin-delete-user")
async def admin_delete_user(request: Request, background_tasks: BackgroundTasks, user_id: int = Form(...)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
    conn.commit()
    conn.close()
    background_tasks.add_task(log_audit, admin_user, "حذف مستخدم", f"حذف حساب رقم {user_id}")
    return RedirectResponse(url="/admin-users?notice=تم حذف الحساب بنجاح", status_code=303)

@app.get("/api/notifications")
async def get_notifications(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "message": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 20")
        notifs = []
        for row in cursor.fetchall():
            item = dict(row)
            for k, v in item.items():
                if isinstance(v, (date, datetime)):
                    item[k] = v.isoformat()
            notifs.append(item)
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE is_read = FALSE")
        unread_count = cursor.fetchone()[0]
        conn.close()
        return JSONResponse({"success": True, "notifications": notifs, "unread_count": unread_count})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

@app.post("/api/notifications/mark-read")
async def mark_notifications_read(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "message": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE notifications SET is_read = TRUE WHERE is_read = FALSE")
        conn.commit()
        conn.close()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

# ==================== مُنشئ الداشبورد (Dashboard Builder) ====================

PERCENT_FIELDS = {"act_prog_cur", "act_prog_prev", "plan_prog_cur", "plan_prog_prev"}
CURRENCY_FIELDS = {"consultant_val", "contractor_val", "consultant_mods_val", "contractor_mods_val",
                   "cons_inv_val", "cont_inv_val"}
DATE_FIELDS = {"current_data_date", "start_contractual", "end_contractual", "start_actual", "end_expected",
               "cons_inv_date", "cont_inv_date", "consultant_mods_end_date", "contractor_mods_end_date",
               "submission_date"}
TEXT_FIELDS = {"manager_name", "project_name", "project_desc", "project_type", "project_owner",
               "project_developer", "project_contractor", "works_completed", "works_ongoing",
               "works_planned", "username", "submission_time"}
IMAGE_FIELDS = {"file_link_1", "file_link_2", "file_link_3", "file_link_4", "master_plan_link", "isometric_link"}
JSON_FIELDS = {"obstacles_data"}
DAYS_FIELDS = {"consultant_mods_time", "contractor_mods_time"}


def field_type(col):
    if col in PERCENT_FIELDS: return "percent"
    if col in DAYS_FIELDS: return "days"
    if col in CURRENCY_FIELDS: return "currency"
    if col in DATE_FIELDS: return "date"
    if col in TEXT_FIELDS: return "text"
    if col in IMAGE_FIELDS: return "image"
    if col in JSON_FIELDS: return "json"
    return "number"


def build_field_meta():
    """قائمة بكل الحقول المتاحة داخل مُنشئ الداشبورد، بالاسم العربي والإنجليزي."""
    return [{"key": k, "label": v, "label_en": ENGLISH_COLUMNS.get(k, k), "type": field_type(k)}
            for k, v in ARABIC_COLUMNS.items() if k != "id"]


def fetch_all_project_records(project_name):
    """كل تحديثات المشروع بكل الأعمدة مرتبة زمنياً (الأقدم أولاً)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates WHERE project_name = %s ORDER BY current_data_date ASC", (project_name,))
    cols = [desc[0] for desc in cursor.description]
    records = []
    for row in cursor.fetchall():
        rec = dict(zip(cols, row))
        for k, v in rec.items():
            if isinstance(v, (date, datetime)):
                rec[k] = str(v)
            elif isinstance(v, Decimal):
                rec[k] = float(v)
        records.append(rec)
    conn.close()
    return records


@app.get("/builder", response_class=HTMLResponse)
async def builder_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM project_updates WHERE project_name IS NOT NULL ORDER BY project_name")
    projects = [row[0] for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "builder.html", {
        "admin_user": admin_user, "projects": projects, "active_page": "builder",
        "field_meta": build_field_meta()
    })


ALL_PROJECTS = "__ALL__"


def fetch_every_record():
    """كل تحديثات كل المشاريع (لوضع الداشبورد المجمّع)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates ORDER BY project_name, current_data_date ASC")
    cols = [desc[0] for desc in cursor.description]
    records = []
    for row in cursor.fetchall():
        rec = dict(zip(cols, row))
        for k, v in rec.items():
            if isinstance(v, (date, datetime)):
                rec[k] = str(v)
            elif isinstance(v, Decimal):
                rec[k] = float(v)
        records.append(rec)
    conn.close()
    return records


@app.get("/api/builder/data")
async def builder_data(project: str, request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        if project == ALL_PROJECTS:
            records = fetch_every_record()
            names = sorted({r.get("project_name") for r in records if r.get("project_name")})
            return {"success": True, "mode": "all", "records": records,
                    "projects": names, "pm": None, "fields": build_field_meta()}
        records = fetch_all_project_records(project)
        pm = None
        if records:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT manager_name, phone, email, profile_image FROM pm_directory WHERE manager_name = %s LIMIT 1",
                           (records[-1].get("manager_name"),))
            row = cursor.fetchone()
            conn.close()
            if row:
                pm = {"manager_name": row[0], "phone": row[1], "email": row[2], "profile_image": row[3]}
        return {"success": True, "mode": "single", "records": records, "pm": pm,
                "fields": build_field_meta()}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/builder/layouts")
async def builder_list_layouts(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT id, name, project_name, share_token, updated_at, is_default FROM dashboard_layouts
                          ORDER BY is_default DESC, updated_at DESC""")
        rows = cursor.fetchall()
        conn.close()
        return {"success": True, "layouts": [
            {"id": r[0], "name": r[1], "project_name": r[2], "share_token": r[3],
             "updated_at": str(r[4]), "is_default": r[5]} for r in rows
        ]}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/builder/layouts/{layout_id}")
async def builder_get_layout(layout_id: int, request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT id, name, project_name, layout, share_token, is_default
                          FROM dashboard_layouts WHERE id = %s""", (layout_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return JSONResponse({"success": False, "error": "القالب غير موجود"}, status_code=404)
        layout = row[3]
        if isinstance(layout, str):
            layout = json.loads(layout)
        return {"success": True, "layout": {"id": row[0], "name": row[1], "project_name": row[2],
                                            "data": layout, "share_token": row[4], "is_default": row[5]}}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/builder/layouts")
async def builder_save_layout(request: Request, background_tasks: BackgroundTasks):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        body = await request.json()
        layout_id = body.get("id")
        name = (body.get("name") or "").strip() or "قالب بدون اسم"
        # "default" = قالب عام لكل المشاريع، "project" = مخصص لمشروع،
        # "library" = نسخة محفوظة غير مطبَّقة على أي مشروع (للتجهيز أو كنسخة احتياطية)
        scope = body.get("scope") or "project"
        if scope not in ("default", "project", "library"):
            scope = "project"
        as_new = bool(body.get("as_new"))               # «حفظ كنسخة جديدة» — لا يلمس القالب الأصلي
        if as_new:
            layout_id = None
        project_name = body.get("project_name") or None
        data = body.get("data") or {}
        payload = json.dumps(data, ensure_ascii=False)

        if scope in ("default", "library"):
            project_name = None                          # القالب العام والنسخ المحفوظة غير مرتبطة بمشروع
        elif not project_name:
            return JSONResponse({"success": False, "error": "لا بد من اختيار مشروع لحفظ قالب مخصص"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()

        # لكل مشروع قالب مخصص واحد فقط، وقالب عام واحد فقط على مستوى النظام
        if not layout_id and not as_new and scope != "library":
            if scope == "default":
                cursor.execute("SELECT id FROM dashboard_layouts WHERE is_default = TRUE LIMIT 1")
            else:
                cursor.execute("SELECT id FROM dashboard_layouts WHERE project_name = %s AND is_default = FALSE LIMIT 1",
                               (project_name,))
            found = cursor.fetchone()
            if found: layout_id = found[0]

        if layout_id:
            cursor.execute("""UPDATE dashboard_layouts
                              SET name=%s, project_name=%s, layout=%s, is_default=%s, updated_at=NOW()
                              WHERE id=%s RETURNING id, share_token""",
                           (name, project_name, payload, scope == "default", layout_id))
        else:
            cursor.execute("""INSERT INTO dashboard_layouts (name, project_name, layout, is_default, share_token, created_by)
                              VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, share_token""",
                           (name, project_name, payload, scope == "default",
                            secrets.token_urlsafe(16), admin_user))
        row = cursor.fetchone()

        # القالب اللي كان شاغل نفس المكان ما يتمسحش — يتحوّل لنسخة محفوظة في «القوالب المحفوظة»
        if scope == "default":                            # قالب عام واحد فقط
            cursor.execute("UPDATE dashboard_layouts SET is_default = FALSE WHERE id <> %s", (row[0],))
        elif scope == "project":                          # قالب مخصص واحد فقط لكل مشروع
            cursor.execute("""UPDATE dashboard_layouts SET project_name = NULL
                              WHERE project_name = %s AND is_default = FALSE AND id <> %s""", (project_name, row[0]))

        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, admin_user, "حفظ قالب داشبورد",
                                  f"{'قالب عام' if scope == 'default' else ('نسخة محفوظة' if scope == 'library' else 'قالب مشروع ' + str(project_name))}: {name}")
        return {"success": True, "id": row[0], "share_token": row[1],
                "scope": scope, "project_name": project_name}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/builder/upload")
async def builder_upload_image(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """رفع لوجو أو صورة لاستخدامها داخل قوالب الداشبورد (تُرفع على Cloudinary)."""
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        if not getattr(file, "filename", None):
            return JSONResponse({"success": False, "error": "لم يتم اختيار ملف"}, status_code=400)
        url = upload_to_cloudinary(file)
        if not url:
            return JSONResponse({"success": False, "error": "فشل رفع الصورة"}, status_code=500)
        background_tasks.add_task(log_audit, admin_user, "رفع صورة داشبورد", file.filename)
        return {"success": True, "url": url}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/builder/resolve")
async def builder_resolve_layout(request: Request, project: str = None):
    """يرجّع القالب المطبَّق على مشروع معيّن: المخصص له إن وُجد، وإلا القالب العام."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        row, source = None, "none"
        if project:
            cursor.execute("""SELECT id, name, project_name, layout, share_token FROM dashboard_layouts
                              WHERE project_name = %s AND is_default = FALSE LIMIT 1""", (project,))
            row = cursor.fetchone()
            if row: source = "project"
        if not row:
            cursor.execute("""SELECT id, name, project_name, layout, share_token FROM dashboard_layouts
                              WHERE is_default = TRUE LIMIT 1""")
            row = cursor.fetchone()
            if row: source = "default"
        conn.close()
        if not row:
            return {"success": True, "source": "none", "layout": None}
        data = row[3]
        if isinstance(data, str): data = json.loads(data)
        return {"success": True, "source": source,
                "layout": {"id": row[0], "name": row[1], "project_name": row[2],
                           "data": data, "share_token": row[4]}}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/builder/layouts/{layout_id}")
async def builder_delete_layout(layout_id: int, request: Request, background_tasks: BackgroundTasks):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM dashboard_layouts WHERE id = %s", (layout_id,))
        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, admin_user, "حذف قالب داشبورد", f"القالب رقم {layout_id}")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

# ==================== الجداول المخصصة (Sheets) ====================
# جداول يعرّفها المستخدم بنفسه: الأعمدة وأنواعها في JSONB، والصفوف في JSONB كمان،
# فإضافة عمود أو حذفه ما يحتاجش أي تعديل في قاعدة البيانات.
# أنواع الأعمدة: text | number | date | select | bool | project | lookup | formula
#   project = اختيار مشروع من مشاريع النظام
#   lookup  = قيمة جاهزة من آخر تحديث للمشروع المختار في عمود المشروع (تتحدث تلقائياً)
#   formula = معادلة حسابية على أعمدة نفس الصف

SHEET_TYPES = {"text", "number", "date", "select", "bool", "project", "lookup", "formula"}
_LATEST_CACHE = {"at": 0.0, "data": None}


def _latest_by_project():
    """آخر تحديث لكل مشروع (لأعمدة lookup) — بكاش قصير عشان ما نحمّلش القاعدة مع كل طلب."""
    now = time.time()
    if _LATEST_CACHE["data"] is not None and now - _LATEST_CACHE["at"] < 60:
        return _LATEST_CACHE["data"]
    latest = {}
    for rec in fetch_every_record():
        name = rec.get("project_name")
        if name:
            latest[name] = rec
    _LATEST_CACHE["data"] = latest
    _LATEST_CACHE["at"] = now
    return latest


# ---------- مُقيِّم المعادلات (بدون eval) ----------
_FORMULA_FUNCS = {
    "sum": lambda *a: sum(_fnum(x) for x in a),
    "min": lambda *a: min([_fnum(x) for x in a] or [0]),
    "max": lambda *a: max([_fnum(x) for x in a] or [0]),
    "abs": lambda x: abs(_fnum(x)),
    "round": lambda x, n=0: round(_fnum(x), int(_fnum(n))),
    "avg": lambda *a: (sum(_fnum(x) for x in a) / len(a)) if a else 0,
    "if": lambda c, a, b=0: a if c else b,
}


def _fnum(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


class _FormulaParser:
    """محلل بسيط: أرقام، [اسم_العمود]، + - * / ^ ( )، مقارنات، ودوال SUM/IF/ROUND/MIN/MAX/ABS/AVG."""

    def __init__(self, text, values):
        self.s = str(text or "")
        self.i = 0
        self.v = values

    def parse(self):
        val = self._cmp()
        self._ws()
        if self.i < len(self.s):
            raise ValueError("bad formula")
        return val

    def _ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def _eat(self, token):
        self._ws()
        if self.s.startswith(token, self.i):
            self.i += len(token)
            return True
        return False

    def _cmp(self):
        left = self._add()
        for op in (">=", "<=", "<>", "!=", "=", ">", "<"):
            if self._eat(op):
                right = self._add()
                a, b = _fnum(left), _fnum(right)
                return {">=": a >= b, "<=": a <= b, "<>": a != b, "!=": a != b,
                        "=": a == b, ">": a > b, "<": a < b}[op]
        return left

    def _add(self):
        val = self._mul()
        while True:
            if self._eat("+"):
                val = _fnum(val) + _fnum(self._mul())
            elif self._eat("-"):
                val = _fnum(val) - _fnum(self._mul())
            else:
                return val

    def _mul(self):
        val = self._pow()
        while True:
            if self._eat("*"):
                val = _fnum(val) * _fnum(self._pow())
            elif self._eat("/"):
                d = _fnum(self._pow())
                val = (_fnum(val) / d) if d else 0.0
            elif self._eat("%"):
                d = _fnum(self._pow())
                val = (_fnum(val) % d) if d else 0.0
            else:
                return val

    def _pow(self):
        val = self._unary()
        if self._eat("^"):
            return _fnum(val) ** _fnum(self._pow())
        return val

    def _unary(self):
        if self._eat("-"):
            return -_fnum(self._unary())
        if self._eat("+"):
            return self._unary()
        return self._atom()

    def _atom(self):
        self._ws()
        if self.i >= len(self.s):
            raise ValueError("bad formula")
        ch = self.s[self.i]
        if ch == "(":
            self.i += 1
            val = self._cmp()
            if not self._eat(")"):
                raise ValueError("bad formula")
            return val
        if ch == "[":
            end = self.s.find("]", self.i)
            if end < 0:
                raise ValueError("bad formula")
            key = self.s[self.i + 1:end].strip()
            self.i = end + 1
            return self.v.get(key, 0)
        if ch.isdigit() or ch == ".":
            j = self.i
            while j < len(self.s) and (self.s[j].isdigit() or self.s[j] == "."):
                j += 1
            num = float(self.s[self.i:j])
            self.i = j
            return num
        if ch.isalpha() or ch == "_":
            j = self.i
            while j < len(self.s) and (self.s[j].isalnum() or self.s[j] == "_"):
                j += 1
            name = self.s[self.i:j].lower()
            self.i = j
            if not self._eat("("):
                raise ValueError("bad formula")
            args = []
            if not self._eat(")"):
                while True:
                    args.append(self._cmp())
                    if self._eat(","):
                        continue
                    if self._eat(")"):
                        break
                    raise ValueError("bad formula")
            fn = _FORMULA_FUNCS.get(name)
            if not fn:
                raise ValueError("unknown function")
            return fn(*args)
        raise ValueError("bad formula")


def _eval_formula(expr, values):
    try:
        out = _FormulaParser(expr, values).parse()
        if isinstance(out, bool):
            return 1 if out else 0
        return round(float(out), 6) if isinstance(out, (int, float)) else out
    except Exception:
        return None


def _compute_row(columns, data, latest):
    """يرجّع نسخة من بيانات الصف مضافاً لها قيم أعمدة lookup والمعادلات."""
    out = dict(data or {})
    proj_key = next((c["key"] for c in columns if c.get("type") == "project"), None)
    project = out.get(proj_key) if proj_key else None
    rec = latest.get(project) if project else None
    for col in columns:
        if col.get("type") == "lookup":
            src = col.get("source")
            out[col["key"]] = (rec or {}).get(src) if src else None
    for _ in range(4):                       # معادلة ممكن تعتمد على معادلة تانية
        changed = False
        for col in columns:
            if col.get("type") != "formula":
                continue
            val = _eval_formula(col.get("formula"), out)
            if out.get(col["key"]) != val:
                out[col["key"]] = val
                changed = True
        if not changed:
            break
    return out


def _sheet_meta(row):
    cols = row[3]
    if isinstance(cols, str):
        cols = json.loads(cols)
    settings = row[4]
    if isinstance(settings, str):
        settings = json.loads(settings)
    return {"id": row[0], "name": row[1], "name_en": row[2], "columns": cols or [],
            "settings": settings or {}, "updated_at": str(row[5])}


SHEET_AGGS = ("sum", "avg", "count", "min", "max")


def _clean_fmt(f):
    """تنسيق خلية أو عمود: ألوان وخط ومحاذاة فقط — أي شيء آخر يُتجاهل."""
    if not isinstance(f, dict):
        return None
    out = {}
    for k in ("bg", "fg"):
        v = str(f.get(k) or "")
        if re.match(r"^#[0-9a-fA-F]{6}$", v):
            out[k] = v
    for k in ("bold", "italic", "under"):
        if f.get(k) is True:
            out[k] = True
    if f.get("align") in ("left", "center", "right"):
        out["align"] = f["align"]
    size = int(_fnum(f.get("size")) or 0)
    if 9 <= size <= 28:
        out["size"] = size
    return out or None


def _clean_columns(raw):
    """تنظيف تعريف الأعمدة الجاي من المتصفح."""
    out, seen = [], set()
    for i, c in enumerate(raw or []):
        if not isinstance(c, dict):
            continue
        key = re.sub(r"[^a-zA-Z0-9_]", "", str(c.get("key") or "")) or f"c{i + 1}"
        while key in seen:
            key += "_"
        seen.add(key)
        typ = c.get("type") if c.get("type") in SHEET_TYPES else "text"
        col = {"key": key, "type": typ,
               "label": str(c.get("label") or key)[:80],
               "label_en": str(c.get("label_en") or "")[:80],
               "width": max(70, min(900, int(_fnum(c.get("width")) or 150)))}
        if typ == "select":
            col["options"] = [str(o)[:60] for o in (c.get("options") or [])][:60]
        if typ == "number":
            col["decimals"] = max(0, min(4, int(_fnum(c.get("decimals")))))
            col["unit"] = str(c.get("unit") or "")[:12]
        if typ == "formula":
            col["formula"] = str(c.get("formula") or "")[:400]
            col["decimals"] = max(0, min(4, int(_fnum(c.get("decimals")))))
            col["unit"] = str(c.get("unit") or "")[:12]
        if typ == "lookup":
            col["source"] = str(c.get("source") or "")[:60]
        fmt = _clean_fmt(c.get("fmt"))
        if fmt:
            col["fmt"] = fmt
        if c.get("agg") in SHEET_AGGS:
            col["agg"] = c["agg"]
        out.append(col)
    return out


def _sheet_admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


@app.get("/admin-sheets", response_class=HTMLResponse)
async def sheets_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM project_updates WHERE project_name IS NOT NULL ORDER BY project_name")
    projects = [r[0] for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "sheets.html", {
        "admin_user": admin_user, "projects": projects, "active_page": "sheets",
        "field_meta": build_field_meta()
    })


@app.get("/api/sheets")
async def sheets_list(request: Request):
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT s.id, s.name, s.name_en, s.columns, s.settings, s.updated_at,
                                 (SELECT COUNT(*) FROM sheet_rows r WHERE r.sheet_id = s.id)
                          FROM sheets s ORDER BY s.updated_at DESC""")
        rows = cursor.fetchall()
        conn.close()
        out = []
        for r in rows:
            meta = _sheet_meta(r)
            meta["rows"] = r[6]
            out.append(meta)
        return {"success": True, "sheets": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/sheets")
async def sheets_create(request: Request, background_tasks: BackgroundTasks):
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        body = await request.json()
        name = (body.get("name") or "").strip() or "جدول جديد"
        columns = _clean_columns(body.get("columns") or [
            {"key": "project", "type": "project", "label": "المشروع", "label_en": "Project", "width": 220},
            {"key": "item", "type": "text", "label": "البند", "label_en": "Item", "width": 220},
            {"key": "value", "type": "number", "label": "القيمة", "label_en": "Value", "width": 130},
        ])
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO sheets (name, name_en, columns, settings, created_by)
                          VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                       (name, (body.get("name_en") or "").strip() or None,
                        json.dumps(columns, ensure_ascii=False),
                        json.dumps(body.get("settings") or {}, ensure_ascii=False),
                        request.cookies.get("super_admin_auth")))
        sid = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, request.cookies.get("super_admin_auth"), "إنشاء جدول مخصص", name)
        return {"success": True, "id": sid}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/sheets/{sheet_id}")
async def sheets_update(sheet_id: int, request: Request):
    """تعديل اسم الجدول أو تعريف أعمدته."""
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        body = await request.json()
        sets, vals = [], []
        if "name" in body:
            sets.append("name = %s"); vals.append((body.get("name") or "").strip() or "جدول بدون اسم")
        if "name_en" in body:
            sets.append("name_en = %s"); vals.append((body.get("name_en") or "").strip() or None)
        if "columns" in body:
            sets.append("columns = %s"); vals.append(json.dumps(_clean_columns(body["columns"]), ensure_ascii=False))
        if "settings" in body:
            sets.append("settings = %s"); vals.append(json.dumps(body.get("settings") or {}, ensure_ascii=False))
        if not sets:
            return {"success": True}
        sets.append("updated_at = NOW()")
        vals.append(sheet_id)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE sheets SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/sheets/{sheet_id}")
async def sheets_delete(sheet_id: int, request: Request, background_tasks: BackgroundTasks):
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sheets WHERE id = %s", (sheet_id,))
        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, request.cookies.get("super_admin_auth"), "حذف جدول مخصص", str(sheet_id))
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _load_sheet(cursor, sheet_id):
    cursor.execute("SELECT id, name, name_en, columns, settings, updated_at FROM sheets WHERE id = %s", (sheet_id,))
    row = cursor.fetchone()
    return _sheet_meta(row) if row else None


@app.get("/api/sheets/_latest")
async def sheets_latest(request: Request):
    """آخر تحديث لكل مشروع — يستعمله المتصفح لتعبئة أعمدة «من بيانات المشروع» فور اختيار المشروع."""
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        keys = [f["key"] for f in build_field_meta()]
        out = {}
        for name, rec in _latest_by_project().items():
            out[name] = {k: rec.get(k) for k in keys if rec.get(k) is not None}
        return {"success": True, "latest": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _sheet_view_rows(sheet, raw, q="", sort="", direction="asc", flt=None):
    """يحوّل صفوف قاعدة البيانات لصفوف معروضة: حساب lookup والمعادلات ثم بحث وفلترة وفرز."""
    latest = _latest_by_project()
    cols = sheet["columns"]
    rows = []
    for r in raw:
        data = r[2] if isinstance(r[2], dict) else json.loads(r[2] or "{}")
        rows.append({"id": r[0], "seq": r[1], "data": _compute_row(cols, data, latest),
                     "updated_at": str(r[3])[:16]})
    needle = (q or "").strip().lower()
    if needle:
        rows = [x for x in rows
                if any(needle in str(v).lower()
                       for k, v in x["data"].items() if v is not None and not k.startswith("__"))]
    for key, want in (flt or {}).items():
        if want in ("", None):
            continue
        rows = [x for x in rows if str(x["data"].get(key, "")).lower().find(str(want).lower()) >= 0]
    if sort:
        col = next((c for c in cols if c["key"] == sort), None)
        numeric = col and col.get("type") in ("number", "formula", "lookup", "bool")

        def keyf(x):
            v = x["data"].get(sort)
            if numeric:
                return (v is None, _fnum(v))
            return (v is None, str(v if v is not None else "").lower())
        rows.sort(key=keyf, reverse=(direction == "desc"))
    return rows


def _load_sheet_rows(sheet_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    sheet = _load_sheet(cursor, sheet_id)
    if not sheet:
        conn.close()
        return None, None
    cursor.execute("SELECT id, seq, data, updated_at FROM sheet_rows WHERE sheet_id = %s ORDER BY seq, id", (sheet_id,))
    raw = cursor.fetchall()
    conn.close()
    return sheet, raw


@app.get("/api/sheets/{sheet_id}/rows")
async def sheets_rows(sheet_id: int, request: Request, q: str = "", sort: str = "", dir: str = "asc",
                      limit: int = 200, offset: int = 0, filters: str = ""):
    """صفوف الجدول بعد حساب أعمدة lookup والمعادلات، مع بحث وفرز وفلترة وتحميل بالصفحات."""
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        sheet, raw = _load_sheet_rows(sheet_id)
        if not sheet:
            return JSONResponse({"success": False, "error": "الجدول غير موجود"}, status_code=404)
        try:
            flt = json.loads(filters) if filters else {}
        except Exception:
            flt = {}
        rows = _sheet_view_rows(sheet, raw, q, sort, dir, flt)
        total = len(rows)
        limit = max(1, min(1000, int(limit or 200)))
        offset = max(0, int(offset or 0))
        return {"success": True, "sheet": sheet, "total": total,
                "rows": rows[offset:offset + limit]}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _xl_color(v):
    """#RRGGBB -> FFRRGGBB (صيغة openpyxl)."""
    v = str(v or "")
    return "FF" + v[1:].upper() if re.match(r"^#[0-9a-fA-F]{6}$", v) else None


def _sheet_export_name(sheet, lang):
    name = (sheet.get("name_en") or sheet.get("name")) if lang == "en" else (sheet.get("name") or sheet.get("name_en"))
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", str(name or "sheet")).strip() or "sheet"


def _dispo(filename, ext):
    """اسم ملف يشتغل مع العربي على كل المتصفحات."""
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "", filename).strip() or "sheet"
    return (f'attachment; filename="{ascii_name}.{ext}"; '
            f"filename*=UTF-8''{quote(filename + '.' + ext)}")


@app.get("/api/sheets/{sheet_id}/export")
async def sheets_export(sheet_id: int, request: Request, fmt: str = "xlsx", q: str = "", sort: str = "",
                        dir: str = "asc", filters: str = "", lang: str = "ar", scope: str = "view"):
    """تصدير الجدول كملف إكسيل أو CSV — بنفس ما هو ظاهر (بحث/فلترة/فرز) أو الجدول كله."""
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        sheet, raw = _load_sheet_rows(sheet_id)
        if not sheet:
            return JSONResponse({"success": False, "error": "الجدول غير موجود"}, status_code=404)
        if scope == "all":
            q, sort, filters = "", "", ""
        try:
            flt = json.loads(filters) if filters else {}
        except Exception:
            flt = {}
        rows = _sheet_view_rows(sheet, raw, q, sort, dir, flt)
        cols = sheet["columns"]
        en = (lang == "en")
        label = lambda c: (c.get("label_en") or c.get("label") or c["key"]) if en else (c.get("label") or c.get("label_en") or c["key"])
        fname = _sheet_export_name(sheet, lang)

        def shown(c, v):
            """القيمة زي ما بتتعرض في الشاشة (للـ CSV والطباعة)."""
            if v is None or v == "":
                return ""
            if c.get("type") == "bool":
                return ("Yes" if en else "نعم") if v else ("No" if en else "لا")
            return v

        if fmt == "csv":
            import csv as _csv
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerow([label(c) for c in cols])
            for r in rows:
                w.writerow([shown(c, r["data"].get(c["key"])) for c in cols])
            data = "\ufeff" + buf.getvalue()                 # BOM عشان إكسيل يقرأ العربي
            return Response(content=data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": _dispo(fname, "csv")})

        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        NAVY = "FF1F3A5F"
        wb = Workbook()
        ws = wb.active
        ws.title = (fname[:28] or "Sheet")
        ws.sheet_view.rightToLeft = not en

        thin = Side(style="thin", color="FFD9DEE8")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        ws.cell(row=1, column=1, value=fname).font = Font(name="Arial", size=14, bold=True, color=NAVY)
        sub = (datetime.now().strftime("%Y-%m-%d %H:%M"))
        if q or flt:
            sub += ("  ·  filtered view" if en else "  ·  حسب الفلترة الظاهرة")
        ws.cell(row=2, column=1, value=sub).font = Font(name="Arial", size=9, color="FF5B6577")
        if len(cols) > 1:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))

        HEAD_ROW = 4
        for i, c in enumerate(cols, start=1):
            cell = ws.cell(row=HEAD_ROW, column=i, value=label(c))
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFFFF")
            cell.fill = PatternFill(start_color=NAVY, end_color=NAVY, fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = max(9, min(58, int(_fnum(c.get("width")) or 150) / 7.2))
        ws.row_dimensions[HEAD_ROW].height = 24

        for ri, r in enumerate(rows):
            row_no = HEAD_ROW + 1 + ri
            cell_fmt = (r["data"].get("__fmt") or {}) if isinstance(r["data"].get("__fmt"), dict) else {}
            for ci, c in enumerate(cols, start=1):
                key, typ = c["key"], c.get("type")
                v = r["data"].get(key)
                cell = ws.cell(row=row_no, column=ci)
                if typ == "bool":
                    cell.value = bool(v)
                elif typ == "date" and v:
                    try:
                        cell.value = datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
                        cell.number_format = "yyyy-mm-dd"
                    except Exception:
                        cell.value = v
                elif typ in ("number", "formula") or (typ == "lookup" and isinstance(v, (int, float))):
                    cell.value = None if v in (None, "") else _fnum(v)
                    dec = int(_fnum(c.get("decimals")) or 0)
                    unit = str(c.get("unit") or "").replace('"', "")
                    cell.number_format = ("#,##0" + ("." + "0" * dec if dec else "")) + (f'" {unit}"' if unit else "")
                else:
                    cell.value = v
                f = dict(c.get("fmt") or {})
                f.update(cell_fmt.get(key) or {})
                cell.font = Font(name="Arial", size=int(_fnum(f.get("size")) or 10),
                                 bold=bool(f.get("bold")), italic=bool(f.get("italic")),
                                 underline="single" if f.get("under") else None,
                                 color=_xl_color(f.get("fg")) or "FF172033")
                bg = _xl_color(f.get("bg"))
                if bg:
                    cell.fill = PatternFill(start_color=bg, end_color=bg, fill_type="solid")
                align = f.get("align")
                cell.alignment = Alignment(horizontal=align if align in ("left", "center", "right") else None,
                                           vertical="center")
                cell.border = border

        last = HEAD_ROW + len(rows)
        if any(c.get("agg") for c in cols) and rows:
            trow = last + 1
            fn = {"sum": "SUM", "avg": "AVERAGE", "count": "COUNTA", "min": "MIN", "max": "MAX"}
            for ci, c in enumerate(cols, start=1):
                cell = ws.cell(row=trow, column=ci)
                if c.get("agg") in fn:
                    L = get_column_letter(ci)
                    cell.value = f"={fn[c['agg']]}({L}{HEAD_ROW + 1}:{L}{last})"   # معادلة حية
                    dec = int(_fnum(c.get("decimals")) or 0)
                    cell.number_format = "#,##0" + ("." + "0" * dec if dec else "")
                elif ci == 1:
                    cell.value = "Total" if en else "الإجمالي"
                cell.font = Font(name="Arial", size=10, bold=True, color=NAVY)
                cell.fill = PatternFill(start_color="FFEDF1F7", end_color="FFEDF1F7", fill_type="solid")
                cell.border = border

        if rows:
            ws.auto_filter.ref = f"A{HEAD_ROW}:{get_column_letter(len(cols))}{last}"
        ws.freeze_panes = f"A{HEAD_ROW + 1}"

        out = io.BytesIO()
        wb.save(out)
        return Response(content=out.getvalue(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": _dispo(fname, "xlsx")})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/sheets/{sheet_id}/rows")
async def sheets_rows_save(sheet_id: int, request: Request):
    """حفظ التعديلات دفعة واحدة: إضافة/تعديل/حذف صفوف."""
    if not _sheet_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        body = await request.json()
        who = request.cookies.get("super_admin_auth")
        conn = get_db_connection()
        cursor = conn.cursor()
        sheet = _load_sheet(cursor, sheet_id)
        if not sheet:
            conn.close()
            return JSONResponse({"success": False, "error": "الجدول غير موجود"}, status_code=404)
        keys = {c["key"] for c in sheet["columns"] if c.get("type") not in ("formula", "lookup")}
        all_keys = {c["key"] for c in sheet["columns"]}

        def clean(d):
            d = d or {}
            out = {k: v for k, v in d.items() if k in keys}
            fmt = d.get("__fmt")                      # تنسيق الخلايا (ألوان وخطوط)
            if isinstance(fmt, dict):
                kept = {}
                for k, v in list(fmt.items())[:300]:
                    if k in all_keys:
                        cf = _clean_fmt(v)
                        if cf:
                            kept[k] = cf
                if kept:
                    out["__fmt"] = kept
            return out

        new_ids = []
        for item in (body.get("add") or [])[:2000]:
            cursor.execute("""INSERT INTO sheet_rows (sheet_id, seq, data, updated_by)
                              VALUES (%s, COALESCE((SELECT MAX(seq) + 1 FROM sheet_rows WHERE sheet_id = %s), 1), %s, %s)
                              RETURNING id""",
                           (sheet_id, sheet_id, json.dumps(clean(item.get("data")), ensure_ascii=False), who))
            new_ids.append(cursor.fetchone()[0])
        for item in (body.get("update") or [])[:2000]:
            cursor.execute("""UPDATE sheet_rows SET data = %s, updated_by = %s, updated_at = NOW()
                              WHERE id = %s AND sheet_id = %s""",
                           (json.dumps(clean(item.get("data")), ensure_ascii=False), who, item.get("id"), sheet_id))
        dels = [int(x) for x in (body.get("delete") or [])[:5000] if str(x).isdigit()]
        if dels:
            cursor.execute("DELETE FROM sheet_rows WHERE sheet_id = %s AND id = ANY(%s)", (sheet_id, dels))
        for item in (body.get("reorder") or [])[:5000]:
            cursor.execute("UPDATE sheet_rows SET seq = %s WHERE id = %s AND sheet_id = %s",
                           (int(_fnum(item.get("seq"))), item.get("id"), sheet_id))
        cursor.execute("UPDATE sheets SET updated_at = NOW() WHERE id = %s", (sheet_id,))
        conn.commit()
        conn.close()
        return {"success": True, "ids": new_ids}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ==================== طبقة الحقائق (Facts Layer) ====================
# المبدأ: أي مديول (تحديثات المديرين، التدفق النقدي، الجداول المخصصة، وبعدين البرنامج
# الزمني والسجلات) بيكتب أرقامه هنا كـ «حقائق». والداشبورد والتقارير بيقروا من هنا بس،
# فإضافة مديول جديد ما بتحتاجش أي تعديل في الداشبورد.
#
#   project_facts : المشروع + المؤشر + الفترة + القيمة + المصدر
#   fact_metrics  : تعريف كل مؤشر، وأهمه ترتيب أولوية المصادر (مصدر واحد معتمد لكل رقم)

SEED_METRICS = [
    # key, عربي, إنجليزي, وحدة, نوع, تجميع, اتجاه, مصادر بالأولوية, ترتيب
    ("actual_pct",      "الإنجاز الفعلي",        "Actual progress",   "%",   "percent",  "last", "up_good",   ["cashflow", "updates"], 10),
    ("planned_pct",     "الإنجاز المخطط",        "Planned progress",  "%",   "percent",  "last", "neutral",   ["cashflow", "updates"], 20),
    ("progress_dev",    "الانحراف عن المخطط",    "Progress deviation", "%",  "percent",  "last", "up_good",   ["cashflow", "updates"], 30),
    ("spi",             "مؤشر أداء الجدول SPI",  "Schedule index",    "",    "number",   "last", "up_good",   ["cashflow"],            40),
    ("contract_value",  "قيمة العقد",            "Contract value",    "SAR", "currency", "last", "neutral",   ["cashflow", "updates"], 50),
    ("revised_value",   "القيمة بعد التعديلات",  "Revised value",     "SAR", "currency", "last", "neutral",   ["cashflow", "updates"], 60),
    ("planned_cost",    "المخطط صرفه (شهري)",    "Planned cost",      "SAR", "currency", "sum",  "neutral",   ["cashflow"],            70),
    ("actual_cost",     "المنصرف الفعلي (شهري)", "Actual cost",       "SAR", "currency", "sum",  "neutral",   ["cashflow"],            80),
    ("invoiced_value",  "قيمة الفواتير",         "Invoiced value",    "SAR", "currency", "last", "neutral",   ["updates"],             90),
    ("delay_days",      "التأخير عن نهاية العقد", "Delay",            "day", "number",   "last", "down_good", ["updates"],            100),
    ("end_expected",    "النهاية المتوقعة",      "Expected finish",   "",    "date",     "last", "neutral",   ["updates"],            110),
    ("end_contractual", "نهاية العقد",           "Contract finish",   "",    "date",     "last", "neutral",   ["updates"],            120),
    ("drawings_pending", "مخططات قيد المراجعة",  "Drawings in review", "",   "count",    "last", "down_good", ["updates"],            130),
    ("ir_pending",      "طلبات IR قيد المراجعة", "IRs in review",     "",    "count",    "last", "down_good", ["updates"],            140),
    ("ncr_open",        "مخالفات NCR مفتوحة",    "Open NCRs",         "",    "count",    "last", "down_good", ["updates"],            150),
]

FACT_SOURCES = {
    "updates":  {"label": "تحديثات مديري المشاريع", "label_en": "Manager updates"},
    "cashflow": {"label": "التدفق النقدي",          "label_en": "Cash flow"},
    "sheet":    {"label": "جدول مخصص",              "label_en": "Custom sheet"},
    "wbs":      {"label": "هيكل الأعمال",           "label_en": "WBS"},
    "manual":   {"label": "إدخال يدوي",             "label_en": "Manual entry"},
}


def _seed_metrics(cursor):
    """يضيف المؤشرات القياسية مرة واحدة — وما بيلمسش أي تعديل عملته إنت عليها."""
    for m in SEED_METRICS:
        cursor.execute("""INSERT INTO fact_metrics (key, label, label_en, unit, kind, agg, direction, sources, seq)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING""",
                       (m[0], m[1], m[2], m[3], m[4], m[5], m[6], json.dumps(m[7]), m[8]))


def _metrics_map(cursor=None):
    own = cursor is None
    if own:
        conn = get_db_connection()
        cursor = conn.cursor()
    cursor.execute("""SELECT key, label, label_en, unit, kind, agg, direction, sources, note, active, seq
                      FROM fact_metrics ORDER BY seq, key""")
    out = {}
    for r in cursor.fetchall():
        src = r[7] if isinstance(r[7], list) else json.loads(r[7] or "[]")
        out[r[0]] = {"key": r[0], "label": r[1], "label_en": r[2], "unit": r[3] or "", "kind": r[4] or "number",
                     "agg": r[5] or "last", "direction": r[6] or "neutral", "sources": src,
                     "note": r[8] or "", "active": bool(r[9]), "seq": r[10]}
    if own:
        conn.close()
    return out


def _fact_period(v, default=None):
    """يحوّل أي تاريخ (نص أو date أو YYYY-MM) لتاريخ حقيقي."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = str(v or "").strip()[:10]
    if len(t) == 7:
        t += "-01"
    try:
        return datetime.strptime(t, "%Y-%m-%d").date()
    except Exception:
        return default


def facts_write(rows, replace_source=None, project=None):
    """يكتب الحقائق (upsert). replace_source = يمسح حقائق المصدر ده الأول (إعادة بناء نظيفة)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if replace_source:
            if project:
                cursor.execute("DELETE FROM project_facts WHERE source = %s AND project_name = %s",
                               (replace_source, project))
            else:
                cursor.execute("DELETE FROM project_facts WHERE source = %s", (replace_source,))
        n = 0
        for f in rows:
            per = _fact_period(f.get("period"))
            proj = (f.get("project") or f.get("project_name") or "").strip()
            metric = (f.get("metric") or "").strip()
            if not (per and proj and metric):
                continue
            val = f.get("value")
            val = None if val in ("", None) else _fnum(val)
            cursor.execute("""INSERT INTO project_facts
                                (project_name, metric, period, value, text_value, source, source_ref, is_baseline)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                              ON CONFLICT (project_name, metric, period, source, COALESCE(source_ref, ''), is_baseline)
                              DO UPDATE SET value = EXCLUDED.value, text_value = EXCLUDED.text_value,
                                            updated_at = NOW()""",
                           (proj, metric, per, val,
                            (str(f.get("text")) if f.get("text") not in (None, "") else None),
                            f.get("source") or "manual", f.get("ref"), bool(f.get("baseline"))))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


# ---------- جامعو الحقائق من المديولات الموجودة ----------

def collect_updates(project=None):
    """حقائق من تحديثات مديري المشاريع — نقطة لكل تاريخ بيانات، فبتطلع سلسلة زمنية."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if project:
        cursor.execute("SELECT * FROM project_updates WHERE project_name = %s ORDER BY current_data_date", (project,))
    else:
        cursor.execute("SELECT * FROM project_updates ORDER BY project_name, current_data_date")
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()

    out = []
    for rec in rows:
        proj = rec.get("project_name")
        per = _fact_period(rec.get("current_data_date"))
        if not proj or not per:
            continue
        ref = str(rec.get("id") or "")

        def add(metric, value=None, text=None):
            if value in ("", None) and text in ("", None):
                return
            out.append({"project": proj, "metric": metric, "period": per, "value": value,
                        "text": text, "source": "updates", "ref": ref})

        act = _fnum(rec.get("act_prog_cur"))
        plan = _fnum(rec.get("plan_prog_cur"))
        add("actual_pct", act if rec.get("act_prog_cur") not in (None, "") else None)
        add("planned_pct", plan if rec.get("plan_prog_cur") not in (None, "") else None)
        if rec.get("act_prog_cur") not in (None, "") and rec.get("plan_prog_cur") not in (None, ""):
            add("progress_dev", round(act - plan, 2))
        cv = _fnum(rec.get("contractor_val"))
        add("contract_value", cv or None)
        mods = _fnum(rec.get("contractor_mods_val"))
        if cv:
            add("revised_value", round(cv + mods, 2))
        add("invoiced_value", _fnum(rec.get("cont_inv_val")) or None)
        add("drawings_pending", _fnum(rec.get("drawings_rev")) or None)
        add("ir_pending", _fnum(rec.get("ir_rev")) or None)
        add("ncr_open", _fnum(rec.get("ncr_open")) or None)
        end_c = _fact_period(rec.get("end_contractual"))
        end_e = _fact_period(rec.get("end_expected"))
        if end_c:
            add("end_contractual", None, str(end_c))
        if end_e:
            add("end_expected", None, str(end_e))
        if end_c and end_e:
            add("delay_days", (end_e - end_c).days)
    return out


def collect_cashflow(project=None):
    """حقائق من التدفق النقدي: النسب المخططة والفعلية والمبالغ الشهرية و SPI."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if project:
        cursor.execute("SELECT DISTINCT project_name FROM cashflow_meta WHERE project_name = %s", (project,))
    else:
        cursor.execute("SELECT DISTINCT project_name FROM cashflow_meta")
    projects = [r[0] for r in cursor.fetchall()]
    conn.close()

    out = []
    for proj in projects:
        try:
            p = _cashflow_payload(proj)
        except Exception:
            continue
        meta, rows = p["meta"], p["rows"]
        base = float(meta.get("plan_base") or meta.get("contract_value") or 0)
        rev = float(meta.get("revised_value") or meta.get("contract_value") or 0)
        cum_plan, prev_act_amount = 0.0, 0.0
        for r in rows:
            per = _fact_period(r.get("ym"))
            if not per:
                continue

            def add(metric, value=None, text=None, when=per):
                if value in ("", None) and text in ("", None):
                    return
                out.append({"project": proj, "metric": metric, "period": when, "value": value,
                            "text": text, "source": "cashflow", "ref": r.get("ym")})

            pa = r.get("plan_amount")
            if pa not in (None, ""):
                cum_plan += float(pa)
                add("planned_cost", float(pa))
            plan_pct = (cum_plan / base * 100) if base else None
            if plan_pct is not None and pa not in (None, ""):
                add("planned_pct", round(plan_pct, 2))
            ap = r.get("act_pct")
            if ap not in (None, ""):
                when = _fact_period(r.get("act_date"), per)
                out.append({"project": proj, "metric": "actual_pct", "period": when,
                            "value": round(float(ap), 2), "source": "cashflow", "ref": r.get("ym")})
                act_amount = float(ap) / 100 * rev
                out.append({"project": proj, "metric": "actual_cost", "period": per,
                            "value": round(act_amount - prev_act_amount, 2), "source": "cashflow", "ref": r.get("ym")})
                prev_act_amount = act_amount
                if plan_pct:
                    out.append({"project": proj, "metric": "progress_dev", "period": when,
                                "value": round(float(ap) - plan_pct, 2), "source": "cashflow", "ref": r.get("ym")})
                    out.append({"project": proj, "metric": "spi", "period": when,
                                "value": round(float(ap) / plan_pct, 3), "source": "cashflow", "ref": r.get("ym")})
        if base:
            first = _fact_period((rows[0] or {}).get("ym")) if rows else None
            if first:
                out.append({"project": proj, "metric": "contract_value", "period": first,
                            "value": float(meta.get("contract_value") or 0), "source": "cashflow", "ref": "meta"})
                out.append({"project": proj, "metric": "revised_value", "period": first,
                            "value": rev, "source": "cashflow", "ref": "meta"})
    return out


COLLECTORS = {"updates": collect_updates, "cashflow": collect_cashflow}


def facts_rebuild(source="all", project=None):
    """يعيد بناء الحقائق من المديولات — آمن تكراره، بيمسح حقائق المصدر ويكتبها من الأول."""
    done = {}
    names = list(COLLECTORS.keys()) if source in ("all", "", None) else [source]
    for name in names:
        fn = COLLECTORS.get(name)
        if not fn:
            continue
        rows = fn(project)
        done[name] = facts_write(rows, replace_source=name, project=project)
    return done


# ---------- القراءة: أولوية المصادر + التجميع ----------

def facts_read(metric, projects=None, date_from=None, date_to=None, meta=None):
    """يرجّع حقائق مؤشر واحد بعد تطبيق أولوية المصادر: لكل (مشروع، فترة) القيمة من
       أول مصدر متاح حسب ترتيب المؤشر — وده تطبيق قاعدة «مصدر واحد معتمد لكل رقم»."""
    meta = meta or _metrics_map().get(metric)
    if not meta:
        return []
    order = meta.get("sources") or []
    conn = get_db_connection()
    cursor = conn.cursor()
    sql = ["SELECT project_name, period, value, text_value, source FROM project_facts",
           "WHERE metric = %s AND is_baseline = FALSE"]
    args = [metric]
    if projects:
        sql.append("AND project_name = ANY(%s)")
        args.append(list(projects))
    if date_from:
        sql.append("AND period >= %s")
        args.append(_fact_period(date_from))
    if date_to:
        sql.append("AND period <= %s")
        args.append(_fact_period(date_to))
    sql.append("ORDER BY project_name, period")
    cursor.execute(" ".join(sql), args)
    raw = cursor.fetchall()
    conn.close()

    # المصدر بيتقرر لكل مشروع كله، مش لكل نقطة — عشان السلسلة ما تبقاش خليط مصدرين
    rank = lambda src: order.index(src) if src in order else len(order) + 1
    chosen = {}
    for proj, per, val, txt, src in raw:
        r = rank(src)
        if proj not in chosen or r < chosen[proj]:
            chosen[proj] = r
    out = []
    for proj, per, val, txt, src in raw:
        if rank(src) != chosen.get(proj):
            continue
        out.append({"project": proj, "period": str(per), "value": val, "text": txt, "source": src})
    out.sort(key=lambda x: (x["project"], x["period"]))
    return out


def facts_latest(metrics, projects=None, as_of=None):
    """آخر قيمة لكل مؤشر لكل مشروع حتى تاريخ معيّن (الافتراضي: النهارده — عشان الخطة
       اللي لسه جاية في المستقبل ما تتحسبش كإنها الوضع الحالي)."""
    as_of = as_of or _today_ksa()
    mm = _metrics_map()
    out = {}
    for key in metrics:
        meta = mm.get(key)
        if not meta:
            continue
        rows = facts_read(key, projects, None, as_of, meta)
        for r in rows:
            cur = out.setdefault(r["project"], {})
            old = cur.get(key)
            if meta.get("agg") == "sum":
                cur[key] = {"value": (old or {}).get("value", 0) + (r["value"] or 0),
                            "period": r["period"], "source": r["source"]}
            elif not old or r["period"] >= old["period"]:
                cur[key] = {"value": r["value"], "text": r["text"],
                            "period": r["period"], "source": r["source"]}
    return out


def _fact_admin(request):
    return request.cookies.get("super_admin_auth") == "admin_mohamed"


@app.get("/admin-facts", response_class=HTMLResponse)
async def facts_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    return templates.TemplateResponse(request, "facts.html", {"admin_user": admin_user, "active_page": "facts"})


@app.get("/api/facts/metrics")
async def api_fact_metrics(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    conn = get_db_connection()
    cursor = conn.cursor()
    _seed_metrics(cursor)
    conn.commit()
    mm = _metrics_map(cursor)
    conn.close()
    return {"success": True, "metrics": list(mm.values()), "sources": FACT_SOURCES}


@app.post("/api/facts/metrics")
async def api_fact_metrics_save(request: Request):
    """إضافة أو تعديل تعريف مؤشر (الاسم، الوحدة، طريقة التجميع، ترتيب أولوية المصادر)."""
    if not _fact_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        key = re.sub(r"[^a-z0-9_]", "", str(b.get("key") or "").lower())
        if not key:
            return JSONResponse({"success": False, "error": "المفتاح مطلوب"}, status_code=400)
        srcs = [s for s in (b.get("sources") or []) if s in FACT_SOURCES or str(s).startswith("sheet:")]
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO fact_metrics (key, label, label_en, unit, kind, agg, direction, sources, note, active, seq)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, label_en = EXCLUDED.label_en,
                            unit = EXCLUDED.unit, kind = EXCLUDED.kind, agg = EXCLUDED.agg,
                            direction = EXCLUDED.direction, sources = EXCLUDED.sources,
                            note = EXCLUDED.note, active = EXCLUDED.active, seq = EXCLUDED.seq""",
                       (key, str(b.get("label") or key)[:80], str(b.get("label_en") or "")[:80],
                        str(b.get("unit") or "")[:12],
                        b.get("kind") if b.get("kind") in ("number", "percent", "currency", "count", "date", "text") else "number",
                        b.get("agg") if b.get("agg") in ("last", "sum", "avg", "min", "max") else "last",
                        b.get("direction") if b.get("direction") in ("up_good", "down_good", "neutral") else "neutral",
                        json.dumps(srcs), str(b.get("note") or "")[:300],
                        bool(b.get("active", True)), int(_fnum(b.get("seq")) or 100)))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/facts")
async def api_facts_write(request: Request):
    """كتابة حقائق من أي مديول (أو يدوي)."""
    if not _fact_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        rows = b.get("facts") or []
        if not isinstance(rows, list):
            return JSONResponse({"success": False, "error": "facts لازم تكون قائمة"}, status_code=400)
        n = facts_write(rows[:5000], replace_source=b.get("replace_source"), project=b.get("project"))
        return {"success": True, "written": n}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/facts/series")
async def api_facts_series(request: Request, metric: str, projects: str = "", date_from: str = "", date_to: str = ""):
    """سلسلة زمنية لمؤشر واحد لمشروع أو أكتر — ده اللي الداشبورد هيقرا منه."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        mm = _metrics_map()
        meta = mm.get(metric)
        if not meta:
            return JSONResponse({"success": False, "error": "مؤشر غير معروف"}, status_code=404)
        plist = [p for p in projects.split("|") if p] or None
        rows = facts_read(metric, plist, date_from or None, date_to or None, meta)
        series = {}
        for r in rows:
            series.setdefault(r["project"], []).append(
                {"period": r["period"], "value": r["value"], "text": r["text"], "source": r["source"]})
        return {"success": True, "metric": meta,
                "series": [{"project": k, "points": v} for k, v in sorted(series.items())]}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/facts/latest")
async def api_facts_latest(request: Request, metrics: str = "", projects: str = "", as_of: str = ""):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        keys = [k for k in metrics.split(",") if k] or list(_metrics_map().keys())
        plist = [p for p in projects.split("|") if p] or None
        return {"success": True, "latest": facts_latest(keys, plist, as_of or None)}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/facts/rebuild")
async def api_facts_rebuild(request: Request, background_tasks: BackgroundTasks):
    """إعادة بناء الحقائق من المديولات الحالية."""
    if not _fact_admin(request):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        conn = get_db_connection()
        cursor = conn.cursor()
        _seed_metrics(cursor)
        conn.commit()
        conn.close()
        done = facts_rebuild(b.get("source") or "all", (b.get("project") or "").strip() or None)
        background_tasks.add_task(log_audit, request.cookies.get("super_admin_auth"),
                                  "إعادة بناء طبقة الحقائق", json.dumps(done, ensure_ascii=False))
        return {"success": True, "written": done}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/facts/health")
async def api_facts_health(request: Request):
    """نظرة سريعة: كام حقيقة لكل مصدر ومؤشر، وآخر تحديث — عشان تعرف الطبقة صحية ولا لأ."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT source, metric, COUNT(*), COUNT(DISTINCT project_name),
                             MIN(period), MAX(period), MAX(updated_at)
                      FROM project_facts GROUP BY source, metric ORDER BY source, metric""")
    stats = [{"source": r[0], "metric": r[1], "facts": r[2], "projects": r[3],
              "from": str(r[4]), "to": str(r[5]), "updated_at": str(r[6])[:16]} for r in cursor.fetchall()]
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT project_name), COUNT(DISTINCT metric) FROM project_facts")
    t = cursor.fetchone()
    conn.close()
    return {"success": True, "total": {"facts": t[0], "projects": t[1], "metrics": t[2]}, "stats": stats}


# ==================== التدفق النقدي ومنحنى الإنجاز (Cash Flow & S-Curve) ====================
# صفحة إدارية مخفية عن مديري المشاريع. لإظهارها لهم لاحقاً كتبويب في صفحة التحديث
# غيّر CASHFLOW_TAB_FOR_PM إلى True (ولا شيء آخر يحتاج تعديلاً).

CASHFLOW_TAB_FOR_PM = False


def _ym_add(ym, n):
    """يضيف n شهراً إلى 'YYYY-MM'."""
    y, m = int(ym[:4]), int(ym[5:7])
    t = (y * 12 + (m - 1)) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _ym_range(start_ym, end_ym, cap=180):
    out, cur = [], start_ym
    while cur <= end_ym and len(out) < cap:
        out.append(cur)
        cur = _ym_add(cur, 1)
    return out


def _latest_project_row(project):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT contractor_val, contractor_mods_val, start_contractual, end_contractual,
                             start_actual, end_expected, contractor_mods_end_date
                      FROM project_updates WHERE project_name = %s
                      ORDER BY current_data_date DESC LIMIT 1""", (project,))
    row = cursor.fetchone()
    conn.close()
    return row


def _suggest_meta(project):
    """قيم مقترحة عند فتح مشروع لأول مرة: قيمة العقد ومدى الشهور من تواريخ العقد."""
    row = _latest_project_row(project)
    if not row:
        today = datetime.utcnow().strftime("%Y-%m")
        return {"contract_value": 0, "revised_value": 0, "start_month": today,
                "end_month": _ym_add(today, 11)}
    base = float(row[0] or 0)
    revised = base + float(row[1] or 0)
    starts = [d for d in (row[2], row[4]) if d]
    # نهاية المدى: نهاية عقد المقاول المعدّلة (row[6]) أولاً، وإلا نهاية العقد الأصلية (row[3]).
    # "النهاية المتوقعة" (row[5]) مقصودة الاستبعاد هنا لأنها تقدير إداري وليست تاريخاً تعاقدياً،
    # وأخذها ضمن max() كان بيخلي مدى التدفق النقدي يتمدد لتاريخ غير مرتبط بعقد المقاول.
    end_source = row[6] or row[3]
    ends = [end_source] if end_source else []
    start_ym = min(str(d)[:7] for d in starts) if starts else datetime.utcnow().strftime("%Y-%m")
    end_ym = str(ends[0])[:7] if ends else _ym_add(start_ym, 11)
    if end_ym < start_ym: end_ym = _ym_add(start_ym, 11)
    return {"contract_value": base, "revised_value": revised,
            "start_month": start_ym, "end_month": end_ym}


WEEKS_PER_MONTH = 4          # الشهر يُقسَّم إلى 4 أسابيع (قسمة تلقائية)

# مصدر خط الأساس — يُظهر بوضوح هل المنحنى المخطط مأخوذ من برنامج زمني معتمد أم مولَّد تقديرياً،
# حتى لا يُقارَن انحراف مشروع له برنامج معتمد بانحراف مشروع خط أساسه تقديري.
BASELINE_SOURCES = {
    "programme": "برنامج زمني معتمد",
    "auto":      "منحنى تقديري مولَّد",
    "stages":    "أوزان مراحل تقديرية",
    "manual":    "إدخال يدوي",
}


def _merge_weeks(month, wks):
    """يدمج تفصيل الأسابيع في صف الشهر.

    القاعدة: المخطط الشهري = مجموع أسابيعه (قيم تُجمَّع)،
    والنسبة الفعلية = نسبة آخر أسبوع مُدخل (نِسَب تراكمية لا تُجمَّع).
    """
    out = dict(month)
    out["weeks"] = [None] * WEEKS_PER_MONTH
    out["has_weeks"] = False
    if not wks:
        return out
    for k, v in wks.items():
        out["weeks"][k - 1] = v
    out["has_weeks"] = True

    plans = [v["plan_amount"] for _, v in sorted(wks.items()) if v["plan_amount"] is not None]
    acts  = [v["act_pct"]     for _, v in sorted(wks.items()) if v["act_pct"] is not None]
    if plans:
        out["plan_amount"] = round(sum(plans), 2)
    if acts:
        out["act_pct"] = acts[-1]
    return out


def _cashflow_payload(project):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT contract_value, revised_value, start_month, end_month, locked, updated_at,
                             COALESCE(baseline_source, 'manual'), plan_base
                      FROM cashflow_meta WHERE project_name = %s""", (project,))
    m = cursor.fetchone()
    if m:
        meta = {"contract_value": float(m[0] or 0), "revised_value": float(m[1] or 0),
                "start_month": m[2], "end_month": m[3], "locked": bool(m[4]),
                "updated_at": str(m[5]), "baseline_source": m[6],
                "plan_base": None if m[7] is None else float(m[7]), "is_new": False}
    else:
        meta = _suggest_meta(project)
        meta.update({"locked": False, "updated_at": None, "baseline_source": "manual",
                     "plan_base": None, "is_new": True})
    # فارغ = النسبة المخططة تُحسب على قيمة العقد الأصلية
    if not meta.get("plan_base"): meta["plan_base"] = meta["contract_value"]
    meta["baseline_source_label"] = BASELINE_SOURCES.get(meta["baseline_source"], BASELINE_SOURCES["manual"])

    cursor.execute("""SELECT ym, COALESCE(wk, 0), plan_amount, plan_pct, act_pct, act_amount, note, act_date
                      FROM cashflow_rows WHERE project_name = %s ORDER BY ym, COALESCE(wk, 0)""",
                   (project,))
    rows, weeks = {}, {}
    for r in cursor.fetchall():
        rec = {"ym": r[0], "wk": int(r[1] or 0),
               "plan_amount": None if r[2] is None else float(r[2]),
               "plan_pct":    None if r[3] is None else float(r[3]),
               "act_pct":     None if r[4] is None else float(r[4]),
               "act_amount":  None if r[5] is None else float(r[5]),
               "note": r[6], "act_date": _clean_date(r[7])}
        if rec["wk"] == 0:
            rows[r[0]] = rec
        elif 1 <= rec["wk"] <= WEEKS_PER_MONTH:
            weeks.setdefault(r[0], {})[rec["wk"]] = rec
    conn.close()

    months = _ym_range(meta["start_month"], meta["end_month"])
    for ym in sorted(set(list(rows.keys()) + list(weeks.keys()))):   # شهور خارج المدى تبقى ظاهرة
        if ym not in months: months.append(ym)
    months.sort()
    data = []
    for ym in months:
        base = rows.get(ym, {"ym": ym, "wk": 0, "plan_amount": None, "plan_pct": None,
                             "act_pct": None, "act_amount": None, "note": None, "act_date": None})
        data.append(_merge_weeks(base, weeks.get(ym)))
    return {"success": True, "project": project, "meta": meta, "rows": data}


@app.get("/admin-cashflow", response_class=HTMLResponse)
async def admin_cashflow_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM project_updates WHERE project_name IS NOT NULL ORDER BY project_name")
    projects = [r[0] for r in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "admin_cashflow.html", {
        "admin_user": admin_user, "projects": projects, "active_page": "cashflow"})


@app.get("/api/cashflow")
async def api_cashflow(project: str, request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        return _cashflow_payload(project)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/meta")
async def api_cashflow_meta(request: Request, background_tasks: BackgroundTasks):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        if not project:
            return JSONResponse({"success": False, "error": "المشروع مطلوب"}, status_code=400)
        cv = float(b.get("contract_value") or 0)
        rv = float(b.get("revised_value") or 0) or cv
        sm = (b.get("start_month") or "")[:7]
        em = (b.get("end_month") or "")[:7]
        if not sm or not em or em < sm:
            return JSONResponse({"success": False, "error": "مدى الشهور غير صحيح"}, status_code=400)
        locked = bool(b.get("locked"))
        src = b.get("baseline_source") or "manual"
        if src not in BASELINE_SOURCES: src = "manual"
        pb = b.get("plan_base")
        pb = None if (pb is None or pb == "") else float(pb)
        if not pb: pb = cv

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO cashflow_meta
                            (project_name, contract_value, revised_value, start_month, end_month, locked,
                             baseline_source, plan_base, updated_by, updated_at)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                          ON CONFLICT (project_name) DO UPDATE SET
                            contract_value=EXCLUDED.contract_value, revised_value=EXCLUDED.revised_value,
                            start_month=EXCLUDED.start_month, end_month=EXCLUDED.end_month,
                            locked=EXCLUDED.locked, baseline_source=EXCLUDED.baseline_source,
                            plan_base=EXCLUDED.plan_base,
                            updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                       (project, cv, rv, sm, em, locked, src, pb, admin_user))
        # لو المدى اتقصّر (زي بعد استيراد XER وسّع المدى لآخر البرنامج)، نشيل شهور خارج المدى
        # الجديد اللي معندهاش أي بيانات فعلية حقيقية على الإطلاق (بواقي تخطيط بس) — عشان الجدول
        # يستجيب فعلاً لما المستخدم يقصّر «آخر شهر» ويحفظ. أي شهر فيه ولو رقم فعلي واحد (شهرياً
        # أو أسبوعياً) بيفضل ظاهر ومحمي من الحذف، حتى لو برّه المدى، منعاً لفقد بيانات حقيقية.
        cursor.execute("""DELETE FROM cashflow_rows
                          WHERE project_name = %s
                            AND (ym < %s OR ym > %s)
                            AND ym NOT IN (
                                SELECT DISTINCT ym FROM cashflow_rows
                                WHERE project_name = %s AND act_pct IS NOT NULL
                            )""", (project, sm, em, project))
        conn.commit(); conn.close()
        background_tasks.add_task(log_audit, admin_user, "إعدادات التدفق النقدي", f"مشروع {project}")
        return _cashflow_payload(project)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/cell")
async def api_cashflow_cell(request: Request, background_tasks: BackgroundTasks):
    """حفظ تلقائي لخانة واحدة. القيم المشتقة تُحسب في الواجهة وتُرسل معها لتخزينها جاهزة."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        ym = (b.get("ym") or "")[:7]
        try:
            wk = int(b.get("wk") or 0)
        except (TypeError, ValueError):
            wk = 0
        if wk < 0 or wk > WEEKS_PER_MONTH:
            wk = 0
        if not project or len(ym) != 7:
            return JSONResponse({"success": False, "error": "بيانات ناقصة"}, status_code=400)

        b["ym"] = ym
        fields, values = _cell_fields(b)
        if not fields:
            return JSONResponse({"success": False, "error": "لا يوجد ما يُحفظ"}, status_code=400)

        cols = ", ".join(fields)
        marks = ", ".join(["%s"] * len(fields))
        upd = ", ".join(f"{f}=EXCLUDED.{f}" for f in fields)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"""INSERT INTO cashflow_rows (project_name, ym, wk, {cols}, updated_by, updated_at)
                           VALUES (%s, %s, %s, {marks}, %s, NOW())
                           ON CONFLICT (project_name, ym, wk) DO UPDATE SET
                             {upd}, updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                       [project, ym, wk] + values + [admin_user])
        conn.commit(); conn.close()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/cells")
async def api_cashflow_cells(request: Request):
    """حفظ دفعة واحدة من الخانات (يُستخدم في التقسيم الأسبوعي التلقائي)."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        cells = b.get("cells") or []
        if not project or not isinstance(cells, list):
            return JSONResponse({"success": False, "error": "بيانات ناقصة"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()
        n = 0
        for c in cells[:800]:
            ym = (c.get("ym") or "")[:7]
            if len(ym) != 7:
                continue
            try:
                wk = int(c.get("wk") or 0)
            except (TypeError, ValueError):
                wk = 0
            if wk < 0 or wk > WEEKS_PER_MONTH:
                wk = 0
            c["ym"] = ym
            fields, values = _cell_fields(c)
            if not fields:
                continue
            cols = ", ".join(fields)
            marks = ", ".join(["%s"] * len(fields))
            upd = ", ".join(f"{f}=EXCLUDED.{f}" for f in fields)
            cursor.execute(f"""INSERT INTO cashflow_rows (project_name, ym, wk, {cols}, updated_by, updated_at)
                               VALUES (%s, %s, %s, {marks}, %s, NOW())
                               ON CONFLICT (project_name, ym, wk) DO UPDATE SET
                                 {upd}, updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                           [project, ym, wk] + values + [admin_user])
            n += 1
        conn.commit(); conn.close()
        return {"success": True, "saved": n}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/unsplit")
async def api_cashflow_unsplit(request: Request):
    """يحذف التفصيل الأسبوعي لشهر (أو لكل الشهور) ويُبقي صف الشهر كما هو."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        ym = (b.get("ym") or "")[:7]
        if not project:
            return JSONResponse({"success": False, "error": "المشروع مطلوب"}, status_code=400)
        conn = get_db_connection()
        cursor = conn.cursor()
        if len(ym) == 7:
            cursor.execute("DELETE FROM cashflow_rows WHERE project_name=%s AND ym=%s AND COALESCE(wk,0)>0",
                           (project, ym))
        else:
            cursor.execute("DELETE FROM cashflow_rows WHERE project_name=%s AND COALESCE(wk,0)>0", (project,))
        conn.commit(); conn.close()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


SNAPSHOT_KEEP = 20          # أقصى عدد نسخ محفوظة لكل مشروع


def _snapshot_capture(project, name, user, conn=None):
    """يلتقط الحالة الحالية (الإعدادات + كل الصفوف والأسابيع) كنسخة يمكن الرجوع إليها."""
    own = conn is None
    if own: conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT contract_value, revised_value, start_month, end_month, locked,
                             COALESCE(baseline_source,'manual'), plan_base
                      FROM cashflow_meta WHERE project_name = %s""", (project,))
    m = cursor.fetchone()
    meta = None if not m else {"contract_value": float(m[0] or 0), "revised_value": float(m[1] or 0),
                               "start_month": m[2], "end_month": m[3], "locked": bool(m[4]),
                               "baseline_source": m[5],
                               "plan_base": None if m[6] is None else float(m[6])}
    cursor.execute("""SELECT ym, COALESCE(wk,0), plan_amount, plan_pct, act_pct, act_amount, note, act_date
                      FROM cashflow_rows WHERE project_name = %s ORDER BY ym, COALESCE(wk,0)""", (project,))
    rows = [[r[0], int(r[1] or 0)] + [None if v is None else float(v) for v in r[2:6]] + [r[6], r[7]]
            for r in cursor.fetchall()]
    cursor.execute("SELECT d, amount FROM cashflow_plan_daily WHERE project_name = %s ORDER BY d", (project,))
    daily = [[str(d)[:10], float(a or 0)] for d, a in cursor.fetchall()]
    cursor.execute("""INSERT INTO cashflow_snapshots (project_name, name, payload, created_by)
                      VALUES (%s,%s,%s,%s) RETURNING id""",
                   (project, (name or "نسخة")[:120],
                    json.dumps({"meta": meta, "rows": rows, "daily": daily}), user))
    sid = cursor.fetchone()[0]
    cursor.execute("""DELETE FROM cashflow_snapshots WHERE project_name = %s AND id NOT IN
                      (SELECT id FROM cashflow_snapshots WHERE project_name = %s
                       ORDER BY created_at DESC, id DESC LIMIT %s)""",
                   (project, project, SNAPSHOT_KEEP))
    if own: conn.commit(); conn.close()
    return sid, len(rows)


@app.get("/api/cashflow/snapshots")
async def api_cashflow_snapshots(project: str, request: Request):
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT id, name, created_by, created_at,
                                 jsonb_array_length(payload->'rows')
                          FROM cashflow_snapshots WHERE project_name = %s
                          ORDER BY created_at DESC, id DESC""", (project,))
        out = [{"id": r[0], "name": r[1], "by": r[2], "at": str(r[3])[:19], "rows": r[4]}
               for r in cursor.fetchall()]
        conn.close()
        return {"success": True, "snapshots": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/snapshot")
async def api_cashflow_snapshot(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        if not project:
            return JSONResponse({"success": False, "error": "المشروع مطلوب"}, status_code=400)
        sid, n = _snapshot_capture(project, b.get("name"), admin_user)
        return {"success": True, "id": sid, "rows": n}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/snapshot/restore")
async def api_cashflow_snapshot_restore(request: Request, background_tasks: BackgroundTasks):
    """يرجّع نسخة محفوظة — ويأخذ نسخة من الحالة الحالية أولاً حتى لا يضيع شيء."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        sid = int(b.get("id") or 0)
        if not project or not sid:
            return JSONResponse({"success": False, "error": "بيانات ناقصة"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, payload FROM cashflow_snapshots WHERE id = %s AND project_name = %s",
                       (sid, project))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return JSONResponse({"success": False, "error": "النسخة غير موجودة"}, status_code=404)
        snap_name, payload = row[0], row[1]
        if isinstance(payload, str): payload = json.loads(payload)

        _snapshot_capture(project, f"قبل استرجاع «{snap_name}»", admin_user, conn)

        cursor.execute("DELETE FROM cashflow_rows WHERE project_name = %s", (project,))
        for r in (payload.get("rows") or []):
            cursor.execute("""INSERT INTO cashflow_rows
                                (project_name, ym, wk, plan_amount, plan_pct, act_pct, act_amount, note,
                                 act_date, updated_by, updated_at)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                           (project, r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                            r[7] if len(r) > 7 else None, admin_user))    # نسخ قديمة بلا act_date
        # التوزيع اليومي يرجع مع النسخة؛ نسخة قديمة من قبل التوزيع اليومي ← توزيع متساوٍ
        cursor.execute("DELETE FROM cashflow_plan_daily WHERE project_name = %s", (project,))
        daily = [(project, d, a) for d, a in (payload.get("daily") or []) if _clean_date(d)]
        if daily:
            psycopg2.extras.execute_values(cursor,
                "INSERT INTO cashflow_plan_daily (project_name, d, amount) VALUES %s", daily, page_size=1000)
        m = payload.get("meta")
        if m:
            cursor.execute("""INSERT INTO cashflow_meta
                                (project_name, contract_value, revised_value, start_month, end_month,
                                 locked, baseline_source, plan_base, updated_by, updated_at)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                              ON CONFLICT (project_name) DO UPDATE SET
                                contract_value=EXCLUDED.contract_value, revised_value=EXCLUDED.revised_value,
                                start_month=EXCLUDED.start_month, end_month=EXCLUDED.end_month,
                                locked=EXCLUDED.locked, baseline_source=EXCLUDED.baseline_source,
                                plan_base=EXCLUDED.plan_base,
                                updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                           (project, m.get("contract_value") or 0, m.get("revised_value") or 0,
                            m.get("start_month"), m.get("end_month"), bool(m.get("locked")),
                            m.get("baseline_source") or "manual", m.get("plan_base"), admin_user))
        conn.commit(); conn.close()
        background_tasks.add_task(log_audit, admin_user, "استرجاع نسخة تدفق نقدي",
                                  f"مشروع {project} — {snap_name}")
        return _cashflow_payload(project)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/wipe")
async def api_cashflow_wipe(request: Request, background_tasks: BackgroundTasks):
    """مسح بيانات التدفق النقدي — للمشرف الرئيسي وحده، ودائماً بعد أخذ نسخة يمكن الرجوع إليها."""
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return JSONResponse({"success": False, "error": "هذه العملية متاحة للمشرف الرئيسي فقط"},
                            status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        scope = b.get("scope") or "all"                 # actual | plan | all
        all_projects = bool(b.get("all_projects"))
        if scope not in ("actual", "plan", "all"):
            return JSONResponse({"success": False, "error": "نطاق غير معروف"}, status_code=400)
        if not all_projects and not project:
            return JSONResponse({"success": False, "error": "المشروع مطلوب"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()
        if all_projects:
            cursor.execute("""SELECT project_name FROM cashflow_rows
                              UNION SELECT project_name FROM cashflow_meta""")
            targets = [r[0] for r in cursor.fetchall()][:50]
        else:
            targets = [project]

        label = {"actual": "قبل مسح البيانات الفعلية", "plan": "قبل مسح خط الأساس",
                 "all": "قبل مسح كل بيانات التدفق النقدي"}[scope]
        for p in targets:
            _snapshot_capture(p, label, admin_user, conn)

        n = 0
        for p in targets:
            if scope == "actual":
                cursor.execute("""UPDATE cashflow_rows SET act_pct = NULL, act_amount = NULL, act_date = NULL,
                                         updated_by = %s, updated_at = NOW()
                                  WHERE project_name = %s""", (admin_user, p))
            elif scope == "plan":
                cursor.execute("DELETE FROM cashflow_plan_daily WHERE project_name = %s", (p,))
                cursor.execute("""UPDATE cashflow_rows SET plan_amount = NULL, plan_pct = NULL,
                                         updated_by = %s, updated_at = NOW()
                                  WHERE project_name = %s""", (admin_user, p))
            else:
                cursor.execute("DELETE FROM cashflow_rows WHERE project_name = %s", (p,))
                cursor.execute("DELETE FROM cashflow_plan_daily WHERE project_name = %s", (p,))
                cursor.execute("DELETE FROM cashflow_meta WHERE project_name = %s", (p,))
            n += cursor.rowcount or 0
        conn.commit(); conn.close()

        background_tasks.add_task(log_audit, admin_user, "مسح بيانات التدفق النقدي",
                                  f"{'كل المشاريع' if all_projects else project} — {scope}")
        if all_projects:
            return {"success": True, "projects": len(targets), "rows": n}
        return _cashflow_payload(project)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/snapshot/delete")
async def api_cashflow_snapshot_delete(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cashflow_snapshots WHERE id = %s AND project_name = %s",
                       (int(b.get("id") or 0), (b.get("project") or "").strip()))
        conn.commit(); conn.close()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/import-weekly")
async def api_cashflow_import_weekly(request: Request, background_tasks: BackgroundTasks):
    """يقترح نسب الإنجاز الفعلية الشهرية من آخر تحديث أسبوعي داخل كل شهر (لا يحفظ شيئاً)."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT current_data_date, act_prog_cur FROM project_updates
                          WHERE project_name = %s AND current_data_date IS NOT NULL
                          ORDER BY current_data_date ASC""", (project,))
        by_month, by_week, month_dates = {}, {}, {}
        for d, pct in cursor.fetchall():
            ym = str(d)[:7]
            v = float(pct or 0)
            v = round((v * 100 if v <= 1.0001 else v), 2)
            by_month[ym] = v                                   # آخر قيمة في الشهر
            month_dates[ym] = _clean_date(d)                   # وتاريخها (تاريخ بيانات التحديث)
            day = int(str(d)[8:10] or 1)
            wk = min(WEEKS_PER_MONTH, max(1, (day + 6) // 7))   # 1-7→1، 8-14→2، 15-21→3، الباقي→4
            by_week.setdefault(ym, {})[str(wk)] = v             # آخر قيمة داخل الأسبوع
        conn.close()
        return {"success": True, "months": by_month, "weeks": by_week, "month_dates": month_dates}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _pm_updates(cursor, projects):
    """تحديثات مديري المشاريع الأسبوعية: {مشروع: {شهر: [(تاريخ, نسبة فعلية %), ...]}} مرتّبة بالتاريخ.
    نفس تحويل النسبة المستخدم في /api/cashflow/import-weekly (كسر ≤ 1 ← ×100)."""
    cursor.execute("""SELECT project_name, current_data_date, act_prog_cur FROM project_updates
                      WHERE project_name = ANY(%s) AND current_data_date IS NOT NULL
                      ORDER BY project_name, current_data_date""", (list(projects),))
    out = {}
    for proj, d, pct in cursor.fetchall():
        dt = _clean_date(d)
        if not dt or pct is None:
            continue
        v = float(pct)
        v = round((v * 100 if v <= 1.0001 else v), 2)
        out.setdefault(proj, {}).setdefault(dt[:7], []).append((dt, v))
    return out


def _month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = date(y + (m // 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")


def _cashflow_series(meta, data_rows, pm=None):
    """يبني سلسلة {ym, plan_amount, plan_cum, plan_pct, act_pct, act_cum, act_period, weeks} من صفوف
    مُدمَجة (بعد _merge_weeks) وميتاداتا مشروع — مشتركة بين /api/cashflow/data ونسختها المجمَّعة
    لكل المشاريع، حتى يبقى حساب المنحنى في مكان واحد بدل تكراره.
    act_period هو الفعلي الشهري (غير التراكمي) = فرق act_cum بين هذا الشهر والشهر السابق مباشرة.

    weeks: تفصيل أسبوعي (WEEKS_PER_MONTH أسابيع) لكل شهر، للعرض الأسبوعي في منشئ الداشبورد:
    - المخطط: الشهر المقسَّم في صفحة التدفق النقدي بأرقام أسابيعه، وغير المقسَّم ÷ 4 بالتساوي
      (زي البريمافيرا).
    - الفعلي — قاعدة واحدة: المنحنى ما يتحركش إلا برقم حقيقي، ويقف عند آخر رقم حقيقي.
      * الشهر المقسَّم: أرقام أسابيعه الحقيقية كما هي.
      * غير المقسَّم: رقم الشهر يتحط في أسبوع تاريخه (act_date = تاريخ تحديث مدير المشروع أو
        Data Date في XER أو نهاية الفترة المالية أو يوم الإدخال). الأسابيع قبله في نفس الشهر تاخد
        تحديثات مديري المشاريع الحقيقية لو موجودة، والأسبوع اللي مفيهوش رقم جديد يفضل على آخر رقم
        قبله (من غير أي تقدير). بعد أسبوع التاريخ: فاضي في آخر شهر فيه فعلي، ويفضل على رقم الشهر
        في الشهور الأقدم.
      * رقم قديم متخزّن من غير تاريخ: ياخد تاريخ تحديث مدير المشروع اللي بنفس النسبة في نفس الشهر
        لو موجود، وإلا نهاية الشهر (بحد أقصى النهارده)."""
    W = WEEKS_PER_MONTH
    pm = pm or {}
    today = _today_ksa()
    cv = meta["contract_value"] or 0
    rv = meta["revised_value"] or cv
    pb = meta.get("plan_base") or cv          # أساس النسبة المخططة
    act_months = [r["ym"] for r in data_rows if r["act_pct"] is not None]
    last_act_ym = max(act_months) if act_months else None
    out, cum_plan, prev_act_cum = [], 0.0, None
    prev_plan_pct, carry, wk_prev_act_cum = 0.0, None, None
    for r in data_rows:
        ym = r["ym"]
        m_plan = float(r["plan_amount"] or 0)
        start_cum = cum_plan
        cum_plan += m_plan
        plan_pct = r["plan_pct"] if r["plan_pct"] is not None else (cum_plan / pb * 100 if pb else 0)
        plan_pct = float(plan_pct or 0)
        act_pct = r["act_pct"]
        act_amt = r["act_amount"] if r["act_amount"] is not None else (
            (act_pct or 0) / 100 * rv if act_pct is not None else None)
        act_cum = None if act_amt is None else round(float(act_amt), 2)
        act_period = None if (act_cum is None or prev_act_cum is None) else round(act_cum - prev_act_cum, 2)
        if act_cum is not None:
            prev_act_cum = act_cum

        # ---- الفعلي الأسبوعي للشهر غير المقسَّم: نقاط حقيقية فقط ----
        real = bool(r.get("has_weeks"))
        a = None if act_pct is None else float(act_pct)
        points, Lw = {}, None
        if not real and a is not None:
            pm_m = pm.get(ym, [])
            d = r.get("act_date")
            if not d:                                   # رقم قديم بلا تاريخ
                match = [dt for dt, v in pm_m if abs(v - a) < 0.05]
                d = match[-1] if match else min(_month_end(ym), today)
            if d[:7] > ym:
                Lw = W
            elif d[:7] < ym:
                Lw = 1
            else:
                Lw = _week_of(d) or W
            for dt, v in pm_m:                          # تحديثات حقيقية قبل أسبوع رقم الشهر
                wkp = _week_of(dt)
                if wkp and wkp < Lw:
                    points[wkp] = v
            points[Lw] = a
        is_latest = (ym == last_act_ym)

        run, weeks_out = start_cum, []
        base = round(m_plan / W, 2)
        for i in range(W):
            wk = i + 1
            if real:
                wks = r.get("weeks") or [None] * W
                w = wks[i] if i < len(wks) else None
                wp = float(w["plan_amount"]) if (w and w.get("plan_amount") is not None) else 0.0
                wa = w.get("act_pct") if w else None
                wamt = w.get("act_amount") if w else None
            else:
                wp = base if wk < W else round(m_plan - base * (W - 1), 2)
                wamt = None
                if a is None:
                    wa = None
                elif wk in points:
                    wa = points[wk]
                elif wk < Lw:
                    wa = carry                          # ما فيش رقم جديد ← آخر رقم حقيقي قبله
                else:
                    wa = None if is_latest else a       # بعد آخر رقم حقيقي
            if wa is not None:
                carry = float(wa)
            run += wp
            frac = ((run - start_cum) / m_plan) if m_plan else wk / W
            wpp = prev_plan_pct + (plan_pct - prev_plan_pct) * frac
            if not real and a is not None and wa is not None and wk >= Lw:
                wcum = act_cum                          # = رقم الشهر الحقيقي بالظبط
            elif wamt is not None:
                wcum = round(float(wamt), 2)
            else:
                wcum = None if wa is None else round(float(wa) / 100 * rv, 2)
            wper = None if (wcum is None or wk_prev_act_cum is None) else round(wcum - wk_prev_act_cum, 2)
            if wcum is not None:
                wk_prev_act_cum = wcum
            weeks_out.append({"wk": wk, "plan_amount": round(wp, 2), "plan_cum": round(run, 2),
                              "plan_pct": round(wpp, 2),
                              "act_pct": None if wa is None else round(float(wa), 2),
                              "act_cum": wcum, "act_period": wper})
        prev_plan_pct = plan_pct
        if a is not None:
            carry = a

        out.append({"ym": ym, "plan_amount": m_plan,
                    "plan_cum": round(cum_plan, 2), "plan_pct": round(plan_pct, 2),
                    "act_pct": None if act_pct is None else round(float(act_pct), 2),
                    "act_cum": act_cum, "act_period": act_period, "weeks": weeks_out})
    return out


@app.get("/api/cashflow/data")
async def api_cashflow_data(project: str, request: Request):
    """مصدر بيانات جاهز للداشبورد: منحنى مخطط وفعلي بالنسبة والمبلغ."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        p = _cashflow_payload(project)
        try:
            conn = get_db_connection()
            pm = _pm_updates(conn.cursor(), [project]).get(project, {})
            conn.close()
        except Exception:
            pm = {}
        out = _cashflow_series(p["meta"], p["rows"], pm)
        return {"success": True, "project": project, "meta": p["meta"], "series": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/cashflow/data-all")
async def api_cashflow_data_all(request: Request):
    """نسخة مجمّعة من /api/cashflow/data لكل المشاريع في طلب واحد فقط (3 استعلامات على اتصال واحد بقاعدة البيانات
    مهما كان عدد المشاريع)، بدل ما يفتح منشئ الداشبورد طلب HTTP + اتصال قاعدة بيانات منفصل
    لكل مشروع عند وضع «كل المشاريع» — كان هذا يُبطئ تحديث الصفحة بشكل واضح مع كثرة المشاريع."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT project_name, contract_value, revised_value, start_month, end_month,
                                  plan_base FROM cashflow_meta""")
        metas = {}
        for name, cv, rv, sm, em, pb in cursor.fetchall():
            metas[name] = {"contract_value": float(cv or 0), "revised_value": float(rv or 0),
                           "start_month": sm, "end_month": em,
                           "plan_base": None if pb is None else float(pb)}
        if not metas:
            conn.close()
            return {"success": True, "projects": {}}
        for m in metas.values():
            if not m.get("plan_base"): m["plan_base"] = m["contract_value"]

        cursor.execute("""SELECT project_name, ym, COALESCE(wk, 0), plan_amount, plan_pct,
                                  act_pct, act_amount, act_date
                          FROM cashflow_rows WHERE project_name = ANY(%s)
                          ORDER BY project_name, ym, COALESCE(wk, 0)""", (list(metas.keys()),))
        rows_by_proj, weeks_by_proj = {}, {}
        for project, ym, wk, plan_amount, plan_pct, act_pct, act_amount, act_date in cursor.fetchall():
            wk = int(wk or 0)
            rec = {"ym": ym, "wk": wk,
                   "plan_amount": None if plan_amount is None else float(plan_amount),
                   "plan_pct": None if plan_pct is None else float(plan_pct),
                   "act_pct": None if act_pct is None else float(act_pct),
                   "act_amount": None if act_amount is None else float(act_amount),
                   "act_date": _clean_date(act_date)}
            if wk == 0:
                rows_by_proj.setdefault(project, {})[ym] = rec
            elif 1 <= wk <= WEEKS_PER_MONTH:
                weeks_by_proj.setdefault(project, {}).setdefault(ym, {})[wk] = rec
        try:
            pm_all = _pm_updates(cursor, metas.keys())
        except Exception:
            conn.rollback()
            pm_all = {}
        conn.close()

        out = {}
        for project, meta in metas.items():
            rows = rows_by_proj.get(project, {})
            weeks = weeks_by_proj.get(project, {})
            months = _ym_range(meta["start_month"], meta["end_month"])
            for ym in sorted(set(list(rows.keys()) + list(weeks.keys()))):
                if ym not in months: months.append(ym)
            months.sort()
            data_rows = []
            for ym in months:
                base = rows.get(ym, {"ym": ym, "wk": 0, "plan_amount": None, "plan_pct": None,
                                     "act_pct": None, "act_amount": None, "note": None, "act_date": None})
                data_rows.append(_merge_weeks(base, weeks.get(ym)))
            series = _cashflow_series(meta, data_rows, pm_all.get(project, {}))
            if series:
                out[project] = {"meta": meta, "series": series}
        return {"success": True, "projects": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


CF_WEEK_START = 5          # بداية الأسبوع: السبت (الاثنين=0 … السبت=5) — زي البريمافيرا


def _as_date(txt):
    return date(int(txt[:4]), int(txt[5:7]), int(txt[8:10]))


def _cf_load(cursor, names=None):
    """يحمّل كل ما يلزم لمنحنى التدفق النقدي لمشروع أو أكثر (أو كل المشاريع لو names=None) في
    4 استعلامات: {مشروع: {"meta", "rows" (مدمجة بأسابيعها), "daily" {تاريخ: مبلغ}, "pm" {شهر: [(تاريخ, %)]}}}"""
    if names is None:
        cursor.execute("""SELECT project_name, contract_value, revised_value, start_month, end_month, plan_base
                          FROM cashflow_meta""")
    else:
        cursor.execute("""SELECT project_name, contract_value, revised_value, start_month, end_month, plan_base
                          FROM cashflow_meta WHERE project_name = ANY(%s)""", (list(names),))
    metas = {}
    for name, cv, rv, sm, em, pb in cursor.fetchall():
        m = {"contract_value": float(cv or 0), "revised_value": float(rv or 0),
             "start_month": sm, "end_month": em, "plan_base": None if pb is None else float(pb)}
        if not m["plan_base"]:
            m["plan_base"] = m["contract_value"]
        metas[name] = m
    if not metas:
        return {}
    keys = list(metas.keys())
    cursor.execute("""SELECT project_name, ym, COALESCE(wk, 0), plan_amount, plan_pct, act_pct, act_amount, act_date
                      FROM cashflow_rows WHERE project_name = ANY(%s)
                      ORDER BY project_name, ym, COALESCE(wk, 0)""", (keys,))
    rows_by, weeks_by = {}, {}
    for proj, ym, wk, pa, pp, ap, aa, ad in cursor.fetchall():
        wk = int(wk or 0)
        rec = {"ym": ym, "wk": wk,
               "plan_amount": None if pa is None else float(pa), "plan_pct": None if pp is None else float(pp),
               "act_pct": None if ap is None else float(ap), "act_amount": None if aa is None else float(aa),
               "act_date": _clean_date(ad)}
        if wk == 0:
            rows_by.setdefault(proj, {})[ym] = rec
        elif 1 <= wk <= WEEKS_PER_MONTH:
            weeks_by.setdefault(proj, {}).setdefault(ym, {})[wk] = rec
    daily_by = {}
    try:
        cursor.execute("""SELECT project_name, d, amount FROM cashflow_plan_daily
                          WHERE project_name = ANY(%s)""", (keys,))
        for proj, d, amt in cursor.fetchall():
            daily_by.setdefault(proj, {})[str(d)[:10]] = float(amt or 0)
    except Exception:
        cursor.connection.rollback()
    try:
        pm_all = _pm_updates(cursor, keys)
    except Exception:
        cursor.connection.rollback()
        pm_all = {}
    out = {}
    for proj, meta in metas.items():
        rows, weeks = rows_by.get(proj, {}), weeks_by.get(proj, {})
        months = _ym_range(meta["start_month"], meta["end_month"])
        for ym in sorted(set(list(rows.keys()) + list(weeks.keys()))):
            if ym not in months:
                months.append(ym)
        months.sort()
        data_rows = []
        for ym in months:
            base = rows.get(ym, {"ym": ym, "wk": 0, "plan_amount": None, "plan_pct": None,
                                 "act_pct": None, "act_amount": None, "act_date": None})
            data_rows.append(_merge_weeks(base, weeks.get(ym)))
        out[proj] = {"meta": meta, "rows": data_rows, "daily": daily_by.get(proj, {}),
                     "pm": pm_all.get(proj, {})}
    return out


def _cf_daily(meta, rows, daily, pm):
    """يحوّل مشروع واحد إلى أيام: المخطط اليومي (مبلغ + نسبة تراكمية) ونقاط فعلية حقيقية بتواريخها.

    المخطط: مبلغ كل شهر من صفحة التدفق النقدي (المرجع للمجاميع) يتوزع على أيامه بشكل التوزيع
    اليومي المستورد من XER لو موجود (بنِسَبه، فلو اتعدّل مبلغ الشهر يدوياً يفضل المجموع مظبوط)، وإلا
    بأرقام الأسابيع لو الشهر متقسّم في الصفحة، وإلا بالتساوي على أيام الشهر. النسبة المخططة اليومية
    تتدرّج بين نسبة نهاية الشهر السابق ونسبة نهاية الشهر المحفوظة، فنهاية كل شهر = العرض الشهري بالظبط.

    الفعلي: نقاط حقيقية فقط (تاريخ، نسبة، مبلغ تراكمي): أرقام أسابيع الشهر المتقسّم في نهاية كل أسبوع؛
    ولغير المتقسّم رقم الشهر في تاريخه (act_date) وقبله تحديثات مديري المشاريع الحقيقية في نفس الشهر.
    رقم قديم من غير تاريخ: تاريخ تحديث مدير المشروع اللي بنفس النسبة، وإلا نهاية الشهر (بحد أقصى النهارده)."""
    W = WEEKS_PER_MONTH
    cv = meta["contract_value"] or 0
    rv = meta["revised_value"] or cv
    pb = meta.get("plan_base") or cv
    today = _as_date(_today_ksa())
    days, amts, pcts, points = [], [], [], []
    cum, prev_pct = 0.0, 0.0
    for r in rows:
        ym = r["ym"]
        first, last = date(int(ym[:4]), int(ym[5:7]), 1), _as_date(_month_end(ym))
        D = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        mp = float(r["plan_amount"] or 0)
        wks = r.get("weeks") or []
        prof = [daily.get(d.isoformat(), 0.0) for d in D]
        ps = sum(prof)
        if mp and ps > 0:
            dist = [mp * x / ps for x in prof]
        elif mp and r.get("has_weeks") and any(w and w.get("plan_amount") is not None for w in wks):
            dist = [0.0] * len(D)
            for i, w in enumerate(wks[:W]):
                if not w or w.get("plan_amount") is None:
                    continue
                lo, hi = i * 7, ((i + 1) * 7 if i < W - 1 else len(D))
                for k in range(lo, hi):
                    dist[k] += float(w["plan_amount"]) / (hi - lo)
        else:
            dist = [mp / len(D)] * len(D)
        month_pct = r["plan_pct"] if r["plan_pct"] is not None else ((cum + mp) / pb * 100 if pb else 0)
        month_pct = float(month_pct or 0)
        run = 0.0
        for i, d in enumerate(D):
            run += dist[i]
            frac = (run / mp) if mp else (i + 1) / len(D)
            days.append(d)
            amts.append(dist[i])
            pcts.append(prev_pct + (month_pct - prev_pct) * frac)
        cum += mp
        prev_pct = month_pct

        a = r["act_pct"]
        if a is None:
            continue
        a = float(a)
        month_cum = float(r["act_amount"]) if r["act_amount"] is not None else a / 100 * rv
        if r.get("has_weeks"):
            for i, w in enumerate(wks[:W]):
                if w and w.get("act_pct") is not None:
                    we = D[(i + 1) * 7 - 1] if i < W - 1 else D[-1]
                    wc = float(w["act_amount"]) if w.get("act_amount") is not None else float(w["act_pct"]) / 100 * rv
                    points.append((we, float(w["act_pct"]), wc))
        else:
            pm_m = pm.get(ym, [])
            dtxt = r.get("act_date")
            if not dtxt:
                match = [dt for dt, v in pm_m if abs(v - a) < 0.05]
                dtxt = match[-1] if match else max(first, min(last, today)).isoformat()
            dd = min(max(_as_date(dtxt), first), last)
            for dt, v in pm_m:
                pd = _as_date(dt)
                if pd < dd:
                    points.append((pd, v, v / 100 * rv))
            points.append((dd, a, month_cum))
    points.sort(key=lambda x: x[0])
    return {"days": days, "amts": amts, "pcts": pcts, "points": points, "pb": pb}


def _cf_bucket_key(d, gran):
    if gran == "day":
        return d
    if gran == "week":
        return d - timedelta(days=(d.weekday() - CF_WEEK_START) % 7)
    return date(d.year, d.month, 1)


def _cf_curve(projects, gran, frm=None, to=None, portfolio=False):
    """يجمّع أيام مشروع (أو كل المشاريع) في فترات يوم/أسبوع/شهر.
    لكل فترة: plan_pct/act_pct (تراكمي عند نهاية الفترة) و plan_period_pct/act_period_pct (الفترة وحدها).
    الفعلي يقف عند آخر رقم حقيقي: الفترات بعده فاضية. في وضع كل المشاريع كل مشروع يفضل على آخر رقم حقيقي
    ليه لحد آخر رقم حقيقي في المحفظة كلها، والمشروع اللي لسه ما بدأش ما يدخلش."""
    P = [p for p in projects if p["days"]]
    if not P:
        return []
    g0 = min(p["days"][0] for p in P)
    g1 = max(p["days"][-1] for p in P)
    buckets, d = [], g0
    while d <= g1:
        k = _cf_bucket_key(d, gran)
        if buckets and buckets[-1]["key"] == k:
            buckets[-1]["end"] = d
        else:
            buckets.append({"key": k, "start": d, "end": d})
        d += timedelta(days=1)

    # لكل مشروع: قيم عند نهاية كل فترة
    per = []
    for p in P:
        days, amts, pcts, pts = p["days"], p["amts"], p["pcts"], p["points"]
        i, j, cum = 0, 0, 0.0
        last_pt = pts[-1][0] if pts else None
        rec, cur_pt = [], None
        for b in buckets:
            period = 0.0
            pct_end = None
            while i < len(days) and days[i] <= b["end"]:
                cum += amts[i]
                if days[i] >= b["start"]:
                    period += amts[i]
                pct_end = pcts[i]
                i += 1
            if pct_end is None and i > 0:
                pct_end = pcts[i - 1]                       # بعد نهاية المشروع
            while j < len(pts) and pts[j][0] <= b["end"]:
                cur_pt = pts[j]
                j += 1
            started = days[0] <= b["end"]
            rec.append({"started": started, "plan_cum": cum, "plan_period": period, "plan_pct": pct_end,
                        "act": cur_pt})
        per.append({"rec": rec, "pb": p["pb"], "last_pt": last_pt})

    out = []
    if not portfolio:
        q = per[0]
        pb = q["pb"] or 0
        prev_cum = None
        for bi, b in enumerate(buckets):
            r = q["rec"][bi]
            act = r["act"] if (r["act"] and q["last_pt"] and b["start"] <= q["last_pt"]) else None
            act_cum = act[2] if act else None
            act_period = None if (act_cum is None or prev_cum is None) else act_cum - prev_cum
            if act_cum is not None:
                prev_cum = act_cum
            out.append({"b": b, "plan_pct": round(r["plan_pct"] or 0, 2),
                        "act_pct": None if act is None else round(act[1], 2),
                        "plan_period_pct": round(r["plan_period"] / pb * 100, 2) if pb else None,
                        "act_period_pct": None if (act_period is None or not pb) else round(act_period / pb * 100, 2),
                        # المبالغ نفسها — لصفحة التدفق النقدي (عرض بالمبلغ وجدول الفترات)
                        "plan_period": round(r["plan_period"], 2), "plan_cum": round(r["plan_cum"], 2),
                        "act_cum": None if act_cum is None else round(act_cum, 2),
                        "act_period": None if act_period is None else round(act_period, 2)})
    else:
        total = sum(q["pb"] or 0 for q in per)
        if total <= 0:
            return []
        last_all = max((q["last_pt"] for q in per if q["last_pt"]), default=None)
        prev = [None] * len(per)
        for bi, b in enumerate(buckets):
            plan_sum = plan_per = act_sum = per_sum = 0.0
            any_act = any_per = False
            for qi, q in enumerate(per):
                r = q["rec"][bi]
                if not r["started"]:
                    continue
                plan_sum += r["plan_cum"]
                plan_per += r["plan_period"]
                if r["act"]:
                    c = r["act"][2]
                    act_sum += c
                    any_act = True
                    if prev[qi] is not None:
                        per_sum += c - prev[qi]
                        any_per = True
                    prev[qi] = c
            live = any_act and last_all is not None and b["start"] <= last_all
            out.append({"b": b, "plan_pct": round(plan_sum / total * 100, 2),
                        "act_pct": round(act_sum / total * 100, 2) if live else None,
                        "plan_period_pct": round(plan_per / total * 100, 2),
                        "act_period_pct": round(per_sum / total * 100, 2) if (live and any_per) else None,
                        "plan_period": round(plan_per, 2), "plan_cum": round(plan_sum, 2),
                        "act_cum": round(act_sum, 2) if live else None,
                        "act_period": round(per_sum, 2) if (live and any_per) else None})

    res = []
    for o in out:
        b = o.pop("b")
        if frm and b["end"] < frm:
            continue
        if to and b["start"] > to:
            continue
        key = b["key"]
        o["key"] = key.isoformat()
        o["label"] = key.strftime("%Y-%m") if gran == "month" else key.isoformat()
        o["start"], o["end"] = b["start"].isoformat(), b["end"].isoformat()
        res.append(o)
    return res


@app.get("/api/cashflow/curve")
async def api_cashflow_curve(request: Request, project: str = "", gran: str = "month",
                             frm: str = "", to: str = ""):
    """منحنى التدفق النقدي الجاهز لمنشئ الداشبورد: project فاضي = كل المشاريع مجمّعة.
    gran = day | week | month، والمدى من تاريخ إلى تاريخ (اختياري). كل الحساب على السيرفر ويرجع
    بس الفترات المطلوبة، فالصفحة ما بتحمّلش آلاف الأيام."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        gran = gran if gran in ("day", "week", "month") else "month"
        f = _clean_date(frm)
        t = _clean_date(to)
        conn = get_db_connection()
        data = _cf_load(conn.cursor(), [project] if project else None)
        conn.close()
        projs = [_cf_daily(v["meta"], v["rows"], v["daily"], v["pm"]) for v in data.values()]
        if project and not projs:
            return {"success": True, "buckets": []}
        buckets = _cf_curve(projs, gran, _as_date(f) if f else None, _as_date(t) if t else None,
                            portfolio=not project)
        return {"success": True, "gran": gran, "buckets": buckets}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/at")
async def api_cashflow_at(request: Request):
    """النسبة المخططة والفعلية من التدفق النقدي في تواريخ محددة — لخيار «مصدر نسب الإنجاز» في
    الداشبورد. body: {"items": {مشروع: [تاريخ, ...]}}. المخطط من التوزيع اليومي لخط الأساس، والفعلي
    = آخر رقم حقيقي في التاريخ ده أو قبله (null لو مفيش). مشروع مالوش تدفق نقدي ما بيرجعش."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        items = b.get("items") or {}
        if not isinstance(items, dict) or not items:
            return {"success": True, "projects": {}}
        conn = get_db_connection()
        data = _cf_load(conn.cursor(), list(items.keys())[:300])
        conn.close()
        out = {}
        for proj, v in data.items():
            P = _cf_daily(v["meta"], v["rows"], v["daily"], v["pm"])
            if not P["days"]:
                continue
            idx = {d: i for i, d in enumerate(P["days"])}
            first, last = P["days"][0], P["days"][-1]
            pts = P["points"]
            at = {}
            for txt in (items.get(proj) or [])[:2000]:
                t = _clean_date(txt)
                if not t:
                    continue
                d = _as_date(t)
                plan = 0.0 if d < first else P["pcts"][-1] if d > last else P["pcts"][idx[d]]
                act = None
                for pd, pv, _ in pts:
                    if pd <= d:
                        act = pv
                    else:
                        break
                at[t] = [round(plan, 4), None if act is None else round(act, 4)]
            meta = v["meta"]
            cv = meta["contract_value"] or 0
            out[proj] = {"pb": meta.get("plan_base") or cv, "rv": meta["revised_value"] or cv, "at": at}
        return {"success": True, "projects": out}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/cashflow/plan-daily")
async def api_cashflow_plan_daily(request: Request):
    """يستبدل التوزيع اليومي لخط الأساس لمشروع ({تاريخ: مبلغ}) — من استيراد XER. قائمة فاضية = مسح."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        b = await request.json()
        project = (b.get("project") or "").strip()
        days = b.get("days") or {}
        if not project or not isinstance(days, dict):
            return JSONResponse({"success": False, "error": "بيانات ناقصة"}, status_code=400)
        vals = []
        for k, v in days.items():
            d = _clean_date(k)
            try:
                amt = float(v)
            except (TypeError, ValueError):
                continue
            if d and amt:
                vals.append((project, d, round(amt, 4)))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cashflow_plan_daily WHERE project_name = %s", (project,))
        if vals:
            psycopg2.extras.execute_values(cursor,
                "INSERT INTO cashflow_plan_daily (project_name, d, amount) VALUES %s", vals, page_size=1000)
        conn.commit(); conn.close()
        return {"success": True, "days": len(vals)}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/cashflow/excel")
async def api_cashflow_excel(project: str, request: Request, background_tasks: BackgroundTasks):
    """تصدير جدول التدفق النقدي ومنحنى الإنجاز كملف إكسيل بمعادلات حية ورسم بياني."""
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.chart import LineChart, Reference
        from openpyxl.utils import get_column_letter
        import io

        p = _cashflow_payload(project)
        meta, rows = p["meta"], p["rows"]

        wb = Workbook()
        ws = wb.active
        ws.title = "التدفق النقدي"
        ws.sheet_view.rightToLeft = True

        NAVY = "1A2B4C"; GOLD = "D4A373"; GREEN = "1F4D3D"; PURPLE = "4A3B6B"
        MUTED = "EEF1F6"
        f_title = Font(name="Arial", size=14, bold=True, color=NAVY)
        f_head  = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        f_cell  = Font(name="Arial", size=10)
        f_input = Font(name="Arial", size=10, color="0000FF")      # المدخلات بالأزرق
        f_lab   = Font(name="Arial", size=10, bold=True)
        thin    = Side(style="thin", color="D9DEE8")
        border  = Border(left=thin, right=thin, top=thin, bottom=thin)
        center  = Alignment(horizontal="center", vertical="center")

        ws["A1"] = f"التدفق النقدي ومنحنى الإنجاز — {project}"
        ws["A1"].font = f_title
        ws.merge_cells("A1:J1")

        # ---- الإعدادات (مدخلات) ----
        ws["A3"] = "قيمة العقد الأصلية"; ws["A3"].font = f_lab
        ws["B3"] = float(meta.get("contract_value") or 0); ws["B3"].font = f_input
        ws["B3"].number_format = "#,##0"
        ws["A4"] = "القيمة بعد أوامر التغيير"; ws["A4"].font = f_lab
        ws["B4"] = float(meta.get("revised_value") or meta.get("contract_value") or 0); ws["B4"].font = f_input
        ws["B4"].number_format = "#,##0"
        ws["A6"] = "أساس النسبة المخططة"; ws["A6"].font = f_lab
        ws["B6"] = float(meta.get("plan_base") or meta.get("contract_value") or 0)
        ws["B6"].font = f_input; ws["B6"].number_format = "#,##0"
        ws["D6"] = "النسبة المخططة = التراكمي المخطط ÷ هذه القيمة (قد تختلف عن قيمة العقد بعد أوامر التغيير)"
        ws["D6"].font = Font(name="Arial", size=9, italic=True, color="6C7A91")
        ws["A5"] = "مصدر خط الأساس"; ws["A5"].font = f_lab
        _src = meta.get("baseline_source") or "manual"
        ws["B5"] = BASELINE_SOURCES.get(_src, BASELINE_SOURCES["manual"])
        ws["B5"].font = Font(name="Arial", size=10, bold=True,
                             color=("1F4D3D" if _src == "programme" else "9A6A00"))
        if _src != "programme":
            ws["D5"] = "تنبيه: خط الأساس هنا تقديري وليس من برنامج زمني معتمد — الانحراف و SPI مؤشرات استرشادية"
            ws["D5"].font = Font(name="Arial", size=9, italic=True, bold=True, color="9A6A00")
        ws["D3"] = "الخانات الزرقاء مدخلات — البقية معادلات تُحسب تلقائياً"
        ws["D3"].font = Font(name="Arial", size=9, italic=True, color="6C7A91")
        ws["D4"] = f"صُدِّر بواسطة {admin_user} — {datetime.utcnow().strftime('%Y-%m-%d')}"
        ws["D4"].font = Font(name="Arial", size=9, italic=True, color="6C7A91")

        # ---- رؤوس الجدول ----
        HR = 8                       # صف مجموعات الأعمدة
        HR2 = 9                      # صف أسماء الأعمدة
        groups = [("الشهر", 1, 1, NAVY), ("خط الأساس (المخطط)", 2, 4, GOLD),
                  ("الفعلي", 5, 7, GREEN), ("التحليل", 8, 10, PURPLE)]
        for label, c1, c2, color in groups:
            cell = ws.cell(row=HR, column=c1, value=label)
            cell.font = f_head; cell.alignment = center
            cell.fill = PatternFill("solid", fgColor=color)
            if c2 > c1:
                ws.merge_cells(start_row=HR, start_column=c1, end_row=HR, end_column=c2)
                for c in range(c1 + 1, c2 + 1):
                    ws.cell(row=HR, column=c).fill = PatternFill("solid", fgColor=color)

        headers = ["الشهر", "المخطط الشهري", "التراكمي المخطط", "% مخططة",
                   "% فعلية", "التراكمي الفعلي", "الفعلي الشهري",
                   "الانحراف (نقطة)", "SPI", "ملاحظة"]
        colors  = [NAVY, GOLD, GOLD, GOLD, GREEN, GREEN, GREEN, PURPLE, PURPLE, PURPLE]
        for i, (h, col) in enumerate(zip(headers, colors), start=1):
            c = ws.cell(row=HR2, column=i, value=h)
            c.font = f_head; c.alignment = center; c.border = border
            c.fill = PatternFill("solid", fgColor=col)

        # ---- الصفوف بمعادلات حية ----
        first = HR2 + 1
        for i, r in enumerate(rows):
            rw = first + i
            ws.cell(row=rw, column=1, value=r["ym"]).font = f_lab
            ws.cell(row=rw, column=1).alignment = center

            a = ws.cell(row=rw, column=2, value=float(r["plan_amount"] or 0))   # مدخل
            a.font = f_input; a.number_format = "#,##0"

            prev_cum = f"C{rw-1}" if i > 0 else "0"
            ws.cell(row=rw, column=3, value=f"={prev_cum}+B{rw}").number_format = "#,##0"
            ws.cell(row=rw, column=4, value=f"=IF($B$6=0,0,C{rw}/$B$6)").number_format = "0.0%"

            if r["act_pct"] is None:
                ws.cell(row=rw, column=5, value=None)
            else:
                ws.cell(row=rw, column=5, value=float(r["act_pct"]) / 100.0)     # مدخل ككسر
            ws.cell(row=rw, column=5).font = f_input
            ws.cell(row=rw, column=5).number_format = "0.0%"

            ws.cell(row=rw, column=6, value=f'=IF(E{rw}="","",E{rw}*$B$4)').number_format = "#,##0"
            prev_amt = f"F{rw-1}" if i > 0 else "0"
            ws.cell(row=rw, column=7,
                    value=f'=IF(E{rw}="","",F{rw}-IF({prev_amt}="",0,{prev_amt}))').number_format = "#,##0"
            ws.cell(row=rw, column=8, value=f'=IF(E{rw}="","",(E{rw}-D{rw})*100)').number_format = "+0.0;-0.0;0.0"
            ws.cell(row=rw, column=9, value=f'=IF(OR(E{rw}="",D{rw}=0),"",E{rw}/D{rw})').number_format = "0.00"
            ws.cell(row=rw, column=10, value=r.get("note") or "")

            for c in range(1, 11):
                cell = ws.cell(row=rw, column=c)
                cell.border = border
                if cell.font is None or cell.font.color is None or cell.font.color.rgb != "000000FF":
                    if c not in (2, 5):
                        cell.font = f_cell
                if c not in (1, 10):
                    cell.alignment = center

        last = first + len(rows) - 1

        # ---- صف الإجمالي ----
        tot = last + 1
        ws.cell(row=tot, column=1, value="الإجمالي").font = f_lab
        ws.cell(row=tot, column=2, value=f"=SUM(B{first}:B{last})").number_format = "#,##0"
        ws.cell(row=tot, column=2).font = f_lab
        ws.cell(row=tot, column=3, value=f"=C{last}").number_format = "#,##0"
        ws.cell(row=tot, column=3).font = f_lab
        for c in range(1, 11):
            ws.cell(row=tot, column=c).fill = PatternFill("solid", fgColor=MUTED)
            ws.cell(row=tot, column=c).border = border

        ws.cell(row=tot + 2, column=1, value="فرق مجموع الخطة عن أساس النسبة المخططة").font = f_lab
        ws.cell(row=tot + 2, column=3, value=f"=B{tot}-$B$6").number_format = "#,##0;-#,##0;0"

        widths = [11, 16, 17, 11, 11, 17, 16, 15, 9, 26]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = ws.cell(row=first, column=2)

        # ---- رسم منحنى الإنجاز ----
        chart = LineChart()
        chart.title = "منحنى الإنجاز التراكمي (S-Curve)"
        chart.style = 2
        chart.y_axis.title = "نسبة الإنجاز التراكمية"
        chart.x_axis.title = "الشهر"
        chart.height = 10; chart.width = 24
        data = Reference(ws, min_col=4, max_col=5, min_row=HR2, max_row=last)
        cats = Reference(ws, min_col=1, min_row=first, max_row=last)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.series[0].graphicalProperties.line.dashStyle = "dash"
        chart.series[0].graphicalProperties.line.width = 28000
        chart.series[1].graphicalProperties.line.width = 28000
        ws.add_chart(chart, f"A{tot + 5}")

        # ---- ورقة التفصيل الأسبوعي (تظهر فقط للشهور المقسَّمة) ----
        split_months = [r for r in rows if r.get("has_weeks")]
        if split_months:
            w2 = wb.create_sheet("التفصيل الأسبوعي")
            w2.sheet_view.rightToLeft = True
            w2["A1"] = f"التفصيل الأسبوعي — {project}"
            w2["A1"].font = f_title
            w2.merge_cells("A1:F1")
            w2["A2"] = "المخطط الأسبوعي يُجمَع ليعطي المخطط الشهري · النسبة الفعلية تراكمية فتُؤخذ من آخر أسبوع مُدخل"
            w2["A2"].font = Font(name="Arial", size=9, italic=True, color="6C7A91")

            wheads = ["الشهر", "الأسبوع", "المخطط الأسبوعي", "% فعلية", "التراكمي الفعلي", "ملاحظة"]
            wcolors = [NAVY, NAVY, GOLD, GREEN, GREEN, PURPLE]
            for i, (h, col) in enumerate(zip(wheads, wcolors), start=1):
                c = w2.cell(row=4, column=i, value=h)
                c.font = f_head; c.alignment = center; c.border = border
                c.fill = PatternFill("solid", fgColor=col)

            rv_ref = float(meta.get("revised_value") or meta.get("contract_value") or 0)
            rw = 5
            for r in split_months:
                m_first = rw
                for wi in range(1, WEEKS_PER_MONTH + 1):
                    wrec = (r.get("weeks") or [None] * WEEKS_PER_MONTH)[wi - 1] or {}
                    w2.cell(row=rw, column=1, value=r["ym"]).alignment = center
                    w2.cell(row=rw, column=2, value=f"أسبوع {wi}").alignment = center
                    a = w2.cell(row=rw, column=3, value=float(wrec.get("plan_amount") or 0))
                    a.font = f_input; a.number_format = "#,##0"
                    ap = wrec.get("act_pct")
                    b_ = w2.cell(row=rw, column=4, value=None if ap is None else float(ap) / 100.0)
                    b_.font = f_input; b_.number_format = "0.0%"
                    w2.cell(row=rw, column=5,
                            value=f'=IF(D{rw}="","",D{rw}*{rv_ref})').number_format = "#,##0"
                    w2.cell(row=rw, column=6, value=wrec.get("note") or "")
                    for c in range(1, 7):
                        w2.cell(row=rw, column=c).border = border
                    rw += 1
                t = w2.cell(row=rw, column=2, value="مجموع الشهر"); t.font = f_lab; t.alignment = center
                s = w2.cell(row=rw, column=3, value=f"=SUM(C{m_first}:C{rw-1})")
                s.font = f_lab; s.number_format = "#,##0"
                lp = w2.cell(row=rw, column=4, value=f'=IF(COUNT(D{m_first}:D{rw-1})=0,"",LOOKUP(2,1/(D{m_first}:D{rw-1}<>""),D{m_first}:D{rw-1}))')
                lp.font = f_lab; lp.number_format = "0.0%"
                for c in range(1, 7):
                    w2.cell(row=rw, column=c).fill = PatternFill("solid", fgColor=MUTED)
                    w2.cell(row=rw, column=c).border = border
                rw += 1

            for i, w in enumerate([11, 12, 17, 11, 17, 26], start=1):
                w2.column_dimensions[get_column_letter(i)].width = w
            w2.freeze_panes = w2.cell(row=5, column=3)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        background_tasks.add_task(log_audit, admin_user, "تصدير التدفق النقدي", f"مشروع {project}")
        # اسم الملف بالعربية عبر ترميز RFC 5987 مع بديل لاتيني للمتصفحات القديمة
        from urllib.parse import quote as _q
        safe = re.sub(r'[^A-Za-z0-9]+', '_', project).strip('_')[:40] or 'project'
        pretty = _q(f"التدفق_النقدي_{project}.xlsx")
        return StreamingResponse(
            buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f"attachment; filename=\"cashflow_{safe}.xlsx\"; filename*=UTF-8''{pretty}"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@app.get("/admin-gallery", response_class=HTMLResponse)
async def admin_gallery_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM project_updates WHERE project_name IS NOT NULL ORDER BY project_name")
    projects = [row[0] for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "admin_gallery.html", {"projects": projects, "admin_user": admin_user, "active_page": "gallery"})

@app.get("/api/project-dates")
async def get_project_dates(project: str, request: Request):
    if request.cookies.get("super_admin_auth") != "admin_mohamed": return {"error": "غير مصرح"}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT current_data_date FROM project_updates WHERE project_name = %s AND current_data_date IS NOT NULL ORDER BY current_data_date DESC", (project,))
    dates = [row[0] for row in cursor.fetchall()]
    conn.close()
    return {"success": True, "dates": dates}

@app.get("/api/gallery-data")
async def get_gallery_data(project: str, date: str, request: Request):
    if request.cookies.get("super_admin_auth") != "admin_mohamed": return {"error": "غير مصرح"}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link, submission_date 
        FROM project_updates WHERE project_name = %s AND current_data_date = %s ORDER BY id DESC LIMIT 1
    ''', (project, date))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "success": True,
            "images": {"prog1": row[0], "prog2": row[1], "prog3": row[2], "prog4": row[3], "master": row[4], "iso": row[5]},
            "submission_date": str(row[6]) if row[6] else "غير محدد"
        }
    return {"success": False}

@app.get("/update-portal", response_class=HTMLResponse)
async def update_portal_page(request: Request):
    auth_user = request.cookies.get("auth_user")
    if not auth_user: return RedirectResponse(url="/login")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT manager_name, project_name FROM users WHERE username=%s", (auth_user,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        res = RedirectResponse(url="/login")
        res.delete_cookie("auth_user")
        return res
    return templates.TemplateResponse(request, "index.html", {"username": auth_user, "manager_name": user[0], "project_name": user[1]})

@app.post("/submit")
async def submit_data(request: Request, background_tasks: BackgroundTasks):
    auth_user = request.cookies.get("auth_user")
    if not auth_user: 
        return HTMLResponse(content="<h3>انتهت الجلسة، يرجى تسجيل الدخول.</h3>", status_code=401)
    
    form_data = await request.form()
    username = form_data.get("username")

    ksa_time = datetime.utcnow() + timedelta(hours=3)
    sub_date = ksa_time.strftime("%Y-%m-%d")
    sub_time = ksa_time.strftime("%I:%M %p") 
    
    wd = ksa_time.weekday()
    if wd == 2: days_to_add = 0             
    elif wd == 3: days_to_add = -1          
    elif wd == 4: days_to_add = -2          
    elif wd == 5: days_to_add = -3          
    elif wd == 6 and ksa_time.hour < 9: days_to_add = -4  
    elif wd == 6 and ksa_time.hour >= 9: days_to_add = 3  
    elif wd == 0: days_to_add = 2           
    elif wd == 1: days_to_add = 1           
    
    calculated_data_date = (ksa_time + timedelta(days=days_to_add)).date().isoformat()

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link FROM project_updates WHERE username=%s AND current_data_date=%s", (username, calculated_data_date))
        existing_record = cursor.fetchone()

        cursor.execute("SELECT file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link FROM project_updates WHERE username=%s AND project_name=%s ORDER BY current_data_date DESC LIMIT 1", (username, form_data.get("project_name")))
        previous_record = cursor.fetchone()

        attachments = [form_data.get(f"attachment_{i}") for i in range(1, 5)]
        links = ["لا يوجد مرفق"] * 4
        
        for i in range(4):
            flag_val = form_data.get(f"flag_attachment_{i+1}")
            file_obj = attachments[i]
            if flag_val == "true" and existing_record and existing_record[i+1] and existing_record[i+1] != "لا يوجد مرفق":
                links[i] = existing_record[i+1]
            elif flag_val == "true" and previous_record and previous_record[i] and previous_record[i] != "لا يوجد مرفق":
                links[i] = previous_record[i]
            elif file_obj and getattr(file_obj, "filename", None):
                links[i] = upload_to_cloudinary(file_obj) or "لا يوجد مرفق"
            elif existing_record and existing_record[i+1]:
                links[i] = existing_record[i+1]

        master_plan = form_data.get("master_plan")
        flag_master = form_data.get("flag_master_plan")
        if flag_master == "true" and existing_record and existing_record[5] and existing_record[5] != "لا يوجد مرفق":
            master_plan_link = existing_record[5]
        elif flag_master == "true" and previous_record and previous_record[4] and previous_record[4] != "لا يوجد مرفق":
            master_plan_link = previous_record[4]
        elif master_plan and getattr(master_plan, "filename", None):
            master_plan_link = upload_to_cloudinary(master_plan) or "لا يوجد مرفق"
        else:
            master_plan_link = existing_record[5] if existing_record and existing_record[5] else "لا يوجد مرفق"

        isometric = form_data.get("isometric")
        flag_iso = form_data.get("flag_isometric")
        if flag_iso == "true" and existing_record and existing_record[6] and existing_record[6] != "لا يوجد مرفق":
            isometric_link = existing_record[6]
        elif flag_iso == "true" and previous_record and previous_record[5] and previous_record[5] != "لا يوجد مرفق":
            isometric_link = previous_record[5]
        elif isometric and getattr(isometric, "filename", None):
            isometric_link = upload_to_cloudinary(isometric) or "لا يوجد مرفق"
        else:
            isometric_link = existing_record[6] if existing_record and existing_record[6] else "لا يوجد مرفق"

        def clean_num(val):
            if val is None or str(val).strip() == "": return 0
            try: return float(val)
            except: return 0

        def clean_date(val):
            return val if val and str(val).strip() != "" else None
            
        def clamped(val, lo, hi):
            """حدّ أخير على الخادم: الواجهة تمنع، وهذا يضمن ألا تصل قاعدة البيانات
            قيمة خارج المدى مهما كان مصدر الطلب."""
            return max(lo, min(hi, clean_num(val)))

        def process_percentage(val):
            if val is None or str(val).strip() == "": return 0.0
            try: return max(0.0, min(100.0, float(val))) / 100.0
            except: return 0.0

        def clean_json(val):
            return val if val and str(val).strip() != "" else "[]"

        data_values = (
            form_data.get("manager_name"), form_data.get("project_name"), form_data.get("project_desc"), form_data.get("project_type"), 
            calculated_data_date, form_data.get("project_owner"), form_data.get("project_developer"), form_data.get("project_contractor"),
            clean_num(form_data.get("consultant_val")), clean_num(form_data.get("contractor_val")),
            clean_num(form_data.get("consultant_mods_count")), clean_num(form_data.get("consultant_mods_val")), 
            clean_num(form_data.get("consultant_mods_time")), clean_date(form_data.get("consultant_mods_end_date")),
            clean_num(form_data.get("contractor_mods_count")), clean_num(form_data.get("contractor_mods_val")), 
            clean_num(form_data.get("contractor_mods_time")), clean_date(form_data.get("contractor_mods_end_date")),
            clean_num(form_data.get("cons_inv_count")), clean_num(form_data.get("cons_inv_val")), clean_date(form_data.get("cons_inv_date")),
            clean_num(form_data.get("cont_inv_count")), clean_num(form_data.get("cont_inv_val")), clean_date(form_data.get("cont_inv_date")),
            clean_date(form_data.get("start_contractual")), clean_date(form_data.get("end_contractual")), clean_date(form_data.get("start_actual")), clean_date(form_data.get("end_expected")),
            process_percentage(form_data.get("act_prog_cur")), process_percentage(form_data.get("act_prog_prev")), process_percentage(form_data.get("plan_prog_cur")), process_percentage(form_data.get("plan_prog_prev")),
            form_data.get("works_completed"), form_data.get("works_ongoing"), form_data.get("works_planned"), clean_json(form_data.get("obstacles_json")),
            clamped(form_data.get("eval_labor"), 0, 10), clamped(form_data.get("eval_equip"), 0, 10),
            clamped(form_data.get("eval_financial"), 0, 10), clamped(form_data.get("eval_hse"), 0, 10),
            clean_num(form_data.get("drawings_sub")), clean_num(form_data.get("drawings_app")), clean_num(form_data.get("drawings_rev")),
            clean_num(form_data.get("ir_sub")), clean_num(form_data.get("ir_app")), clean_num(form_data.get("ir_rev")),
            clean_num(form_data.get("ncr_open")), clean_num(form_data.get("ncr_closed")),
            links[0], links[1], links[2], links[3], master_plan_link, isometric_link
        )

        if existing_record:
            cursor.execute('''UPDATE project_updates SET manager_name=%s, project_name=%s, project_desc=%s, project_type=%s, current_data_date=%s, project_owner=%s, project_developer=%s, project_contractor=%s, consultant_val=%s, contractor_val=%s, consultant_mods_count=%s, consultant_mods_val=%s, consultant_mods_time=%s, consultant_mods_end_date=%s, contractor_mods_count=%s, contractor_mods_val=%s, contractor_mods_time=%s, contractor_mods_end_date=%s, cons_inv_count=%s, cons_inv_val=%s, cons_inv_date=%s, cont_inv_count=%s, cont_inv_val=%s, cont_inv_date=%s, start_contractual=%s, end_contractual=%s, start_actual=%s, end_expected=%s, act_prog_cur=%s, act_prog_prev=%s, plan_prog_cur=%s, plan_prog_prev=%s, works_completed=%s, works_ongoing=%s, works_planned=%s, obstacles_data=%s, eval_labor=%s, eval_equip=%s, eval_financial=%s, eval_hse=%s, drawings_sub=%s, drawings_app=%s, drawings_rev=%s, ir_sub=%s, ir_app=%s, ir_rev=%s, ncr_open=%s, ncr_closed=%s, file_link_1=%s, file_link_2=%s, file_link_3=%s, file_link_4=%s, master_plan_link=%s, isometric_link=%s, submission_date=%s, submission_time=%s WHERE id = %s''', data_values + (sub_date, sub_time, existing_record[0],))
        else:
            cursor.execute('''INSERT INTO project_updates (manager_name, project_name, project_desc, project_type, current_data_date, project_owner, project_developer, project_contractor, consultant_val, contractor_val, consultant_mods_count, consultant_mods_val, consultant_mods_time, consultant_mods_end_date, contractor_mods_count, contractor_mods_val, contractor_mods_time, contractor_mods_end_date, cons_inv_count, cons_inv_val, cons_inv_date, cont_inv_count, cont_inv_val, cont_inv_date, start_contractual, end_contractual, start_actual, end_expected, act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev, works_completed, works_ongoing, works_planned, obstacles_data, eval_labor, eval_equip, eval_financial, eval_hse, drawings_sub, drawings_app, drawings_rev, ir_sub, ir_app, ir_rev, ncr_open, ncr_closed, file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link, username, submission_date, submission_time) VALUES (''' + ",".join(["%s"] * 54) + ''', %s, %s, %s)''', data_values + (username, sub_date, sub_time))

        conn.commit()
        conn.close()
        background_tasks.add_task(send_telegram_alert, "submit", form_data.get("manager_name"), form_data.get("project_name"))
        background_tasks.add_task(create_notification, f"تم رفع تحديث جديد لمشروع {form_data.get('project_name')}")
        background_tasks.add_task(log_audit, username, "تحديث أسبوعي", f"تم إرسال تحديث لمشروع {form_data.get('project_name')}")

        success_html = """
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>تم بنجاح | المعماريون السعوديون</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css" rel="stylesheet">
        </head>
        <body style="background-color: #f4f6f9; display: flex; align-items: center; justify-content: center; height: 100vh; font-family: 'Segoe UI', Tahoma, sans-serif;">
            <div class="text-center bg-white p-5 rounded-4 shadow-sm" style="max-width: 500px; width: 100%;">
                <div style="font-size: 5rem; line-height: 1; margin-bottom: 20px;">✅</div>
                <h2 class="text-success fw-bold mb-3">تم إرسال التحديث بنجاح!</h2>
                <p class="text-muted mb-4">شكراً لك، تم حفظ بيانات المشروع في النظام المركزي للإدارة.</p>
                <div class="d-flex justify-content-center gap-3">
                    <a href="/update-portal" class="btn btn-primary px-4 fw-bold">رجوع للبوابة</a>
                    <a href="/logout" class="btn btn-outline-danger px-4 fw-bold">تسجيل الخروج</a>
                </div>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=success_html, status_code=200)

    except Exception as e:
        error_html = f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>خطأ | المعماريون السعوديون</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css" rel="stylesheet">
        </head>
        <body style="background-color: #f4f6f9; display: flex; align-items: center; justify-content: center; height: 100vh; font-family: 'Segoe UI', Tahoma, sans-serif;">
            <div class="text-center bg-white p-5 rounded-4 shadow-sm" style="max-width: 500px; width: 100%;">
                <div style="font-size: 5rem; line-height: 1; margin-bottom: 20px;">❌</div>
                <h2 class="text-danger fw-bold mb-3">عفواً، حدث خطأ أثناء الحفظ!</h2>
                <p class="text-muted mb-4 text-break">تفاصيل الخطأ: {str(e)}</p>
                <a href="/update-portal" class="btn btn-primary px-4 fw-bold">الرجوع والمحاولة مرة أخرى</a>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=error_html, status_code=500)

@app.get("/admin-analytics", response_class=HTMLResponse)
async def admin_analytics_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_analytics.html", {"admin_user": admin_user, "active_page": "analytics"})

@app.get("/api/analytics-data")
async def get_analytics_data(request: Request):
    if not request.cookies.get("super_admin_auth"): return {"error": "غير مصرح"}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT project_name, manager_name, project_type, current_data_date, 
                   contractor_val, consultant_val,
                   consultant_mods_count, contractor_mods_count,
                   consultant_mods_val, contractor_mods_val,
                   cons_inv_count, cont_inv_count, cons_inv_val, cont_inv_val,
                   drawings_sub, drawings_app, drawings_rev,
                   ir_sub, ir_app, ir_rev, ncr_open, ncr_closed,
                   consultant_mods_time, contractor_mods_time,
                   start_contractual, end_contractual, start_actual, end_expected,
                   plan_prog_cur, plan_prog_prev, act_prog_cur, act_prog_prev,
                   obstacles_data, eval_labor, eval_equip, eval_financial, eval_hse
            FROM project_updates
        ''')
        updates_cols = [desc[0] for desc in cursor.description]
        updates = [dict(zip(updates_cols, row)) for row in cursor.fetchall()]
        
        cursor.execute('SELECT manager_name, phone, email, profile_image FROM pm_directory')
        pm_cols = [desc[0] for desc in cursor.description]
        pms = [dict(zip(pm_cols, row)) for row in cursor.fetchall()]
        
        conn.close()
        return {"success": True, "updates": updates, "pms": pms}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/powerbi")
async def powerbi_feed():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]

if __name__ == "__main__":
    import uvicorn
    # uvicorn.run(app, host="0.0.0.0", port=8000)
