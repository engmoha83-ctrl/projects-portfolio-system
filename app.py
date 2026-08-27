import os
import json
from datetime import datetime, timedelta, date
from fastapi import FastAPI, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import psycopg2
import cloudinary
import cloudinary.uploader
import requests

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. الإعدادات الأساسية 
# ==========================================
DB_URL = "postgresql://postgres.rofppixfbshgdkhqoevo:Saudi_Architects2026@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
TELEGRAM_BOT_TOKEN = "8966674077:AAEF72u60b8wjWapVBSbnsBZqhWNwkYcvDA"
TELEGRAM_CHAT_ID = "5838048978"

def get_db_connection():
    return psycopg2.connect(DB_URL)

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
    'master_plan_link': 'المخطط العام', 'isometric_link': 'أيزومتريك', 'submission_date': 'تاريخ الإرسال'
}

# ==========================================
# 2. إشعارات التليجرام و Cloudinary
# ==========================================
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

cloudinary.config(cloud_name="wu5wjket", api_key="241572682214285", api_secret="K-susQH7Qh5lMeD7nwtdznSYnnU")
def upload_to_cloudinary(file: UploadFile):
    try:
        if file.content_type and file.content_type.startswith("image/"):
            return cloudinary.uploader.upload(file.file, resource_type="image", quality="auto", fetch_format="auto", width=1920, crop="limit").get("secure_url")
        return cloudinary.uploader.upload(file.file, resource_type="auto").get("secure_url")
    except:
        return None

# ==========================================
# 3. تسجيل الدخول
# ==========================================
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
    cursor.execute("SELECT manager_name, project_name FROM users WHERE username=%s AND password=%s", (username, password))
    user = cursor.fetchone()
    conn.close()
    if user:
        background_tasks.add_task(send_telegram_alert, "login", user[0], user[1])
        response = RedirectResponse(url="/update-portal", status_code=303)
        response.set_cookie(key="auth_user", value=username)
        return response
    return templates.TemplateResponse(request, "login.html", {"error": "اسم المستخدم أو كلمة المرور غير صحيحة"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("auth_user")
    return response

# ==========================================
# 4. لوحات الإدارة
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})

@app.post("/admin")
async def do_admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ADMIN_ACCOUNTS = {"admin_mohamed": "admin_2026", "admin_assistant": "admin_1234"}
    if username in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[username] == password:
        # التعديل هنا: التوجيه أصبح لصفحة البوابة المركزية بدلاً من الداشبورد مباشرة
        response = RedirectResponse(url="/admin-hub", status_code=303) 
        response.set_cookie(key="super_admin_auth", value=username, max_age=86400)
        return response
    return templates.TemplateResponse(request, "admin_login.html", {"error": "بيانات الدخول غير صحيحة"})
    
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
    return templates.TemplateResponse(request, "admin_dashboard.html", {"original_columns": original_columns, "translated_columns": translated_columns, "rows": rows, "admin_user": admin_user})

# ==========================================
# المسار الجديد للبوابة المركزية (Admin Hub)
# ==========================================
@app.get("/admin-hub", response_class=HTMLResponse)
async def admin_hub_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_hub.html", {"admin_user": admin_user})
    
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
    return templates.TemplateResponse(request, "admin_dashboard.html", {"original_columns": original_columns, "translated_columns": translated_columns, "rows": rows, "admin_user": admin_user})

@app.post("/api/update-cell")
async def update_cell(request: Request):
    if not request.cookies.get("super_admin_auth"): return {"success": False, "error": "غير مصرح"}
    data = await request.json()
    row_id, column, value = data.get("id"), data.get("column"), data.get("value")
    if column == 'obstacles_data' and str(value).strip() == "": value = "[]"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE project_updates SET {column} = %s WHERE id = %s", (value, row_id))
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/admin-directory", response_class=HTMLResponse)
async def admin_directory_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, manager_name, project_name, phone, email, location_link, profile_image FROM pm_directory ORDER BY id ASC")
    pms = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(request, "admin_directory.html", {"pms": pms, "admin_user": admin_user})

@app.post("/admin-update-pm")
async def admin_update_pm(request: Request, pm_id: int = Form(...), phone: str = Form(""), email: str = Form(""), location_link: str = Form(""), profile_image: UploadFile = File(None)):
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
    return RedirectResponse(url="/admin-directory", status_code=303)

@app.get("/admin-logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("super_admin_auth")
    return response

# ==========================================
# 5. المعرض المرئي للمشاريع (Gallery) بالتحديث الجديد
# ==========================================
@app.get("/admin-gallery", response_class=HTMLResponse)
async def admin_gallery_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": return RedirectResponse(url="/admin-dashboard", status_code=303)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM project_updates WHERE project_name IS NOT NULL ORDER BY project_name")
    projects = [row[0] for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "admin_gallery.html", {"projects": projects, "admin_user": admin_user})

