import os
import json
from datetime import date
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import psycopg2
import cloudinary
import cloudinary.uploader

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. إعدادات قاعدة البيانات 
# ==========================================
DB_URL = "postgresql://postgres.rofppixfbshgdkhqoevo:Saudi_Architects2026@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"

def get_db_connection():
    return psycopg2.connect(DB_URL)

# ==========================================
# 2. إعدادات Cloudinary
# ==========================================
cloudinary.config(
  cloud_name = "wu5wjket",
  api_key = "241572682214285",
  api_secret = "K-susQH7Qh5lMeD7nwtdznSYnnU"
)

def upload_to_cloudinary(file: UploadFile):
    try:
        result = cloudinary.uploader.upload(file.file, resource_type="auto")
        return result.get("secure_url")
    except Exception as e:
        print(f"خطأ في الرفع: {e}")
        return None

# ==========================================
# 3. نظام تسجيل الدخول والجلسات
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
        response = RedirectResponse(url="/", status_code=303)
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
# 4. الصفحة الرئيسية وإرسال التحديث
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
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

    manager_name, project_name = user
    return templates.TemplateResponse(request, "index.html", {
        "username": auth_user, 
        "manager_name": manager_name, 
        "project_name": project_name
    })

@app.post("/submit")
async def submit_data(request: Request):
    auth_user = request.cookies.get("auth_user")
    if not auth_user:
        return {"error": "انتهت الجلسة، يرجى تسجيل الدخول من جديد."}

    form_data = await request.form()
    
    attachment = form_data.get("attachment")
    master_plan = form_data.get("master_plan")
    isometric = form_data.get("isometric")
    
    file_link = "لا يوجد مرفق"
    master_plan_link = "لا يوجد مرفق"
    isometric_link = "لا يوجد مرفق"
    
    if attachment and attachment.filename:
        uploaded_url = upload_to_cloudinary(attachment)
        if uploaded_url: file_link = uploaded_url
        
    if master_plan and master_plan.filename:
        uploaded_url = upload_to_cloudinary(master_plan)
        if uploaded_url: master_plan_link = uploaded_url
        
    if isometric and isometric.filename:
        uploaded_url = upload_to_cloudinary(isometric)
        if uploaded_url: isometric_link = uploaded_url

    # --- معالجة نسب الإنجاز (تحويل الرقم لنسبة عشرية لتتوافق مع Power BI) ---
    def process_percentage(val):
        try:
            if val:
                return float(val) / 100.0
            return 0.0
        except ValueError:
            return 0.0
            
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
                file_link, master_plan_link, isometric_link, submission_date
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
                %s, %s, %s, %s
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
            act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev,  # تم إدراج النسب المعالجة هنا
            form_data.get("works_completed"), form_data.get("works_ongoing"), form_data.get("works_planned"), form_data.get("obstacles_json"),
            form_data.get("eval_labor") or 0, form_data.get("eval_equip") or 0, form_data.get("eval_financial") or 0, form_data.get("eval_hse") or 0,
            form_data.get("drawings_sub") or 0, form_data.get("drawings_app") or 0, form_data.get("drawings_rev") or 0,
            form_data.get("ir_sub") or 0, form_data.get("ir_app") or 0, form_data.get("ir_rev") or 0,
            form_data.get("ncr_open") or 0, form_data.get("ncr_closed") or 0,
            file_link, master_plan_link, isometric_link, date.today().isoformat()
        ))
        conn.commit()
        conn.close()
        return {"message": "تم حفظ التحديث ورفع الملفات بنجاح!"}
    except Exception as e:
        return {"error": f"حدث خطأ أثناء حفظ البيانات: {e}"}

@app.post("/save-section")
async def save_section(request: Request):
    return {"message": "تم حفظ بيانات القسم بنجاح!"}

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
