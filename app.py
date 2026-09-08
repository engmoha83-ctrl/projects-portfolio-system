import os
import json
import secrets
import string
from urllib.parse import quote
from datetime import datetime, timedelta, date
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

@app.post("/api/save-dashboard-layout")
async def save_dashboard_layout(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        layout_data = data.get("layout")
        global_settings = data.get("settings")
        username = request.cookies.get("auth_user")
        if not username:
            return JSONResponse({"success": False, "message": "غير مصرح لك"})
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE pm_directory SET custom_dashboard_layout = %s WHERE username = %s", (json.dumps({"layout": layout_data, "settings": global_settings}), username))
        conn.commit()
        conn.close()
        background_tasks.add_task(log_audit, username, "تعديل واجهة الداشبورد", "تم حفظ تصميم جديد للداشبورد التفاعلي")
        return JSONResponse({"success": True, "message": "تم حفظ التصميم بنجاح"})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

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
async def get_notifications():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 20")
        notifs = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE is_read = FALSE")
        unread_count = cursor.fetchone()[0]
        conn.close()
        return JSONResponse({"success": True, "notifications": [dict(n) for n in notifs], "unread_count": unread_count})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

@app.post("/api/notifications/mark-read")
async def mark_notifications_read():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE notifications SET is_read = TRUE WHERE is_read = FALSE")
        conn.commit()
        conn.close()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

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
