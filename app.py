import os
from datetime import date
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import sqlite3

app = FastAPI()
# ربط الكود بمجلد التصميم
templates = Jinja2Templates(directory="templates")

# التأكد من وجود مجلد لحفظ الصور والمرفقات
os.makedirs("uploads", exist_ok=True)

# تجهيز قاعدة البيانات
def init_db():
    conn = sqlite3.connect("portfolio.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            project_name TEXT,
            end_date TEXT,
            progress REAL,
            status TEXT,
            notes TEXT,
            attachment_path TEXT,
            submission_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# 1. عرض صفحة الويب
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return templates.TemplateResponse(name="index.html", request=request)

# 2. استقبال البيانات من صفحة الويب وحفظها
@app.post("/submit")
async def submit_data(
    username: str = Form(...),
    password: str = Form(...),
    project_name: str = Form(...),
    end_date: str = Form(...),
    progress: float = Form(...),
    status: str = Form(...),
    notes: str = Form(""),
    attachment: UploadFile = File(None)
):
    # نظام حماية مبسط (يمكن تطويره لاحقاً للتحقق من قاعدة بيانات المستخدمين)
    if password != "admin123":
        return {"error": "كلمة المرور غير صحيحة، يرجى المحاولة مرة أخرى."}
    
    file_path = ""
    # حفظ الملف المرفق إذا قام المدير برفعه
    if attachment and attachment.filename:
        file_path = f"uploads/{attachment.filename}"
        with open(file_path, "wb") as f:
            f.write(await attachment.read())

    # إدخال البيانات في السجل التاريخي
    conn = sqlite3.connect("portfolio.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO updates (username, project_name, end_date, progress, status, notes, attachment_path, submission_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (username, project_name, end_date, progress, status, notes, file_path, date.today().isoformat()))
    conn.commit()
    conn.close()
    
    return {"message": "تم حفظ التحديث بنجاح! يمكنك إغلاق هذه الصفحة."}

# 3. الرابط السحري لبرنامج Power BI (API Endpoint)
@app.get("/api/powerbi")
async def powerbi_feed():
    conn = sqlite3.connect("portfolio.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM updates")
    rows = cursor.fetchall()
    conn.close()
    
    # تحويل البيانات إلى صيغة JSON ليقرأها Power BI
    data = []
    for row in rows:
        data.append({
            "id": row[0], "manager": row[1], "project": row[2], 
            "expected_finish": row[3], "progress_pct": row[4], 
            "status": row[5], "notes": row[6], "date": row[8]
        })
    return data