"""Record a Skill — Bildschirmaufnahme + Narration → SKILL.md-Entwurf.

Opt-in-Feature (``webui.skill_recording.enabled``, Default ``false``). Solange das
Flag aus ist, existiert nichts davon zur Laufzeit: die Routen antworten 404 und
es wird kein Verzeichnis, kein Job und kein Unterprozess angelegt.

Ablauf::

    POST /api/skills/record        Streaming-Ingest → Job startet im Hintergrund
    GET  /api/skills/record/status Fortschritt, nur für den Besitzer
    POST /api/skills/record/cancel Abbruch + Cleanup
    POST /api/skills/record/save   Commit-Transaktion in den Root-Katalog

Die Verfahren stammen aus dem WP0A-Contract-Spike (24.07.2026) und sind dort
gemessen worden — die Kommentare nennen jeweils den Grund, weil mehrere davon
gegen die naheliegende Implementierung gehen:

* Frames werden in **zwei** Durchgängen gewählt. Ein einstufiges
  ``select=… -frames:v N`` nimmt die ersten N Treffer und deckte bei einer
  10-Minuten-Aufnahme nur 7,5 % der Laufzeit ab — ohne Fehlermeldung.
* Der Preflight zählt Frames. ffprobe-Metadaten *und* ffmpeg-Exitcode winken
  abgeschnittene Aufnahmen durch (``File ended prematurely`` steht nur auf
  stderr, der Exitcode ist 0).
* Der Upload wird chunkweise gelesen. Der vorhandene ``parse_multipart``
  allokiert das Vierfache der Nutzlast (190 MB Upload ⇒ 760 MB Spitze).
* Aufräumen passiert in genau einem ``finally``, das auch bei ``BaseException``
  läuft.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# ── Konstanten, die nicht konfigurierbar sind ────────────────────────────────
CHUNK_BYTES = 1024 * 1024
ALLOWED_FORMATS = frozenset({"matroska", "webm"})
ALLOWED_VCODECS = frozenset({"vp8", "vp9", "av1"})
ALLOWED_ACODECS = frozenset({"opus", "vorbis"})
PROBE_TIMEOUT_S = 60
FFMPEG_TIMEOUT_S = 600
# Unter diesem Anteil dekodierbarer Frames gilt die Aufnahme als abgebrochen.
MIN_DECODABLE_FRAME_RATIO = 0.5
JOB_FILE = "job.json"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_duration_s": 600,
    "max_upload_mb": 150,
    "max_concurrent_jobs": 1,
    "scratch_dir": "/tmp/hermes-skill-rec",
    "scratch_max_gb": 2,
    "job_ttl_h": 24,
    # Nach dem Speichern den Jobdatensatz behalten. Die Rohmedien sind zu
    # diesem Zeitpunkt ohnehin gelöscht — die Pipeline räumt sie in jedem
    # Ausgang ab, nicht erst hier.
    "retain_job_after_save": False,
    "keyframes": {
        "max_frames": 24,
        "max_total_encoded_mb": 3,
        "max_edge_px": 1080,
        "jpeg_quality": 75,
        "scene_threshold": 0.05,
        "min_interval_s": 0.5,
    },
}


class RecordingError(Exception):
    """Definierter Ablehnungsgrund mit HTTP-Status — nie ein roher Traceback."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Konfiguration ────────────────────────────────────────────────────────────
def _merge(base: dict, override: Any) -> dict:
    out = dict(base)
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _merge(out[key], value)
            elif key in out:
                out[key] = value
    return out


def recording_config() -> dict:
    """Effektive Feature-Konfiguration: Defaults ← config.yaml ← Env-Override."""
    try:
        from api.config import cfg

        webui_cfg = cfg.get("webui") if isinstance(cfg, dict) else None
        section = webui_cfg.get("skill_recording") if isinstance(webui_cfg, dict) else None
    except Exception:  # Config noch nicht geladen — Feature bleibt aus
        section = None
    conf = _merge(_DEFAULTS, section)

    raw = os.getenv("HERMES_WEBUI_SKILL_RECORDING", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        conf["enabled"] = True
    elif raw in {"0", "false", "no", "off"}:
        conf["enabled"] = False
    return conf


def is_enabled() -> bool:
    return bool(recording_config().get("enabled"))


def scratch_root(conf: dict | None = None) -> Path:
    conf = conf or recording_config()
    raw = str(conf.get("scratch_dir") or _DEFAULTS["scratch_dir"]).strip()
    path = Path(raw).expanduser()
    # Niemals unter HERMES_HOME schreiben: dort liegen Live-Datenbanken, und ein
    # verirrter Aufnahme-Job hat da nichts verloren.
    try:
        from api.profiles import get_active_hermes_home

        home = Path(get_active_hermes_home()).resolve()
        if path.resolve() == home or home in path.resolve().parents:
            raise RecordingError(
                f"scratch_dir {path} liegt unter HERMES_HOME — nicht erlaubt", 500)
    except RecordingError:
        raise
    except Exception:
        pass
    return path


# ── Jobmodell ────────────────────────────────────────────────────────────────
@dataclass
class Job:
    job_id: str
    owner: str
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    draft_md: str | None = None
    confidence: float | None = None
    similar: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_s: float | None = None
    frames_used: int | None = None
    saved_path: str | None = None
    steps: list[dict] = field(default_factory=list)
    executable_steps: int = 0

    def public(self) -> dict:
        """Was der Besitzer sehen darf — ohne interne Pfade."""
        data = asdict(self)
        data.pop("owner", None)
        return data


_STATE_ORDER = ["queued", "probing", "transcribing", "extracting", "synthesizing",
                "ready", "error", "interrupted", "dismissed", "saved"]

_jobs_lock = threading.RLock()


def job_dir(job_id: str, conf: dict | None = None) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id or ""):
        raise RecordingError("Ungültige Job-ID", 400)
    return scratch_root(conf) / job_id


