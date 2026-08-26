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

# قاموس ترجمة أسماء الأعمدة للعربية في لوحة التحكم
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
            
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        pass

cloudinary.config(cloud_name="wu5wjket", api_key="241572682214285", api_secret="K-susQH7Qh5lMeD7nwtdznSYnnU")

def upload_to_cloudinary(file: UploadFile):
    try:
        if file.content_type and file.content_type.startswith("image/"):
            result = cloudinary.uploader.upload(file.file, resource_type="image", quality="auto", fetch_format="auto", width=1920, crop="limit")
        else:
            result = cloudinary.uploader.upload(file.file, resource_type="auto")
        return result.get("secure_url")
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
    else:
        return templates.TemplateResponse(request, "login.html", {"error": "اسم المستخدم أو كلمة المرور غير صحيحة"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("auth_user")
    return response

# ==========================================
# 4. لوحة الإدارة والدليل
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})

@app.post("/admin")
async def do_admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ADMIN_ACCOUNTS = {"admin_mohamed": "admin_2026", "admin_assistant": "admin_1234"}
    if username in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[username] == password:
        response = RedirectResponse(url="/admin-dashboard", status_code=303)
        response.set_cookie(key="super_admin_auth", value=username, max_age=86400)
        return response
    return templates.TemplateResponse(request, "admin_login.html", {"error": "بيانات الدخول غير صحيحة"})

@app.get("/admin-dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin", status_code=303)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates ORDER BY submission_date DESC, id DESC")
    original_columns = [desc[0] for desc in cursor.description]
    raw_rows = cursor.fetchall()
    conn.close()
    
    # ترجمة الأعمدة للعرض فقط
    translated_columns = [ARABIC_COLUMNS.get(col, col) for col in original_columns]
    
    rows = []
    for row in raw_rows:
        row_list = list(row)
        for i, col in enumerate(original_columns):
            if col == 'obstacles_data' and row_list[i] is not None:
                if isinstance(row_list[i], (dict, list)):
                    row_list[i] = json.dumps(row_list[i], ensure_ascii=False)
        rows.append(row_list)
    
    return templates.TemplateResponse(request, "admin_dashboard.html", {
        "original_columns": original_columns, "translated_columns": translated_columns, 
        "rows": rows, "admin_user": admin_user
    })

