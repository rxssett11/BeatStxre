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

import requests
from dotenv import load_dotenv
from db import (
    init_db, db_get_beats, db_get_beat, db_insert_beat, db_update_beat_status,
    db_get_licenses, db_count_licenses, db_insert_license,
    db_get_history, db_insert_history,
)
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import essentia.standard as es
from basic_pitch.inference import predict_and_save
from basic_pitch import ICASSP_2022_MODEL_PATH

load_dotenv()

app = FastAPI()


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
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{remote_folder_path.strip('/')}"
    try:
        requests.request("MKCOL", url, auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD), timeout=30)
    except requests.RequestException:
        pass


def upload_to_nextcloud(local_path: Path, remote_filename: str, subfolder: str = "") -> bool:
    if not (NEXTCLOUD_URL and NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD):
        return False

    folder_path = f"{NEXTCLOUD_BEATS_FOLDER}/{subfolder}".strip("/") if subfolder else NEXTCLOUD_BEATS_FOLDER
    ensure_nextcloud_folder(folder_path)

    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{folder_path}/{remote_filename}"
    with open(local_path, "rb") as f:
        resp = requests.put(
            url,
            data=f,
            auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD),
            timeout=120,
        )
    return resp.status_code in (200, 201, 204)


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


def next_order_id() -> str:
    year = datetime.now().year
    return f"BT-X{year}-{db_count_licenses() + 1:03d}11"


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
        return HTML_PAGE
    return PLAYER_PAGE


@app.get("/player", response_class=HTMLResponse)
def player():
    return PLAYER_PAGE


@app.get("/panelstxre", response_class=HTMLResponse)
def panel():
    return HTML_PAGE


@app.get("/history")
def get_history():
    return db_get_history()


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

    db_update_beat_status(beat_id, status)

    license_entry = None
    public_share_url = None

    if status == "vendido":
        if not artistic_name or not real_name:
            return JSONResponse(
                status_code=400,
                content={"error": "Para marcar como 'Vendido' debes ingresar Nombre artístico y Nombre completo."},
            )

        order_id = next_order_id()
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

        # 1. Subir Licencia PDF
        pdf_local_path = LICENSES_DIR / pdf_filename
        upload_to_nextcloud(pdf_local_path, pdf_filename, subfolder=remote_delivery_folder)

        # 2. Subir Beat WAV
        genre_folder = safe_genre_folder(beat.get("genre"))
        beat_local_path = BEATS_DIR / genre_folder / beat["filename"]
        
        if not beat_local_path.exists():
            beat_local_path = BEATS_DIR / beat["filename"]

        if beat_local_path.exists():
            upload_to_nextcloud(beat_local_path, beat["filename"], subfolder=remote_delivery_folder)

        # 3. Generar Share Link público para la carpeta del cliente
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

    return {
        "status": "ok",
        "beat_name": beat["beat_name"],
        "beat_file": beat.get("file"),
        "new_status": status,
        "license": license_entry,
    }


