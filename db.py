import os
from datetime import datetime
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Integer, Float, Boolean, Text, DateTime,
    select, func
)

MYSQL_HOST = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "prxdbeats")
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")

DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()

beats_table = Table(
    "beats", metadata,
    Column("id", String(16), primary_key=True),
    Column("beat_name", String(255), nullable=False),
    Column("genre", String(50)),
    Column("bpm", Integer),
    Column("key_scale", String(20)),
    Column("filename", String(255)),
    Column("file", String(500)),
    Column("preview_file", String(500)),
    Column("status", String(20), default="disponible"),
    Column("nextcloud_synced", Boolean, default=False),
    Column("created_at", DateTime, default=datetime.utcnow),
)

licenses_table = Table(
    "licenses", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String(50), unique=True, nullable=False),
    Column("beat_name", String(255)),
    Column("artistic_name", String(255)),
    Column("real_name", String(255)),
    Column("price", String(50)),
    Column("file", String(500)),
    Column("public_share_url", String(500)),
    Column("created_at", DateTime, default=datetime.utcnow),
)

history_table = Table(
    "history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("type", String(20)),
    Column("source", Text),
    Column("file", String(500)),
    Column("midi", String(500)),
    Column("key_note", String(10)),
    Column("scale", String(10)),
    Column("confidence", Float),
    Column("bpm", Float),
    Column("created_at", DateTime, default=datetime.utcnow),
)

    
leads_table = Table(
    "leads", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("beat_id", String(16)),
    Column("beat_name", String(255)),
    Column("name", String(255)),
    Column("contact", String(255)),
    Column("message", Text),
    Column("created_at", DateTime, default=datetime.utcnow),
)



def init_db():
    metadata.create_all(engine)


# ---------- beats ----------

def db_get_beats(genre=None, include_sold=False):
    with engine.connect() as conn:
        rows = conn.execute(beats_table.select()).mappings().all()
    beats = [dict(r) for r in rows]
    beats.sort(key=lambda b: b["created_at"] or datetime.min, reverse=True)
    if not include_sold:
        beats = [b for b in beats if b.get("status") != "vendido"]
    if genre:
        beats = [b for b in beats if b.get("genre") == genre]
    for b in beats:
        b["date"] = b["created_at"].strftime("%Y-%m-%d %H:%M") if b.get("created_at") else None
    return beats


def db_get_beat(beat_id):
    with engine.connect() as conn:
        row = conn.execute(beats_table.select().where(beats_table.c.id == beat_id)).mappings().first()
    return dict(row) if row else None


def db_insert_beat(entry):
    with engine.begin() as conn:
        conn.execute(beats_table.insert().values(
            id=entry["id"], beat_name=entry["beat_name"], genre=entry["genre"],
            bpm=entry.get("bpm"), key_scale=entry.get("key_scale"),
            filename=entry["filename"], file=entry["file"], status=entry["status"],
            preview_file=entry.get("preview_file"),
            nextcloud_synced=entry["nextcloud_synced"],
        ))


def db_update_beat_status(beat_id, status):
    with engine.begin() as conn:
        conn.execute(beats_table.update().where(beats_table.c.id == beat_id).values(status=status))
        

def db_update_beat(beat_id, **fields):
    if not fields:
        return
    with engine.begin() as conn:
        conn.execute(beats_table.update().where(beats_table.c.id == beat_id).values(**fields))


def db_delete_beat(beat_id):
    with engine.begin() as conn:
        conn.execute(beats_table.delete().where(beats_table.c.id == beat_id))


# ---------- licenses ----------

def db_get_licenses():
    with engine.connect() as conn:
        rows = conn.execute(licenses_table.select().order_by(licenses_table.c.created_at.desc())).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        d["date"] = d["created_at"].strftime("%Y-%m-%d %H:%M") if d.get("created_at") else None
        result.append(d)
    return result


def db_count_licenses():
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(licenses_table)).scalar()

def db_count_licenses_by_order_prefix(prefix):
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(licenses_table)
            .where(licenses_table.c.order_id.like(f"{prefix}%"))
        ).scalar() or 0


def db_insert_license(entry):
    with engine.begin() as conn:
        conn.execute(licenses_table.insert().values(
            order_id=entry["order_id"], beat_name=entry["beat_name"],
            artistic_name=entry["artistic_name"], real_name=entry["real_name"],
            price=entry["price"], file=entry["file"],
            public_share_url=entry.get("public_share_url"),
        ))


# ---------- history ----------

def db_get_history():
    with engine.connect() as conn:
        rows = conn.execute(history_table.select().order_by(history_table.c.created_at.desc())).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        d["analysis"] = {
            "key": d.pop("key_note"), "scale": d.pop("scale"),
            "confidence": d.pop("confidence"), "bpm": d.pop("bpm"),
        }
        d["date"] = d["created_at"].strftime("%Y-%m-%d %H:%M") if d.get("created_at") else None
        result.append(d)
    return result


def db_insert_history(entry):
    analysis = entry["analysis"]
    with engine.begin() as conn:
        conn.execute(history_table.insert().values(
            type=entry["type"], source=entry["source"], file=entry["file"],
            midi=entry.get("midi"), key_note=analysis["key"], scale=analysis["scale"],
            confidence=analysis["confidence"], bpm=analysis["bpm"],
        ))
        
        
# ---------- Contac ----------



def db_insert_lead(entry):
    with engine.begin() as conn:
        conn.execute(leads_table.insert().values(
            beat_id=entry["beat_id"], beat_name=entry["beat_name"],
            name=entry["name"], contact=entry["contact"],
            message=entry.get("message", ""),
        ))


def db_get_leads():
    with engine.connect() as conn:
        rows = conn.execute(leads_table.select().order_by(leads_table.c.created_at.desc())).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        d["date"] = d["created_at"].strftime("%Y-%m-%d %H:%M") if d.get("created_at") else None
        result.append(d)
    return result