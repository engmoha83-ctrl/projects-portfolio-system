import os
import json
import bcrypt
import psycopg2
import psycopg2.extras
from datetime import datetime
from fastapi import FastAPI, Request, Form, Response, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# استدعاء دالة توليد تقارير PDF من الملف المنفصل
from pdf_report import build_project_pdf

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. إعدادات قاعدة البيانات والدوال المساعدة
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except ValueError:
        # للتعامل مع كلمات المرور القديمة (Plain text) قبل التشفير
        return plain_password == hashed_password

def log_audit(actor: str, action: str, details: str):
    """تسجيل حركة في سجل التعديلات (Audit Log)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_log (actor, action, details, created_at) VALUES (%s, %s, %s, NOW())",
            (actor, action, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("Audit Log Error:", e)

def create_notification(message: str, link: str = "#"):
    """إنشاء إشعار جديد في النظام"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notifications (message, link, is_read, created_at) VALUES (%s, %s, FALSE, NOW())",
            (message, link)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("Notification Error:", e)

# ==========================================
# 2. مسارات المصادقة وتسجيل الدخول
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
async def login_post(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    
    if user:
        stored_password = user['password']
        if verify_password(password, stored_password):
            # الترقية الذكية لكلمات المرور: إذا كانت مسجلة كنص صريح، شفرها واحفظها
            if not stored_password.startswith("$2b$"):
                new_hashed = hash_password(password)
                cursor.execute("UPDATE users SET password = %s WHERE username = %s", (new_hashed, username))
                conn.commit()
            
            conn.close()
            response = RedirectResponse(url="/update-portal", status_code=303)
            # حماية الكوكيز httponly
            response.set_cookie(key="pm_auth", value=username, httponly=True)
            background_tasks.add_task(log_audit, username, "تسجيل دخول", "تم تسجيل دخول مدير المشروع")
            return response
            
    conn.close()
    return templates.TemplateResponse("login.html", {"request": request, "error": "اسم المستخدم أو كلمة المرور غير صحيحة"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("pm_auth")
    return response

@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = None):
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": error})

@app.post("/admin")
async def admin_login_post(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...)):
    # تحقق مبسط من حسابات الإدارة (يُفضل ربطها بالداتابيز لاحقاً)
    admin_accounts = {
        "admin_mohamed": "mohamed_secret_pass",
        "admin_assistant": "assistant_pass"
    }
    
    if username in admin_accounts and admin_accounts[username] == password:
        response = RedirectResponse(url="/admin-hub", status_code=303)
        response.set_cookie(key="super_admin_auth", value=username, httponly=True)
        background_tasks.add_task(log_audit, username, "تسجيل دخول إداري", f"دخول حساب {username}")
        return response
        
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": "بيانات الدخول غير صحيحة"})

@app.get("/admin-logout")
async def admin_logout():
    response = RedirectResponse(url="/admin")
    response.delete_cookie("super_admin_auth")
    return response

# ==========================================
# 3. مسارات واجهة مديري المشاريع
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

@app.get("/update-portal", response_class=HTMLResponse)
async def update_portal_page(request: Request):
    username = request.cookies.get("pm_auth")
    if not username:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("index.html", {"request": request, "username": username})

@app.get("/my-dashboard", response_class=HTMLResponse)
async def my_dashboard_page(request: Request):
    username = request.cookies.get("pm_auth")
    if not username:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("my_dashboard.html", {"request": request})

# ==========================================
# 4. مسارات الإدارة المركزية (Admin Hub)
# ==========================================
@app.get("/admin-hub", response_class=HTMLResponse)
async def admin_hub_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin")
    return templates.TemplateResponse("admin_hub.html", {"request": request, "admin_user": admin_user})

@app.get("/admin-dashboard", response_class=HTMLResponse)
async def admin_dashboard_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin")
    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "admin_user": admin_user, "active_page": "dashboard"})

@app.get("/admin-analytics", response_class=HTMLResponse)
async def admin_analytics_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-hub")
    return templates.TemplateResponse("admin_analytics.html", {"request": request, "admin_user": admin_user, "active_page": "analytics"})

