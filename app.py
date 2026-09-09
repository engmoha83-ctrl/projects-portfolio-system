import os
import json
import re
import secrets
import string
from urllib.parse import quote
from datetime import datetime, timedelta, date
from decimal import Decimal
from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
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

def get_db_connection():
    return psycopg2.connect(DB_URL)

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
        response.set_cookie(key="super_admin_auth", value=username, httponly=True, max_age=86400)
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
        scope = body.get("scope") or "project"          # "default" = قالب عام لكل المشاريع
        project_name = body.get("project_name") or None
        data = body.get("data") or {}
        payload = json.dumps(data, ensure_ascii=False)

        if scope == "default":
            project_name = None                          # القالب العام غير مرتبط بمشروع
        elif not project_name:
            return JSONResponse({"success": False, "error": "لا بد من اختيار مشروع لحفظ قالب مخصص"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()

        # لكل مشروع قالب مخصص واحد فقط، وقالب عام واحد فقط على مستوى النظام
        if not layout_id:
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

        if scope == "default":                            # قالب عام واحد فقط
            cursor.execute("UPDATE dashboard_layouts SET is_default = FALSE WHERE id <> %s", (row[0],))

        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, admin_user, "حفظ قالب داشبورد",
                                  f"{'قالب عام' if scope == 'default' else 'قالب مشروع ' + str(project_name)}: {name}")
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
    ends = [d for d in (row[3], row[5], row[6]) if d]
    start_ym = min(str(d)[:7] for d in starts) if starts else datetime.utcnow().strftime("%Y-%m")
    end_ym = max(str(d)[:7] for d in ends) if ends else _ym_add(start_ym, 11)
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
                             COALESCE(baseline_source, 'manual')
                      FROM cashflow_meta WHERE project_name = %s""", (project,))
    m = cursor.fetchone()
    if m:
        meta = {"contract_value": float(m[0] or 0), "revised_value": float(m[1] or 0),
                "start_month": m[2], "end_month": m[3], "locked": bool(m[4]),
                "updated_at": str(m[5]), "baseline_source": m[6], "is_new": False}
    else:
        meta = _suggest_meta(project)
        meta.update({"locked": False, "updated_at": None, "baseline_source": "manual", "is_new": True})
    meta["baseline_source_label"] = BASELINE_SOURCES.get(meta["baseline_source"], BASELINE_SOURCES["manual"])

    cursor.execute("""SELECT ym, COALESCE(wk, 0), plan_amount, plan_pct, act_pct, act_amount, note
                      FROM cashflow_rows WHERE project_name = %s ORDER BY ym, COALESCE(wk, 0)""",
                   (project,))
    rows, weeks = {}, {}
    for r in cursor.fetchall():
        rec = {"ym": r[0], "wk": int(r[1] or 0),
               "plan_amount": None if r[2] is None else float(r[2]),
               "plan_pct":    None if r[3] is None else float(r[3]),
               "act_pct":     None if r[4] is None else float(r[4]),
               "act_amount":  None if r[5] is None else float(r[5]),
               "note": r[6]}
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
                             "act_pct": None, "act_amount": None, "note": None})
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

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO cashflow_meta
                            (project_name, contract_value, revised_value, start_month, end_month, locked,
                             baseline_source, updated_by, updated_at)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                          ON CONFLICT (project_name) DO UPDATE SET
                            contract_value=EXCLUDED.contract_value, revised_value=EXCLUDED.revised_value,
                            start_month=EXCLUDED.start_month, end_month=EXCLUDED.end_month,
                            locked=EXCLUDED.locked, baseline_source=EXCLUDED.baseline_source,
                            updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                       (project, cv, rv, sm, em, locked, src, admin_user))
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

        fields, values = [], []
        for k in ("plan_amount", "plan_pct", "act_pct", "act_amount", "note"):
            if k in b:
                fields.append(k)
                v = b[k]
                if k != "note":
                    v = None if (v is None or v == "") else float(v)
                values.append(v)
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
            fields, values = [], []
            for k in ("plan_amount", "plan_pct", "act_pct", "act_amount", "note"):
                if k in c:
                    fields.append(k)
                    v = c[k]
                    if k != "note":
                        v = None if (v is None or v == "") else float(v)
                    values.append(v)
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
                             COALESCE(baseline_source,'manual')
                      FROM cashflow_meta WHERE project_name = %s""", (project,))
    m = cursor.fetchone()
    meta = None if not m else {"contract_value": float(m[0] or 0), "revised_value": float(m[1] or 0),
                               "start_month": m[2], "end_month": m[3], "locked": bool(m[4]),
                               "baseline_source": m[5]}
    cursor.execute("""SELECT ym, COALESCE(wk,0), plan_amount, plan_pct, act_pct, act_amount, note
                      FROM cashflow_rows WHERE project_name = %s ORDER BY ym, COALESCE(wk,0)""", (project,))
    rows = [[r[0], int(r[1] or 0)] + [None if v is None else float(v) for v in r[2:6]] + [r[6]]
            for r in cursor.fetchall()]
    cursor.execute("""INSERT INTO cashflow_snapshots (project_name, name, payload, created_by)
                      VALUES (%s,%s,%s,%s) RETURNING id""",
                   (project, (name or "نسخة")[:120], json.dumps({"meta": meta, "rows": rows}), user))
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
                                 updated_by, updated_at)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                           (project, r[0], r[1], r[2], r[3], r[4], r[5], r[6], admin_user))
        m = payload.get("meta")
        if m:
            cursor.execute("""INSERT INTO cashflow_meta
                                (project_name, contract_value, revised_value, start_month, end_month,
                                 locked, baseline_source, updated_by, updated_at)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                              ON CONFLICT (project_name) DO UPDATE SET
                                contract_value=EXCLUDED.contract_value, revised_value=EXCLUDED.revised_value,
                                start_month=EXCLUDED.start_month, end_month=EXCLUDED.end_month,
                                locked=EXCLUDED.locked, baseline_source=EXCLUDED.baseline_source,
                                updated_by=EXCLUDED.updated_by, updated_at=NOW()""",
                           (project, m.get("contract_value") or 0, m.get("revised_value") or 0,
                            m.get("start_month"), m.get("end_month"), bool(m.get("locked")),
                            m.get("baseline_source") or "manual", admin_user))
        conn.commit(); conn.close()
        background_tasks.add_task(log_audit, admin_user, "استرجاع نسخة تدفق نقدي",
                                  f"مشروع {project} — {snap_name}")
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
        by_month, by_week = {}, {}
        for d, pct in cursor.fetchall():
            ym = str(d)[:7]
            v = float(pct or 0)
            v = round((v * 100 if v <= 1.0001 else v), 2)
            by_month[ym] = v                                   # آخر قيمة في الشهر
            day = int(str(d)[8:10] or 1)
            wk = min(WEEKS_PER_MONTH, max(1, (day + 6) // 7))   # 1-7→1، 8-14→2، 15-21→3، الباقي→4
            by_week.setdefault(ym, {})[str(wk)] = v             # آخر قيمة داخل الأسبوع
        conn.close()
        return {"success": True, "months": by_month, "weeks": by_week}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/cashflow/data")
async def api_cashflow_data(project: str, request: Request):
    """مصدر بيانات جاهز للداشبورد: منحنى مخطط وفعلي بالنسبة والمبلغ."""
    if not request.cookies.get("super_admin_auth"):
        return JSONResponse({"success": False, "error": "غير مصرح"}, status_code=403)
    try:
        p = _cashflow_payload(project)
        cv = p["meta"]["contract_value"] or 0
        rv = p["meta"]["revised_value"] or cv
        out, cum_plan = [], 0.0
        for r in p["rows"]:
            cum_plan += float(r["plan_amount"] or 0)
            plan_pct = r["plan_pct"] if r["plan_pct"] is not None else (cum_plan / cv * 100 if cv else 0)
            act_pct = r["act_pct"]
            act_amt = r["act_amount"] if r["act_amount"] is not None else (
                (act_pct or 0) / 100 * rv if act_pct is not None else None)
            out.append({"ym": r["ym"], "plan_amount": float(r["plan_amount"] or 0),
                        "plan_cum": round(cum_plan, 2), "plan_pct": round(float(plan_pct or 0), 2),
                        "act_pct": None if act_pct is None else round(float(act_pct), 2),
                        "act_cum": None if act_amt is None else round(float(act_amt), 2)})
        return {"success": True, "project": project, "meta": p["meta"], "series": out}
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
        HR = 7                       # صف مجموعات الأعمدة
        HR2 = 8                      # صف أسماء الأعمدة
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
            ws.cell(row=rw, column=4, value=f"=IF($B$3=0,0,C{rw}/$B$3)").number_format = "0.0%"

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

        ws.cell(row=tot + 2, column=1, value="فرق مجموع الخطة عن قيمة العقد").font = f_lab
        ws.cell(row=tot + 2, column=3, value=f"=B{tot}-$B$3").number_format = "#,##0;-#,##0;0"

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
            
        def process_percentage(val):
            if val is None or str(val).strip() == "": return 0.0
            try: return float(val) / 100.0
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
            clean_num(form_data.get("eval_labor")), clean_num(form_data.get("eval_equip")), clean_num(form_data.get("eval_financial")), clean_num(form_data.get("eval_hse")),
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
