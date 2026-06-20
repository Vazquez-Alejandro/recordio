import asyncio
import logging
from datetime import datetime, timedelta, timezone
from database import SessionLocal, Appointment
from whatsapp import send_whatsapp

logger = logging.getLogger("recordio.scheduler")


async def check_reminders():
    while True:
        try:
            await _check()
        except Exception as e:
            logger.error(f"Error en scheduler: {e}")
        await asyncio.sleep(30)


async def _check():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        appointments = db.query(Appointment).filter(
            Appointment.status == "pending"
        ).all()

        for apt in appointments:
            biz = apt.business
            if not biz or not biz.active:
                continue
            try:
                apt_dt = datetime.strptime(f"{apt.date} {apt.time}", "%Y-%m-%d %H:%M")
                if biz.timezone:
                    import pytz
                    tz = pytz.timezone(biz.timezone)
                    apt_dt = tz.localize(apt_dt)
                else:
                    apt_dt = apt_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            diff = (apt_dt - now).total_seconds()
            diff_hours = diff / 3600

            service_name = apt.service.name if apt.service else "turno"
            template = biz.message_template or "Hola {client}! Te recordamos tu turno de {service} el {date} a las {time}. Respondé CONFIRMAR para confirmar o CANCELAR para cancelar."
            msg = template.replace("{client}", apt.client_name) \
                          .replace("{service}", service_name) \
                          .replace("{date}", apt.date) \
                          .replace("{time}", apt.time)

            if biz.reminder_24h and not apt.reminder_24h_sent and 23.5 <= diff_hours <= 24.5:
                result = await send_whatsapp(apt.client_phone, msg)
                if result.get("success"):
                    apt.reminder_24h_sent = True
                    db.commit()
                    logger.info(f"Recordatorio 24h enviado a {apt.client_name} ({apt.client_phone})")

            if biz.reminder_1h and not apt.reminder_1h_sent and 0.5 <= diff_hours <= 1.5:
                result = await send_whatsapp(apt.client_phone, msg)
                if result.get("success"):
                    apt.reminder_1h_sent = True
                    db.commit()
                    logger.info(f"Recordatorio 1h enviado a {apt.client_name} ({apt.client_phone})")

            if biz.reminder_before_minutes and not apt.reminder_before_sent:
                mins_before = biz.reminder_before_minutes
                target_secs = mins_before * 60
                if target_secs - 60 <= diff <= target_secs + 60:
                    result = await send_whatsapp(apt.client_phone, msg)
                    if result.get("success"):
                        apt.reminder_before_sent = True
                        db.commit()
                        logger.info(f"Recordatorio {mins_before}min enviado a {apt.client_name}")

            if diff < -3600 and apt.status == "pending":
                apt.status = "no_show"
                db.commit()
    finally:
        db.close()
