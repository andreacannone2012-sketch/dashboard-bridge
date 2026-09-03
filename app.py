from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import os
import shutil
import time
import json
import threading
from datetime import date, datetime, timezone, timedelta
from icalendar import Calendar

app = Flask(__name__)
CORS(app)

# ---------------- CONFIGURAZIONE ----------------
ICS_URL = os.environ.get("ICS_URL", "")
NAS_PATH = os.environ.get("NAS_PATH", "/nas")            # sola lettura: solo per statistiche spazio

# Percorso SCRIVIBILE per i dati propri del bridge (stato sonno, log, cache).
# Deve puntare a un volume Docker montato in lettura/scrittura (vedi docker-compose.yml),
# diverso da NAS_PATH che resta :ro apposta per sicurezza.
DATA_PATH = os.environ.get("DATA_PATH", "/data")
SLEEP_STATE_FILE = os.path.join(DATA_PATH, "sleep_state.json")
SLEEP_HISTORY_FILE = os.path.join(DATA_PATH, "sleep_history.json")
SLEEP_HISTORY_MAX = int(os.environ.get("SLEEP_HISTORY_MAX", "30"))  # quante notti tenere

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")  # es. http://192.168.1.50:2283/api
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")
IMMICH_UNKNOWN_ALBUM = os.environ.get("IMMICH_UNKNOWN_ALBUM", "Da taggare")
FACE_WAIT_SECONDS = float(os.environ.get("FACE_WAIT_SECONDS", "4"))

# Pulizia automatica file vecchi (audio/log) dentro DATA_PATH
CLEANUP_DAYS = int(os.environ.get("CLEANUP_DAYS", "7"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))
CLEANUP_SUBFOLDERS = ["recordings", "logs"]  # sottocartelle di DATA_PATH da ripulire
CLEANUP_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".log", ".json"}

os.makedirs(DATA_PATH, exist_ok=True)
_state_lock = threading.Lock()


def immich_headers(accept_json=True):
    h = {"x-api-key": IMMICH_API_KEY}
    if accept_json:
        h["Accept"] = "application/json"
    return h


# ==================================================================
# AGENDA (Google Calendar via ICS) — invariato
# ==================================================================
@app.route("/agenda")
def agenda():
    """Restituisce gli eventi di OGGI dal calendario Google (link ICS segreto)."""
    if not ICS_URL:
        return jsonify([])
    try:
        r = requests.get(ICS_URL, timeout=10)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
        today = date.today()
        events = []
        for component in cal.walk("VEVENT"):
            dtstart = component.get("dtstart").dt
            if isinstance(dtstart, datetime):
                ev_date = dtstart.date()
                time_str = dtstart.strftime("%H:%M")
            else:
                ev_date = dtstart
                time_str = "tutto il giorno"
            if ev_date == today:
                events.append({
                    "time": time_str,
                    "text": str(component.get("summary")),
                })
        events.sort(key=lambda e: e["time"])
        return jsonify(events)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================================================================
# NAS — invariato (sola lettura)
# ==================================================================
@app.route("/nas")
def nas():
    """Restituisce lo spazio disco reale del NAS (basato sul volume montato)."""
    try:
        total, used, _free = shutil.disk_usage(NAS_PATH)
        return jsonify({
            "online": True,
            "used_tb": round(used / (1024 ** 4), 2),
            "total_tb": round(total / (1024 ** 4), 2),
        })
    except Exception as e:
        return jsonify({"online": False, "error": str(e)}), 500


# ==================================================================
# IMMICH FACE — invariato
# ==================================================================
def immich_upload(file_storage):
    now_iso = datetime.now(timezone.utc).isoformat()
    files = {"assetData": (file_storage.filename or "scatto.jpg", file_storage.stream, file_storage.mimetype)}
    data = {
        "deviceAssetId": f"dashboard-{int(time.time()*1000)}",
        "deviceId": "dashboard-bridge",
        "fileCreatedAt": now_iso,
        "fileModifiedAt": now_iso,
    }
    r = requests.post(f"{IMMICH_URL}/assets", headers=immich_headers(accept_json=False),
                       files=files, data=data, timeout=20)
    r.raise_for_status()
    return r.json()["id"]


def immich_get_faces(asset_id):
    r = requests.get(f"{IMMICH_URL}/faces", headers=immich_headers(), params={"id": asset_id}, timeout=10)
    r.raise_for_status()
    return r.json()


def immich_delete_asset(asset_id):
    try:
        requests.delete(f"{IMMICH_URL}/assets", headers=immich_headers(),
                         json={"ids": [asset_id], "force": True}, timeout=10)
    except Exception:
        pass


