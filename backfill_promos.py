# backfill_promos.py
import re, subprocess
from pathlib import Path
from typing import Optional
from db import db_get_beats

BASE_DIR = Path(__file__).parent
BEATS_DIR = BASE_DIR / "beats"
PROMO_DIR = BASE_DIR / "promo"
GENRES = ["Boom Bap", "Trap", "Reggaeton"]
PROMO_MAX_SECONDS = 60
FONT_PATH = BASE_DIR / "assets" / "fonts" / "BebasNeue-Regular.ttf"


def safe_genre_folder(genre):
    g = (genre or "").strip()
    return g if g in GENRES else "Otros"


def escape_drawtext(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("%", "\\%")
    return text


def generate_promo_video(source_path, output_path, beat_name="", bpm=None, key_scale=None,
                          max_duration=PROMO_MAX_SECONDS):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
        capture_output=True, text=True)
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = max_duration
    clip_duration = min(duration, max_duration) if duration else max_duration

    title_text = escape_drawtext(beat_name.upper())
    sub_parts = []
    if bpm:
        sub_parts.append(f"{bpm} BPM")
    if key_scale:
        sub_parts.append(key_scale)
    subtitle_text = escape_drawtext(" | ".join(sub_parts))

    filter_complex = (
        "[0:a]showwaves=s=1080x700:mode=cline:colors=0xE50914:draw=full[vis];"
        f"color=c=black:s=1080x1920:d={clip_duration}[bg];"
        "[bg][vis]overlay=(W-w)/2:(H-h)/2:shortest=1"
        f",drawtext=fontfile='{FONT_PATH}':text='{title_text}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=1350"
    )
    if subtitle_text:
        filter_complex += (
            f",drawtext=fontfile='{FONT_PATH}':text='{subtitle_text}':fontcolor=0xCCCCCC:fontsize=36:x=(w-text_w)/2:y=1440"
        )
    filter_complex += "[outv]"

    tmp_path = output_path.with_suffix(".tmp.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(source_path), "-t", str(clip_duration),
           "-filter_complex", filter_complex, "-map", "[outv]", "-map", "0:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-shortest", str(tmp_path)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0 and tmp_path.exists():
        tmp_path.replace(output_path)
        return True
    if tmp_path.exists(): tmp_path.unlink()
    print(f"⚠ Fallo: {source_path.name}: {result.stderr.decode(errors='replace')[-500:]}")
    return False


def main():
    beats = db_get_beats(include_sold=True)
    print(f"Encontrados {len(beats)} beats")
    ok = fail = 0
    for b in beats:
        genre_folder = safe_genre_folder(b.get("genre"))
        local_path = BEATS_DIR / genre_folder / b["filename"]
        if not local_path.exists():
            local_path = BEATS_DIR / b["filename"]
        if not local_path.exists():
            print(f"⚠ Archivo no encontrado: {b['beat_name']}"); fail += 1; continue
        promo_filename = re.sub(r"[^A-Za-z0-9_-]", "_", b["beat_name"]) + "_promo.mp4"
        print(f"→ {b['beat_name']}")
        if generate_promo_video(
            local_path, PROMO_DIR / promo_filename,
            beat_name=b["beat_name"], bpm=b.get("bpm"), key_scale=b.get("key_scale"),
        ): ok += 1
        else: fail += 1
    print(f"Listo. OK: {ok}, Fallidos: {fail}")


if __name__ == "__main__":
    main()