# backfill_promos.py
from pathlib import Path
from db import db_get_beats
from app import generate_promo_video, BEATS_DIR, PROMO_DIR, safe_genre_folder
import re

beats = db_get_beats(include_sold=True)
print(f"Encontrados {len(beats)} beats.")

for b in beats:
    genre_folder = safe_genre_folder(b.get("genre"))
    local_path = BEATS_DIR / genre_folder / b["filename"]
    if not local_path.exists():
        local_path = BEATS_DIR / b["filename"]
    if not local_path.exists():
        print(f"⚠ No se encontró archivo para '{b['beat_name']}' ({b['filename']}), se omite.")
        continue

    promo_filename = re.sub(r"[^A-Za-z0-9_-]", "_", b["beat_name"]) + "_promo.mp4"
    promo_path = PROMO_DIR / promo_filename

    if promo_path.exists():
        print(f"✓ Ya existe promo para '{b['beat_name']}', se omite.")
        continue

    print(f"→ Generando promo para '{b['beat_name']}'...")
    generate_promo_video(local_path, promo_path)
    print(f"  ✓ Listo: {promo_filename}")

print("Backfill completo.")