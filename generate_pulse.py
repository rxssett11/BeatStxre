"""
Generador de video promo "logo pulse": el logo crece/pulsa cada vez que
detecta un golpe de graves (bajo / 808) en el beat. Se muxea con el audio
completo y el texto (nombre/BPM/tonalidad) igual que el diseño de waveform.
"""
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from scipy.signal import butter, filtfilt
from PIL import Image

BASE_DIR = Path("/app")  # coincide con BASE_DIR de app.py dentro del contenedor
LOGO_PATH = BASE_DIR / "assets" / "img" / "icon.png"
FONT_PATH = BASE_DIR / "assets" / "fonts" / "BebasNeue-Regular.ttf"

CANVAS_W, CANVAS_H = 1080, 1920
FPS = 25
BASS_CUTOFF_HZ = 220           # Subido de 150Hz a 220Hz para captar armónicos del 808
LOGO_BASE_WIDTH = 500          # ancho del logo en reposo (px)
PULSE_MAX_GROWTH = 0.35        # +32% de tamaño máximo en golpes fuertes
LOGO_CENTER_Y = 820            # centro vertical del logo (deja espacio abajo para texto)


def escape_drawtext(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("%", "\\%")
    return text


def detect_bass_envelope(wav_path: Path, duration: float, fps: int = FPS):
    """Calcula la envolvente de graves fotograma a fotograma en lugar de buscar picos rígidos."""
    data, sr = sf.read(str(wav_path))
    if data.ndim > 1:
        data = data.mean(axis=1)

    # Filtro paso bajo calibrado para sub-bass y 808s
    b, a = butter(4, BASS_CUTOFF_HZ / (sr / 2), btype="low")
    bass = filtfilt(b, a, data)

    hop = int(sr / fps)
    n_frames = int(len(bass) / hop)
    if n_frames < 2:
        return np.ones(1)

    # Energía RMS por fotograma
    envelope = np.array([
        np.sqrt(np.mean(bass[i * hop:(i + 1) * hop] ** 2))
        for i in range(n_frames)
    ])

    max_val = envelope.max()
    if max_val <= 1e-6:
        return np.zeros(n_frames)

    # Normalizar entre 0 y 1
    envelope = envelope / max_val

    # Puerta de ruido suave para evitar que el logo baile con frecuencias residuales
    envelope = np.clip(envelope - 0.15, 0, None)
    
    # Exponente para aumentar el contraste entre partes suaves y golpes fuertes
    envelope = envelope ** 1.6

    return envelope


def build_scale_curve(envelope, decay_rate: float = 0.75):
    """
    Construye la curva de escala garantizando que el ataque sea instantáneo 
    y la caída (decay) sea fluida entre fotogramas.
    """
    scale_curve = np.zeros_like(envelope)
    current_val = 0.0

    for i, env_val in enumerate(envelope):
        # Si la energía del fotograma actual es mayor, el logo salta al nuevo valor
        if env_val > current_val:
            current_val = env_val
        else:
            # Si la energía cae, el logo se "desinfla" suavemente
            current_val *= decay_rate
        
        scale_curve[i] = 1.0 + (PULSE_MAX_GROWTH * current_val)

    return scale_curve


def generate_promo_video_pulse(source_path: Path, output_path: Path, beat_name: str = "",
                                bpm: Optional[int] = None, key_scale: Optional[str] = None,
                                max_duration: int = 60) -> bool:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = max_duration
    clip_duration = min(duration, max_duration) if duration else max_duration

    if not LOGO_PATH.exists():
        print(f"⚠ No se encontró el logo en {LOGO_PATH}")
        return False

    tmp_wav = output_path.with_suffix(".analysis.wav")
    conv_cmd = ["ffmpeg", "-y", "-i", str(source_path), "-t", str(clip_duration),
                "-ac", "1", "-ar", "44100", str(tmp_wav)]
    subprocess.run(conv_cmd, capture_output=True)
    if not tmp_wav.exists():
        print(f"⚠ Fallo convirtiendo audio para análisis: {source_path.name}")
        return False

    # Análisis continuo de la envolvente de graves
    envelope = detect_bass_envelope(tmp_wav, clip_duration)
    scale_curve = build_scale_curve(envelope)
    tmp_wav.unlink(missing_ok=True)

    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo_ratio = logo.height / logo.width
    resize_cache = {}

    black = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))

    tmp_path = output_path.with_suffix(".tmp.mp4")
    title_text = escape_drawtext(beat_name.upper())
    sub_parts = []
    if bpm:
        sub_parts.append(f"{bpm} BPM")
    if key_scale:
        sub_parts.append(key_scale)
    subtitle_text = escape_drawtext(" | ".join(sub_parts))

    filter_complex = (
        f"[0:v]drawtext=fontfile='{FONT_PATH}':text='{title_text}':"
        f"fontcolor=white:fontsize=64:x=(w-text_w)/2:y=1350"
    )
    if subtitle_text:
        filter_complex += (
            f",drawtext=fontfile='{FONT_PATH}':text='{subtitle_text}':"
            f"fontcolor=white:fontsize=38:x=(w-text_w)/2:y=1440"
        )
    filter_complex += "[outv]"

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{CANVAS_W}x{CANVAS_H}", "-r", str(FPS),
        "-i", "-",
        "-i", str(source_path),
        "-t", str(clip_duration),
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(tmp_path),
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    for scale in scale_curve:
        key = round(float(scale), 2)
        if key not in resize_cache:
            w = max(1, int(LOGO_BASE_WIDTH * key))
            h = max(1, int(w * logo_ratio))
            resize_cache[key] = logo.resize((w, h), Image.BICUBIC)
        resized = resize_cache[key]

        frame = black.copy()
        box = (
            (CANVAS_W - resized.width) // 2,
            LOGO_CENTER_Y - resized.height // 2,
        )
        frame.paste(resized, box, resized)
        proc.stdin.write(frame.tobytes())

    proc.stdin.close()
    stderr = proc.stderr.read()
    proc.wait()

    if proc.returncode == 0 and tmp_path.exists():
        tmp_path.replace(output_path)
        return True

    if tmp_path.exists():
        tmp_path.unlink()
    print(f"⚠ Fallo generando promo pulse para {source_path.name} (código {proc.returncode}):")
    print(stderr.decode(errors="replace")[-1500:])
    return False


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    name = sys.argv[3] if len(sys.argv) > 3 else src.stem
    bpm_arg = int(sys.argv[4]) if len(sys.argv) > 4 else None
    key_arg = sys.argv[5] if len(sys.argv) > 5 else None
    ok = generate_promo_video_pulse(src, out, beat_name=name, bpm=bpm_arg, key_scale=key_arg)
    print("OK" if ok else "FALLÓ")