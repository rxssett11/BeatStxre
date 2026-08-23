import json
from pathlib import Path
from datetime import datetime
from db import engine, beats_table, licenses_table, history_table, init_db

BASE_DIR = Path(__file__).parent


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return datetime.utcnow()


def load(name):
    p = BASE_DIR / name
    return json.loads(p.read_text()) if p.exists() else []


def migrate_beats():
    beats = load("beats.json")
    with engine.begin() as conn:
        for b in beats:
            conn.execute(beats_table.insert().values(
                id=b["id"], beat_name=b["beat_name"], genre=b.get("genre"),
                filename=b.get("filename"), file=b.get("file"),
                status=b.get("status", "disponible"),
                nextcloud_synced=bool(b.get("nextcloud_synced", False)),
                created_at=parse_date(b.get("date", "")),
            ))
    print(f"Migrados {len(beats)} beats")


def migrate_licenses():
    licenses = load("licenses.json")
    with engine.begin() as conn:
        for l in licenses:
            conn.execute(licenses_table.insert().values(
                order_id=l["order_id"], beat_name=l.get("beat_name"),
                artistic_name=l.get("artistic_name"), real_name=l.get("real_name"),
                price=l.get("price"), file=l.get("file"),
                public_share_url=l.get("public_share_url"),
                created_at=parse_date(l.get("date", "")),
            ))
    print(f"Migradas {len(licenses)} licencias")


def migrate_history():
    history = load("history.json")
    with engine.begin() as conn:
        for h in history:
            a = h.get("analysis", {})
            conn.execute(history_table.insert().values(
                type=h.get("type"), source=h.get("source"),
                file=h.get("file"), midi=h.get("midi"),
                key_note=a.get("key"), scale=a.get("scale"),
                confidence=a.get("confidence"), bpm=a.get("bpm"),
                created_at=parse_date(h.get("date", "")),
            ))
    print(f"Migrados {len(history)} registros de historial")


if __name__ == "__main__":
    init_db()
    migrate_beats()
    migrate_licenses()
    migrate_history()
    print("Migración completa.")
