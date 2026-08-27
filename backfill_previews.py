from pathlib import Path
from app import generate_watermarked_preview, BEATS_DIR
from db import db_update_beat, db_get_beats

for b in db_get_beats(include_sold=True):
    if not b.get("preview_file"):
        genre_dir = BEATS_DIR / b["genre"]
        src = genre_dir / b["filename"]
        if src.exists():
            preview_name = f"{Path(b['filename']).stem}_preview.mp3"
            dst = genre_dir / preview_name
            generate_watermarked_preview(src, dst)
            if dst.exists():
                db_update_beat(b["id"], preview_file=f"/beat-files/{b['genre']}/{preview_name}")
                print("Preview generado:", b["beat_name"])