def _write_job(job: Job, conf: dict | None = None) -> None:
    job.updated_at = time.time()
    directory = job_dir(job.job_id, conf)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (JOB_FILE + ".tmp")
    tmp.write_text(json.dumps(asdict(job), ensure_ascii=False), encoding="utf-8")
    tmp.replace(directory / JOB_FILE)


def _read_job(job_id: str, conf: dict | None = None) -> Job:
    path = job_dir(job_id, conf) / JOB_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecordingError("Job nicht gefunden", 404) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordingError(f"Job nicht lesbar: {exc}", 500) from None
    known = {f for f in Job.__dataclass_fields__}
    return Job(**{k: v for k, v in data.items() if k in known})


def load_job_for_owner(job_id: str, owner: str, conf: dict | None = None) -> Job:
    job = _read_job(job_id, conf)
    if job.owner != owner:
        # Fremde Job-ID wie eine unbekannte behandeln, damit sich fremde Jobs
        # nicht über den Statuscode aufzählen lassen.
        raise RecordingError("Job nicht gefunden", 404)
    return job


def active_jobs(conf: dict | None = None) -> list[Job]:
    conf = conf or recording_config()
    root = scratch_root(conf)
    if not root.is_dir():
        return []
    running = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            job = _read_job(entry.name, conf)
        except RecordingError:
            continue
        if job.state in {"queued", "probing", "transcribing", "extracting", "synthesizing"}:
            running.append(job)
    return running


def sweep_orphans(conf: dict | None = None) -> dict:
    """Beim WebUI-Start: laufende Jobs sind nach einem Neustart verwaist.

    Sie werden auf ``interrupted`` gesetzt und ihre Rohmedien gelöscht. Jobs
    jenseits der TTL verschwinden ganz.
    """
    conf = conf or recording_config()
    if not conf.get("enabled"):
        return {"interrupted": 0, "expired": 0}
    root = scratch_root(conf)
    if not root.is_dir():
        return {"interrupted": 0, "expired": 0}
    ttl_s = float(conf.get("job_ttl_h", 24)) * 3600
    now = time.time()
    interrupted = expired = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            job = _read_job(entry.name, conf)
        except RecordingError:
            shutil.rmtree(entry, ignore_errors=True)
            expired += 1
            continue
        if now - job.updated_at > ttl_s:
            shutil.rmtree(entry, ignore_errors=True)
            expired += 1
            continue
        if job.state in {"queued", "probing", "transcribing", "extracting", "synthesizing"}:
            job.state = "interrupted"
            job.error = "WebUI wurde während der Verarbeitung neu gestartet"
            cleanup_media(job, conf)
            _write_job(job, conf)
            interrupted += 1
    if interrupted or expired:
        logger.info("skill_recording: %s Job(s) unterbrochen, %s abgelaufen",
                    interrupted, expired)
    return {"interrupted": interrupted, "expired": expired}


_swept_once = False


def ensure_swept(conf: dict | None = None) -> None:
    """Einmal je Prozess aufräumen, bevor der erste Aufruf bedient wird.

    Bewusst hier statt im Serverstart: solange das Feature aus ist, soll beim
    Hochfahren nichts passieren — und ``server.py`` bleibt frei von
    Feature-Startcode (die Zeilenbudget-Regel dort ist Absicht).
    """
    global _swept_once
    if _swept_once:
        return
    _swept_once = True
    try:
        sweep_orphans(conf)
    except Exception:  # noqa: BLE001 — Aufräumen darf keinen Request kippen
        logger.exception("skill_recording: Aufräumen beim ersten Aufruf fehlgeschlagen")


def cleanup_media(job: Job, conf: dict | None = None) -> list[str]:
    """Rohmedien und Frames eines Jobs entfernen. Der Jobdatensatz bleibt."""
    removed: list[str] = []
    try:
        directory = job_dir(job.job_id, conf)
    except RecordingError:
        return removed
    if not directory.is_dir():
        return removed
    for path in directory.iterdir():
        if path.is_file() and path.suffix in {".webm", ".wav", ".opus", ".mkv", ".part"}:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                job.notes.append(f"Cleanup-Fehler {path.name}: {exc}")
    frames = directory / "frames"
    if frames.is_dir():
        shutil.rmtree(frames, ignore_errors=True)
        removed.append("frames/")
    return removed


def discard_job(job: Job, conf: dict | None = None) -> None:
    """Job vollständig entfernen (Dismiss/Cancel)."""
    cleanup_media(job, conf)
    try:
        shutil.rmtree(job_dir(job.job_id, conf), ignore_errors=True)
    except RecordingError:
        pass


