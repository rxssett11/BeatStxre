# cleanup_and_backup.py
import os, subprocess, zipfile
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote
import requests
from dotenv import load_dotenv
from db import db_get_beats

load_dotenv()
BASE_DIR = Path(__file__).parent
DOWNLOADS = BASE_DIR / "downloads"
PROMO_DIR = BASE_DIR / "promo"

NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "").rstrip("/")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER", "")
NEXTCLOUD_APP_PASSWORD = os.getenv("NEXTCLOUD_APP_PASSWORD", "")
RETENTION_DAYS = 15
CUTOFF = datetime.now() - timedelta(days=RETENTION_DAYS)

def ensure_folder(path):
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{quote(path.strip('/'))}"
    try: requests.request("MKCOL", url, auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD), timeout=30)
    except requests.RequestException: pass

def upload_file(local_path, remote_filename, subfolder):
    ensure_folder(subfolder)
    url = f"{NEXTCLOUD_URL}/remote.php/dav/files/{NEXTCLOUD_USER}/{quote(subfolder.strip('/'))}/{quote(remote_filename)}"
    with open(local_path, "rb") as f:
        resp = requests.put(url, data=f, auth=(NEXTCLOUD_USER, NEXTCLOUD_APP_PASSWORD), timeout=300)
    return resp.status_code in (200, 201, 204)

def old_files(directory, pattern):
    return [f for f in directory.glob(pattern)
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < CUTOFF]

def compress_audio(src, dst):
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-b:a", "64k", str(dst)], capture_output=True)
    return dst.exists()

def compress_video(src, dst):
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-vf", "scale=480:854",
                     "-c:v", "libx264", "-crf", "32", "-preset", "veryslow",
                     "-c:a", "aac", "-b:a", "96k", str(dst)], capture_output=True)
    return dst.exists()

def backup_samples():
    tag = datetime.now().strftime("%Y-%m")
    targets = old_files(DOWNLOADS, "*.wav")
    if not targets: print("Sin samples viejos."); return
    zip_path = BASE_DIR / f"samples_backup_{tag}_{datetime.now():%H%M%S}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for wav in targets:
            comp = wav.with_suffix(".compressed.mp3")
            if compress_audio(wav, comp):
                zf.write(comp, arcname=comp.name); comp.unlink()
            midi = wav.with_suffix(".mid")
            if midi.exists(): zf.write(midi, arcname=midi.name)
    if upload_file(zip_path, zip_path.name, f"Backups/Samples/{tag}"):
        for wav in targets:
            wav.unlink(missing_ok=True)
            wav.with_suffix(".mid").unlink(missing_ok=True)
        print(f"✔ {len(targets)} samples respaldados y eliminados.")
    else:
        print("⚠ Fallo subiendo backup, nada se borró.")
    zip_path.unlink(missing_ok=True)

def backup_inactive_promos():
    beats = db_get_beats(include_sold=True)
    active_names = {b["beat_name"] for b in beats if b.get("status") in ("disponible", "apartado")}
    import re
    active_files = {re.sub(r"[^A-Za-z0-9_-]", "_", n) + "_promo.mp4" for n in active_names}

    tag = datetime.now().strftime("%Y-%m")
    candidates = old_files(PROMO_DIR, "*_promo.mp4")
    targets = [f for f in candidates if f.name not in active_files]
    if not targets: print("Sin promos inactivos viejos."); return

    zip_path = BASE_DIR / f"promos_backup_{tag}_{datetime.now():%H%M%S}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for vid in targets:
            comp = vid.with_suffix(".compressed.mp4")
            if compress_video(vid, comp):
                zf.write(comp, arcname=comp.name); comp.unlink()
    if upload_file(zip_path, zip_path.name, f"Backups/Promos/{tag}"):
        for vid in targets: vid.unlink(missing_ok=True)
        print(f"✔ {len(targets)} promos inactivos respaldados y eliminados.")
    else:
        print("⚠ Fallo subiendo backup, nada se borró.")
    zip_path.unlink(missing_ok=True)

if __name__ == "__main__":
    backup_samples()
    backup_inactive_promos()