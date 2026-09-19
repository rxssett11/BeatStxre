import re
from pathlib import Path
from db import db_get_beats
from generate_pulse import generate_promo_video_pulse

BASE_DIR = Path(__file__).parent
BEATS_DIR = BASE_DIR / "beats"
PROMO_DIR = BASE_DIR / "promo"
GENRES = ["Boom Bap", "Trap", "Reggaeton"]


def safe_genre_folder(genre):
    g = (genre or "").strip()
    return g if g in GENRES else "Otros"


def main():
    beats = db_get_beats(include_sold=True)
    print(f"Encontrados {len(beats)} beats", flush=True)
    ok = fail = 0
    for b in beats:
        genre_folder = safe_genre_folder(b.get("genre"))
        local_path = BEATS_DIR / genre_folder / b["filename"]
        if not local_path.exists():
            local_path = BEATS_DIR / b["filename"]
        if not local_path.exists():
            print(f"⚠ Archivo no encontrado: {b['beat_name']}", flush=True)
            fail += 1
            continue

        promo_filename = re.sub(r"[^A-Za-z0-9_-]", "_", b["beat_name"]) + "_promo_pulse.mp4"
        print(f"→ {b['beat_name']}", flush=True)

        success = generate_promo_video_pulse(
            local_path, PROMO_DIR / promo_filename,
            beat_name=b["beat_name"], bpm=b.get("bpm"), key_scale=b.get("key_scale"),
        )
        ok += success
        fail += not success

    print(f"Listo. OK: {ok}, Fallidos: {fail}", flush=True)


if __name__ == "__main__":
    main()