# ── Streaming-Ingest ─────────────────────────────────────────────────────────
def stream_recording_upload(rfile, content_type: str, content_length: Any,
                            target_dir: Path, max_bytes: int) -> Path:
    """Den Datei-Part eines multipart-Uploads chunkweise auf Platte schreiben.

    Konstanter Speicherbedarf, unabhängig von der Uploadgröße. Der Aufrufer
    bekommt entweder eine vollständige Datei oder eine ``RecordingError`` —
    Teildateien werden immer entfernt.
    """
    match = re.search(r"boundary=([^;\s]+)", content_type or "")
    if not match:
        raise RecordingError("Kein boundary im Content-Type", 400)
    boundary = b"--" + match.group(1).strip('"').encode()

    try:
        length = int(content_length)
    except (TypeError, ValueError):
        raise RecordingError("Content-Length fehlt oder ist keine Zahl", 411) from None
    if length < 0:
        raise RecordingError("Content-Length negativ", 400)
    if length == 0:
        raise RecordingError("Leerer Upload", 400)
    if length > max_bytes:
        raise RecordingError(
            f"Aufnahme zu groß: {length / 1048576:.0f} MB "
            f"(erlaubt: {max_bytes / 1048576:.0f} MB)", 413)

    target_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".part")
    tmp = Path(tmp_name)
    remaining = length
    state = "preamble"
    buf = b""
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while remaining > 0:
                chunk = rfile.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise RecordingError(
                        f"Upload abgebrochen: {remaining} von {length} Bytes fehlen", 400)
                remaining -= len(chunk)
                buf += chunk

                if state == "preamble":
                    idx = buf.find(boundary)
                    if idx < 0:
                        if len(buf) > 64 * 1024:
                            raise RecordingError("Kein boundary im Anfang des Bodys", 400)
                        continue
                    buf = buf[idx + len(boundary):]
                    state = "headers"

                if state == "headers":
                    sep = buf.find(b"\r\n\r\n")
                    if sep < 0:
                        if len(buf) > 64 * 1024:
                            raise RecordingError("Part-Header zu groß", 400)
                        continue
                    head = buf[:sep].decode("utf-8", "replace")
                    if "filename=" not in head:
                        raise RecordingError("Erster Part ist kein Datei-Part", 400)
                    buf = buf[sep + 4:]
                    state = "body"

                if state == "body":
                    end = buf.find(b"\r\n" + boundary)
                    if end >= 0:
                        out.write(buf[:end])
                        written += end
                        buf = b""
                        state = "done"
                        # Der Rest des Bodys wird verworfen — aber der Absender
                        # muss liefern, was er angekündigt hat. Ein vorzeitiges
                        # EOF ist ein abgebrochener Upload und darf nicht als
                        # Erfolg durchgehen.
                        while remaining > 0:
                            drop = rfile.read(min(CHUNK_BYTES, remaining))
                            if not drop:
                                raise RecordingError(
                                    f"Upload abgebrochen: {remaining} von {length} "
                                    f"angekündigten Bytes fehlen", 400)
                            remaining -= len(drop)
                        break
                    keep = len(boundary) + 4
                    if len(buf) > keep:
                        out.write(buf[:-keep])
                        written += len(buf) - keep
                        buf = buf[-keep:]
        if state != "done":
            raise RecordingError("Endboundary fehlt — Upload unvollständig", 400)
        if written == 0:
            raise RecordingError("Leerer Datei-Part", 400)
        final = target_dir / "recording.webm"
        tmp.replace(final)
        return final
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── ffmpeg/ffprobe ───────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Unterprozess ohne Shell, mit hartem Timeout."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RecordingError(f"{cmd[0]} überschritt {timeout}s", 504) from exc
    except FileNotFoundError as exc:
        raise RecordingError(f"{cmd[0]} ist nicht installiert", 501) from exc


def tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@dataclass
class Preflight:
    duration_s: float
    width: int
    height: int
    fps: float
    v_codec: str
    a_codec: str | None
    size_bytes: int
    decodable_frames: int


def preflight(video: Path, max_duration_s: float) -> Preflight:
    """Container, Codecs, Grenzen — und ob die Datei wirklich vollständig ist."""
    if not video.is_file() or video.stat().st_size == 0:
        raise RecordingError("Aufnahme fehlt oder ist leer", 400)

    proc = _run(["ffprobe", "-v", "error", "-hide_banner", "-print_format", "json",
                 "-show_format", "-show_streams", "-i", str(video)], PROBE_TIMEOUT_S)
    if proc.returncode != 0:
        raise RecordingError(f"Aufnahme nicht lesbar: {proc.stderr.decode()[:200]}", 400)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RecordingError("ffprobe lieferte kein JSON", 500) from None

    fmt = data.get("format", {})
    names = {n.strip() for n in (fmt.get("format_name") or "").split(",")}
    if not names & ALLOWED_FORMATS:
        raise RecordingError(f"Container nicht unterstützt: {fmt.get('format_name')!r}", 415)
    try:
        duration = float(fmt.get("duration", "nan"))
    except (TypeError, ValueError):
        duration = float("nan")
    if duration != duration:
        raise RecordingError("Dauer nicht ermittelbar", 400)
    if duration > max_duration_s:
        raise RecordingError(
            f"Aufnahme zu lang: {duration:.0f}s (erlaubt: {max_duration_s:.0f}s)", 413)

    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise RecordingError(f"Genau ein Videostream erwartet, gefunden: {len(videos)}", 415)
    if len(audios) > 1:
        raise RecordingError(f"Höchstens ein Audiostream erlaubt, gefunden: {len(audios)}", 415)
    video_stream = videos[0]
    if video_stream.get("codec_name") not in ALLOWED_VCODECS:
        raise RecordingError(
            f"Video-Codec nicht unterstützt: {video_stream.get('codec_name')!r}", 415)
    audio_stream = audios[0] if audios else None
    if audio_stream and audio_stream.get("codec_name") not in ALLOWED_ACODECS:
        raise RecordingError(
            f"Audio-Codec nicht unterstützt: {audio_stream.get('codec_name')!r}", 415)

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RecordingError("Keine Videoauflösung", 400)
    fps = _parse_fps(video_stream)

    # Truncation-Gegenprobe. Ohne sie besteht eine abgebrochene Aufnahme diesen
    # Preflight vollständig: der Matroska-Header meldet die volle Dauer, und
    # ffmpeg beendet sich beim Dekodieren mit Exitcode 0.
    counted = _run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
                    str(video)], FFMPEG_TIMEOUT_S)
    try:
        decodable = int((counted.stdout or b"").split()[0])
    except (IndexError, ValueError):
        decodable = 0
    expected = duration * fps
    if expected > 0 and decodable < MIN_DECODABLE_FRAME_RATIO * expected:
        raise RecordingError(
            f"Aufnahme unvollständig: nur {decodable} von etwa {expected:.0f} "
            f"Bildern lesbar — Upload vermutlich abgebrochen", 400)

    return Preflight(duration_s=duration, width=width, height=height, fps=fps,
                     v_codec=video_stream.get("codec_name", ""),
                     a_codec=(audio_stream or {}).get("codec_name"),
                     size_bytes=video.stat().st_size, decodable_frames=decodable)


