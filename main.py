import os
import logging
import secrets
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import SessionLocal, Business, Service, Availability, Appointment
from whatsapp import send_whatsapp
from scheduler import check_reminders, send_daily_summaries

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("recordio")

JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

from jose import jwt, JWTError
JWT_ALGORITHM = "HS256"


def make_token(business_id: int) -> str:
    return jwt.encode({"sub": str(business_id), "iat": datetime.now(timezone.utc)}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_business_from_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(check_reminders())
    asyncio.create_task(send_daily_summaries())
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_business(req: Request) -> int:
    token = req.cookies.get("token")
    if not token:
        raise HTTPException(status_code=401)
    biz_id = get_business_from_token(token)
    if not biz_id:
        raise HTTPException(status_code=401)
    return biz_id


# ─── Auth ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(req: Request):
    token = req.cookies.get("token")
    if token and get_business_from_token(token):
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse("landing.html", {"request": req})


@app.get("/register", response_class=HTMLResponse)
async def register_page(req: Request):
    return templates.TemplateResponse("register.html", {"request": req})


@app.post("/register")
async def register(name: str = Form(...), email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    existing = db.query(Business).filter(Business.email == email).first()
    if existing:
        return templates.TemplateResponse("register.html", {"request": {}, "error": "Email ya registrado"})
    import bcrypt
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    slug = name.lower().replace(" ", "-").replace("ñ", "n").replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")[:30]
    slug_base = slug
    counter = 1
    while db.query(Business).filter(Business.slug == slug).first():
        slug = f"{slug_base}-{counter}"
        counter += 1
    biz = Business(name=name, email=email, password_hash=pw_hash, slug=slug)
    db.add(biz)
    db.commit()
    db.refresh(biz)
    token = make_token(biz.id)
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(key="token", value=token, httponly=True, max_age=86400 * 30)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(req: Request):
    return templates.TemplateResponse("login.html", {"request": req})


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.email == email).first()
    if not biz:
        return templates.TemplateResponse("login.html", {"request": {}, "error": "Email no registrado"})
    import bcrypt
    if not bcrypt.checkpw(password.encode(), biz.password_hash.encode()):
        return templates.TemplateResponse("login.html", {"request": {}, "error": "Contraseña incorrecta"})
    token = make_token(biz.id)
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(key="token", value=token, httponly=True, max_age=86400 * 30)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("token")
    return resp


# ─── Dashboard ─────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(req: Request, date: str = "", db: Session = Depends(get_db)):
    try:
        biz_id = get_current_business(req)
    except HTTPException:
        return RedirectResponse(url="/login")
    biz = db.query(Business).filter(Business.id == biz_id).first()
    today = datetime.now().strftime("%Y-%m-%d")
    filter_date = date if date else today
    appointments_today = db.query(Appointment).filter(
        Appointment.business_id == biz_id,
        Appointment.date == today
    ).order_by(Appointment.time).all()
    appointments_all = db.query(Appointment).filter(
        Appointment.business_id == biz_id
    )
    if date:
        appointments_all = appointments_all.filter(Appointment.date == date)
    appointments_all = appointments_all.order_by(Appointment.date.desc(), Appointment.time).all()
    confirmed_count = sum(1 for a in appointments_today if a.status == "confirmed")
    pending_count = sum(1 for a in appointments_today if a.status == "pending")
    cancelled_count = sum(1 for a in appointments_today if a.status == "cancelled")
    total_count = len(appointments_today)
    services = db.query(Service).filter(Service.business_id == biz_id).all()
    avails = db.query(Availability).filter(Availability.business_id == biz_id).all()
    weekdays = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    return templates.TemplateResponse("dashboard.html", {
        "request": req, "biz": biz,
        "appointments": appointments_today,
        "appointments_all": appointments_all,
        "services": services, "availabilities": avails,
        "weekdays": weekdays, "today": today,
        "filter_date": filter_date,
        "confirmed_count": confirmed_count,
        "pending_count": pending_count,
        "cancelled_count": cancelled_count,
        "total_count": total_count
    })


# ─── Appointments ──────────────────────────────────────────────────

@app.post("/appointments")
async def create_appointment(
    client_name: str = Form(...), client_phone: str = Form(...),
    date: str = Form(...), time: str = Form(...),
    service_id: int = Form(0), notes: str = Form(""),
    db: Session = Depends(get_db), req: Request = None
):
    biz_id = get_current_business(req)
    sid = service_id if service_id > 0 else None
    apt = Appointment(
        business_id=biz_id, service_id=sid,
        client_name=client_name, client_phone=client_phone,
        date=date, time=time, notes=notes or None
    )
    db.add(apt)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/appointments/{apt_id}/confirm")
async def confirm_appointment(apt_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    apt = db.query(Appointment).filter(Appointment.id == apt_id, Appointment.business_id == biz_id).first()
    if apt:
        apt.status = "confirmed"
        apt.confirmed_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/appointments/{apt_id}/cancel")
async def cancel_appointment(apt_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    apt = db.query(Appointment).filter(Appointment.id == apt_id, Appointment.business_id == biz_id).first()
    if apt:
        apt.status = "cancelled"
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/appointments/{apt_id}/delete")
async def delete_appointment(apt_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    apt = db.query(Appointment).filter(Appointment.id == apt_id, Appointment.business_id == biz_id).first()
    if apt:
        db.delete(apt)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/appointments/{apt_id}/edit")
async def edit_appointment(apt_id: int, client_name: str = Form(...), client_phone: str = Form(...), date: str = Form(...), time: str = Form(...), service_id: int = Form(0), notes: str = Form(""), db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    apt = db.query(Appointment).filter(Appointment.id == apt_id, Appointment.business_id == biz_id).first()
    if apt:
        apt.client_name = client_name
        apt.client_phone = client_phone
        apt.date = date
        apt.time = time
        apt.service_id = service_id if service_id > 0 else None
        apt.notes = notes or None
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/appointments/{apt_id}/resend")
async def resend_reminder(apt_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    apt = db.query(Appointment).filter(Appointment.id == apt_id, Appointment.business_id == biz_id).first()
    if not apt:
        return RedirectResponse(url="/dashboard", status_code=302)
    biz = apt.business
    service_name = apt.service.name if apt.service else "turno"
    msg = f"Hola {apt.client_name}! Te recordamos tu turno de {service_name} el {apt.date} a las {apt.time}."
    await send_whatsapp(apt.client_phone, msg)
    return RedirectResponse(url="/dashboard", status_code=302)


# ─── Services ──────────────────────────────────────────────────────

@app.post("/services")
async def create_service(name: str = Form(...), duration: int = Form(30), price: float = Form(0), db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    svc = Service(business_id=biz_id, name=name, duration_minutes=duration, price=price if price > 0 else None)
    db.add(svc)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/services/{svc_id}/delete")
async def delete_service(svc_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    svc = db.query(Service).filter(Service.id == svc_id, Service.business_id == biz_id).first()
    if svc:
        db.delete(svc)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


# ─── Settings ──────────────────────────────────────────────────────

@app.post("/settings")
async def update_settings(
    name: str = Form(...), phone: str = Form(""), timezone: str = Form("America/Argentina/Buenos_Aires"),
    reminder_24h: str = Form("off"), reminder_1h: str = Form("off"),
    reminder_before_minutes: int = Form(0), message_template: str = Form(""),
    db: Session = Depends(get_db), req: Request = None
):
    biz_id = get_current_business(req)
    biz = db.query(Business).filter(Business.id == biz_id).first()
    if biz:
        biz.name = name
        biz.phone = phone or None
        biz.timezone = timezone
        biz.reminder_24h = reminder_24h == "on"
        biz.reminder_1h = reminder_1h == "on"
        biz.reminder_before_minutes = reminder_before_minutes
        biz.message_template = message_template
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/settings/availability")
async def add_availability(
    day_of_week: int = Form(...), start_hour: int = Form(9), start_minute: int = Form(0),
    end_hour: int = Form(18), end_minute: int = Form(0),
    db: Session = Depends(get_db), req: Request = None
):
    biz_id = get_current_business(req)
    from datetime import time as tm
    av = Availability(
        business_id=biz_id, day_of_week=day_of_week,
        start_time=tm(start_hour, start_minute), end_time=tm(end_hour, end_minute)
    )
    db.add(av)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/settings/availability/{av_id}/delete")
async def delete_availability(av_id: int, db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    av = db.query(Availability).filter(Availability.id == av_id, Availability.business_id == biz_id).first()
    if av:
        db.delete(av)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)


# ─── API (for WhatsApp webhook) ────────────────────────────────────

@app.post("/api/whatsapp")
async def whatsapp_webhook(req: Request, db: Session = Depends(get_db)):
    body = await req.form()
    from_number = body.get("From", "")
    message_body = (body.get("Body") or "").strip().upper()
    phone = from_number.replace("whatsapp:", "")
    apt = db.query(Appointment).filter(Appointment.client_phone == phone).first()
    if apt:
        if message_body == "CONFIRMAR":
            apt.status = "confirmed"
            apt.confirmed_at = datetime.now(timezone.utc)
        elif message_body == "CANCELAR":
            apt.status = "cancelled"
            svc_name = apt.service.name if apt.service else "turno"
            await send_whatsapp(phone, f"Hola {apt.client_name}! Confirmamos la cancelación de tu {svc_name} del {apt.date} a las {apt.time}. Si querés reagendar, comunicate con el negocio.")
        db.commit()
    return "<Response></Response>"


# ─── Export ────────────────────────────────────────────────────────

@app.get("/export/csv")
async def export_csv(db: Session = Depends(get_db), req: Request = None):
    biz_id = get_current_business(req)
    biz = db.query(Business).filter(Business.id == biz_id).first()
    appointments = db.query(Appointment).filter(Appointment.business_id == biz_id).order_by(Appointment.date.desc(), Appointment.time).all()
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fecha", "Hora", "Cliente", "Telefono", "Servicio", "Estado", "Notas", "Creado"])
    for apt in appointments:
        svc = apt.service.name if apt.service else ""
        writer.writerow([apt.date, apt.time, apt.client_name, apt.client_phone, svc, apt.status, apt.notes or "", apt.created_at.strftime("%Y-%m-%d %H:%M") if apt.created_at else ""])
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="turnos-{biz.name}.csv"'})


# ─── Public booking page ──────────────────────────────────────────

DAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


@app.get("/book/{slug}", response_class=HTMLResponse)
async def public_booking_page(slug: str, req: Request, db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.slug == slug, Business.active == True).first()
    if not biz:
        return HTMLResponse("Negocio no encontrado", status_code=404)
    services = db.query(Service).filter(Service.business_id == biz.id).all()
    return templates.TemplateResponse("public-book.html", {
        "request": req, "biz": biz, "services": services,
        "slug": slug, "days": DAYS
    })


@app.get("/api/{slug}/slots")
async def public_slots(slug: str, date: str = "", service_id: int = 0, db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.slug == slug, Business.active == True).first()
    if not biz:
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    if not date or not service_id:
        return {"slots": []}
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"slots": []}
    day_idx = dt.weekday()
    avails = db.query(Availability).filter(
        Availability.business_id == biz.id,
        Availability.day_of_week == day_idx
    ).all()
    if not avails:
        return {"slots": []}
    svc = db.query(Service).filter(Service.id == service_id, Service.business_id == biz.id).first()
    duration = svc.duration_minutes if svc else 30
    booked = db.query(Appointment).filter(
        Appointment.business_id == biz.id,
        Appointment.date == date,
        Appointment.status.in_(["pending", "confirmed"])
    ).all()
    booked_times = set(apt.time for apt in booked)
    slots = []
    for av in avails:
        h, m = av.start_time.hour, av.start_time.minute
        end_h, end_m = av.end_time.hour, av.end_time.minute
        start_min = h * 60 + m
        end_min = end_h * 60 + end_m
        t = start_min
        while t + duration <= end_min:
            slot = f"{t // 60:02d}:{t % 60:02d}"
            if slot not in booked_times:
                slots.append(slot)
            t += duration
    return {"slots": slots, "date": date, "service_id": service_id}


@app.post("/api/{slug}/book")
async def public_book(slug: str, req: Request, db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.slug == slug, Business.active == True).first()
    if not biz:
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    body = await req.json()
    apt = Appointment(
        business_id=biz.id,
        service_id=body.get("service_id"),
        client_name=body.get("name", ""),
        client_phone=body.get("phone", ""),
        date=body.get("date", ""),
        time=body.get("time", ""),
    )
    db.add(apt)
    db.commit()
    db.refresh(apt)
    svc = db.query(Service).filter(Service.id == apt.service_id).first() if apt.service_id else None
    svc_name = svc.name if svc else "turno"
    msg = f"Hola {apt.client_name}! Gracias por agendar tu {svc_name} para el {apt.date} a las {apt.time}."
    await send_whatsapp(apt.client_phone, msg)
    return {"ok": True}


@app.get("/api/{slug}/services")
async def public_services(slug: str, db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.slug == slug, Business.active == True).first()
    if not biz:
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    services = db.query(Service).filter(Service.business_id == biz.id).all()
    return {"services": [{"id": s.id, "name": s.name, "duration": s.duration_minutes, "price": s.price} for s in services]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8223, reload=True)
