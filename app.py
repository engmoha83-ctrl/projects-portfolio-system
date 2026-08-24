import os
import json
from datetime import date
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import psycopg2
import cloudinary
import cloudinary.uploader

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. إعدادات قاعدة البيانات (Supabase PostgreSQL)
# ==========================================
DB_URL = "رابط_قاعدة_بيانات_Supabase_هنا"

def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS project_updates (
            id SERIAL PRIMARY KEY,
            username TEXT,
            manager_name TEXT,
            project_name TEXT,
            project_desc TEXT,
            project_type TEXT,
            current_data_date TEXT,
            
            consultant_val REAL,
            contractor_val REAL,
            consultant_mods TEXT,
            contractor_mods TEXT,
            cons_inv_count INTEGER,
            cons_inv_val REAL,
            cons_inv_date TEXT,
            
            start_contractual TEXT,
            end_contractual TEXT,
            start_actual TEXT,
            end_expected TEXT,
            
            act_prog_cur REAL,
            act_prog_prev REAL,
            plan_prog_cur REAL,
            plan_prog_prev REAL,
            
            works_completed TEXT,
            works_ongoing TEXT,
            works_planned TEXT,
            obstacles_data JSONB,
            
            eval_labor INTEGER,
            eval_equip INTEGER,
            eval_financial INTEGER,
            eval_hse INTEGER,
            
            file_link TEXT,
            submission_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print("تنبيه: تأكد من وضع رابط قاعدة البيانات.", e)

# ==========================================
# 2. إعدادات Cloudinary (رفع الملفات)
# ==========================================
# قم بلصق المفاتيح الثلاثة التي نسختها هنا داخل علامات التنصيص
cloudinary.config(
  cloud_name = "wu5wjket",
  api_key = "241572682214285",
  api_secret = "K-susQH7Qh5lMeD7nwtdznSYnnU"
)

def upload_to_cloudinary(file: UploadFile):
    try:
        # رفع الملف والحصول على الرابط الآمن (يدعم الصور و PDF)
        result = cloudinary.uploader.upload(file.file, resource_type="auto")
        return result.get("secure_url")
    except Exception as e:
        print(f"خطأ في الرفع إلى Cloudinary: {e}")
        return None

# ==========================================
# 3. مسارات واجهة المستخدم والبيانات
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return templates.TemplateResponse(name="index.html", request=request)

@app.post("/submit")
async def submit_data(request: Request):
    form_data = await request.form()
    
    if form_data.get("password") != "admin123":
        return {"error": "كلمة المرور غير صحيحة."}

    attachment = form_data.get("attachment")
    file_link = "لا يوجد مرفق"
    if attachment and attachment.filename:
        uploaded_url = upload_to_cloudinary(attachment)
        if uploaded_url:
            file_link = uploaded_url

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO project_updates (
                username, manager_name, project_name, project_desc, project_type, current_data_date,
                consultant_val, contractor_val, consultant_mods, contractor_mods, cons_inv_count, cons_inv_val, cons_inv_date,
                start_contractual, end_contractual, start_actual, end_expected,
                act_prog_cur, act_prog_prev, plan_prog_cur, plan_prog_prev,
                works_completed, works_ongoing, works_planned, obstacles_data,
                eval_labor, eval_equip, eval_financial, eval_hse,
                file_link, submission_date
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            )
        ''', (
            form_data.get("username"), form_data.get("manager_name"), form_data.get("project_name"), form_data.get("project_desc"), form_data.get("project_type"), form_data.get("current_data_date"),
            form_data.get("consultant_val") or 0, form_data.get("contractor_val") or 0, form_data.get("consultant_mods"), form_data.get("contractor_mods"), form_data.get("cons_inv_count") or 0, form_data.get("cons_inv_val") or 0, form_data.get("cons_inv_date"),
            form_data.get("start_contractual"), form_data.get("end_contractual"), form_data.get("start_actual"), form_data.get("end_expected"),
            form_data.get("act_prog_cur") or 0, form_data.get("act_prog_prev") or 0, form_data.get("plan_prog_cur") or 0, form_data.get("plan_prog_prev") or 0,
            form_data.get("works_completed"), form_data.get("works_ongoing"), form_data.get("works_planned"), form_data.get("obstacles_json"),
            form_data.get("eval_labor") or 0, form_data.get("eval_equip") or 0, form_data.get("eval_financial") or 0, form_data.get("eval_hse") or 0,
            file_link, date.today().isoformat()
        ))
        conn.commit()
        conn.close()
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
