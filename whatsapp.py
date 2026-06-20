import os
import httpx
from base64 import b64encode

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def normalize_phone(phone: str) -> str | None:
    cleaned = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if not cleaned.isdigit() or len(cleaned) < 10:
        return None
    return f"whatsapp:+{cleaned}"


async def send_whatsapp(to: str, message: str) -> dict:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"success": False, "error": "Twilio no configurado"}
    to_norm = normalize_phone(to)
    if not to_norm:
        return {"success": False, "error": "Número inválido"}
    auth = b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
                headers={"Authorization": f"Basic {auth}"},
                data={"From": TWILIO_FROM, "To": to_norm, "Body": message},
            )
            data = resp.json()
            if not resp.is_success:
                return {"success": False, "error": data.get("message", "Error Twilio")}
            return {"success": True, "sid": data.get("sid")}
    except Exception as e:
        return {"success": False, "error": str(e)}