@app.post("/api/update-cell")
async def update_cell(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return {"success": False, "error": "غير مصرح"}
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

@app.get("/admin-logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("super_admin_auth")
    return response

@app.get("/admin-directory", response_class=HTMLResponse)
async def admin_directory_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, manager_name, project_name, phone, email, location_link, profile_image FROM pm_directory ORDER BY id ASC")
    pms = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse(request, "admin_directory.html", {"pms": pms, "admin_user": admin_user})

@app.post("/admin-update-pm")
async def admin_update_pm(request: Request, pm_id: int = Form(...), phone: str = Form(""), email: str = Form(""), location_link: str = Form(""), profile_image: UploadFile = File(None)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)
    update_query = "UPDATE pm_directory SET phone=%s, email=%s, location_link=%s"
    params = [phone, email, location_link]
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

# ==========================================
# 5. بوابة التحديث والاستبدال الذكي (Update Portal)
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
        response = RedirectResponse(url="/login")
        response.delete_cookie("auth_user")
        return response
    return templates.TemplateResponse(request, "index.html", {"username": auth_user, "manager_name": user[0], "project_name": user[1]})

@app.post("/submit")
async def submit_data(request: Request, background_tasks: BackgroundTasks):
    auth_user = request.cookies.get("auth_user")
    if not auth_user: return {"error": "انتهت الجلسة، يرجى تسجيل الدخول."}

    form_data = await request.form()
    today_date = date.today().isoformat()
    username = form_data.get("username")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. التحقق مما إذا كان هناك تحديث لنفس المشروع اليوم
        cursor.execute("SELECT id, file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link FROM project_updates WHERE username=%s AND submission_date=%s", (username, today_date))
        existing_record = cursor.fetchone()

        # 2. معالجة الصور الذكية (نأخذ الجديد، وإن لم يوجد نحتفظ بالقديم)
        attachments = [form_data.get(f"attachment_{i}") for i in range(1, 5)]
        links = ["لا يوجد مرفق"] * 4
        for i in range(4):
            if attachments[i] and getattr(attachments[i], "filename", None):
                url = upload_to_cloudinary(attachments[i])
                if url: links[i] = url
            elif existing_record: # الاحتفاظ بالصورة القديمة لو كانت موجودة ولم يرفع جديد
                links[i] = existing_record[i+1]

        master_plan = form_data.get("master_plan")
        isometric = form_data.get("isometric")
        
        master_plan_link = "لا يوجد مرفق"
        if master_plan and master_plan.filename: master_plan_link = upload_to_cloudinary(master_plan) or "لا يوجد مرفق"
        elif existing_record: master_plan_link = existing_record[5]

        isometric_link = "لا يوجد مرفق"
        if isometric and isometric.filename: isometric_link = upload_to_cloudinary(isometric) or "لا يوجد مرفق"
        elif existing_record: isometric_link = existing_record[6]

        def process_percentage(val):
            try: return float(val) / 100.0 if val else 0.0
            except ValueError: return 0.0

        act_prog_cur = process_percentage(form_data.get("act_prog_cur"))
        act_prog_prev = process_percentage(form_data.get("act_prog_prev"))
        plan_prog_cur = process_percentage(form_data.get("plan_prog_cur"))
        plan_prog_prev = process_percentage(form_data.get("plan_prog_prev"))

        # تجهيز البيانات
        data_values = (
            form_data.get("manager_name"), form_data.get("project_name"), form_data.get("project_desc"), form_data.get("project_type"), form_data.get("current_data_date"),
            form_data.get("project_owner"), form_data.get("project_developer"), form_data.get("project_contractor"),
            form_data.get("consultant_val") or 0, form_data.get("contractor_val") or 0,
            form_data.get("consultant_mods_count") or 0, form_data.get("consultant_mods_val") or 0, form_data.get("consultant_mods_time"), form_data.get("consultant_mods_end_date"),
            form_data.get("contractor_mods_count") or 0, form_data.get("contractor_mods_val") or 0, form_data.get("contractor_mods_time"), form_data.get("contractor_mods_end_date"),
            form_data.get("cons_inv_count") or 0, form_data.get("cons_inv_val") or 0, form_data.get("cons_inv_date"),
            form_data.get("cont_inv_count") or 0, form_data.get("cont_inv_val") or 0, form_data.get("cont_inv_date"),
            form_data.get("start_contractual"), form_data.get("end_contractual"), form_data.get("start_actual"), form_data.get("end_expected"),
            act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev,
            form_data.get("works_completed"), form_data.get("works_ongoing"), form_data.get("works_planned"), form_data.get("obstacles_json"),
            form_data.get("eval_labor") or 0, form_data.get("eval_equip") or 0, form_data.get("eval_financial") or 0, form_data.get("eval_hse") or 0,
            form_data.get("drawings_sub") or 0, form_data.get("drawings_app") or 0, form_data.get("drawings_rev") or 0,
            form_data.get("ir_sub") or 0, form_data.get("ir_app") or 0, form_data.get("ir_rev") or 0,
            form_data.get("ncr_open") or 0, form_data.get("ncr_closed") or 0,
            links[0], links[1], links[2], links[3], master_plan_link, isometric_link
        )

        if existing_record:
            # تحديث السجل الحالي لنفس اليوم
            update_sql = '''
                UPDATE project_updates SET
                    manager_name=%s, project_name=%s, project_desc=%s, project_type=%s, current_data_date=%s,
                    project_owner=%s, project_developer=%s, project_contractor=%s,
                    consultant_val=%s, contractor_val=%s, 
                    consultant_mods_count=%s, consultant_mods_val=%s, consultant_mods_time=%s, consultant_mods_end_date=%s,
                    contractor_mods_count=%s, contractor_mods_val=%s, contractor_mods_time=%s, contractor_mods_end_date=%s,
                    cons_inv_count=%s, cons_inv_val=%s, cons_inv_date=%s,
                    cont_inv_count=%s, cont_inv_val=%s, cont_inv_date=%s,
                    start_contractual=%s, end_contractual=%s, start_actual=%s, end_expected=%s,
                    act_prog_cur=%s, act_prog_prev=%s, plan_prog_cur=%s, plan_prog_prev=%s,
                    works_completed=%s, works_ongoing=%s, works_planned=%s, obstacles_data=%s,
                    eval_labor=%s, eval_equip=%s, eval_financial=%s, eval_hse=%s,
                    drawings_sub=%s, drawings_app=%s, drawings_rev=%s,
                    ir_sub=%s, ir_app=%s, ir_rev=%s,
                    ncr_open=%s, ncr_closed=%s,
                    file_link_1=%s, file_link_2=%s, file_link_3=%s, file_link_4=%s, master_plan_link=%s, isometric_link=%s
                WHERE id = %s
            '''
            cursor.execute(update_sql, data_values + (existing_record[0],))
        else:
            # إضافة سجل جديد ليوم جديد
            insert_sql = '''
                INSERT INTO project_updates (
                    manager_name, project_name, project_desc, project_type, current_data_date,
                    project_owner, project_developer, project_contractor,
                    consultant_val, contractor_val, 
                    consultant_mods_count, consultant_mods_val, consultant_mods_time, consultant_mods_end_date,
                    contractor_mods_count, contractor_mods_val, contractor_mods_time, contractor_mods_end_date,
                    cons_inv_count, cons_inv_val, cons_inv_date,
                    cont_inv_count, cont_inv_val, cont_inv_date,
                    start_contractual, end_contractual, start_actual, end_expected,
                    act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev,
                    works_completed, works_ongoing, works_planned, obstacles_data,
                    eval_labor, eval_equip, eval_financial, eval_hse,
                    drawings_sub, drawings_app, drawings_rev,
                    ir_sub, ir_app, ir_rev,
                    ncr_open, ncr_closed,
                    file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link,
                    username, submission_date
                ) VALUES (''' + ",".join(["%s"] * 54) + ''', %s, %s)
            '''
            cursor.execute(insert_sql, data_values + (username, today_date))

        conn.commit()
        conn.close()
        
        background_tasks.add_task(send_telegram_alert, "submit", form_data.get("manager_name"), form_data.get("project_name"))
        return {"message": "تم حفظ التحديث (واستبدال بيانات اليوم إن وجدت) بنجاح!"}
    except Exception as e:
        return {"error": f"حدث خطأ أثناء الحفظ: {e}"}

@app.get("/api/powerbi")
async def powerbi_feed():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    data = [dict(zip(columns, row)) for row in rows]
    return data