import httpx

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
    file: UploadFile = File(...),
):
    ext = Path(file.filename).suffix or ".wav"
    genre_folder = safe_genre_folder(genre)
    
    # 1. Conservamos exactamente el nombre ingresado/subido con su extensión
    local_filename = f"{beat_name}{ext}" if not beat_name.endswith(ext) else beat_name

    genre_dir = BEATS_DIR / genre_folder
    genre_dir.mkdir(exist_ok=True)
    local_path = genre_dir / local_filename

    with open(local_path, "wb") as f:
        f.write(await file.read())

    # 2. Generar preview MP3 con watermark para el reproductor público
    preview_filename = f"{Path(local_filename).stem}_preview.mp3"
    preview_path = genre_dir / preview_filename
    generate_watermarked_preview(local_path, preview_path)

    # 3. Subida a Nextcloud respetando el nombre tal cual (solo el WAV original)
    uploaded = upload_to_nextcloud(local_path, local_filename, subfolder=genre_folder)

    job_id = str(uuid.uuid4())[:8]

    entry = {
        "id": job_id,
        "beat_name": beat_name,
        "genre": genre_folder,
        "filename": local_filename,
        "file": f"/beat-files/{genre_folder}/{local_filename}",
        "preview_file": f"/beat-files/{genre_folder}/{preview_filename}",
        "status": "disponible",
        "nextcloud_synced": uploaded,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    db_insert_beat(entry)

    return entry
  


HTML_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Panel - BeatStxre</title>
  <style>
    :root {
      --red: #e50914; --red-dark: #9c060c; --bg: #0a0a0a;
      --panel: #161616; --panel-light: #1f1f1f; --border: #2a2a2a;
      --text: #eee; --text-dim: #999;
    }
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; flex-direction: column; min-height: 100vh; }
    #navbar { display: flex; align-items: center; justify-content: space-between; background: var(--panel); border-bottom: 1px solid var(--border); padding: 14px 24px; position: sticky; top: 0; z-index: 10; }
    #navbar .nav-brand { color: var(--red); font-weight: bold; font-size: 18px; }
    #navbar .nav-links a { color: var(--text-dim); text-decoration: none; margin-left: 20px; font-size: 14px; padding: 6px 12px; border-radius: 6px; transition: background 0.2s, color 0.2s; }
    #navbar .nav-links a:hover, #navbar .nav-links a.active { color: #fff; background: var(--red); }
    #layout { display: flex; flex: 1; min-height: 0; }
    #sidebar { width: 300px; background: var(--panel); border-right: 1px solid var(--border); padding: 20px; overflow-y: auto; max-height: calc(100vh - 57px); }
    #sidebar h3 { color: var(--red); margin-top: 0; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
    .hist-item { background: var(--panel-light); border-left: 3px solid var(--red); border-radius: 4px; padding: 10px; margin-bottom: 10px; font-size: 13px; }
    .hist-item .src { color: var(--text-dim); font-size: 11px; word-break: break-all; margin-bottom: 6px; }
    .hist-item .meta { color: var(--text); font-weight: bold; margin-bottom: 6px; }
    .hist-item a { color: var(--red); text-decoration: none; font-size: 12px; margin-right: 10px; }
    .hist-item a:hover { text-decoration: underline; }
    #main { flex: 1; max-width: 650px; margin: 40px auto; padding: 0 20px; }
    h2 { color: var(--red); font-size: 28px; }
    .box { background: var(--panel); border: 1px solid var(--border); padding: 24px; border-radius: 10px; margin-bottom: 24px; }
    .box h3 { margin-top: 0; color: #fff; }
    input[type=text], input[type=file], select {
      width: 100%; padding: 10px; margin: 8px 0; background: var(--panel-light);
      border: 1px solid var(--border); color: var(--text); border-radius: 6px;
    }
    button { width: 100%; padding: 12px; margin-top: 8px; background: var(--red); border: none; color: #fff; font-weight: bold; border-radius: 6px; cursor: pointer; transition: background 0.2s; }
    button:hover { background: var(--red-dark); }
    .result { background: var(--panel-light); border-radius: 6px; padding: 14px; margin-top: 14px; min-height: 20px; }
    .result a { display: block; color: var(--red); margin-top: 8px; text-decoration: none; font-weight: bold; }
    .result a:hover { text-decoration: underline; }
    pre { white-space: pre-wrap; color: #f66; }
    .file-drop {
      display: flex; align-items: center; justify-content: center; width: 100%;
      padding: 30px 16px; margin: 8px 0; background: var(--panel-light);
      border: 2px dashed var(--border); color: var(--text-dim); border-radius: 8px;
      cursor: pointer; text-align: center; transition: border-color 0.2s, color 0.2s, background 0.2s; font-size: 14px;
    }
    .file-drop:hover { border-color: var(--red); color: var(--text); background: #201414; }
    .file-drop.has-file { border-color: var(--red); border-style: solid; color: #fff; background: #1a1010; }
  </style>
</head>
<body>

  <div id="navbar">
    <div class="nav-brand"><img src="/assets/img/logo.png" alt="Logo" style="height: 28px; vertical-align: middle;"></div>
    <div class="nav-links">
      <a href="/" class="active">Herramientas</a>
      <a href="https://beatstxre.ismaelrxssett.cloud" target="_blank">Stxre</a>
</div>
  </div>

  <div id="layout">
  <div id="sidebar">
    <h3>Historial</h3>
    <div id="history-list">Cargando...</div>
  </div>

  <div id="main">
      <div class="nav-brand"><img src="/assets/img/logo.png" alt="Logo" style="height: 28px; vertical-align: middle;"></div>


    <div class="box">
      <h3>Descargar de link (YT / TikTok / IG / FB)</h3>
      <input type="text" id="url" placeholder="Pega el link aquí">
      <button onclick="downloadUrl()">Descargar y analizar</button>
      <div id="result-download" class="result"></div>
    </div>

    <div class="box">
      <h3>Subir sample</h3>
      <label id="file-label" for="file" class="file-drop"><span id="file-text">Arrastra tu sample o haz clic aquí</span></label>
      <input type="file" id="file" hidden accept="audio/*">
      <button onclick="analyzeFile()">Analizar y convertir a MIDI</button>
      <div id="result-upload" class="result"></div>
    </div>

    <div class="box">
      <h3>Subir beat (a Nextcloud)</h3>
      <input type="text" id="beat-name" placeholder="Nombre del beat">
      <select id="beat-genre">
        <option value="">Selecciona género...</option>
        <option value="Boom Bap">Boom Bap</option>
        <option value="Trap">Trap</option>
        <option value="Reggaeton">Reggaetón</option>
      </select>
      <label id="beatfile-label" for="beatfile" class="file-drop"><span id="beatfile-text">Arrastra tu beat o haz clic aquí</span></label>
      <input type="file" id="beatfile" hidden accept="audio/*">
      <button onclick="uploadBeat()">Subir beat</button>
      <div id="beat-progress-wrap" style="display:none; background:var(--panel-light); border-radius:6px; overflow:hidden; height:18px; margin-top:10px;">
        <div id="beat-progress-bar" style="height:100%; width:0%; background:var(--red); transition:width .15s;"></div>
      </div>
      <div id="beat-progress-text" style="text-align:center; font-size:12px; color:var(--text-dim); margin-top:4px;"></div>
      <div id="result-beat" class="result"></div>
    </div>

    <div class="box">
      <h3>Registrar venta y generar licencia</h3>
      
      <select id="unified-beat-select">
        <option value="">Selecciona un beat...</option>
      </select>
      
      <select id="unified-status-select" onchange="toggleLicenseFields()">
        <option value="vendido">🔴 Vendido (Generar licencia PDF y ocultar del reproductor)</option>
        <option value="apartado">🟡 Apartado (Reservar en el reproductor)</option>
        <option value="disponible">🟢 Disponible (Público)</option>
      </select>

      <div id="license-fields">
        <input type="text" id="artistic-name" placeholder="Nombre artístico del cliente">
        <input type="text" id="real-name" placeholder="Nombre completo del cliente">
        <input type="text" id="price" placeholder="Precio (ej. $600.00 MXN)" value="$600.00 MXN">
        <input type="text" id="payment-note" placeholder="Nota de pago (opcional, ej. Dos exhibiciones de $300 MXN)">
      </div>

      <button onclick="processBeatAction()">Procesar Registro</button>
      <div id="result-unified" class="result"></div>
    </div>
  </div>
  </div>

  <script>
    function setupDropzone(inputId, labelId, textId) {
      const input = document.getElementById(inputId);
      const label = document.getElementById(labelId);
      const text = document.getElementById(textId);
      input.addEventListener('change', () => {
        if (input.files.length) { text.textContent = input.files[0].name; label.classList.add('has-file'); }
        else { text.textContent = "Arrastra tu archivo o haz clic aquí"; label.classList.remove('has-file'); }
      });
      label.addEventListener('dragover', (e) => { e.preventDefault(); label.classList.add('has-file'); });
      label.addEventListener('dragleave', () => { if (!input.files.length) label.classList.remove('has-file'); });
      label.addEventListener('drop', (e) => {
        e.preventDefault();
        if (e.dataTransfer.files.length) {
          input.files = e.dataTransfer.files;
          text.textContent = input.files[0].name;
          label.classList.add('has-file');
        }
      });
    }
    setupDropzone('file', 'file-label', 'file-text');
    setupDropzone('beatfile', 'beatfile-label', 'beatfile-text');

    function renderResult(container, data) {
      if (data.error) { container.innerHTML = "<pre>" + JSON.stringify(data, null, 2) + "</pre>"; return; }
      let html = `
        <div><strong>Key:</strong> ${data.analysis.key} ${data.analysis.scale} (confianza: ${data.analysis.confidence})</div>
        <div><strong>BPM:</strong> ${data.analysis.bpm}</div>
        <a href="${data.file}" download>⬇ Descargar WAV</a>
      `;
      if (data.midi) html += `<a href="${data.midi}" download>⬇ Descargar MIDI</a>`;
      container.innerHTML = html;
    }

    async function downloadUrl() {
      const url = document.getElementById('url').value;
      const out = document.getElementById('result-download');
      out.innerHTML = "Procesando...";
      const form = new FormData(); form.append('url', url);
      const res = await fetch('/download', { method: 'POST', body: form });
      renderResult(out, await res.json());
      loadHistory();
    }

    async function analyzeFile() {
      const fileInput = document.getElementById('file');
      const out = document.getElementById('result-upload');
      if (!fileInput.files.length) return;
      out.innerHTML = "Procesando...";
      const form = new FormData(); form.append('file', fileInput.files[0]);
      const res = await fetch('/analyze', { method: 'POST', body: form });
      renderResult(out, await res.json());
      loadHistory();
    }

    function uploadBeat() {
      const name = document.getElementById('beat-name').value;
      const genre = document.getElementById('beat-genre').value;
      const fileInput = document.getElementById('beatfile');
      const out = document.getElementById('result-beat');
      const progWrap = document.getElementById('beat-progress-wrap');
      const progBar = document.getElementById('beat-progress-bar');
      const progText = document.getElementById('beat-progress-text');

      if (!name || !genre || !fileInput.files.length) { out.innerHTML = "Falta el nombre, género o archivo."; return; }

      out.innerHTML = "";
      progWrap.style.display = 'block';
      progBar.style.width = '0%';
      progBar.style.background = 'var(--red)';
      progText.textContent = '0%';

      const form = new FormData();
      form.append('beat_name', name);
      form.append('genre', genre);
      form.append('file', fileInput.files[0]);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/beats/upload');

      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        progBar.style.width = pct + '%';
        progText.textContent = pct + '%';
        const hue = Math.round((pct / 100) * 120);
        progBar.style.background = `hsl(${hue}, 80%, 45%)`;
      };

      xhr.onload = () => {
        progWrap.style.display = 'none';
        let data;
        try { data = JSON.parse(xhr.responseText); } catch (e) { out.innerHTML = "Error leyendo la respuesta del servidor."; return; }
        if (data.error) { out.innerHTML = "<pre>" + JSON.stringify(data, null, 2) + "</pre>"; return; }
        out.innerHTML = `<div>"${data.beat_name}" (${data.genre}) subido ${data.nextcloud_synced ? '(sincronizado con Nextcloud)' : '(⚠ no se sincronizó con Nextcloud, revisa .env)'}</div>`;
        loadBeats();
      };

      xhr.onerror = () => {
        progWrap.style.display = 'none';
        out.innerHTML = "Error de conexión al subir el beat.";
      };

      xhr.send(form);
    }

    function toggleLicenseFields() {
      const status = document.getElementById('unified-status-select').value;
      const fields = document.getElementById('license-fields');
      fields.style.display = (status === 'vendido') ? 'block' : 'none';
    }

    async function processBeatAction() {
      const beatId = document.getElementById('unified-beat-select').value;
      const status = document.getElementById('unified-status-select').value;
      const artisticName = document.getElementById('artistic-name').value;
      const realName = document.getElementById('real-name').value;
      const price = document.getElementById('price').value;
      const paymentNote = document.getElementById('payment-note').value;
      const out = document.getElementById('result-unified');

      if (!beatId) {
        out.innerHTML = "Selecciona un beat.";
        return;
      }

      out.innerHTML = "Procesando venta, generando PDF y creando carpeta en Nextcloud...";

      const form = new FormData();
      form.append('beat_id', beatId);
      form.append('status', status);
      form.append('artistic_name', artisticName);
      form.append('real_name', realName);
      form.append('price', price);
      form.append('payment_note', paymentNote);

      const res = await fetch('/beats/sell-and-license', { method: 'POST', body: form });
      const data = await res.json();

      if (data.error) {
        out.innerHTML = "<pre>" + JSON.stringify(data, null, 2) + "</pre>";
        return;
      }

      let html = `<div><strong>Beat:</strong> ${data.beat_name} | <strong>Nuevo estado:</strong> ${data.new_status}</div>`;
      
      if (data.license) {
        html += `<div style="font-size: 12px; color: #aaa; margin-top: 4px;"><strong>Orden:</strong> ${data.license.order_id}</div>`;
        
        if (data.license.public_share_url) {
          html += `<div style="margin-top: 12px; padding: 12px; background: #201414; border: 1px solid var(--red); border-radius: 6px;">`;
          html += `<div style="font-size: 12px; color: #fff; font-weight: bold; margin-bottom: 6px;">🔗 Link Público de Entrega (Nextcloud):</div>`;
          html += `<input type="text" readonly value="${data.license.public_share_url}" style="width: 100%; margin: 0; font-size: 12px; background: #0a0a0a; cursor: pointer;" onclick="this.select(); document.execCommand('copy'); alert('¡Enlace de entrega copiado!');">`;
          html += `</div>`;
        } else {
          html += `<div style="font-size: 11px; color: #f88; margin-top: 6px;">⚠ Licencia guardada localmente (no se pudo generar enlace público en Nextcloud).</div>`;
        }

        html += `<div style="margin-top: 12px; display: flex; gap: 12px;">`;
        html += `<a href="${data.license.file}" download style="color: var(--red); text-decoration: none; font-weight: bold;">📄 Descargar Licencia PDF</a>`;
        if (data.beat_file) {
          html += `<a href="${data.beat_file}" download style="color: #4ec960; text-decoration: none; font-weight: bold;">🎵 Descargar WAV del Beat</a>`;
        }
        html += `</div>`;
      }

      out.innerHTML = html;
      loadBeats();
    }

    async function loadHistory() {
      const list = document.getElementById('history-list');
      const items = await (await fetch('/history')).json();
      if (!items.length) { list.innerHTML = "<div style='color:#888;font-size:13px;'>Sin conversiones aún</div>"; return; }
      list.innerHTML = items.map(item => {
        const midiLink = item.midi ? `<a href="${item.midi}" download>MIDI</a>` : "";
        return `<div class="hist-item">
          <div class="src">${item.date} · ${item.type === 'download' ? '🔗 link' : '📁 upload'}</div>
          <div class="src">${item.source.length > 45 ? item.source.slice(0,45)+'...' : item.source}</div>
          <div class="meta">${item.analysis.key} ${item.analysis.scale} · ${item.analysis.bpm} BPM</div>
          <a href="${item.file}" download>WAV</a>${midiLink}
        </div>`;
      }).join("");
    }

    async function loadBeats() {
      const select = document.getElementById('unified-beat-select');
      const items = await (await fetch('/beats?include_sold=false')).json();
      
      if (!items.length) {
        select.innerHTML = '<option value="">No hay beats disponibles para venta</option>';
        return;
      }

      select.innerHTML = '<option value="">Selecciona un beat...</option>' +
        items.map(b => {
          const st = b.status ? ` [${b.status}]` : '';
          return `<option value="${b.id}">${b.beat_name} (${b.genre || 'Sin género'})${st}</option>`;
        }).join("");
    }

    loadHistory();
    loadBeats();
  </script>
</body>
</html>
"""


PLAYER_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BeatStxre - rxxsettprxd</title>
  <script src="https://unpkg.com/wavesurfer.js@7"></script>
  <style>
    :root { 
      --red: #e50914; --red-dark: #9c060c; --bg: #0a0a0a; 
      --panel: #141414; --panel-light: #1f1f1f; --border: #262626; 
      --text: #eee; --text-dim: #888;
    }
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding-bottom: 110px; }
    
    #navbar { display: flex; align-items: center; background: var(--panel); border-bottom: 1px solid var(--border); padding: 14px 24px; position: sticky; top: 0; z-index: 5; }
    #navbar .nav-brand { color: var(--red); font-weight: bold; font-size: 20px; }
    
    #player-main { max-width: 800px; margin: 30px auto; padding: 0 20px; }
    h2 { color: #fff; font-size: 24px; margin-bottom: 20px; }
    
    #genre-tabs { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
    .genre-tab { background: var(--panel-light); border: 1px solid var(--border); color: var(--text-dim); padding: 8px 18px; border-radius: 20px; cursor: pointer; font-size: 13px; font-weight: 600; transition: all .2s; }
    .genre-tab:hover { color: #fff; border-color: #444; }
    .genre-tab.active { background: var(--red); color: #fff; border-color: var(--red); }
    
    .beat-card { 
      display: flex; align-items: center; justify-content: space-between; 
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px; 
      padding: 14px 18px; margin-bottom: 10px; transition: background 0.2s;
    }
    .beat-card:hover { background: var(--panel-light); }
    .beat-card.playing { border-color: var(--red); background: #1a0d0d; }
    
    .beat-left { display: flex; align-items: center; gap: 14px; }
    .play-btn-item { 
      width: 38px; height: 38px; border-radius: 50%; background: var(--red); border: none; 
      color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 14px; flex-shrink: 0;
    }
    .beat-title { font-weight: 600; font-size: 15px; color: #fff; }
    .beat-tags { display: flex; gap: 6px; margin-top: 4px; align-items: center; }
    .beat-genre { font-size: 11px; color: var(--red); background: rgba(229,9,20,0.15); padding: 2px 8px; border-radius: 10px; }
    
    .status-badge { font-size: 10px; font-weight: bold; text-transform: uppercase; padding: 2px 8px; border-radius: 10px; }
    .status-disponible { background: #1b381e; color: #4ec960; }
    .status-apartado { background: #382c1b; color: #e5a93c; }

    #bottom-player {
      position: fixed; bottom: 0; left: 0; right: 0; height: 100px;
      background: #121212; border-top: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between; padding: 0 24px; z-index: 100; gap: 20px;
    }
    .player-track-info { width: 220px; flex-shrink: 0; }
    .player-track-title { font-weight: bold; color: #fff; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .player-track-sub { font-size: 12px; color: var(--text-dim); }

    .player-center { flex: 1; display: flex; align-items: center; gap: 16px; }
    .main-play-btn { width: 44px; height: 44px; border-radius: 50%; background: #fff; border: none; color: #000; font-size: 16px; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .main-play-btn:hover { transform: scale(1.05); }

    .waveform-wrapper { flex: 1; display: flex; flex-direction: column; gap: 4px; }
    #waveform { width: 100%; height: 45px; cursor: pointer; }
    
    .time-container { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-dim); }

    .player-right { width: 120px; display: flex; justify-content: flex-end; align-items: center; gap: 8px; flex-shrink: 0; }
    .volume-slider { width: 70px; accent-color: var(--red); }
        @media (max-width: 640px) {
      #navbar { padding: 12px 16px; }
      #player-main { margin: 20px auto; padding: 0 14px; }
      h2 { font-size: 20px; margin-bottom: 14px; }
      #genre-tabs { gap: 6px; margin-bottom: 18px; }
      .genre-tab { padding: 6px 12px; font-size: 12px; }

      .beat-card { padding: 12px 14px; }
      .beat-left { gap: 10px; }
      .play-btn-item { width: 34px; height: 34px; font-size: 12px; }
      .beat-title { font-size: 13px; }
      .beat-genre, .status-badge { font-size: 10px; }

      body { padding-bottom: 130px; }

      #bottom-player {
        flex-direction: column;
        height: auto;
        padding: 10px 14px 12px;
        gap: 8px;
      }
      .player-track-info { width: 100%; text-align: center; }
      .player-center { width: 100%; order: 2; }
      .player-right { width: 100%; justify-content: center; order: 3; }
      .volume-slider { width: 120px; }
      .main-play-btn { width: 40px; height: 40px; font-size: 14px; }
    }

    @media (max-width: 400px) {
      .beat-tags { flex-wrap: wrap; }
      .player-track-title { font-size: 13px; }
    }
  </style>
</head>
<body oncontextmenu="return false;">

  <div id="navbar">
   <div class="nav-brand"><img src="/assets/img/logo.png" alt="Logo" style="height: 32px; vertical-align: middle;"></div>
  </div>

  <div id="player-main">
    <h2>Catálogo de beats</h2>
    <div id="genre-tabs">
      <button class="genre-tab active" data-genre="">Todos</button>
      <button class="genre-tab" data-genre="Boom Bap">Boom Bap</button>
      <button class="genre-tab" data-genre="Trap">Trap</button>
      <button class="genre-tab" data-genre="Reggaeton">Reggaetón</button>
    </div>
    <div id="beat-list">Cargando...</div>
  </div>

  <div id="bottom-player">
    <div class="player-track-info">
      <div id="current-title" class="player-track-title">Selecciona un beat</div>
      <div id="current-genre" class="player-track-sub">--</div>
    </div>

    <div class="player-center">
      <button id="main-play-toggle" class="main-play-btn" onclick="togglePlay()">▶</button>
      <div class="waveform-wrapper">
        <div id="waveform"></div>
        <div class="time-container">
          <span id="time-current">0:00</span>
          <span id="time-total">0:00</span>
        </div>
      </div>
    </div>

    <div class="player-right">
      <span style="font-size: 12px; color: var(--text-dim);">🔊</span>
      <input type="range" class="volume-slider" min="0" max="1" step="0.05" value="1" oninput="setVolume(this.value)">
    </div>
  </div>

  <script>
    let currentGenre = "";
    let beatsData = [];
    let currentBeatIndex = -1;
    let wavesurfer = null;

    document.addEventListener('DOMContentLoaded', () => {
      wavesurfer = WaveSurfer.create({
        container: '#waveform',
        waveColor: '#444444',
        progressColor: '#e50914',
        cursorColor: '#ffffff',
        barWidth: 2,
        barGap: 2,
        barRadius: 2,
        height: 45,
        normalize: true,
      });

      wavesurfer.on('audioprocess', () => {
        document.getElementById('time-current').textContent = formatTime(wavesurfer.getCurrentTime());
      });

      wavesurfer.on('ready', () => {
        document.getElementById('time-total').textContent = formatTime(wavesurfer.getDuration());
      });

      wavesurfer.on('finish', () => {
        if (currentBeatIndex + 1 < beatsData.length) {
          playBeat(currentBeatIndex + 1);
        } else {
          document.getElementById('main-play-toggle').textContent = '▶';
        }
      });
    });

    document.querySelectorAll('.genre-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.genre-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentGenre = btn.dataset.genre;
        loadBeats();
      });
    });

    async function loadBeats() {
      const list = document.getElementById('beat-list');
      let url = '/beats?include_sold=false';
      if (currentGenre) {
        url += `&genre=${encodeURIComponent(currentGenre)}`;
      }

      const res = await fetch(url);
      beatsData = await res.json();
      
      if (!beatsData.length) { list.innerHTML = "<div style='color:#888;'>No hay beats disponibles.</div>"; return; }
      
      list.innerHTML = beatsData.map((b, idx) => {
        const status = b.status || 'disponible';
        const isPlaying = currentBeatIndex === idx && wavesurfer && wavesurfer.isPlaying();
        return `
          <div class="beat-card ${currentBeatIndex === idx ? 'playing' : ''}">
            <div class="beat-left">
              <button class="play-btn-item" onclick="playBeat(${idx})">
                ${isPlaying ? '⏸' : '▶'}
              </button>
              <div>
                <div class="beat-title">${b.beat_name}</div>
                <div class="beat-tags">
                  <span class="beat-genre">${b.genre || 'Sin género'}</span>
                  <span class="status-badge status-${status}">${status}</span>
                </div>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }

    function playBeat(index) {
      if (currentBeatIndex === index) {
        togglePlay();
        return;
      }
      currentBeatIndex = index;
      const beat = beatsData[index];
      
      wavesurfer.load(beat.preview_file || beat.file);
      wavesurfer.on('ready', () => { wavesurfer.play(); });
      
      document.getElementById('current-title').textContent = beat.beat_name;
      document.getElementById('current-genre').textContent = beat.genre || 'Sin género';
      document.getElementById('main-play-toggle').textContent = '⏸';
      
      loadBeats();
    }

    function togglePlay() {
      if (currentBeatIndex === -1) return;
      if (wavesurfer.isPlaying()) {
        wavesurfer.pause();
        document.getElementById('main-play-toggle').textContent = '▶';
      } else {
        wavesurfer.play();
        document.getElementById('main-play-toggle').textContent = '⏸';
      }
      loadBeats();
    }

    function setVolume(val) {
      if (wavesurfer) wavesurfer.setVolume(val);
    }

    function formatTime(sec) {
      if (isNaN(sec)) return "0:00";
      const m = Math.floor(sec / 60);
      const s = Math.floor(sec % 60);
      return `${m}:${s < 10 ? '0' : ''}${s}`;
    }

    loadBeats();
  </script>
</body>
</html>
"""