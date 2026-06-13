import os
import io
import time
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account

TZ_MEXICO = timezone(timedelta(hours=-5))  # igual que app.py
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('watcher.log', encoding='utf-8')
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS  = os.path.join(SCRIPT_DIR, "google_credentials.json")
DB           = os.path.join(SCRIPT_DIR, "publicaciones.db")
SCOPES       = ["https://www.googleapis.com/auth/drive.readonly"]

# ID de tu carpeta raíz en Google Drive
# (la parte final de la URL de tu carpeta)
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "1VXiduG9NawnYchtEz3Wcrt_o1U-azWKl")

# Subcarpetas que monitoreamos dentro de SERVERHUB_MEDIA
CARPETAS = {
    "FOTOS":   {"plataformas": ["instagram", "facebook"], "tipo": "imagen"},
    "VIDEOS":  {"plataformas": ["instagram", "facebook"], "tipo": "video"},
    "TIKTOKS": {"plataformas": ["tiktok"],                "tipo": "video"},
    "STORIES": {"plataformas": ["instagram"],             "tipo": "story"},
}

EXTENSIONES = {
    "imagen": [".jpg", ".jpeg", ".png", ".webp"],
    "video":  [".mp4", ".mov", ".avi", ".mkv"],
    "story":  [".jpg", ".jpeg", ".png", ".mp4", ".mov"],
}

# ─────────────────────────────────────────
# GOOGLE DRIVE — CONEXIÓN
# ─────────────────────────────────────────

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)

def listar_archivos_en_carpeta(service, folder_id):
    """Lista todos los archivos dentro de una carpeta de Drive."""
    resultado = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token
        ).execute()

        resultado.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return resultado

def listar_subcarpetas(service, folder_id):
    """Devuelve dict {nombre_carpeta: id_carpeta} dentro de folder_id."""
    response = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
        spaces="drive",
        fields="files(id, name)"
    ).execute()
    return {f["name"].upper(): f["id"] for f in response.get("files", [])}

# ─────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def ya_registrado(drive_file_id):
    """Usa el ID de Drive como identificador único (nunca cambia)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM publicaciones WHERE archivo = ?", (drive_file_id,)
            ).fetchone()
        return row is not None
    except Exception as e:
        log.error(f"❌ Error verificando duplicado: {e}")
        return False

def registrar(nombre_archivo, drive_file_id, plataformas, tipo):
    """Encola el archivo en la base de datos."""
    fecha_hora = datetime.now(TZ_MEXICO).strftime("%Y-%m-%d %H:%M")
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO publicaciones (plataformas, texto, fecha_hora, archivo, estado)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    json.dumps(plataformas),
                    f"[{tipo.upper()}] {nombre_archivo}",
                    fecha_hora,
                    drive_file_id,   # guardamos el ID de Drive
                    "Programado"
                )
            )
        log.info(f"✅ Encolado: {nombre_archivo} → {', '.join(plataformas)}")
    except Exception as e:
        log.error(f"❌ Error al registrar {nombre_archivo}: {e}")

# ─────────────────────────────────────────
# ESCANEO PRINCIPAL
# ─────────────────────────────────────────

def escanear(service):
    log.info("🔍 Escaneando Google Drive...")
    encontrados = 0

    # Obtener subcarpetas reales en Drive
    subcarpetas_drive = listar_subcarpetas(service, DRIVE_FOLDER_ID)
    log.info(f"   Subcarpetas encontradas en Drive: {list(subcarpetas_drive.keys()) or 'ninguna'}")

    for nombre_carpeta, config in CARPETAS.items():
        folder_id = subcarpetas_drive.get(nombre_carpeta)

        if not folder_id:
            log.debug(f"ℹ️  Carpeta '{nombre_carpeta}' no existe en Drive, se omite.")
            continue

        archivos = listar_archivos_en_carpeta(service, folder_id)
        log.info(f"   [{nombre_carpeta}] {len(archivos)} archivo(s) encontrado(s)")

        for archivo in archivos:
            nombre = archivo["name"]
            file_id = archivo["id"]
            ext = os.path.splitext(nombre)[1].lower()

            # Verificar extensión
            if ext not in EXTENSIONES.get(config["tipo"], []):
                log.debug(f"⏭️  Extensión ignorada: {nombre}")
                continue

            # Evitar duplicados
            if ya_registrado(file_id):
                continue

            registrar(nombre, file_id, config["plataformas"], config["tipo"])
            encontrados += 1

    if encontrados == 0:
        log.info("   Sin archivos nuevos.")
    else:
        log.info(f"   ✅ {encontrados} archivo(s) nuevo(s) encolado(s).")

# ─────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  SERVERHUB — Watcher Google Drive activo")
    log.info("=" * 50)
    log.info(f"  Carpeta Drive ID : {DRIVE_FOLDER_ID}")
    log.info(f"  Base de datos    : {DB}")
    log.info(f"  Revisando cada 60 segundos...\n")

    # Verificaciones iniciales
    if not os.path.exists(CREDENTIALS):
        log.error(f"❌ No se encontró google_credentials.json en: {SCRIPT_DIR}")
        log.error("   Descárgalo de Google Cloud Console y ponlo en la carpeta del proyecto.")
        exit(1)

    if not os.path.exists(DB):
        log.error(f"❌ Base de datos no encontrada: {DB}")
        log.error("   Corre app.py al menos una vez para crearla.")
        exit(1)

    # Conectar a Drive
    try:
        service = get_drive_service()
        log.info("✅ Conectado a Google Drive correctamente.\n")
    except Exception as e:
        log.error(f"❌ Error al conectar con Google Drive: {e}")
        exit(1)

    # Loop
    while True:
        try:
            escanear(service)
        except Exception as e:
            log.error(f"❌ Error en escaneo: {e}")
        time.sleep(60)