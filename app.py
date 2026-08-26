import os
import json
from datetime import datetime, timedelta, date
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import psycopg2
import cloudinary
import cloudinary.uploader
import requests  # مكتبة جديدة لإرسال الإشعارات

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. الإعدادات الأساسية (بياناتك)
# ==========================================
DB_URL = "postgresql://postgres.rofppixfbshgdkhqoevo:Saudi_Architects2026@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"

# إعدادات إشعارات تليجرام
TELEGRAM_BOT_TOKEN = "8966674077:AAEF72u60b8wjWapVBSbnsBZqhWNwkYcvDA"
TELEGRAM_CHAT_ID = "5838048978"

def get_db_connection():
    return psycopg2.connect(DB_URL)

# ==========================================
# 2. دالة إرسال إشعارات التليجرام
# ==========================================
def send_telegram_alert(action_type, manager, project):
    try:
        # حساب وقت السعودية (UTC + 3)
        ksa_time = datetime.utcnow() + timedelta(hours=3)
        time_str = ksa_time.strftime("%Y-%m-%d | %I:%M %p")
        
        if action_type == "login":
            msg = f"🟢 *تسجيل دخول جديد*\n\n👤 المدير: {manager}\n🏢 المشروع: {project}\n🕒 الوقت: {time_str}"
        elif action_type == "submit":
            msg = f"✅ *تم إرسال تحديث أسبوعي*\n\n👤 المدير: {manager}\n🏢 المشروع: {project}\n🕒 الوقت: {time_str}"
            
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload, timeout=5)  # timeout لعدم تعطيل السيرفر
    except Exception as e:
        print(f"خطأ في إرسال الإشعار: {e}")

# ==========================================
# 3. إعدادات Cloudinary
# ==========================================
cloudinary.config(
  cloud_name = "wu5wjket",
  api_key = "241572682214285",
  api_secret = "K-susQH7Qh5lMeD7nwtdznSYnnU"
)

def upload_to_cloudinary(file: UploadFile):
    try:
        if file.content_type and file.content_type.startswith("image/"):
            result = cloudinary.uploader.upload(
                file.file, resource_type="image", quality="auto", fetch_format="auto", width=1920, crop="limit"
            )
        else:
            result = cloudinary.uploader.upload(file.file, resource_type="auto")
        return result.get("secure_url")
    except Exception as e:
        print(f"خطأ في الرفع: {e}")
        return None

# ==========================================
# 4. الصفحة الرئيسية 
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def main_landing_page(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})

# ==========================================
# 5. تسجيل دخول المديرين (مع إشعار التليجرام)
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})

@app.post("/login")
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT manager_name, project_name FROM users WHERE username=%s AND password=%s", (username, password))
    user = cursor.fetchone()
    conn.close()

    if user:
        # إرسال إشعار تليجرام فور تسجيل الدخول الناجح
        send_telegram_alert("login", user[0], user[1])
        
        response = RedirectResponse(url="/update-portal", status_code=303)
        response.set_cookie(key="auth_user", value=username, max_age=86400)
        return response
    else:
        return templates.TemplateResponse(request, "login.html", {"error": "اسم المستخدم أو كلمة المرور غير صحيحة"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("auth_user")
    return response

# ==========================================
# 6. لوحة تحكم الإدارة
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})

@app.post("/admin")
async def do_admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ADMIN_ACCOUNTS = {
        "admin_mohamed": "admin_2026",
        "admin_assistant": "admin_1234"
    }
    if username in ADMIN_ACCOUNTS and ADMIN_ACCOUNTS[username] == password:
        response = RedirectResponse(url="/admin-dashboard", status_code=303)
        response.set_cookie(key="super_admin_auth", value="authorized", max_age=86400)
        return response
    else:
        return templates.TemplateResponse(request, "admin_login.html", {"error": "بيانات الدخول غير صحيحة"})

@app.get("/admin-dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return RedirectResponse(url="/admin", status_code=303)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates ORDER BY id DESC")
    columns = [desc[0] for desc in cursor.description]
    raw_rows = cursor.fetchall()
    conn.close()
    
    rows = []
    for row in raw_rows:
        row_list = list(row)
        for i, col in enumerate(columns):
            if col == 'obstacles_data' and row_list[i] is not None:
                if isinstance(row_list[i], (dict, list)):
                    row_list[i] = json.dumps(row_list[i], ensure_ascii=False)
        rows.append(row_list)
    
    return templates.TemplateResponse(request, "admin_dashboard.html", {"columns": columns, "rows": rows})

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

# ==========================================
# 7. بوابة التحديث للمديرين (Update Portal)
# ==========================================
@app.get("/update-portal", response_class=HTMLResponse)
async def update_portal_page(request: Request):
    auth_user = request.cookies.get("auth_user")
    if not auth_user:
        return RedirectResponse(url="/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT manager_name, project_name FROM users WHERE username=%s", (auth_user,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        response = RedirectResponse(url="/login")
        response.delete_cookie("auth_user")
        return response

    return templates.TemplateResponse(request, "index.html", {
        "username": auth_user, "manager_name": user[0], "project_name": user[1]
    })

@app.post("/submit")
async def submit_data(request: Request):
    auth_user = request.cookies.get("auth_user")
    if not auth_user:
        return {"error": "انتهت الجلسة، يرجى تسجيل الدخول."}

    form_data = await request.form()
    
    attachments = [form_data.get(f"attachment_{i}") for i in range(1, 5)]
    links = ["لا يوجد مرفق"] * 4
    for i in range(4):
        if attachments[i] and getattr(attachments[i], "filename", None):
            url = upload_to_cloudinary(attachments[i])
            if url: links[i] = url

    master_plan = form_data.get("master_plan")
    isometric = form_data.get("isometric")
    master_plan_link = upload_to_cloudinary(master_plan) if master_plan and master_plan.filename else "لا يوجد مرفق"
    isometric_link = upload_to_cloudinary(isometric) if isometric and isometric.filename else "لا يوجد مرفق"

    def process_percentage(val):
        try: return float(val) / 100.0 if val else 0.0
        except ValueError: return 0.0
            
    act_prog_cur = process_percentage(form_data.get("act_prog_cur"))
    act_prog_prev = process_percentage(form_data.get("act_prog_prev"))
    plan_prog_cur = process_percentage(form_data.get("plan_prog_cur"))
    plan_prog_prev = process_percentage(form_data.get("plan_prog_prev"))

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO project_updates (
                username, manager_name, project_name, project_desc, project_type, current_data_date,
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
                file_link_1, file_link_2, file_link_3, file_link_4, master_plan_link, isometric_link, submission_date
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
        ''', (
            form_data.get("username"), form_data.get("manager_name"), form_data.get("project_name"), form_data.get("project_desc"), form_data.get("project_type"), form_data.get("current_data_date"),
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
            links[0], links[1], links[2], links[3], master_plan_link, isometric_link, date.today().isoformat()
        ))
        conn.commit()
        conn.close()
        
        # إرسال إشعار التليجرام بعد نجاح الحفظ
        send_telegram_alert("submit", form_data.get("manager_name"), form_data.get("project_name"))
        
        return {"message": "تم حفظ التحديث ورفع الملفات بنجاح!"}
    except Exception as e:
        return {"error": f"حدث خطأ أثناء حفظ البيانات: {e}"}

@app.get("/api/powerbi")
async def powerbi_feed():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_updates")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    
    data = []
    for row in rows:
        data.append(dict(zip(columns, row)))
    return data
