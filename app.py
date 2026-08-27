import os
import re
import json
import uuid
import shutil
import httpx
import subprocess
import xml.etree.ElementTree as ET
from typing import Optional
from pathlib import Path
from datetime import datetime
import httpx
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from db import (
    init_db, db_get_beats, db_get_beat, db_insert_beat, db_update_beat_status,
    db_get_licenses, db_count_licenses, db_insert_license,
    db_get_history, db_insert_history, db_count_licenses_by_order_prefix,
    db_update_beat, db_delete_beat, db_insert_lead, db_get_leads
)
from fastapi import FastAPI, UploadFile, File, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import essentia.standard as es
from basic_pitch.inference import predict_and_save
from basic_pitch import ICASSP_2022_MODEL_PATH

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def on_startup():
    init_db()
    

BASE_DIR = Path(__file__).parent
DOWNLOADS = BASE_DIR / "downloads"
BEATS_DIR = BASE_DIR / "beats"
LICENSES_DIR = BASE_DIR / "licenses"
for d in (DOWNLOADS, BEATS_DIR, LICENSES_DIR):
    d.mkdir(exist_ok=True)

HISTORY_FILE = BASE_DIR / "history.json"
BEATS_FILE = BASE_DIR / "beats.json"
LICENSES_FILE = BASE_DIR / "licenses.json"

NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "").rstrip("/")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER", "")
NEXTCLOUD_APP_PASSWORD = os.getenv("NEXTCLOUD_APP_PASSWORD", "")
NEXTCLOUD_BEATS_FOLDER = os.getenv("NEXTCLOUD_BEATS_FOLDER", "/Beats").strip("/")
PRODUCER_NAME = os.getenv("PRODUCER_NAME", "")
PRODUCER_ALIAS = os.getenv("PRODUCER_ALIAS", "")

WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "")
N8N_LEAD_WEBHOOK = os.getenv("N8N_LEAD_WEBHOOK", "") 

GENRES = ["Boom Bap", "Trap", "Reggaeton"]


def safe_genre_folder(genre: str) -> str:
    g = (genre or "").strip()
    return g if g in GENRES else "Otros"


app.mount("/files", StaticFiles(directory=DOWNLOADS), name="files")
app.mount("/assets", StaticFiles(directory=BASE_DIR / "assets"), name="assets")
app.mount("/beat-files", StaticFiles(directory=BEATS_DIR), name="beat-files")
app.mount("/license-files", StaticFiles(directory=LICENSES_DIR), name="license-files")


# ---------- utilidades de historial / json ----------

def load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def save_history_entry(entry: dict):
    db_insert_history(entry)


# ---------- análisis de audio ----------

def analyze_audio(wav_path: str):
    audio = es.MonoLoader(filename=wav_path)()

    key_extractor = es.KeyExtractor()
    key, scale, strength = key_extractor(audio)

    rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
    bpm, beats, beats_confidence, _, _ = rhythm_extractor(audio)

    return {
        "key": key,
        "scale": scale,
        "confidence": round(float(strength), 3),
        "bpm": round(float(bpm), 1),
    }


def audio_to_midi(wav_path: str, output_dir: str):
    predict_and_save(
        [wav_path],
        output_dir,
        save_midi=True,
        sonify_midi=False,
        save_model_outputs=False,
        save_notes=False,
        model_or_model_path=ICASSP_2022_MODEL_PATH,
    )
    base_name = Path(wav_path).stem
    return f"{base_name}_basic_pitch.mid"


def build_filename(prefix: str, job_id: str, analysis: dict, ext: str) -> str:
    key = analysis["key"].replace("#", "sharp").replace("b", "flat")
    scale = analysis["scale"]
    bpm = int(round(analysis["bpm"]))
    safe = re.sub(r"[^A-Za-z0-9_-]", "", f"{prefix}_{job_id}_{bpm}bpm_{key}{scale}")
    return f"{safe}{ext}"
  
  
TAG_PATH = BASE_DIR / "assets" / "tag" / "tag-rxssettprxd.wav"
TAG_INTERVAL_SECONDS = 18