# دالة جديدة لجلب التواريخ المتاحة للمشروع المختار
@app.get("/api/project-dates")
async def get_project_dates(project: str, request: Request):
    if request.cookies.get("super_admin_auth") != "admin_mohamed": return {"error": "غير مصرح"}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT current_data_date FROM project_updates WHERE project_name = %s AND current_data_date IS NOT NULL ORDER BY current_data_date DESC", (project,))
    dates = [row[0] for row in cursor.fetchall()]
    conn.close()
    return {"success": True, "dates": dates}

# دالة المعرض معدلة لتقبل (المشروع + تاريخ البيانات)
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

# ==========================================
# 6. بوابة التحديث والاستبدال الذكي 
# ==========================================
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
    if not auth_user: return {"error": "انتهت الجلسة، يرجى تسجيل الدخول."}
    form_data = await request.form()
    username = form_data.get("username")
    # ==========================================
# 7. لوحة المؤشرات التفاعلية (Analytics Dashboard)
# ==========================================
@app.get("/admin-analytics", response_class=HTMLResponse)
async def admin_analytics_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user: return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_analytics.html", {"admin_user": admin_user})

@app.get("/api/analytics-data")
async def get_analytics_data(request: Request):
    if not request.cookies.get("super_admin_auth"): return {"error": "غير مصرح"}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # جلب بيانات المشاريع الأساسية للتحليل
        cursor.execute('''
            SELECT project_name, manager_name, project_type, current_data_date, 
                   contractor_val, plan_prog_cur, act_prog_cur, ncr_open,
                   eval_labor, eval_equip, eval_financial, eval_hse
            FROM project_updates
        ''')
        updates_cols = [desc[0] for desc in cursor.description]
        updates = [dict(zip(updates_cols, row)) for row in cursor.fetchall()]
        
        # جلب بيانات مديري المشاريع لبطاقة التعريف
        cursor.execute('SELECT manager_name, phone, email, profile_image FROM pm_directory')
        pm_cols = [desc[0] for desc in cursor.description]
        pms = [dict(zip(pm_cols, row)) for row in cursor.fetchall()]
        
        conn.close()
        return {"success": True, "updates": updates, "pms": pms}
    except Exception as e:
        return {"success": False, "error": str(e)}

    # ========================================================
    # التحديث الذكي: حساب الـ Data Date ووقت الإرسال بدقة
    # ========================================================
    ksa_time = datetime.utcnow() + timedelta(hours=3)
    # تسجيل وقت الإرسال الفعلي بالدقيقة
    submission_timestamp = ksa_time.strftime("%Y-%m-%d | %I:%M %p") 
    
    wd = ksa_time.weekday() # الإثنين=0, الأحد=6
    if wd == 2: days_to_add = 0             # الأربعاء
    elif wd == 3: days_to_add = -1          # الخميس
    elif wd == 4: days_to_add = -2          # الجمعة
    elif wd == 5: days_to_add = -3          # السبت
    elif wd == 6 and ksa_time.hour < 9: days_to_add = -4  # الأحد قبل 9 صباحاً
    elif wd == 6 and ksa_time.hour >= 9: days_to_add = 3  # الأحد بعد 9 صباحاً
    elif wd == 0: days_to_add = 2           # الإثنين
    elif wd == 1: days_to_add = 1           # الثلاثاء
    
    # تثبيت تاريخ البيانات ليكون الأربعاء الخاص بهذه الدورة
    calculated_data_date = (ksa_time + timedelta(days=days_to_add)).date().isoformat()
    # ========================================================

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # التحديث الذكي: البحث عن السجل بناءً على دورة التحديث (Data Date) وليس تاريخ الإرسال
        cursor.execute("SELECT id, file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link FROM project_updates WHERE username=%s AND current_data_date=%s", (username, calculated_data_date))
        existing_record = cursor.fetchone()

        attachments = [form_data.get(f"attachment_{i}") for i in range(1, 5)]
        links = ["لا يوجد مرفق"] * 4
        for i in range(4):
            if attachments[i] and getattr(attachments[i], "filename", None):
                links[i] = upload_to_cloudinary(attachments[i]) or "لا يوجد مرفق"
            elif existing_record: links[i] = existing_record[i+1]

        master_plan = form_data.get("master_plan")
        isometric = form_data.get("isometric")
        master_plan_link = upload_to_cloudinary(master_plan) if master_plan and getattr(master_plan, "filename", None) else (existing_record[5] if existing_record else "لا يوجد مرفق")
        isometric_link = upload_to_cloudinary(isometric) if isometric and getattr(isometric, "filename", None) else (existing_record[6] if existing_record else "لا يوجد مرفق")

        def process_percentage(val):
            try: return float(val) / 100.0 if val else 0.0
            except ValueError: return 0.0

        data_values = (
            form_data.get("manager_name"), form_data.get("project_name"), form_data.get("project_desc"), form_data.get("project_type"), 
            calculated_data_date, # الاعتماد المطلق على تاريخ السيرفر المحسوب
            form_data.get("project_owner"), form_data.get("project_developer"), form_data.get("project_contractor"),
            form_data.get("consultant_val") or 0, form_data.get("contractor_val") or 0,
            form_data.get("consultant_mods_count") or 0, form_data.get("consultant_mods_val") or 0, form_data.get("consultant_mods_time"), form_data.get("consultant_mods_end_date"),
            form_data.get("contractor_mods_count") or 0, form_data.get("contractor_mods_val") or 0, form_data.get("contractor_mods_time"), form_data.get("contractor_mods_end_date"),
            form_data.get("cons_inv_count") or 0, form_data.get("cons_inv_val") or 0, form_data.get("cons_inv_date"),
            form_data.get("cont_inv_count") or 0, form_data.get("cont_inv_val") or 0, form_data.get("cont_inv_date"),
            form_data.get("start_contractual"), form_data.get("end_contractual"), form_data.get("start_actual"), form_data.get("end_expected"),
            process_percentage(form_data.get("act_prog_cur")), process_percentage(form_data.get("act_prog_prev")), process_percentage(form_data.get("plan_prog_cur")), process_percentage(form_data.get("plan_prog_prev")),
            form_data.get("works_completed"), form_data.get("works_ongoing"), form_data.get("works_planned"), form_data.get("obstacles_json"),
            form_data.get("eval_labor") or 0, form_data.get("eval_equip") or 0, form_data.get("eval_financial") or 0, form_data.get("eval_hse") or 0,
            form_data.get("drawings_sub") or 0, form_data.get("drawings_app") or 0, form_data.get("drawings_rev") or 0,
            form_data.get("ir_sub") or 0, form_data.get("ir_app") or 0, form_data.get("ir_rev") or 0,
            form_data.get("ncr_open") or 0, form_data.get("ncr_closed") or 0,
            links[0], links[1], links[2], links[3], master_plan_link, isometric_link
        )

        if existing_record:
            # تحديث السجل مع تسجيل وقت التعديل الجديد في submission_date
            cursor.execute('''UPDATE project_updates SET manager_name=%s, project_name=%s, project_desc=%s, project_type=%s, current_data_date=%s, project_owner=%s, project_developer=%s, project_contractor=%s, consultant_val=%s, contractor_val=%s, consultant_mods_count=%s, consultant_mods_val=%s, consultant_mods_time=%s, consultant_mods_end_date=%s, contractor_mods_count=%s, contractor_mods_val=%s, contractor_mods_time=%s, contractor_mods_end_date=%s, cons_inv_count=%s, cons_inv_val=%s, cons_inv_date=%s, cont_inv_count=%s, cont_inv_val=%s, cont_inv_date=%s, start_contractual=%s, end_contractual=%s, start_actual=%s, end_expected=%s, act_prog_cur=%s, act_prog_prev=%s, plan_prog_cur=%s, plan_prog_prev=%s, works_completed=%s, works_ongoing=%s, works_planned=%s, obstacles_data=%s, eval_labor=%s, eval_equip=%s, eval_financial=%s, eval_hse=%s, drawings_sub=%s, drawings_app=%s, drawings_rev=%s, ir_sub=%s, ir_app=%s, ir_rev=%s, ncr_open=%s, ncr_closed=%s, file_link_1=%s, file_link_2=%s, file_link_3=%s, file_link_4=%s, master_plan_link=%s, isometric_link=%s, submission_date=%s WHERE id = %s''', data_values + (submission_timestamp, existing_record[0],))
        else:
            cursor.execute('''INSERT INTO project_updates (manager_name, project_name, project_desc, project_type, current_data_date, project_owner, project_developer, project_contractor, consultant_val, contractor_val, consultant_mods_count, consultant_mods_val, consultant_mods_time, consultant_mods_end_date, contractor_mods_count, contractor_mods_val, contractor_mods_time, contractor_mods_end_date, cons_inv_count, cons_inv_val, cons_inv_date, cont_inv_count, cont_inv_val, cont_inv_date, start_contractual, end_contractual, start_actual, end_expected, act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev, works_completed, works_ongoing, works_planned, obstacles_data, eval_labor, eval_equip, eval_financial, eval_hse, drawings_sub, drawings_app, drawings_rev, ir_sub, ir_app, ir_rev, ncr_open, ncr_closed, file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link, username, submission_date) VALUES (''' + ",".join(["%s"] * 54) + ''', %s, %s)''', data_values + (username, submission_timestamp))

        conn.commit()
        conn.close()
        background_tasks.add_task(send_telegram_alert, "submit", form_data.get("manager_name"), form_data.get("project_name"))
        return {"message": "تم حفظ التحديث بنجاح!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/powerbi")
async def powerbi_feed():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]
