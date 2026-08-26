# ==========================================
# 6. لوحة تحكم الإدارة الأساسية
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
        # التعديل هنا: حفظنا اسم المستخدم الفعلي في الكوكيز لمعرفة من دخل
        response.set_cookie(key="super_admin_auth", value=username, max_age=86400)
        return response
    else:
        return templates.TemplateResponse(request, "admin_login.html", {"error": "بيانات الدخول غير صحيحة"})

@app.get("/admin-dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    if not admin_user:
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
    
    # نرسل اسم الأدمن للصفحة لكي نخفي الأزرار عن المساعد
    return templates.TemplateResponse(request, "admin_dashboard.html", {"columns": columns, "rows": rows, "admin_user": admin_user})

@app.post("/api/update-cell")
async def update_cell(request: Request):
    if not request.cookies.get("super_admin_auth"):
        return {"success": False, "error": "غير مصرح"}
    # ... (باقي كود الدالة كما هو بدون تغيير)
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
# 7. الإضافات الجديدة: دليل مديري المشاريع
# ==========================================
@app.get("/admin-directory", response_class=HTMLResponse)
async def admin_directory_page(request: Request):
    admin_user = request.cookies.get("super_admin_auth")
    
    # التعديل الجذري للحماية: إذا لم يكن الأدمن هو "admin_mohamed"، اطرده لصفحة الجدول
    if admin_user != "admin_mohamed":
        return RedirectResponse(url="/admin-dashboard", status_code=303)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, manager_name, project_name, phone, email, location_link, profile_image FROM pm_directory ORDER BY id ASC")
    pms = cursor.fetchall()
    conn.close()

    return templates.TemplateResponse(request, "admin_directory.html", {"pms": pms, "admin_user": admin_user})

@app.post("/admin-update-pm")
async def admin_update_pm(
    request: Request, pm_id: int = Form(...), phone: str = Form(""), email: str = Form(""),
    location_link: str = Form(""), profile_image: UploadFile = File(None)
):
    admin_user = request.cookies.get("super_admin_auth")
    # حماية أمر التحديث أيضاً لك أنت فقط
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