def generate_watermarked_preview(source_path: Path, output_path: Path):
    """
    Genera un MP3 preview a 128kbps con el tag del productor insertado
    cada TAG_INTERVAL_SECONDS, usando ffmpeg. Si no existe tag.wav,
    genera el preview sin watermark.
    """
    if not TAG_PATH.exists():
        cmd = [
            "ffmpeg", "-y", "-i", str(source_path),
            "-af", "loudnorm=I=-14:TP=-1:LRA=11",
            "-codec:a", "libmp3lame", "-b:a", "128k",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True)
        return

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = 0

    delays = []
    t = TAG_INTERVAL_SECONDS
    while t < duration:
        delays.append(t)
        t += TAG_INTERVAL_SECONDS

    if not delays:
        cmd = [
            "ffmpeg", "-y", "-i", str(source_path),
            "-af", "loudnorm=I=-14:TP=-1:LRA=11",
            "-codec:a", "libmp3lame", "-b:a", "128k",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True)
        return

    tag_inputs = []
    filter_parts = []
    for i, delay_sec in enumerate(delays):
        tag_inputs.extend(["-i", str(TAG_PATH)])
        delay_ms = int(delay_sec * 1000)
        filter_parts.append(f"[{i+1}:a]adelay={delay_ms}|{delay_ms}[tag{i}]")

    mix_inputs = "[0:a]" + "".join(f"[tag{i}]" for i in range(len(delays)))
    filter_complex = ";".join(filter_parts) + f";{mix_inputs}amix=inputs={len(delays)+1}:duration=first:dropout_transition=0,loudnorm=I=-14:TP=-1:LRA=11[out]"
            
    cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        *tag_inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-codec:a", "libmp3lame", "-b:a", "128k",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True)


# ---------- Nextcloud (WebDAV & OCS Sharing) ----------

def ensure_nextcloud_folder(remote_folder_path: str):
    if not (NEXTCLOUD_URL and NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD):
        return
    encoded_path = quote(remote_folder_path.strip("/"))
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{encoded_path}"
    try:
        requests.request("MKCOL", url, auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD), timeout=30)
    except requests.RequestException:
        pass


def upload_to_nextcloud(local_path: Path, remote_filename: str, subfolder: str = "") -> bool:
    if not (NEXTCLOUD_URL and NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD):
        return False

    folder_path = f"{NEXTCLOUD_BEATS_FOLDER}/{subfolder}".strip("/") if subfolder else NEXTCLOUD_BEATS_FOLDER
    ensure_nextcloud_folder(folder_path)

    encoded_folder = quote(folder_path)
    encoded_filename = quote(remote_filename)
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{encoded_folder}/{encoded_filename}"
    with open(local_path, "rb") as f:
        resp = requests.put(
            url,
            data=f,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD),
            timeout=120,
        )
    return resp.status_code in (200, 201, 204)
  

def delete_from_nextcloud(remote_filename: str, subfolder: str = "") -> bool:
    if not (NEXTCLOUD_URL and NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD):
        return False
    folder_path = f"{NEXTCLOUD_BEATS_FOLDER}/{subfolder}".strip("/") if subfolder else NEXTCLOUD_BEATS_FOLDER
    encoded_folder = quote(folder_path)
    encoded_filename = quote(remote_filename)
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{encoded_folder}/{encoded_filename}"
    try:
        resp = requests.delete(url, auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD), timeout=30)
        return resp.status_code in (200, 204, 404)  # 404 = ya no existía, lo tratamos como éxito
    except requests.RequestException:
        return False

  

def create_nextcloud_public_share(folder_path: str) -> Optional[str]:
    """
    Crea un enlace público de lectura para la carpeta especificada usando la API OCS Share de Nextcloud.
    """
    if not (NEXTCLOUD_URL and NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD):
        return None

    base_folder = NEXTCLOUD_BEATS_FOLDER.strip("/")
    clean_folder = folder_path.strip("/")
    
    full_path = f"/{base_folder}/{clean_folder}" if base_folder else f"/{clean_folder}"

    share_url = f"{NEXTCLOUD_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    headers = {"OCS-APIRequest": "true"}

    payload = {
        "path": full_path,
        "shareType": 3,   # 3 = Link Público
        "permissions": 1, # 1 = Permiso de lectura
    }

    try:
        resp = requests.post(
            share_url,
            data=payload,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD),
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            url_node = root.find(".//url")
            if url_node is not None and url_node.text:
                return url_node.text
    except Exception as e:
        print(f"Error creando Share Link: {e}")
    return None