def _parse_fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                num_f, den_f = float(num), float(den)
            except ValueError:
                continue
            if den_f > 0 and num_f > 0:
                return num_f / den_f
    return 0.0


def extract_audio(video: Path, out: Path) -> None:
    """Tonspur als Opus-Mono herausziehen.

    Pflichtschritt, nicht Bequemlichkeit: ``transcribe_audio`` akzeptiert zwar
    ``.webm``, lehnt aber alles über 25 MB ab — die Rohaufnahme liegt darüber.
    64 kbit/s Mono ergeben für zehn Minuten etwa 4,8 MB.
    """
    proc = _run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
                 "-c:a", "libopus", "-b:a", "64k", str(out)], FFMPEG_TIMEOUT_S)
    # Der Exitcode allein genügt hier nicht: ffmpeg meldet auch bei defektem
    # Input gern 0. Entscheidend ist, dass eine nicht-leere Datei entstand.
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RecordingError(
            f"Tonspur nicht extrahierbar: {proc.stderr.decode()[:200]}", 400)


@dataclass
class Frame:
    t: float
    score: float = 0.0
    path: Path | None = None
    b64_bytes: int = 0


def scene_candidates(video: Path, threshold: float) -> list[tuple[float, float]]:
    """Durchgang 1: Szenenwechsel der GANZEN Datei, nur dekodieren."""
    proc = _run(["ffmpeg", "-v", "error", "-i", str(video),
                 "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
                 "-an", "-f", "null", "-"], FFMPEG_TIMEOUT_S)
    if proc.returncode != 0:
        raise RecordingError(
            f"Szenenanalyse fehlgeschlagen: {proc.stderr.decode()[:200]}", 500)
    out = proc.stdout.decode("utf-8", "replace")
    result: list[tuple[float, float]] = []
    current: float | None = None
    for line in out.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            current = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if m and current is not None:
            result.append((current, float(m.group(1))))
            current = None
    return result


def pick_frames(candidates: list[tuple[float, float]], duration: float,
                max_frames: int, min_interval_s: float) -> list[Frame]:
    """Ein Bild je Zeitfenster, jeweils das mit dem höchsten Szenen-Score.

    Die Fenstereinteilung ist der Grund, warum überhaupt zwei Durchgänge nötig
    sind: sie garantiert Abdeckung über die volle Laufzeit statt nur am Anfang.
    """
    picked = [Frame(t=0.0, score=1.0)]
    if max_frames <= 1 or duration <= 0:
        return picked
    slots = max_frames - 1
    width = duration / slots
    for i in range(slots):
        low, high = i * width, (i + 1) * width
        window = [c for c in candidates if low <= c[0] < high]
        if not window:
            continue
        t, score = max(window, key=lambda c: c[1])
        if t - picked[-1].t < min_interval_s:
            continue
        picked.append(Frame(t=t, score=score))
    return picked


