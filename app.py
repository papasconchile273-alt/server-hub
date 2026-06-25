import os
import json
import sqlite3
import secrets
import requests
import threading
import time
from datetime import datetime, timedelta, timezone

TZ_MEXICO = timezone(timedelta(hours=-5))
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ── SECRET_KEY ──
# Nunca usar un valor por defecto predecible: con un SECRET_KEY conocido
# cualquiera puede falsificar la cookie de sesión y entrar sin contraseña.
# En Render: configúralo en Environment. Si falta, generamos uno aleatorio
# por proceso (seguro, pero cierra las sesiones al reiniciar).
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("[WARN] SECRET_KEY no configurada. Usando una temporal aleatoria. "
          "Configúrala en Render para mantener las sesiones entre reinicios.")
app.config['SECRET_KEY'] = SECRET_KEY

# ── Cookies de sesión endurecidas ──
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '1') == '1',  # Render sirve por HTTPS
    # Limita el tamaño del cuerpo de la petición (anti abuso / DoS básico): 1 MB
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

# ── Protección CSRF en todos los formularios/POST ──
csrf = CSRFProtect(app)

# ── Rate limiting (anti fuerza bruta y abuso) ──
# Almacenamiento en memoria: válido porque corremos 1 worker (ver Procfile).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)

# ── Cabeceras de seguridad HTTP en todas las respuestas ──
@app.after_request
def cabeceras_seguridad(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "img-src 'self' https://drive.google.com data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return resp

# ── Timeout de sesión: 60 minutos ──
INACTIVIDAD_MAX = timedelta(minutes=60)

@app.before_request
def verificar_inactividad():
    rutas_publicas = {'login', 'static'}
    if request.endpoint in rutas_publicas:
        return
    if current_user.is_authenticated:
        ultima = session.get('ultima_actividad')
        ahora  = datetime.now(TZ_MEXICO)
        if ultima:
            ultima_dt = datetime.fromisoformat(ultima)
            if ultima_dt.tzinfo is None:
                ultima_dt = ultima_dt.replace(tzinfo=TZ_MEXICO)
            if ahora - ultima_dt > INACTIVIDAD_MAX:
                logout_user()
                session.clear()
                flash('Tu sesión expiró por inactividad. Vuelve a identificarte.')
                return redirect(url_for('login'))
        session['ultima_actividad'] = ahora.isoformat()

# ── RUTA FIJA — misma que watcher.py ──
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publicaciones.db")

# ─────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Acceso restringido. Por favor identifícate."

class Usuario(UserMixin):
    def __init__(self, id, username, password_encriptada):
        self.id = id
        self.username = username
        self.password = password_encriptada

def _build_users():
    # Usuarios y contraseñas SIEMPRE desde variables de entorno.
    # Nada hardcodeado: un usuario/contraseña por defecto en el código es una
    # credencial pública que permite entrar a cualquiera que lo lea.
    users = {}
    slots = [
        ('1', 'ADMIN_USERNAME',  'ADMIN_PASSWORD'),
        ('2', 'ADMIN_USERNAME2', 'ADMIN_PASSWORD2'),
        ('3', 'ADMIN_USERNAME3', 'ADMIN_PASSWORD3'),
    ]
    for uid, uenv, penv in slots:
        u = os.getenv(uenv)
        p = os.getenv(penv)
        if u and p:
            users[uid] = Usuario(
                id=uid,
                username=u,
                password_encriptada=generate_password_hash(p)
            )
    return users

USUARIOS = _build_users()
if not USUARIOS:
    print("[WARN] No hay usuarios configurados. Define ADMIN_USERNAME y "
          "ADMIN_PASSWORD en las variables de entorno de Render.")

@login_manager.user_loader
def load_user(user_id):
    return USUARIOS.get(user_id)

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 50 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated and not request.args.get('ok'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        usuario_ingresado  = request.form.get('username', '').strip()
        password_ingresada = request.form.get('password', '')
        autenticado = None
        for u in USUARIOS.values():
            if u.username == usuario_ingresado and check_password_hash(u.password, password_ingresada):
                autenticado = u
                break
        if autenticado:
            login_user(autenticado, remember=False)
            session['ultima_actividad'] = datetime.now(TZ_MEXICO).isoformat()
            return redirect(url_for('login') + '?ok=1')
        else:
            flash('Credenciales incorrectas. Intenta de nuevo.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# WEBHOOK DE MAKE
# ─────────────────────────────────────────

# URL del webhook de Make desde variable de entorno (no exponerla en el código).
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")

# Clave para la API externa (bot de Telegram, etc.). Si no se configura,
# el endpoint /api/programar queda deshabilitado (devuelve 401).
API_KEY = os.getenv("API_KEY")

LIMITES = {
    "instagram": 2200,
    "facebook":  63206,
    "tiktok":    2200,
}

# ─────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS publicaciones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plataformas TEXT    NOT NULL,
                texto       TEXT    NOT NULL,
                fecha_hora  TEXT    NOT NULL,
                archivo     TEXT,
                estado      TEXT    DEFAULT 'Programado'
            )
        """)
        # Sin seeds: posts sin archivo rompen Drive en Make (BundleValidationError).

init_db()

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def validar_post(plataformas, texto, fecha_hora, archivo=None):
    errores = []
    if not plataformas:
        errores.append("Selecciona al menos una plataforma.")
    if not texto or not texto.strip():
        errores.append("El texto no puede estar vacío.")
    else:
        for p in plataformas:
            limite = LIMITES.get(p, 2200)
            if len(texto) > limite:
                errores.append(f"Texto demasiado largo para {p} ({len(texto)}/{limite} caracteres).")
    if not fecha_hora:
        errores.append("La fecha y hora son obligatorias.")
    # IG/FB/TT exigen media — sin File ID de Drive el webhook a Make falla.
    if plataformas and not archivo:
        errores.append("Pega el File ID de Google Drive en el campo 'archivo'.")
    return errores

def row_a_dict(row):
    d = dict(row)
    d["plataformas"] = json.loads(d["plataformas"])
    ESTADOS_VALIDOS = {'programado': 'Programado', 'pausado': 'Pausado',
                       'publicado': 'Publicado', 'error': 'Error'}
    d["estado"] = ESTADOS_VALIDOS.get(d["estado"].lower(), d["estado"].capitalize())
    return d

# ─────────────────────────────────────────
# ENVIAR A MAKE WEBHOOK
# ─────────────────────────────────────────

def enviar_a_make(publicacion):
    if not MAKE_WEBHOOK_URL:
        print("[Make] ❌ MAKE_WEBHOOK_URL no configurada en variables de entorno")
        return False
    payload = {
        "texto":       publicacion["texto"],
        "plataformas": publicacion["plataformas"],
        # Campo plano para filtrar en Make sin join() sobre array (más robusto).
        "plataformas_str": ",".join(publicacion["plataformas"]),
        "fecha_hora":  publicacion["fecha_hora"],
        "archivo":     publicacion.get("archivo"),
        "id":          publicacion["id"],
    }
    try:
        response = requests.post(MAKE_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"[Make] ✅ Post ID {publicacion['id']} enviado correctamente")
            return True
        else:
            print(f"[Make] ❌ Error {response.status_code}: {response.text}")
            return False
    except requests.exceptions.Timeout:
        print(f"[Make] ❌ Timeout al enviar post ID {publicacion['id']}")
        return False
    except Exception as e:
        print(f"[Make] ❌ Error inesperado: {str(e)}")
        return False

# ─────────────────────────────────────────
# SCHEDULER AUTOMÁTICO
# ─────────────────────────────────────────

def scheduler():
    print("[Scheduler] ✅ Iniciado, revisando cada minuto (hora México CDT UTC-5)...")
    while True:
        ahora = datetime.now(TZ_MEXICO).strftime("%Y-%m-%d %H:%M")
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT * FROM publicaciones WHERE LOWER(estado)='programado' AND fecha_hora=?",
                    (ahora,)
                ).fetchall()
            for row in rows:
                pub = row_a_dict(row)
                print(f"[Scheduler] 🚀 Publicando: {pub['texto'][:50]}...")
                exito = enviar_a_make(pub)
                nuevo_estado = "Publicado" if exito else "Error"
                with get_db() as conn:
                    conn.execute(
                        "UPDATE publicaciones SET estado=? WHERE id=?",
                        (nuevo_estado, pub["id"])
                    )
        except Exception as e:
            print(f"[Scheduler] ❌ Error: {str(e)}")
        time.sleep(60)

# Arrancar el scheduler solo donde corresponde. Con varios workers de gunicorn
# cada uno importaría este módulo y correría su propio scheduler -> el mismo post
# se publicaría N veces. Mantén UN solo worker (ver Procfile) o desactiva el
# scheduler aquí con RUN_SCHEDULER=0 si lo mueves a un proceso aparte.
if os.getenv("RUN_SCHEDULER", "1") == "1":
    hilo = threading.Thread(target=scheduler, daemon=True)
    hilo.start()

# ─────────────────────────────────────────
# RUTAS PROTEGIDAS
# ─────────────────────────────────────────

@app.route("/")
@login_required
def index():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM publicaciones ORDER BY fecha_hora ASC"
        ).fetchall()
    publicaciones = [row_a_dict(r) for r in rows]
    return render_template("index.html", publicaciones=publicaciones)

@app.route("/agregar", methods=["POST"])
@login_required
def agregar():
    plataformas = request.form.getlist("plataformas")
    texto       = request.form.get("texto", "").strip()
    fecha_hora  = request.form.get("fecha_hora", "").strip().replace("T", " ")
    archivo     = request.form.get("archivo", "").strip() or None
    errores = validar_post(plataformas, texto, fecha_hora, archivo)
    if errores:
        for e in errores:
            flash(e)
        return redirect(url_for("index"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO publicaciones (plataformas, texto, fecha_hora, archivo) VALUES (?,?,?,?)",
            (json.dumps(plataformas), texto, fecha_hora, archivo)
        )
    return redirect(url_for("index"))

@app.route("/editar/<int:pub_id>", methods=["POST"])
@login_required
def editar(pub_id):
    plataformas = request.form.getlist("plataformas")
    texto       = request.form.get("texto", "").strip()
    fecha_hora  = request.form.get("fecha_hora", "").strip().replace("T", " ")
    archivo     = request.form.get("archivo", "").strip() or None
    errores = validar_post(plataformas, texto, fecha_hora, archivo)
    if errores:
        for e in errores:
            flash(e)
    else:
        with get_db() as conn:
            conn.execute(
                """UPDATE publicaciones
                   SET plataformas=?, texto=?, fecha_hora=?, archivo=?
                   WHERE id=?""",
                (json.dumps(plataformas), texto, fecha_hora, archivo, pub_id)
            )
    return redirect(url_for("index"))

@app.route("/eliminar/<int:pub_id>", methods=["POST"])
@login_required
def eliminar(pub_id):
    with get_db() as conn:
        conn.execute("DELETE FROM publicaciones WHERE id=?", (pub_id,))
    return redirect(url_for("index"))

@app.route("/estado/<int:pub_id>", methods=["POST"])
@login_required
def cambiar_estado(pub_id):
    nuevo = request.json.get("estado", "").strip().capitalize()
    if nuevo in ("Programado", "Pausado"):
        with get_db() as conn:
            conn.execute(
                "UPDATE publicaciones SET estado=? WHERE id=?",
                (nuevo, pub_id)
            )
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400

@app.route("/publicar-ahora/<int:pub_id>", methods=["POST"])
@login_required
def publicar_ahora(pub_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM publicaciones WHERE id=?", (pub_id,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "No encontrado"}), 404
    pub = row_a_dict(row)
    exito = enviar_a_make(pub)
    nuevo_estado = "Publicado" if exito else "Error"
    with get_db() as conn:
        conn.execute(
            "UPDATE publicaciones SET estado=? WHERE id=?",
            (nuevo_estado, pub_id)
        )
    return jsonify({"ok": exito})

@app.route("/admin/normalizar-estados")
@login_required
def normalizar_estados():
    with get_db() as conn:
        conn.execute("UPDATE publicaciones SET estado='Programado' WHERE LOWER(estado)='programado'")
        conn.execute("UPDATE publicaciones SET estado='Pausado'    WHERE LOWER(estado)='pausado'")
        conn.execute("UPDATE publicaciones SET estado='Publicado'  WHERE LOWER(estado)='publicado'")
        conn.execute("UPDATE publicaciones SET estado='Error'      WHERE LOWER(estado)='error'")
    return "✅ Estados normalizados. Ya puedes cerrar esta pestaña."

@app.route("/api/publicaciones")
@login_required
def api_publicaciones():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM publicaciones WHERE estado='Programado' ORDER BY fecha_hora ASC"
        ).fetchall()
    return jsonify([row_a_dict(r) for r in rows])

def _api_key_valida():
    """Auth para la API externa. Acepta header X-API-Key o Authorization: Bearer."""
    if not API_KEY:
        return False  # sin clave configurada => endpoint deshabilitado
    enviada = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    if not enviada and auth.startswith("Bearer "):
        enviada = auth[7:]
    return secrets.compare_digest(enviada, API_KEY)

@app.route("/api/programar", methods=["POST"])
@csrf.exempt
@limiter.limit("30 per minute")
def api_programar():
    """Programa un post desde afuera (bot de Telegram, etc.). Entra a la cola
    como 'Programado'; el scheduler lo publica solo a su fecha_hora."""
    if not _api_key_valida():
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    data = request.get_json(silent=True) or {}
    plataformas = data.get("plataformas") or []
    if isinstance(plataformas, str):
        plataformas = [p.strip() for p in plataformas.split(",") if p.strip()]
    texto      = (data.get("texto") or "").strip()
    fecha_hora = (data.get("fecha_hora") or "").strip().replace("T", " ")
    archivo    = (data.get("archivo") or "").strip() or None

    errores = validar_post(plataformas, texto, fecha_hora, archivo)
    if errores:
        return jsonify({"ok": False, "errores": errores}), 400

    # La fecha DEBE tener este formato exacto o el scheduler nunca la dispara.
    try:
        datetime.strptime(fecha_hora, "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"ok": False,
                        "error": "fecha_hora debe ser 'YYYY-MM-DD HH:MM' (hora México, UTC-5)"}), 400

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO publicaciones (plataformas, texto, fecha_hora, archivo) VALUES (?,?,?,?)",
            (json.dumps(plataformas), texto, fecha_hora, archivo)
        )
        nuevo_id = cur.lastrowid
    return jsonify({"ok": True, "id": nuevo_id, "estado": "Programado",
                    "fecha_hora": fecha_hora, "plataformas": plataformas})

# ─────────────────────────────────────────
# ARRANQUE
# ─────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)