# ---------- LaTeX / licencia ----------

def escape_latex(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    for orig, esc in replacements.items():
        text = text.replace(orig, esc)
    return text


def next_order_id(beat_id: str) -> str:
    base = f"bt{beat_id}11"
    previas = db_count_licenses_by_order_prefix(base)
    if previas == 0:
        return base
    return f"{base}-{previas + 1}"


def generate_license_pdf(beat_name, artistic_name, real_name, price, order_id, payment_note=""):
    template = (BASE_DIR / "license_template.tex").read_text(encoding="utf-8")

    payment_note_str = f" ({payment_note})" if payment_note.strip() else ""

    tokens = {
        "@@PRODUCER_ALIAS@@": escape_latex(PRODUCER_ALIAS),
        "@@PRODUCER_NAME@@": escape_latex(PRODUCER_NAME),
        "@@ORDER_ID@@": escape_latex(order_id),
        "@@BEAT_NAME@@": escape_latex(beat_name),
        "@@ARTISTIC_NAME@@": escape_latex(artistic_name),
        "@@REAL_NAME@@": escape_latex(real_name),
        "@@PRICE@@": escape_latex(price),
        "@@PAYMENT_NOTE@@": escape_latex(payment_note_str),
        "@@DATE@@": escape_latex(datetime.now().strftime("%d de %B de %Y")),
    }
    for token, value in tokens.items():
        template = template.replace(token, value)

    job_id = str(uuid.uuid4())[:8]
    tex_filename = f"license_{job_id}.tex"
    tex_path = LICENSES_DIR / tex_filename
    tex_path.write_text(template, encoding="utf-8")

    for _ in range(2):
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", str(LICENSES_DIR), str(tex_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    pdf_path = LICENSES_DIR / f"license_{job_id}.pdf"
    if not pdf_path.exists():
        raise RuntimeError(result.stdout[-1500:])

    for ext in (".aux", ".log", ".tex", ".out"):
        aux = LICENSES_DIR / f"license_{job_id}{ext}"
        if aux.exists():
            aux.unlink()

    final_name = re.sub(r"[^A-Za-z0-9_-]", "", f"licencia_{artistic_name}_{order_id}") + ".pdf"
    final_path = LICENSES_DIR / final_name
    pdf_path.rename(final_path)

    return final_name


# ---------- rutas API ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    host = request.headers.get("host", "")
    if host.startswith("panelstxre."):
        return templates.TemplateResponse(request=request, name="panel.html")
    return templates.TemplateResponse(
        request=request,
        name="player.html",
        context={"whatsapp_number": WHATSAPP_NUMBER},
    )



@app.get("/player", response_class=HTMLResponse)
def player(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return templates.TemplateResponse(
        request=request,
        name="player.html",
        context={"whatsapp_number": WHATSAPP_NUMBER},
    )
  

@app.get("/panelstxre", response_class=HTMLResponse)
def panel(request: Request):
    return templates.TemplateResponse(request=request, name="panel.html")


@app.get("/history")
def get_history():
    return db_get_history()


@app.post("/beats/interest")
async def register_interest(
    beat_id: str = Form(...),
    name: str = Form(...),
    contact: str = Form(...),
    message: str = Form(""),
):
    beat = db_get_beat(beat_id)
    if not beat:
        return JSONResponse(status_code=404, content={"error": "Beat no encontrado"})

    entry = {
        "beat_id": beat_id,
        "beat_name": beat["beat_name"],
        "name": name,
        "contact": contact,
        "message": message,
    }
    db_insert_lead(entry)

    if N8N_LEAD_WEBHOOK:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(N8N_LEAD_WEBHOOK, json=entry)
        except Exception:
            pass  

    return {"status": "ok"}
  
  
@app.get("/leads")
def get_leads():
    return db_get_leads()


@app.get("/beats")
def get_beats(genre: Optional[str] = None, include_sold: bool = False):
    return db_get_beats(genre=genre, include_sold=include_sold)


@app.get("/licenses")
def get_licenses():
    return db_get_licenses()


@app.post("/beats/sell-and-license")
def sell_and_generate_license(
    beat_id: str = Form(...),
    status: str = Form(...),
    artistic_name: Optional[str] = Form(""),
    real_name: Optional[str] = Form(""),
    price: str = Form("$600.00 MXN"),
    payment_note: str = Form(""),
):
    if status not in ("disponible", "apartado", "vendido"):
        return JSONResponse(status_code=400, content={"error": "Estado no válido"})

    beat = db_get_beat(beat_id)
    if not beat:
        return JSONResponse(status_code=404, content={"error": "Beat no encontrado"})

    license_entry = None
    public_share_url = None

    if status == "vendido":
        if not artistic_name or not real_name:
            return JSONResponse(
                status_code=400,
                content={"error": "Para marcar como 'Vendido' debes ingresar Nombre artístico y Nombre completo."},
            )

        order_id = next_order_id(beat["id"])
        try:
            pdf_filename = generate_license_pdf(
                beat["beat_name"], artistic_name, real_name, price, order_id, payment_note
            )
        except Exception as e:
            return JSONResponse(
                status_code=500, content={"error": "Fallo generando la licencia PDF", "detail": str(e)[-800:]}
            )

        safe_beat_folder = re.sub(r"[^A-Za-z0-9_-]", "_", beat["beat_name"])
        remote_licencias_root = "Licencias"
        remote_delivery_folder = f"Licencias/{safe_beat_folder}"

        ensure_nextcloud_folder(remote_licencias_root)
        ensure_nextcloud_folder(remote_delivery_folder)

        pdf_local_path = LICENSES_DIR / pdf_filename
        upload_to_nextcloud(pdf_local_path, pdf_filename, subfolder=remote_delivery_folder)

        genre_folder = safe_genre_folder(beat.get("genre"))
        beat_local_path = BEATS_DIR / genre_folder / beat["filename"]
        if not beat_local_path.exists():
            beat_local_path = BEATS_DIR / beat["filename"]
        if beat_local_path.exists():
            upload_to_nextcloud(beat_local_path, beat["filename"], subfolder=remote_delivery_folder)

        public_share_url = create_nextcloud_public_share(remote_delivery_folder)

        license_entry = {
            "order_id": order_id,
            "beat_name": beat["beat_name"],
            "artistic_name": artistic_name,
            "real_name": real_name,
            "price": price,
            "file": f"/license-files/{pdf_filename}",
            "public_share_url": public_share_url,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        db_insert_license(license_entry)

    # Solo llega aquí si "vendido" ya generó la licencia con éxito,
    # o si el status es "disponible"/"apartado" (que no tienen pasos que puedan fallar)
    db_update_beat_status(beat_id, status)

    return {
        "status": "ok",
        "beat_name": beat["beat_name"],
        "beat_file": beat.get("file"),
        "new_status": status,
        "license": license_entry,
    }


@app.post("/download")
async def download_from_url(url: str = Form(...)):
    job_id = str(uuid.uuid4())[:8]
    output_template = str(DOWNLOADS / f"{job_id}.%(ext)s")
    cookies_path = BASE_DIR / "cookies.txt"

    clean_url = url.strip()

    # Manejo especializado para TikTok mediante API pública para evitar WAF/CAPTCHA del VPS
    if "tiktok.com" in clean_url:
        if "?" in clean_url:
            clean_url = clean_url.split("?")[0]
            
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post("https://www.tikwm.com/api/", data={"url": clean_url})
                data = resp.json()
                
                if data.get("code") == 0 and "data" in data:
                    # Preferimos la pista de audio directa (music) o el vídeo no-watermark (play)
                    media_url = data["data"].get("music") or data["data"].get("play")
                    
                    # Descargamos el stream de audio/video directo
                    media_bytes = await client.get(media_url)
                    temp_input = DOWNLOADS / f"{job_id}_temp"
                    with open(temp_input, "wb") as f:
                        f.write(media_bytes.content)
                    
                    # Convertimos a WAV estándar con ffmpeg
                    wav_path = DOWNLOADS / f"{job_id}.wav"
                    conv_cmd = ["ffmpeg", "-y", "-i", str(temp_input), "-ar", "44100", "-ac", "2", str(wav_path)]
                    subprocess.run(conv_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    if temp_input.exists():
                        temp_input.unlink()
                else:
                    raise Exception("No se pudo obtener el enlace de API TikTok")
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": "Fallo la descarga de TikTok", "detail": str(e)})

    # Proceso estándar con yt-dlp para YouTube y otras plataformas
    else:
        cmd = [
            "yt-dlp",
            "-f", "ba/b",
            "-x",
            "--audio-format", "wav",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]

        if "youtube.com" in clean_url or "youtu.be" in clean_url:
            cmd.extend(["--extractor-args", "youtube:player_client=ios,android_vr,tv_embedded"])

        if cookies_path.exists():
            cmd.extend(["--cookies", str(cookies_path)])

        cmd.extend(["-o", output_template, clean_url])

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return JSONResponse(status_code=400, content={"error": "Fallo la descarga", "detail": result.stderr[-500:]})

    wav_path = DOWNLOADS / f"{job_id}.wav"
    if not wav_path.exists():
        return JSONResponse(status_code=400, content={"error": "No se generó el WAV"})

    # Análisis musical y conversión a MIDI
    analysis = analyze_audio(str(wav_path))
    midi_filename = audio_to_midi(str(wav_path), str(DOWNLOADS))

    final_wav_name = build_filename("sample", job_id, analysis, ".wav")
    final_midi_name = build_filename("sample", job_id, analysis, ".mid")

    wav_path.rename(DOWNLOADS / final_wav_name)
    if (DOWNLOADS / midi_filename).exists():
        (DOWNLOADS / midi_filename).rename(DOWNLOADS / final_midi_name)

    entry = {
        "type": "download",
        "source": clean_url,
        "file": f"/files/{final_wav_name}",
        "midi": f"/files/{final_midi_name}",
        "analysis": analysis,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_history_entry(entry)
    return {"file": entry["file"], "midi": entry["midi"], "analysis": analysis}
  
  


@app.post("/analyze")
async def analyze_upload(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    ext = Path(file.filename).suffix or ".wav"
    saved_path = DOWNLOADS / f"{job_id}{ext}"

    with open(saved_path, "wb") as f:
        f.write(await file.read())

    wav_path = DOWNLOADS / f"{job_id}.wav"
    if ext.lower() != ".wav":
        subprocess.run(["ffmpeg", "-y", "-i", str(saved_path), str(wav_path)], capture_output=True)
    else:
        wav_path = saved_path

    analysis = analyze_audio(str(wav_path))
    midi_filename = audio_to_midi(str(wav_path), str(DOWNLOADS))

    final_wav_name = build_filename("sample", job_id, analysis, ".wav")
    final_midi_name = build_filename("sample", job_id, analysis, ".mid")
    wav_path.rename(DOWNLOADS / final_wav_name)
    (DOWNLOADS / midi_filename).rename(DOWNLOADS / final_midi_name)

    entry = {
        "type": "upload", "source": file.filename, "file": f"/files/{final_wav_name}",
        "midi": f"/files/{final_midi_name}", "analysis": analysis,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_history_entry(entry)
    return {"file": entry["file"], "midi": entry["midi"], "analysis": analysis}



@app.post("/beats/upload")
async def upload_beat(
    beat_name: str = Form(...),
    genre: str = Form(...),
    bpm: Optional[str] = Form(None),
    key_scale: Optional[str] = Form(None),
    file: UploadFile = File(...),
):
    ext = Path(file.filename).suffix or ".wav"
    genre_folder = safe_genre_folder(genre)

    local_filename = f"{beat_name}{ext}" if not beat_name.endswith(ext) else beat_name

    genre_dir = BEATS_DIR / genre_folder
    genre_dir.mkdir(exist_ok=True)
    local_path = genre_dir / local_filename

    with open(local_path, "wb") as f:
        f.write(await file.read())

    uploaded = upload_to_nextcloud(local_path, local_filename, subfolder=genre_folder)

    # --- NUEVO: generar preview con watermark ---
    preview_filename = f"{Path(local_filename).stem}_preview.mp3"
    preview_path = genre_dir / preview_filename
    generate_watermarked_preview(local_path, preview_path)
    preview_url = f"/beat-files/{genre_folder}/{preview_filename}" if preview_path.exists() else None
    # ---------------------------------------------

    job_id = str(uuid.uuid4())[:8]
    bpm_value = int(bpm) if bpm and bpm.strip().isdigit() else None

    entry = {
        "id": job_id,
        "beat_name": beat_name,
        "genre": genre_folder,
        "bpm": bpm_value,
        "key_scale": key_scale.strip() if key_scale else None,
        "filename": local_filename,
        "file": f"/beat-files/{genre_folder}/{local_filename}",
        "preview_file": preview_url,
        "status": "disponible",
        "nextcloud_synced": uploaded,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    db_insert_beat(entry)

    return entry    
  
  
@app.put("/beats/{beat_id}")
def update_beat(
    beat_id: str,
    beat_name: Optional[str] = Form(None),
    genre: Optional[str] = Form(None),
    bpm: Optional[str] = Form(None),
    key_scale: Optional[str] = Form(None),
):
    beat = db_get_beat(beat_id)
    if not beat:
        return JSONResponse(status_code=404, content={"error": "Beat no encontrado"})

    updates = {}

    if beat_name and beat_name != beat["beat_name"]:
        updates["beat_name"] = beat_name

    if genre and safe_genre_folder(genre) != beat["genre"]:
        new_genre_folder = safe_genre_folder(genre)
        old_genre_folder = safe_genre_folder(beat["genre"])

        old_path = BEATS_DIR / old_genre_folder / beat["filename"]
        if not old_path.exists():
            old_path = BEATS_DIR / beat["filename"]

        new_dir = BEATS_DIR / new_genre_folder
        new_dir.mkdir(exist_ok=True)
        new_path = new_dir / beat["filename"]

        if old_path.exists():
            shutil.move(str(old_path), str(new_path))

        # mover también el preview con watermark, si existe
        if beat.get("preview_file"):
            preview_name = Path(beat["preview_file"]).name
            old_preview_path = BEATS_DIR / old_genre_folder / preview_name
            new_preview_path = new_dir / preview_name

            if old_preview_path.exists():
                shutil.move(str(old_preview_path), str(new_preview_path))

            updates["preview_file"] = f"/beat-files/{new_genre_folder}/{preview_name}"

        updates["genre"] = new_genre_folder
        updates["file"] = f"/beat-files/{new_genre_folder}/{beat['filename']}"

    if bpm is not None:
        updates["bpm"] = int(bpm) if bpm.strip().isdigit() else None

    if key_scale is not None:
        updates["key_scale"] = key_scale.strip() or None

    if not updates:
        return {"status": "ok", "changed": False}

    db_update_beat(beat_id, **updates)
    return {"status": "ok", "changed": True, "updates": updates}




@app.delete("/beats/{beat_id}")
def delete_beat(beat_id: str):
    beat = db_get_beat(beat_id)
    if not beat:
        return JSONResponse(status_code=404, content={"error": "Beat no encontrado"})

    genre_folder = safe_genre_folder(beat.get("genre"))

    local_path = BEATS_DIR / genre_folder / beat["filename"]
    if not local_path.exists():
        local_path = BEATS_DIR / beat["filename"]
    if local_path.exists():
        local_path.unlink()

    # borrar también el preview con watermark, si existe
    if beat.get("preview_file"):
        preview_name = Path(beat["preview_file"]).name
        preview_path = BEATS_DIR / genre_folder / preview_name
        if not preview_path.exists():
            preview_path = BEATS_DIR / preview_name
        if preview_path.exists():
            preview_path.unlink()

    nextcloud_deleted = delete_from_nextcloud(beat["filename"], subfolder=genre_folder)

    db_delete_beat(beat_id)
    return {"status": "ok", "deleted": beat_id, "nextcloud_deleted": nextcloud_deleted}
  