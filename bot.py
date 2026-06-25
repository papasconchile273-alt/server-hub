"""
Bot de Telegram para Server Hub.

Mandas una FOTO con caption:  plataformas | YYYY-MM-DD HH:MM | texto
Ej:  fb,ig | 2026-06-25 18:30 | Mi post programado 🚀

El bot sube la foto a Google Drive (la hace pública), saca el File ID, y le
pega a /api/programar del Server Hub. El scheduler que ya existe lo publica
solo a su hora. NO instantáneo.

Env vars necesarias:
  TELEGRAM_BOT_TOKEN          token del bot (BotFather)
  SERVERHUB_API_URL           https://tu-app.onrender.com
  SERVERHUB_API_KEY           la API_KEY que pusiste en el Server Hub
  DRIVE_UPLOAD_FOLDER_ID      carpeta de Drive donde sube el bot
                              (¡que NO sea FOTOS/VIDEOS, o el watcher la encola doble!)
  TELEGRAM_AUTHORIZED_CHAT_ID (opcional) restringe a tu chat
google_credentials.json debe estar en esta carpeta y la service account debe
tener acceso de Editor a la carpeta de subida.
"""
import os
import io
import sys
import time
import requests
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

TZ_MEXICO   = timezone(timedelta(hours=-5))
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(SCRIPT_DIR, "google_credentials.json")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]  # escritura (el watcher usa readonly)

ALIASES = {
    "fb": "facebook", "facebook": "facebook",
    "ig": "instagram", "insta": "instagram", "instagram": "instagram",
    "tt": "tiktok", "tiktok": "tiktok",
}

# ── Config desde entorno (lazy: no crashea al importar, se valida en main) ──
TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL          = os.getenv("SERVERHUB_API_URL", "").rstrip("/")
API_KEY          = os.getenv("SERVERHUB_API_KEY", "")
UPLOAD_FOLDER_ID = os.getenv("DRIVE_UPLOAD_FOLDER_ID", "")
AUTH_CHAT        = os.getenv("TELEGRAM_AUTHORIZED_CHAT_ID")  # opcional
TG = f"https://api.telegram.org/bot{TOKEN}"

AYUDA = ("Mándame una *FOTO* con este caption:\n"
         "`plataformas | YYYY-MM-DD HH:MM | texto`\n"
         "Ej: `fb,ig | 2026-06-25 18:30 | Mi post 🚀`\n"
         "Plataformas: fb, ig, tt. Hora = México (UTC-5).")


# ── Google Drive ──
def drive_service():
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds)

def subir_a_drive(svc, data_bytes, filename, mime):
    media = MediaIoBaseUpload(io.BytesIO(data_bytes), mimetype=mime, resumable=False)
    f = svc.files().create(
        body={"name": filename, "parents": [UPLOAD_FOLDER_ID]},
        media_body=media, fields="id",
    ).execute()
    fid = f["id"]
    # público (cualquiera con enlace) para que Meta pueda jalar la imagen
    svc.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    return fid


# ── Telegram ──
def tg(method, **params):
    return requests.post(f"{TG}/{method}", json=params, timeout=60).json()

def responder(chat_id, texto):
    tg("sendMessage", chat_id=chat_id, text=texto, parse_mode="Markdown")

def descargar_foto(file_id):
    info = tg("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{path}"
    return requests.get(url, timeout=60).content


# ── Parseo del caption ──
def parse_caption(caption):
    """Devuelve (datos, None) o (None, mensaje_error)."""
    try:
        plats_raw, resto = caption.split("|", 1)
        fecha, texto = resto.split("|", 1)
    except ValueError:
        return None, AYUDA
    plats_raw, fecha, texto = plats_raw.strip(), fecha.strip(), texto.strip()

    plats = []
    for p in plats_raw.replace(",", " ").split():
        key = ALIASES.get(p.lower())
        if key and key not in plats:
            plats.append(key)
    if not plats:
        return None, "No reconocí plataformas. Usa fb, ig, tt."

    try:
        datetime.strptime(fecha, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, "Fecha mal. Formato: `YYYY-MM-DD HH:MM` (hora México)."

    if not texto:
        return None, "Falta el texto del post."
    return {"plataformas": plats, "fecha_hora": fecha, "texto": texto}, None


# ── Server Hub API ──
def programar(payload):
    r = requests.post(f"{API_URL}/api/programar", json=payload,
                      headers={"X-API-Key": API_KEY}, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


# ── Manejo de mensajes ──
def manejar(msg):
    chat_id = msg["chat"]["id"]
    if AUTH_CHAT and str(chat_id) != str(AUTH_CHAT):
        return  # chat no autorizado, ignora

    if "photo" not in msg:
        responder(chat_id, AYUDA)
        return

    datos, err = parse_caption(msg.get("caption", ""))
    if err:
        responder(chat_id, "⚠️ " + err)
        return

    try:
        img = descargar_foto(msg["photo"][-1]["file_id"])  # [-1] = mayor resolución
        svc = drive_service()
        drive_id = subir_a_drive(svc, img, f"tg_{int(time.time())}.jpg", "image/jpeg")
    except Exception as e:
        responder(chat_id, f"❌ Error subiendo a Drive: {e}")
        return

    code, resp = programar({**datos, "archivo": drive_id})
    if resp.get("ok"):
        responder(chat_id,
                  f"✅ Programado *#{resp['id']}* para {datos['fecha_hora']} "
                  f"→ {', '.join(datos['plataformas'])}")
    else:
        detalle = resp.get("error") or resp.get("errores") or f"HTTP {code}"
        responder(chat_id, f"❌ No se programó: {detalle}")


def main():
    faltan = [k for k in ("TELEGRAM_BOT_TOKEN", "SERVERHUB_API_URL", "SERVERHUB_API_KEY",
                          "DRIVE_UPLOAD_FOLDER_ID") if not os.getenv(k)]
    if faltan:
        sys.exit(f"[bot] faltan env vars: {', '.join(faltan)}")
    if not os.path.exists(CREDENTIALS):
        sys.exit(f"[bot] falta google_credentials.json en {SCRIPT_DIR}")
    print("[bot] arrancando, escuchando Telegram…")
    offset = None
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=50)
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    manejar(u["message"])
        except Exception as e:
            print("[bot] error:", e)
            time.sleep(3)


def _selftest():
    """Chequeo del parser sin red:  python bot.py selftest"""
    ok, err = parse_caption("fb,ig | 2026-06-25 18:30 | Hola | mundo")
    assert err is None and ok["plataformas"] == ["facebook", "instagram"], ok
    assert ok["texto"] == "Hola | mundo", ok  # el texto conserva los pipes
    assert parse_caption("solo texto")[0] is None
    assert parse_caption("xx | 2026-06-25 18:30 | t")[0] is None  # plataforma inválida
    assert parse_caption("fb | 2026/06/25 | t")[0] is None        # fecha mal
    print("[OK] selftest paso")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        main()