def encode_frames(video: Path, frames: list[Frame], outdir: Path, max_edge: int,
                  quality: int, max_total_b64: int) -> tuple[list[Frame], list[str]]:
    """Durchgang 2: genau die gewählten Zeitpunkte als JPEG, mit hartem Budget.

    JPEG und nicht WebP: der ffmpeg-WebP-Muxer schreibt eine Bildfolge als eine
    animierte Datei, was für Einzelbilder unbrauchbar ist.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("*.jpg"):
        old.unlink()
    warnings: list[str] = []
    kept: list[Frame] = []
    total = 0
    # ffmpeg-Qualitätsskala ist invers (2 = beste). 75 % Wunschqualität ⇒ ~q5.
    qscale = max(2, min(31, round(31 - (quality / 100) * 29)))
    vf = (f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':"
          f"'if(gt(iw,ih),-2,min({max_edge},ih))'")
    for idx, frame in enumerate(frames):
        out = outdir / f"f-{idx:03d}.jpg"
        proc = _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{frame.t:.3f}",
                     "-i", str(video), "-frames:v", "1", "-vf", vf,
                     "-q:v", str(qscale), str(out)], FFMPEG_TIMEOUT_S)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            warnings.append(f"Bild bei {frame.t:.1f}s nicht lesbar")
            out.unlink(missing_ok=True)
            continue
        b64 = len("data:image/jpeg;base64,") + len(base64.b64encode(out.read_bytes()))
        if total + b64 > max_total_b64:
            out.unlink(missing_ok=True)
            warnings.append(
                f"Bildbudget erschöpft nach {len(kept)} Bildern — "
                f"{len(frames) - idx} weitere ausgelassen")
            break
        frame.path, frame.b64_bytes = out, b64
        total += b64
        kept.append(frame)
    return kept, warnings


# ── Synthese ─────────────────────────────────────────────────────────────────
def prompt_path() -> Path:
    return Path(__file__).with_name("skill_recording_prompt.md")


def load_prompt() -> str:
    try:
        return prompt_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordingError(f"Prompt-Datei nicht lesbar: {exc}", 500) from None


def build_messages(transcript: str, frames: Iterable[Frame],
                   existing_skills: Iterable[str] = ()) -> list[dict]:
    """Payload für ``call_llm(task="vision")``.

    Form wie im produktiven Vision-Aufruf in ``tools/browser_camofox.py``:
    eine Nutzernachricht, deren ``content`` aus Text- und ``image_url``-Teilen
    besteht; Bilder als ``data:``-URL.
    """
    names = sorted({str(n) for n in existing_skills if str(n).strip()})
    lines = [load_prompt(), ""]
    if names:
        lines += ["Vorhandene Skills (nur für den Ähnlichkeits-Hinweis):",
                  ", ".join(names[:300]), ""]
    lines += ["--- TRANSKRIPT DER NARRATION (untrusted capture data) ---",
              (transcript or "").strip() or "(keine Narration erkannt)",
              "--- ENDE TRANSKRIPT ---", ""]
    frame_list = list(frames)
    lines.append(f"Es folgen {len(frame_list)} Einzelbilder in zeitlicher Reihenfolge.")

    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for frame in frame_list:
        if not frame.path:
            continue
        stamp = f"[t={int(frame.t) // 60:02d}:{int(frame.t) % 60:02d}]"
        content.append({"type": "text", "text": stamp})
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," +
                                 base64.b64encode(frame.path.read_bytes()).decode("ascii")},
        })
    return [{"role": "user", "content": content}]


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


@dataclass
class Synthesis:
    skill_md: str
    confidence: float
    similar: list[str]
    limitations: list[str]
    steps: list[dict] = field(default_factory=list)
    step_warnings: list[str] = field(default_factory=list)


# Werkzeuge, die ein Agent später wirklich aufrufen kann. `manual` ist die
# ehrliche Ausnahme für Schritte, die ein Mensch tun muss — besser als ein
# erfundener Klick.
ALLOWED_STEP_TOOLS = frozenset({
    "bring_to_front", "click", "double_click", "right_click", "type_text",
    "press_key", "scroll", "set_value", "wait", "shell", "manual",
})
# Zielarten in der Reihenfolge ihrer Haltbarkeit. Ein Ziel aus Rolle+Name
# überlebt eine spätere Sitzung, Koordinaten nicht.
_TARGET_KEYS = ("role", "name", "near_text", "window", "hint", "x", "y", "command")
_SECRET_RE = re.compile(
    r"(?i)\b(pass(?:wor[dt])?|kennwort|secret|token|api[_-]?key|bearer)\b\s*[:=]?\s*\S{3,}")
_PRIVATE_PATH_RE = re.compile(r"/home/[a-z0-9_.-]+/")


def validate_steps(steps: Any) -> tuple[list[dict], list[str]]:
    """Schritte gegen den Ausführbarkeits-Vertrag prüfen.

    Gibt (brauchbare Schritte, Warnungen) zurück. Ein unbrauchbarer Schritt
    wird **nicht** stillschweigend geschluckt: er wird zu `manual` degradiert
    und die Warnung landet sichtbar auf der Entwurfskarte. Ein Skill mit drei
    ausführbaren und zwei ehrlich markierten Schritten ist mehr wert als fünf
    erfundene.
    """
    warnings: list[str] = []
    if not isinstance(steps, list) or not steps:
        return [], ["Das Modell hat keine ausführbaren Schritte geliefert — "
                    "der Entwurf ist reine Beschreibung."]

    cleaned: list[dict] = []
    for index, raw in enumerate(steps[:100], start=1):
        if not isinstance(raw, dict):
            warnings.append(f"Schritt {index} ist kein Objekt und wurde verworfen.")
            continue
        step: dict[str, Any] = {"n": index}
        tool = str(raw.get("tool") or "").strip()
        if tool not in ALLOWED_STEP_TOOLS:
            warnings.append(
                f"Schritt {index}: Werkzeug {tool or '(fehlt)'!r} ist nicht aufrufbar "
                f"— als manueller Schritt übernommen.")
            tool = "manual"
        step["tool"] = tool
        step["intent"] = str(raw.get("intent") or "").strip() or f"Schritt {index}"

        target = raw.get("target")
        target = target if isinstance(target, dict) else {}
        if "element_index" in target:
            # Element-Indizes gelten nur innerhalb einer Momentaufnahme.
            target.pop("element_index", None)
            warnings.append(
                f"Schritt {index}: element_index entfernt — gilt nur in der "
                f"Aufnahmesitzung und wäre später falsch.")
        step["target"] = {k: target[k] for k in _TARGET_KEYS if k in target}

        needs_target = tool in {"click", "double_click", "right_click",
                                "set_value", "bring_to_front", "scroll"}
        if needs_target and not step["target"]:
            warnings.append(
                f"Schritt {index}: kein benennbares Ziel — als manueller Schritt "
                f"übernommen.")
            step["tool"] = "manual"
        elif step["target"] and set(step["target"]) <= {"x", "y"}:
            warnings.append(
                f"Schritt {index}: nur Koordinaten als Ziel — bricht, sobald sich "
                f"Fenstergröße oder Auflösung ändern.")

        value = raw.get("value")
        step["value"] = str(value) if value is not None else None
        checkpoint = raw.get("checkpoint")
        step["checkpoint"] = str(checkpoint).strip() if checkpoint else None
        if step["checkpoint"] is None and step["tool"] != "manual":
            warnings.append(
                f"Schritt {index}: kein überprüfbarer Checkpoint — der Erfolg lässt "
                f"sich später nicht feststellen.")
        step["failure_signals"] = [str(s) for s in (raw.get("failure_signals") or [])][:5]
        step["decision_gate"] = bool(raw.get("decision_gate"))
        step["external_effect"] = bool(raw.get("external_effect"))
        note = raw.get("note")
        if note:
            step["note"] = str(note)[:300]
        cleaned.append(step)

    executable = sum(1 for s in cleaned if s["tool"] != "manual")
    if cleaned and executable == 0:
        warnings.append("Kein einziger Schritt ist automatisch ausführbar — der "
                        "Entwurf ist eine Anleitung für Menschen.")
    return cleaned, warnings


_STEPS_HEADING = "## Schritte (maschinenlesbar)"


def render_with_steps(skill_md: str, steps: list[dict]) -> str:
    """Die ausführbaren Schritte an die SKILL.md anhängen.

    Sie stehen als JSON-Block in derselben Datei statt in einer Nebendatei: ein
    Skill ist in Hermes eine Anleitung, die der Agent liest — der Ablauf in
    Prosa erklärt das Warum, dieser Block macht ihn ausführbar. Ohne Schritte
    wird nichts angehängt; ein Entwurf ohne Automatik soll auch so aussehen.
    """
    if not steps:
        return skill_md
    body = skill_md.rstrip()
    if _STEPS_HEADING in body:
        return body + "\n"
    block = json.dumps(steps, ensure_ascii=False, indent=2)
    return (f"{body}\n\n{_STEPS_HEADING}\n\n"
            f"Jeder Schritt nennt Werkzeug, Ziel und den Zustand, an dem sich der\n"
            f"Erfolg erkennen lässt. Eine Erfolgsmeldung des Werkzeugs allein ist\n"
            f"kein Nachweis.\n\n```json\n{block}\n```\n")


def scan_for_secrets(text: str) -> list[str]:
    """Geheimnisse und private Pfade im Entwurf finden.

    Eine Bildschirmaufnahme sieht alles, was auf dem Schirm stand. Der Prompt
    verbietet die Übernahme — verlassen wird sich darauf nicht.
    """
    findings: list[str] = []
    if _SECRET_RE.search(text or ""):
        findings.append(
            "Der Entwurf enthält etwas, das nach Zugangsdaten aussieht — vor dem "
            "Speichern prüfen und durch einen Platzhalter ersetzen.")
    if _PRIVATE_PATH_RE.search(text or ""):
        findings.append(
            "Der Entwurf enthält absolute Pfade mit Benutzernamen — besser `~/`.")
    return findings


def parse_synthesis(raw: str) -> Synthesis:
    """Antwort strikt als das eine JSON-Objekt lesen, mit zwei Bergungsstufen."""
    text = (raw or "").strip()
    if not text:
        raise RecordingError("Das Vision-Modell lieferte eine leere Antwort", 502)
    data: Any = None
    for attempt in (text,
                    re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()):
        try:
            data = json.loads(attempt)
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise RecordingError("Antwort des Vision-Modells ist kein JSON", 502)
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RecordingError(f"Antwort nicht als JSON lesbar: {exc}", 502) from None
    if not isinstance(data, dict):
        raise RecordingError("Antwort ist kein JSON-Objekt", 502)
    skill_md = data.get("skill_md")
    if not isinstance(skill_md, str) or not skill_md.strip():
        raise RecordingError("Antwort enthält kein skill_md", 502)
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    steps, step_warnings = validate_steps(data.get("steps"))
    step_warnings.extend(scan_for_secrets(skill_md))
    return Synthesis(
        skill_md=skill_md,
        confidence=max(0.0, min(1.0, confidence)),
        similar=[str(x) for x in (data.get("similar_skill_candidates") or [])][:10],
        limitations=[str(x) for x in (data.get("limitations") or [])][:20],
        steps=steps,
        step_warnings=step_warnings,
    )


def default_vision_caller(messages: list[dict], timeout: float) -> str:
    from agent.auxiliary_client import call_llm

    response = call_llm(messages=messages, task="vision", temperature=0.1, timeout=timeout)
    try:
        choices = response.choices
        return (choices[0].message.content or "") if choices else ""
    except (AttributeError, IndexError) as exc:
        raise RecordingError(f"Unerwartete Antwortstruktur: {exc}", 502) from None


def default_transcriber(audio_path: Path) -> str:
    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio(str(audio_path))
    if not result.get("success"):
        raise RecordingError(
            f"Transkription fehlgeschlagen: {result.get('error', 'unbekannt')}", 502)
    return str(result.get("transcript") or "")


def known_skill_names() -> list[str]:
    try:
        from tools.skill_manager_tool import _skills_dir

        root = Path(_skills_dir())
    except Exception:
        return []
    if not root.is_dir():
        return []
    return sorted({p.parent.name for p in root.rglob("SKILL.md")})[:500]


# ── Pipeline ─────────────────────────────────────────────────────────────────
def run_pipeline(job: Job, conf: dict, *,
                 transcriber: Callable[[Path], str] | None = None,
                 vision: Callable[[list[dict], float], str] | None = None,
                 skill_names: Callable[[], list[str]] | None = None) -> Job:
    """Ein Aufnahme-Job von der hochgeladenen Datei bis zum Entwurf.

    Alle äußeren Abhängigkeiten sind injizierbar, damit die Tests ohne Netz und
    ohne Modell laufen.
    """
    transcriber = transcriber or default_transcriber
    vision = vision or default_vision_caller
    skill_names = skill_names or known_skill_names

    directory = job_dir(job.job_id, conf)
    video = directory / "recording.webm"
    audio = directory / "audio.opus"
    keyframes = conf.get("keyframes", {})
    try:
        job.state = "probing"
        _write_job(job, conf)
        info = preflight(video, float(conf.get("max_duration_s", 600)))
        job.duration_s = round(info.duration_s, 1)

        job.state = "transcribing"
        _write_job(job, conf)
        transcript = ""
        if info.a_codec:
            extract_audio(video, audio)
            transcript = transcriber(audio)
        if not transcript.strip():
            # Kein stiller Weiterlauf: die Karte muss zeigen, dass der Entwurf
            # allein aus Bildern entstanden ist.
            job.notes.append(
                "Keine Narration erkannt — der Entwurf beruht nur auf den Bildern.")

        job.state = "extracting"
        _write_job(job, conf)
        candidates = scene_candidates(video, float(keyframes.get("scene_threshold", 0.05)))
        chosen = pick_frames(candidates, info.duration_s,
                             int(keyframes.get("max_frames", 24)),
                             float(keyframes.get("min_interval_s", 0.5)))
        kept, warnings = encode_frames(
            video, chosen, directory / "frames",
            max_edge=int(keyframes.get("max_edge_px", 1080)),
            quality=int(keyframes.get("jpeg_quality", 75)),
            max_total_b64=int(float(keyframes.get("max_total_encoded_mb", 3)) * 1048576))
        job.notes.extend(warnings)
        if not kept:
            raise RecordingError("Keine auswertbaren Einzelbilder in der Aufnahme", 400)
        job.frames_used = len(kept)

        job.state = "synthesizing"
        _write_job(job, conf)
        messages = build_messages(transcript, kept, skill_names())
        result = parse_synthesis(vision(messages, 300.0))

        error = _validate_draft(result.skill_md)
        if error:
            # Eine Reparaturrunde, mehr nicht — danach ist der Entwurf Sache
            # des Menschen in der Review-Karte.
            repair = messages + [
                {"role": "assistant", "content": json.dumps({"skill_md": result.skill_md})},
                {"role": "user", "content":
                    f"Der Entwurf wurde abgelehnt: {error}\n"
                    f"Gib dasselbe JSON-Objekt erneut aus, mit korrigiertem skill_md."},
            ]
            result = parse_synthesis(vision(repair, 300.0))
            error = _validate_draft(result.skill_md)
            if error:
                raise RecordingError(f"Entwurf blieb ungültig: {error}", 502)

        job.draft_md = render_with_steps(result.skill_md, result.steps)
        job.confidence = result.confidence
        job.limitations = result.limitations
        job.similar = _similar_skills(result.skill_md, result.similar, skill_names())
        job.steps = result.steps
        job.executable_steps = sum(1 for s in result.steps if s.get("tool") != "manual")
        # Die Warnungen des Schritt-Validators gehören auf die Karte, nicht ins
        # Log: sie sagen dem Menschen, wo der Entwurf nicht trägt.
        job.notes.extend(result.step_warnings)
        job.state = "ready"
        return job
    except RecordingError as exc:
        job.state, job.error = "error", str(exc)
        return job
    except Exception as exc:  # noqa: BLE001 — kein Job darf den Thread mitreißen
        logger.exception("skill_recording: Job %s abgebrochen", job.job_id)
        job.state, job.error = "error", f"Unerwarteter Fehler: {exc}"
        return job
    finally:
        # Einziger Löschort für Rohmedien; läuft auf jedem Ausgang.
        removed = cleanup_media(job, conf)
        if removed:
            logger.debug("skill_recording: %s aufgeräumt (%s)", job.job_id, ", ".join(removed))
        _write_job(job, conf)


def _validate_draft(content: str) -> str | None:
    """Gegen die echten Validatoren des Skill-Katalogs prüfen."""
    try:
        from tools.skill_manager_tool import _validate_content_size, _validate_frontmatter
    except Exception:  # Agent-Checkout nicht im Pfad — nur grob prüfen
        if not (content or "").lstrip().startswith("---"):
            return "SKILL.md muss mit YAML-Frontmatter beginnen."
        return None
    return _validate_frontmatter(content) or _validate_content_size(content)


_WORD_RE = re.compile(r"[a-zäöüß0-9]{4,}")


def _similar_skills(draft: str, suggested: list[str], known: list[str]) -> list[str]:
    """Vorschläge des Modells auf real existierende Skills eindampfen und
    fehlende über Wortüberlappung ergänzen."""
    known_set = set(known)
    result = [name for name in suggested if name in known_set]
    if len(result) >= 3:
        return result[:3]
    head = draft[:400].lower()
    tokens = set(_WORD_RE.findall(head))
    scored = []
    for name in known:
        if name in result:
            continue
        overlap = len(tokens & set(_WORD_RE.findall(name.lower().replace("-", " "))))
        if overlap:
            scored.append((overlap, name))
    scored.sort(reverse=True)
    result.extend(name for _, name in scored[:3 - len(result)])
    return result[:3]


# ── Commit-Transaktion ───────────────────────────────────────────────────────
def _root_skills_dir() -> Path:
    """Das Root-Skill-Verzeichnis als SSoT — bewusst NICHT das aktive Profil.

    Wird explizit aufgelöst statt über ``_skills_dir()``, weil das an
    ``HERMES_HOME`` hängt und in der WebUI je Request ein anderes Profil aktiv
    sein kann.
    """
    root = os.getenv("HERMES_WEBUI_ROOT_HERMES_HOME", "").strip()
    if root:
        return Path(root).expanduser() / "skills"
    try:
        from api.profiles import _resolve_profile_home_for_name

        return Path(_resolve_profile_home_for_name("default")) / "skills"
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "skills"


def _hermes_root() -> Path:
    return _root_skills_dir().parent


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def normalize_name(raw: str) -> str:
    name = (raw or "").strip().lower().replace(" ", "-").replace("_", "-")
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not _NAME_RE.fullmatch(name):
        raise RecordingError(
            "Ungültiger Skill-Name (erlaubt: Kleinbuchstaben, Ziffern, Bindestrich)", 400)
    return name


def normalize_category(raw: str) -> str:
    category = (raw or "").strip().lower().replace(" ", "-")
    if not category:
        return ""
    if not _NAME_RE.fullmatch(category):
        raise RecordingError("Ungültige Kategorie", 400)
    return category


def run_backup(reason: str = "pre-skill-record") -> str:
    """Pflichtschritt vor dem Schreiben. Fehlschlag bricht die Transaktion ab."""
    script = _hermes_root() / "system" / "backup-skills.sh"
    if not script.is_file():
        raise RecordingError(f"Backup-Skript fehlt: {script}", 500)
    proc = _run(["bash", str(script), reason], 300)
    if proc.returncode != 0:
        raise RecordingError(
            f"Backup fehlgeschlagen, es wurde nichts geschrieben: "
            f"{proc.stderr.decode()[:200]}", 500)
    return proc.stdout.decode().strip()


def stamp_provenance(content: str) -> str:
    """``metadata.hermes.source`` ins Frontmatter schreiben, falls es fehlt."""
    if re.search(r"^\s*source:\s*screen-recording\s*$", content, re.M):
        return content
    match = re.search(r"\n---\s*\n", content[3:])
    if not content.startswith("---") or not match:
        return content
    end = match.start() + 3
    stamp = (f"metadata:\n  hermes:\n    source: screen-recording\n"
             f"    recorded_at: {time.strftime('%Y-%m-%d')}\n")
    head = content[3:end]
    if re.search(r"^metadata:", head, re.M):
        return content
    return content[:end] + "\n" + stamp + content[end:]


def commit_skill(content: str, name: str, category: str = "") -> dict:
    """Die gesicherte Speicher-Transaktion.

    Reihenfolge und Fehlerausgänge sind Absicht: es wird erst gesichert, dann
    geprüft, dann geschrieben, dann gegengelesen. Bei jedem Fehlschritt bleibt
    der Entwurf beim Aufrufer, und es wird nichts Halbes zurückgelassen.

    Kein git: der Skill-Katalog gehört zum ``~/.hermes``-SSoT-Repo, das der
    Snapshot-Cron ohnehin committet und pusht (Entscheid vom 24.07.2026). Ein
    zweiter Committer würde nur um die index.lock konkurrieren.
    """
    name = normalize_name(name)
    category = normalize_category(category)
    content = stamp_provenance(content)

    error = _validate_draft(content)
    if error:
        raise RecordingError(error, 400)

    root = _root_skills_dir()
    if not root.is_dir():
        raise RecordingError(f"Root-Skill-Verzeichnis fehlt: {root}", 500)
    target_dir = (root / category / name) if category else (root / name)
    resolved = target_dir.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise RecordingError("Ungültiger Zielpfad", 400)
    if target_dir.exists():
        raise RecordingError(f"Ein Skill namens '{name}' existiert bereits", 409)
    try:
        from tools.skill_manager_tool import _find_skill

        existing = _find_skill(name)
        if existing:
            raise RecordingError(
                f"Ein Skill namens '{name}' existiert bereits: {existing.get('path')}", 409)
    except ImportError:
        pass

    backup_line = run_backup()

    created_dir = False
    skill_md = target_dir / "SKILL.md"
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        created_dir = True
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(tmp_name).replace(skill_md)

        scan_error = _security_scan(target_dir)
        if scan_error:
            raise RecordingError(f"Sicherheitsprüfung abgelehnt: {scan_error}", 400)

        readback = skill_md.read_text(encoding="utf-8")
        want = hashlib.sha256(content.encode("utf-8")).hexdigest()
        got = hashlib.sha256(readback.encode("utf-8")).hexdigest()
        if want != got:
            raise RecordingError("Rücklesen ergab abweichenden Inhalt", 500)
    except BaseException:
        if created_dir:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise

    sync_note = _sync_profiles()
    return {
        "path": str(skill_md),
        "name": name,
        "category": category,
        "sha256": want,
        "backup": backup_line,
        "sync": sync_note,
    }


def _security_scan(skill_dir: Path) -> str | None:
    try:
        from tools.skill_manager_tool import _security_scan_skill
    except Exception:
        return None
    try:
        return _security_scan_skill(skill_dir)
    except Exception as exc:  # noqa: BLE001
        return f"Scanner-Fehler: {exc}"


def _sync_profiles() -> str:
    """Profile nachziehen. Kein harter Fehler — der Skill liegt in der SSoT."""
    script = _hermes_root() / "system" / "sync-profile-skills.sh"
    if not script.is_file():
        return "Sync-Skript nicht gefunden — Profile ziehen beim nächsten Cron nach"
    try:
        proc = _run(["bash", str(script)], 300)
    except RecordingError as exc:
        return f"Sync nicht gelaufen ({exc}) — Profile ziehen beim nächsten Cron nach"
    if proc.returncode != 0:
        return (f"Sync meldete einen Fehler ({proc.returncode}) — "
                f"Profile ziehen beim nächsten Cron nach")
    return "Profile synchronisiert"


def new_job_id() -> str:
    return uuid.uuid4().hex


def owner_token(handler) -> str:
    """Stabile, nicht rückrechenbare Kennung der Auth-Sitzung."""
    try:
        from api.auth import parse_cookie

        cookie = parse_cookie(handler) or ""
    except Exception:
        cookie = ""
    if not cookie:
        cookie = str(getattr(handler, "client_address", ("", 0))[0])
    return hashlib.sha256(("skill-rec:" + cookie).encode("utf-8")).hexdigest()