def immich_find_or_create_album(name):
    r = requests.get(f"{IMMICH_URL}/albums", headers=immich_headers(), timeout=10)
    r.raise_for_status()
    for album in r.json():
        if album.get("albumName") == name:
            return album["id"]
    r = requests.post(f"{IMMICH_URL}/albums", headers=immich_headers(), json={"albumName": name}, timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def immich_add_to_album(album_id, asset_id):
    try:
        requests.put(f"{IMMICH_URL}/albums/{album_id}/assets", headers=immich_headers(),
                     json={"ids": [asset_id]}, timeout=10)
    except Exception:
        pass


@app.route("/identify", methods=["POST"])
def identify():
    """Riceve una foto, la carica su Immich, aspetta il riconoscimento facciale
    e risponde con la persona trovata (o 'sconosciuto')."""
    if not IMMICH_URL or not IMMICH_API_KEY:
        return jsonify({"status": "errore", "message": "IMMICH_URL / IMMICH_API_KEY non configurati"}), 500
    if "foto" not in request.files:
        return jsonify({"status": "errore", "message": "campo 'foto' mancante"}), 400

    try:
        asset_id = immich_upload(request.files["foto"])
    except Exception as e:
        return jsonify({"status": "errore", "message": f"upload fallito: {e}"}), 500

    time.sleep(FACE_WAIT_SECONDS)

    try:
        faces = immich_get_faces(asset_id)
    except Exception:
        faces = []

    person = None
    person_name = None
    for f in faces:
        name = (f.get("person") or {}).get("name") or f.get("personName")
        if f.get("personId") and name:
            person = f
            person_name = name
            break

    if person:
        person_id = person.get("personId")
        immich_delete_asset(asset_id)
        return jsonify({
            "status": "riconosciuto",
            "personId": person_id,
            "name": person_name,
            "thumbnailUrl": f"/persona-foto/{person_id}",
        })
    else:
        try:
            album_id = immich_find_or_create_album(IMMICH_UNKNOWN_ALBUM)
            immich_add_to_album(album_id, asset_id)
        except Exception:
            pass
        return jsonify({
            "status": "sconosciuto",
            "assetId": asset_id,
            "previewUrl": f"/foto-asset/{asset_id}",
        })


@app.route("/persona-foto/<person_id>")
def persona_foto(person_id):
    try:
        r = requests.get(f"{IMMICH_URL}/people/{person_id}/thumbnail",
                          headers=immich_headers(accept_json=False), timeout=10)
        r.raise_for_status()
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/foto-asset/<asset_id>")
def foto_asset(asset_id):
    try:
        r = requests.get(f"{IMMICH_URL}/assets/{asset_id}/thumbnail",
                          headers=immich_headers(accept_json=False), timeout=10)
        r.raise_for_status()
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================================================================
# SLEEP AS ANDROID — webhook + stato
# ==================================================================
# Sleep as Android (Impostazioni > Servizi > Webhook) può inviare una richiesta
# HTTP POST a un URL a tua scelta ad eventi come: inizio/fine tracciamento,
# sveglia suonata/posticipata, report di fine notte pronto. Il corpo esatto
# (JSON) è quello che TU configuri nel template del Webhook dentro l'app —
# questo endpoint accetta qualsiasi struttura JSON, la salva così com'è con
# un timestamp di ricezione, e la espone alla dashboard. Se in Sleep as
# Android non trovi un campo che ti aspetti qui, controlla il template del
# Webhook nell'app: è lì che si decide cosa viene mandato.
@app.route("/api/sleep", methods=["POST"])
def sleep_webhook():
    try:
        payload = request.get_json(silent=True)
        if payload is None:
            # fallback: alcuni webhook mandano form-urlencoded invece di JSON
            payload = request.form.to_dict() or {"raw": request.data.decode("utf-8", "ignore")}
    except Exception as e:
        return jsonify({"status": "errore", "message": f"payload illeggibile: {e}"}), 400

    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }

    with _state_lock:
        # stato "ultimo evento ricevuto", per un accesso rapido
        _write_json(SLEEP_STATE_FILE, entry)

        # storico limitato (utile per un grafico "ultime notti" in futuro)
        history = _read_json(SLEEP_HISTORY_FILE, default=[])
        history.append(entry)
        history = history[-SLEEP_HISTORY_MAX:]
        _write_json(SLEEP_HISTORY_FILE, history)

    return jsonify({"status": "ok"})


@app.route("/api/sleep/stats")
def sleep_stats():
    """Ultimo stato del sonno + storico recente, per la dashboard."""
    with _state_lock:
        last = _read_json(SLEEP_STATE_FILE, default=None)
        history = _read_json(SLEEP_HISTORY_FILE, default=[])
    return jsonify({
        "last": last,
        "history": history,
    })


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # scrittura atomica, evita file corrotti se il container muore a metà


# ==================================================================
# MANUTENZIONE — pulizia automatica file vecchi
# ==================================================================
def cleanup_old_files():
    """Cancella, dentro le sottocartelle CLEANUP_SUBFOLDERS di DATA_PATH,
    i file con estensione audio/log più vecchi di CLEANUP_DAYS giorni.
    NAS_PATH resta intoccato (è montato :ro apposta, per sicurezza)."""
    cutoff = time.time() - (CLEANUP_DAYS * 86400)
    removed = []
    for sub in CLEANUP_SUBFOLDERS:
        folder = os.path.join(DATA_PATH, sub)
        if not os.path.isdir(folder):
            continue
        for root, _dirs, files in os.walk(folder):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in CLEANUP_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        removed.append(fpath)
                except OSError:
                    pass
    if removed:
        print(f"[cleanup] rimossi {len(removed)} file più vecchi di {CLEANUP_DAYS} giorni")
    return removed


def cleanup_loop():
    # una passata subito all'avvio, poi ogni CLEANUP_INTERVAL_HOURS ore
    while True:
        try:
            cleanup_old_files()
        except Exception as e:
            print(f"[cleanup] errore: {e}")
        time.sleep(CLEANUP_INTERVAL_HOURS * 3600)


@app.route("/api/cleanup/run", methods=["POST"])
def cleanup_run_now():
    """Trigger manuale della pulizia (utile per test), invece di aspettare il timer."""
    removed = cleanup_old_files()
    return jsonify({"status": "ok", "removed_count": len(removed), "removed": removed})


# ==================================================================
@app.route("/health")
def healthcheck():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    app.run(host="0.0.0.0", port=8420)
