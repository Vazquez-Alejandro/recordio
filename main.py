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
from scheduler import check_reminders

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
    biz = Business(name=name, email=email, password_hash=pw_hash)
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
async def dashboard(req: Request, db: Session = Depends(get_db)):
    try:
        biz_id = get_current_business(req)
    except HTTPException:
        return RedirectResponse(url="/login")
    biz = db.query(Business).filter(Business.id == biz_id).first()
    today = datetime.now().strftime("%Y-%m-%d")
    appointments = db.query(Appointment).filter(
        Appointment.business_id == biz_id,
        Appointment.date == today
    ).order_by(Appointment.time).all()
    services = db.query(Service).filter(Service.business_id == biz_id).all()
    avails = db.query(Availability).filter(Availability.business_id == biz_id).all()
    weekdays = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    return templates.TemplateResponse("dashboard.html", {
        "request": req, "biz": biz, "appointments": appointments,
        "services": services, "availabilities": avails, "weekdays": weekdays, "today": today
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
        db.commit()
    return "<Response></Response>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8223, reload=True)