@app.get("/project-dashboard", response_class=HTMLResponse)
async def project_dashboard_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-hub")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT project_name FROM pm_directory WHERE project_name IS NOT NULL")
    projects = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return templates.TemplateResponse("project_dashboard.html", {"request": request, "projects": projects, "admin_user": admin_user, "active_page": "dashboard"})

@app.get("/admin-users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": # حماية الصلاحيات للمهندس محمد فقط
        return RedirectResponse(url="/admin-hub")
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM users ORDER BY id ASC")
    users = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse("admin_users.html", {"request": request, "users": users, "admin_user": admin_user, "active_page": "users"})

@app.get("/admin-audit-log", response_class=HTMLResponse)
async def admin_audit_log_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-hub")
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 500")
    logs = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse("admin_audit_log.html", {"request": request, "logs": logs, "admin_user": admin_user})

@app.get("/admin-directory", response_class=HTMLResponse)
async def admin_directory_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed": 
        return RedirectResponse(url="/admin-dashboard")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id, d.username, d.manager_name, d.project_name, d.phone, d.email, d.location_link, d.profile_image,
               (SELECT COUNT(*) FROM project_updates p WHERE p.username = d.username) as updates_count
        FROM pm_directory d ORDER BY d.id ASC
    """)
    pms = cursor.fetchall()
    conn.close()
    return templates.TemplateResponse("admin_directory.html", {"request": request, "pms": pms, "admin_user": admin_user, "active_page": "directory"})

@app.post("/admin-update-pm")
async def admin_update_pm(request: Request, background_tasks: BackgroundTasks, pm_id: int = Form(...), phone: str = Form(""), email: str = Form(""), location_link: str = Form(""), profile_image: UploadFile = File(None)):
    admin_user = request.cookies.get("super_admin_auth")
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-hub")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # تحديث البيانات الأساسية
    cursor.execute(
        "UPDATE pm_directory SET phone = %s, email = %s, location_link = %s WHERE id = %s",
        (phone, email, location_link, pm_id)
    )
    
    # معالجة الصورة إذا تم رفعها
    if profile_image and profile_image.filename:
        file_location = f"static/uploads/{profile_image.filename}"
        with open(file_location, "wb+") as file_object:
            file_object.write(profile_image.file.read())
        cursor.execute("UPDATE pm_directory SET profile_image = %s WHERE id = %s", (f"/{file_location}", pm_id))

    conn.commit()
    conn.close()
    background_tasks.add_task(log_audit, admin_user, "تحديث دليل المديرين", f"تعديل بيانات المدير رقم {pm_id}")
    return RedirectResponse(url="/admin-directory", status_code=303)

@app.get("/admin-gallery", response_class=HTMLResponse)
async def admin_gallery_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return RedirectResponse(url="/admin")
    return templates.TemplateResponse("admin_gallery.html", {"request": request, "admin_user": admin_user, "active_page": "gallery"})

# ==========================================
# 5. واجهات الـ API لمعالجة البيانات (AJAX)
# ==========================================
@app.post("/api/update-cell")
async def update_cell(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    record_id = data.get("id")
    column = data.get("column")
    value = data.get("value")
    
    # حماية ضد SQL Injection: السماح فقط للأعمدة المعروفة
    allowed_columns = ["act_prog_cur", "plan_prog_cur", "contractor_val", "ncr_open", "project_desc", "obstacles_data", "eval_labor", "eval_equip"]
    if column not in allowed_columns:
        return JSONResponse({"success": False, "message": "عمود غير مصرح به"})

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = f"UPDATE project_updates SET {column} = %s WHERE id = %s"
        cursor.execute(query, (value, record_id))
        conn.commit()
        conn.close()
        
        user = request.cookies.get("pm_auth") or request.cookies.get("super_admin_auth") or "نظام"
        background_tasks.add_task(log_audit, user, "تعديل خلية", f"تعديل {column} للسجل {record_id}")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

@app.get("/api/my-dashboard-data")
async def get_my_dashboard_data(request: Request):
    username = request.cookies.get("pm_auth")
    if not username:
        return JSONResponse({"success": False, "message": "غير مصرح"})
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # ربط التحديثات بجدول pm_directory لجلب اسم المشروع
    cursor.execute("""
        SELECT u.*, d.project_name, d.manager_name, d.project_type, d.project_owner, d.project_developer, d.project_contractor 
        FROM project_updates u 
        LEFT JOIN pm_directory d ON u.username = d.username 
        WHERE u.username = %s ORDER BY u.current_data_date ASC
    """, (username,))
    records = cursor.fetchall()
    conn.close()
    
    return JSONResponse({"success": True, "records": [dict(r) for r in records]})

@app.get("/api/project-dashboard-data")
async def get_project_dashboard_data(request: Request, project: str):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
        return JSONResponse({"success": False, "message": "غير مصرح"})
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("""
        SELECT u.*, d.project_name, d.manager_name, d.project_type, d.project_owner, d.project_developer, d.project_contractor 
        FROM project_updates u 
        LEFT JOIN pm_directory d ON u.username = d.username 
        WHERE d.project_name = %s ORDER BY u.current_data_date ASC
    """, (project,))
    records = cursor.fetchall()
    conn.close()
    
    return JSONResponse({"success": True, "records": [dict(r) for r in records]})

@app.post("/api/save-dashboard-layout")
async def save_dashboard_layout(request: Request, background_tasks: BackgroundTasks):
    """
    مسار حفظ الداشبورد التفاعلي (ميزة السحب والإفلات وتغيير المقاسات).
    """
    try:
        data = await request.json()
        layout_data = data.get("layout")
        global_settings = data.get("settings")
        
        username = request.cookies.get("pm_auth")
        if not username:
            return JSONResponse({"success": False, "message": "غير مصرح لك بالحفظ"})
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # حفظ التصميم كـ JSON في عمود custom_dashboard_layout
        cursor.execute(
            "UPDATE pm_directory SET custom_dashboard_layout = %s WHERE username = %s",
            (json.dumps({"layout": layout_data, "settings": global_settings}), username)
        )
        conn.commit()
        conn.close()
        
        background_tasks.add_task(log_audit, username, "تعديل واجهة الداشبورد", "تم حفظ تخطيط (Layout) جديد")
        return JSONResponse({"success": True, "message": "تم حفظ التصميم بنجاح"})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})

@app.get("/api/project-pdf")
async def generate_project_pdf(request: Request, background_tasks: BackgroundTasks, project: str = None):
    # إذا كان المستخدم مدير مشروع، اطبع مشروعه هو. وإذا كان أدمن، اطبع المشروع الممرر في الرابط.
    pm_user = request.cookies.get("pm_auth")
    admin_user = request.cookies.get("super_admin_auth")
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    if pm_user:
        cursor.execute("SELECT project_name FROM pm_directory WHERE username = %s", (pm_user,))
        res = cursor.fetchone()
        if res:
            project_name = res[0]
            cursor.execute("""
                SELECT u.*, d.project_name, d.manager_name, d.project_type, d.project_owner, d.project_developer, d.project_contractor 
                FROM project_updates u LEFT JOIN pm_directory d ON u.username = d.username 
                WHERE u.username = %s ORDER BY u.current_data_date ASC
            """, (pm_user,))
            records = cursor.fetchall()
    elif admin_user and project:
        project_name = project
        cursor.execute("""
            SELECT u.*, d.project_name, d.manager_name, d.project_type, d.project_owner, d.project_developer, d.project_contractor 
            FROM project_updates u LEFT JOIN pm_directory d ON u.username = d.username 
            WHERE d.project_name = %s ORDER BY u.current_data_date ASC
        """, (project_name,))
        records = cursor.fetchall()
    else:
        conn.close()
        return HTMLResponse("غير مصرح أو لم يتم تحديد مشروع.")
        
    conn.close()
    
    # توليد التقرير من دالة ملف pdf_report.py
    pdf_buffer = build_project_pdf(project_name, [dict(r) for r in records], lang="ar")
    
    actor = pm_user if pm_user else admin_user
    background_tasks.add_task(log_audit, actor, "تصدير PDF", f"تصدير تقرير مشروع {project_name}")
    
    headers = {"Content-Disposition": f"attachment; filename=Project_Report_{project_name}.pdf"}
    return Response(content=pdf_buffer.getvalue(), media_type="application/pdf", headers=headers)

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

# ==========================================
# تشغيل التطبيق
# ==========================================
if __name__ == "__main__":
    import uvicorn
    # uvicorn.run(app, host="0.0.0.0", port=8000)
