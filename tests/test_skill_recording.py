"""Tests für „Record a Skill" (Bildschirmaufnahme → SKILL.md-Entwurf).

Schwerpunkt liegt auf den drei Fehlern, die der WP0A-Contract-Spike gefunden
hat — alle drei laufen still durch, wenn man sie nicht ausdrücklich prüft:

1. Frame-Auswahl, die nur den Anfang einer langen Aufnahme abdeckt.
2. Abgeschnittene Aufnahmen, die ffprobe-Metadaten und ffmpeg-Exitcode bestehen.
3. Uploads, die als Erfolg quittiert werden, obwohl Bytes fehlen.

Die Tests laufen ohne Netz und ohne Modell: STT und Vision werden injiziert.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import skill_recording as sr  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe nicht installiert")

BOUNDARY = "----test-boundary"


# ── Hilfsmittel ──────────────────────────────────────────────────────────────
def multipart(payload: bytes, filename: str = "recording.webm") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: video/webm\r\n\r\n"
    ).encode() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()


def content_type() -> str:
    return f"multipart/form-data; boundary={BOUNDARY}"


@pytest.fixture
def conf(tmp_path) -> dict:
    """Feature-Konfiguration mit Scratch im tmp_path (nie unter ~/.hermes)."""
    cfg = dict(sr._DEFAULTS)
    cfg["enabled"] = True
    cfg["scratch_dir"] = str(tmp_path / "scratch")
    cfg["keyframes"] = dict(sr._DEFAULTS["keyframes"])
    return cfg


def make_fixture(path: Path, seconds: int = 6, width: int = 320, height: int = 240,
                 fps: int = 5, with_audio: bool = True) -> Path:
    """Kleines, schnell erzeugtes WebM mit Szenenwechseln."""
    parts = []
    for idx, color in enumerate(("red", "green", "blue")):
        clip = path.parent / f"part{idx}.webm"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"color=c={color}:s={width}x{height}:r={fps}:d={seconds / 3:.2f}",
             "-c:v", "libvpx-vp9", "-b:v", "120k", "-deadline", "realtime",
             "-cpu-used", "8", str(clip)], check=True, capture_output=True)
        parts.append(clip)
    listing = path.parent / "concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    video = path.parent / "video-only.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(video)],
                   check=True, capture_output=True)
    if not with_audio:
        video.replace(path)
        return path
    audio = path.parent / "audio.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=f=440:d={seconds}", "-c:a", "libopus", "-b:a", "32k",
                    str(audio)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
                    "-c", "copy", "-shortest", str(path)], check=True, capture_output=True)
    return path


# ── Feature-Flag ─────────────────────────────────────────────────────────────
def test_feature_is_off_by_default():
    """Ohne ausdrückliche Freigabe existiert das Feature zur Laufzeit nicht."""
    assert sr._DEFAULTS["enabled"] is False


def test_env_override_switches_the_flag(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_SKILL_RECORDING", "1")
    assert sr.is_enabled() is True
    monkeypatch.setenv("HERMES_WEBUI_SKILL_RECORDING", "0")
    assert sr.is_enabled() is False


def test_scratch_dir_under_hermes_home_is_refused(monkeypatch, tmp_path):
    """Ein Aufnahme-Job darf niemals in die Live-Datenhaltung schreiben."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(
        "api.profiles.get_active_hermes_home", lambda: home, raising=False)
    with pytest.raises(sr.RecordingError):
        sr.scratch_root({"scratch_dir": str(home / "skill-rec")})


# ── Streaming-Ingest ─────────────────────────────────────────────────────────
def test_upload_writes_file_and_returns_path(tmp_path):
    payload = os.urandom(200_000)
    body = multipart(payload)
    out = sr.stream_recording_upload(io.BytesIO(body), content_type(), str(len(body)),
                                     tmp_path, 10 * 1024 * 1024)
    assert out.name == "recording.webm"
    assert out.read_bytes() == payload


def test_upload_over_limit_is_refused_before_reading_the_body(tmp_path):
    """Die Grenze greift am Content-Length, nicht erst nach dem Einlesen."""
    body = multipart(os.urandom(50_000))

    class ExplodingReader:
        def read(self, _n):  # pragma: no cover — darf nie aufgerufen werden
            raise AssertionError("Body wurde trotz Überschreitung gelesen")

    with pytest.raises(sr.RecordingError) as exc:
        sr.stream_recording_upload(ExplodingReader(), content_type(), str(len(body)),
                                   tmp_path, 1024)
    assert exc.value.status == 413
    assert not list(tmp_path.glob("*"))


@pytest.mark.parametrize("length,label", [
    ("", "fehlend"),
    ("abc", "keine Zahl"),
    ("-5", "negativ"),
    ("0", "leer"),
])
def test_upload_requires_a_sane_content_length(tmp_path, length, label):
    body = multipart(b"x" * 100)
    with pytest.raises(sr.RecordingError):
        sr.stream_recording_upload(io.BytesIO(body), content_type(), length,
                                   tmp_path, 10 * 1024 * 1024)
    assert not list(tmp_path.glob("*")), f"Reste nach Fall {label!r}"


def test_upload_with_lying_content_length_is_an_error(tmp_path):
    """REGRESSION (WP0A): der Prototyp quittierte diesen Fall zuerst mit ok.

    Der Body ist vollständig, aber der Absender hat mehr angekündigt. Das ist
    ein abgebrochener Upload und darf nicht als Erfolg durchgehen.
    """
    body = multipart(os.urandom(50_000))
    with pytest.raises(sr.RecordingError) as exc:
        sr.stream_recording_upload(io.BytesIO(body), content_type(),
                                   str(len(body) + 5000), tmp_path, 10 * 1024 * 1024)
    assert "abgebrochen" in str(exc.value)
    assert not list(tmp_path.glob("*")), "Teildatei blieb liegen"


def test_upload_with_truncated_body_is_an_error(tmp_path):
    """REGRESSION (WP0A): echt abgeschnittener Body, Endboundary fehlt."""
    body = multipart(os.urandom(50_000))
    with pytest.raises(sr.RecordingError):
        sr.stream_recording_upload(io.BytesIO(body[: len(body) // 2]), content_type(),
                                   str(len(body)), tmp_path, 10 * 1024 * 1024)
    assert not list(tmp_path.glob("*"))


def test_upload_without_boundary_is_refused(tmp_path):
    with pytest.raises(sr.RecordingError):
        sr.stream_recording_upload(io.BytesIO(b"x"), "multipart/form-data", "1",
                                   tmp_path, 1024)


def test_upload_rejects_a_non_file_part(tmp_path):
    body = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\n"
            f"hallo\r\n--{BOUNDARY}--\r\n").encode()
    with pytest.raises(sr.RecordingError):
        sr.stream_recording_upload(io.BytesIO(body), content_type(), str(len(body)),
                                   tmp_path, 1024 * 1024)


# ── Preflight ────────────────────────────────────────────────────────────────
@needs_ffmpeg
def test_preflight_accepts_a_valid_recording(tmp_path):
    video = make_fixture(tmp_path / "ok.webm")
    info = sr.preflight(video, 600)
    assert info.v_codec == "vp9"
    assert info.a_codec == "opus"
    assert 5.0 <= info.duration_s <= 7.0
    assert info.decodable_frames > 0


@needs_ffmpeg
def test_preflight_rejects_a_truncated_recording(tmp_path):
    """REGRESSION (WP0A): der wichtigste stille Fehlerfall.

    Die abgeschnittene Datei besteht sowohl die ffprobe-Metadaten (der
    Matroska-Header meldet weiter die volle Dauer) als auch das Dekodieren
    durch ffmpeg (Exitcode 0). Nur das Zählen der Frames deckt sie auf.
    """
    video = make_fixture(tmp_path / "full.webm")
    truncated = tmp_path / "truncated.webm"
    truncated.write_bytes(video.read_bytes()[:2000])

    # Beleg, dass die naheliegenden Prüfungen hier versagen:
    meta = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(truncated)], capture_output=True)
    assert meta.returncode == 0 and meta.stdout.strip(), "ffprobe meldet weiter eine Dauer"
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(truncated), "-f", "null", "-"],
                            capture_output=True)
    assert decode.returncode == 0, "ffmpeg beendet sich mit 0 — Exitcode taugt nicht als Kriterium"

    with pytest.raises(sr.RecordingError) as exc:
        sr.preflight(truncated, 600)
    assert "unvollständig" in str(exc.value)


@needs_ffmpeg
def test_preflight_rejects_a_foreign_container(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=red:s=160x120:r=5:d=1", "-c:v", "libx264", str(mp4)],
                   check=True, capture_output=True)
    with pytest.raises(sr.RecordingError) as exc:
        sr.preflight(mp4, 600)
    assert exc.value.status == 415


@needs_ffmpeg
def test_preflight_enforces_the_duration_limit(tmp_path):
    video = make_fixture(tmp_path / "long.webm", seconds=6)
    with pytest.raises(sr.RecordingError) as exc:
        sr.preflight(video, 2)
    assert exc.value.status == 413


def test_preflight_rejects_a_missing_file(tmp_path):
    with pytest.raises(sr.RecordingError):
        sr.preflight(tmp_path / "gibtsnicht.webm", 600)


# ── Frame-Auswahl ────────────────────────────────────────────────────────────
def test_frame_selection_covers_the_whole_recording():
    """REGRESSION (WP0A): einstufiges Auswählen deckte nur 7,5 % ab.

    Die Kandidaten liegen gleichmäßig über zehn Minuten. Die Auswahl muss sich
    über die volle Laufzeit verteilen, nicht am Anfang kleben.
    """
    duration = 600.0
    candidates = [(t, 0.1 + (t % 7) / 10) for t in range(1, 600)]
    picked = sr.pick_frames(candidates, duration, max_frames=24, min_interval_s=0.5)

    assert len(picked) == 24
    assert picked[0].t == 0.0
    coverage = picked[-1].t / duration
    assert coverage > 0.9, f"nur {coverage:.1%} der Aufnahme abgedeckt"
    # und gleichmäßig, nicht als Traube am Ende
    gaps = [b.t - a.t for a, b in zip(picked, picked[1:])]
    assert max(gaps) < 2 * duration / len(picked)


def test_frame_selection_is_monotonic_and_bounded():
    candidates = [(float(t), 0.5) for t in range(0, 100)]
    picked = sr.pick_frames(candidates, 100.0, max_frames=10, min_interval_s=0.5)
    assert len(picked) <= 10
    assert [f.t for f in picked] == sorted(f.t for f in picked)


def test_frame_selection_survives_a_video_without_scene_changes():
    """Ohne Szenenwechsel wird gleichmäßig abgetastet — nicht aufgegeben.

    Diese Zusicherung lautete bis zum 25.07. `== [0.0]` und hat damit den Fehler
    als Sollverhalten festgeschrieben: ein einziges Bild aus einer 5-Minuten-
    Aufnahme. Der Durchstich mit einer echten (und damit statischen) Aufnahme
    hat es aufgedeckt.
    """
    picked = sr.pick_frames([], 300.0, max_frames=24, min_interval_s=0.5)
    assert len(picked) == 24
    assert picked[-1].t > 0.9 * 300.0


@needs_ffmpeg
def test_encode_frames_honours_the_total_byte_budget(tmp_path):
    video = make_fixture(tmp_path / "budget.webm")
    frames = [sr.Frame(t=float(i)) for i in range(5)]
    kept, warnings = sr.encode_frames(video, frames, tmp_path / "frames",
                                      max_edge=320, quality=75, max_total_b64=1500)
    assert len(kept) < len(frames)
    assert any("budget" in w.lower() for w in warnings)
    assert sum(f.b64_bytes for f in kept) <= 1500


# ── Synthese-Antwort ─────────────────────────────────────────────────────────
def test_parse_synthesis_reads_a_clean_object():
    raw = json.dumps({"skill_md": "---\nname: x\ndescription: y\n---\n\nBody",
                      "confidence": 0.5, "similar_skill_candidates": ["a"],
                      "limitations": ["b"]})
    result = sr.parse_synthesis(raw)
    assert result.confidence == 0.5
    assert result.similar == ["a"] and result.limitations == ["b"]


@pytest.mark.parametrize("raw,label", [
    ("", "leer"),
    ("Klar, hier ist dein Skill!", "Prosa"),
    ('{"skill_md": "abc",', "abgeschnitten"),
    ('{"confidence": 0.9}', "ohne skill_md"),
    ('{"skill_md": "   "}', "leeres skill_md"),
    ('[{"skill_md": "x"}]', "Liste statt Objekt"),
])
def test_parse_synthesis_rejects_unusable_answers(raw, label):
    with pytest.raises(sr.RecordingError):
        sr.parse_synthesis(raw)


@pytest.mark.parametrize("raw", [
    '```json\n{"skill_md":"---\\nname: y\\n---","confidence":0.5}\n```',
    'Hier:\n{"skill_md":"---\\nname: z\\n---","confidence":"0.4"}\nFertig.',
])
def test_parse_synthesis_recovers_fenced_and_wrapped_json(raw):
    assert sr.parse_synthesis(raw).skill_md.startswith("---")


def test_confidence_is_clamped():
    assert sr.parse_synthesis('{"skill_md":"x","confidence":7}').confidence == 1.0
    assert sr.parse_synthesis('{"skill_md":"x","confidence":-3}').confidence == 0.0
    assert sr.parse_synthesis('{"skill_md":"x","confidence":"weiss nicht"}').confidence == 0.0


def test_prompt_marks_capture_content_as_untrusted():
    text = sr.load_prompt()
    assert "Beobachtungsdaten" in text
    assert "befolgst" in text or "niemals" in text
    assert "Passwörter" in text or "Passwort" in text


def test_messages_carry_timestamps_and_data_urls(tmp_path):
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    frame_file = frame_dir / "f.jpg"
    frame_file.write_bytes(b"\xff\xd8\xff\xe0dummy-jpeg")
    frames = [sr.Frame(t=75.0, score=1.0, path=frame_file)]
    messages = sr.build_messages("Narration", frames, ["vorhandener-skill"])
    parts = messages[0]["content"]
    assert messages[0]["role"] == "user"
    assert any(p.get("text") == "[t=01:15]" for p in parts)
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ── Pipeline und Cleanup ─────────────────────────────────────────────────────
def _fake_vision(_messages, _timeout):
    return json.dumps({
        "skill_md": "---\nname: aufnahme-test\ndescription: Test\n---\n\n# Ablauf\n1. Schritt\n",
        "confidence": 0.6, "similar_skill_candidates": [], "limitations": ["nichts"]})


@needs_ffmpeg
def test_pipeline_produces_a_draft(conf, tmp_path):
    job = sr.Job(job_id=sr.new_job_id(), owner="o")
    directory = sr.job_dir(job.job_id, conf)
    directory.mkdir(parents=True)
    make_fixture(directory / "recording.webm")
    sr._write_job(job, conf)

    sr.run_pipeline(job, conf, transcriber=lambda p: "Ich zeige einen Ablauf.",
                    vision=_fake_vision, skill_names=lambda: [])
    assert job.state == "ready", job.error
    assert job.draft_md.startswith("---")
    assert job.frames_used and job.frames_used > 0


@needs_ffmpeg
@pytest.mark.parametrize("stage", ["probe", "transcribe", "frames", "vision"])
def test_pipeline_leaves_no_raw_media_on_any_failure(conf, tmp_path, stage):
    """Rohmedien dürfen keinen Ausgang überleben — Erfolg wie Fehler."""
    job = sr.Job(job_id=sr.new_job_id(), owner="o")
    directory = sr.job_dir(job.job_id, conf)
    directory.mkdir(parents=True)
    video = directory / "recording.webm"
    make_fixture(video)
    sr._write_job(job, conf)

    def boom_transcriber(_path):
        raise sr.RecordingError("STT kaputt")

    def boom_vision(_messages, _timeout):
        raise sr.RecordingError("Modell kaputt")

    kwargs = {"transcriber": lambda p: "text", "vision": _fake_vision,
              "skill_names": lambda: []}
    if stage == "probe":
        video.write_bytes(b"kein video")
    elif stage == "transcribe":
        kwargs["transcriber"] = boom_transcriber
    elif stage == "frames":
        conf["keyframes"]["max_total_encoded_mb"] = 0.0000001
    elif stage == "vision":
        kwargs["vision"] = boom_vision

    sr.run_pipeline(job, conf, **kwargs)

    leftovers = [p for p in directory.rglob("*")
                 if p.is_file() and p.suffix in {".webm", ".opus", ".wav", ".jpg", ".part"}]
    assert not leftovers, f"übrig nach {stage}: {[p.name for p in leftovers]}"
    assert job.state == "error", f"Stufe {stage} meldete {job.state}"


@needs_ffmpeg
def test_pipeline_cleans_up_after_success(conf):
    job = sr.Job(job_id=sr.new_job_id(), owner="o")
    directory = sr.job_dir(job.job_id, conf)
    directory.mkdir(parents=True)
    make_fixture(directory / "recording.webm")
    sr._write_job(job, conf)

    sr.run_pipeline(job, conf, transcriber=lambda p: "text", vision=_fake_vision,
                    skill_names=lambda: [])
    assert job.state == "ready"
    assert not (directory / "recording.webm").exists()
    assert not (directory / "audio.opus").exists()
    assert not (directory / "frames").exists()
    assert (directory / sr.JOB_FILE).exists(), "der Jobdatensatz selbst muss bleiben"


@needs_ffmpeg
def test_silent_recording_is_flagged_not_silently_accepted(conf):
    """Leeres Transkript heißt: der Entwurf stammt allein aus Bildern."""
    job = sr.Job(job_id=sr.new_job_id(), owner="o")
    directory = sr.job_dir(job.job_id, conf)
    directory.mkdir(parents=True)
    make_fixture(directory / "recording.webm")
    sr._write_job(job, conf)

    sr.run_pipeline(job, conf, transcriber=lambda p: "   ", vision=_fake_vision,
                    skill_names=lambda: [])
    assert job.state == "ready"
    assert any("Narration" in note for note in job.notes)


# ── Jobverwaltung ────────────────────────────────────────────────────────────
def test_job_status_is_not_readable_by_another_owner(conf):
    job = sr.Job(job_id=sr.new_job_id(), owner="besitzer")
    sr._write_job(job, conf)
    assert sr.load_job_for_owner(job.job_id, "besitzer", conf).job_id == job.job_id
    with pytest.raises(sr.RecordingError) as exc:
        sr.load_job_for_owner(job.job_id, "jemand-anderes", conf)
    assert exc.value.status == 404, "fremde Jobs dürfen sich nicht über 403 verraten"


def test_public_view_hides_the_owner(conf):
    job = sr.Job(job_id=sr.new_job_id(), owner="geheim")
    assert "owner" not in job.public()


@pytest.mark.parametrize("bad_id", ["", "../etc", "nope", "a" * 31, "A" * 32])
def test_job_ids_are_validated(conf, bad_id):
    with pytest.raises(sr.RecordingError):
        sr.job_dir(bad_id, conf)


def test_sweeper_marks_running_jobs_as_interrupted(conf):
    """Nach einem WebUI-Neustart ist jeder laufende Job verwaist."""
    job = sr.Job(job_id=sr.new_job_id(), owner="o", state="synthesizing")
    sr._write_job(job, conf)
    directory = sr.job_dir(job.job_id, conf)
    (directory / "recording.webm").write_bytes(b"rohdaten")

    result = sr.sweep_orphans(conf)
    assert result["interrupted"] == 1
    assert sr._read_job(job.job_id, conf).state == "interrupted"
    assert not (directory / "recording.webm").exists()


def test_sweeper_removes_expired_jobs(conf):
    import time as _time
    job = sr.Job(job_id=sr.new_job_id(), owner="o", state="ready")
    job.updated_at = _time.time() - (float(conf["job_ttl_h"]) * 3600 + 60)
    sr._write_job(job, conf)
    # _write_job stempelt updated_at neu — deshalb direkt in die Datei schreiben.
    path = sr.job_dir(job.job_id, conf) / sr.JOB_FILE
    data = json.loads(path.read_text())
    data["updated_at"] = _time.time() - (float(conf["job_ttl_h"]) * 3600 + 60)
    path.write_text(json.dumps(data))

    assert sr.sweep_orphans(conf)["expired"] == 1
    assert not sr.job_dir(job.job_id, conf).exists()


def test_sweeper_does_nothing_when_the_feature_is_off(conf):
    conf["enabled"] = False
    assert sr.sweep_orphans(conf) == {"interrupted": 0, "expired": 0}


# ── Commit-Transaktion ───────────────────────────────────────────────────────
VALID_SKILL = "---\nname: aufnahme-test\ndescription: Ein Test\n---\n\n# Ablauf\n1. Schritt\n"


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """Ein Hermes-Root mit Stub-Skripten — der echte Katalog bleibt unberührt."""
    root = tmp_path / "hermes"
    (root / "skills").mkdir(parents=True)
    system = root / "system"
    system.mkdir()
    backup_log = root / "backup.log"
    (system / "backup-skills.sh").write_text(
        f'#!/usr/bin/env bash\necho "$1" >> {backup_log}\necho "skills-backup: fake"\n',
        encoding="utf-8")
    (system / "sync-profile-skills.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_ROOT_HERMES_HOME", str(root))
    monkeypatch.setattr(sr, "_security_scan", lambda _d: None)
    return root, backup_log


def test_commit_writes_into_the_root_catalog(fake_root):
    root, backup_log = fake_root
    result = sr.commit_skill(VALID_SKILL, "Aufnahme Test", "demo")

    written = root / "skills" / "demo" / "aufnahme-test" / "SKILL.md"
    assert written.is_file()
    assert result["path"] == str(written)
    assert "pre-skill-record" in backup_log.read_text(), "Backup lief nicht"
    text = written.read_text(encoding="utf-8")
    assert "source: screen-recording" in text, "Herkunft fehlt im Frontmatter"


def test_commit_refuses_to_overwrite_an_existing_skill(fake_root):
    root, _ = fake_root
    sr.commit_skill(VALID_SKILL, "aufnahme-test")
    with pytest.raises(sr.RecordingError) as exc:
        sr.commit_skill(VALID_SKILL, "aufnahme-test")
    assert exc.value.status == 409


def test_commit_rejects_a_draft_without_frontmatter(fake_root):
    root, backup_log = fake_root
    with pytest.raises(sr.RecordingError):
        sr.commit_skill("nur text ohne frontmatter", "kaputt")
    assert not backup_log.exists(), "Backup lief, obwohl der Entwurf ungültig war"
    assert not (root / "skills" / "kaputt").exists()


def test_commit_writes_nothing_when_the_backup_fails(fake_root):
    root, _ = fake_root
    (root / "system" / "backup-skills.sh").write_text(
        "#!/usr/bin/env bash\necho kaputt >&2\nexit 1\n", encoding="utf-8")
    with pytest.raises(sr.RecordingError) as exc:
        sr.commit_skill(VALID_SKILL, "ohne-backup")
    assert "Backup" in str(exc.value)
    assert not (root / "skills" / "ohne-backup").exists()


def test_commit_rolls_back_when_the_security_scan_blocks(fake_root, monkeypatch):
    root, _ = fake_root
    monkeypatch.setattr(sr, "_security_scan", lambda _d: "verdächtiges Skript gefunden")
    with pytest.raises(sr.RecordingError) as exc:
        sr.commit_skill(VALID_SKILL, "geblockt")
    assert "Sicherheitsprüfung" in str(exc.value)
    assert not (root / "skills" / "geblockt").exists(), "Rückbau nach Scan-Block fehlt"


def test_commit_does_not_touch_git(fake_root):
    """Entscheid vom 24.07.: Versionierung macht allein der SSoT-Cron.

    Geprüft wird der ausführbare Teil — im Docstring steht git absichtlich, weil
    dort die Begründung nachlesbar sein soll.
    """
    source = Path(sr.__file__).read_text(encoding="utf-8")
    body = source[source.index("def commit_skill"):source.index("def _security_scan")]
    code = "\n".join(line.split("#")[0] for line in body.splitlines())
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert "git" not in code


@pytest.mark.parametrize("name", ["", "Ü" * 5, "../flucht", "a", "x" * 70, "-"])
def test_names_are_validated(name):
    with pytest.raises(sr.RecordingError):
        sr.normalize_name(name)


def test_names_are_normalised():
    assert sr.normalize_name("  Mein Toller Skill ") == "mein-toller-skill"
    assert sr.normalize_name("a__b") == "a-b"
    # Führende/abschließende Trennzeichen werden entfernt, nicht abgelehnt.
    assert sr.normalize_name("-vorn-") == "vorn"


def test_provenance_is_only_stamped_once():
    once = sr.stamp_provenance(VALID_SKILL)
    assert once.count("source: screen-recording") == 1
    assert sr.stamp_provenance(once) == once


def test_provenance_leaves_existing_metadata_alone():
    with_meta = "---\nname: x\ndescription: y\nmetadata:\n  eigenes: 1\n---\n\nBody\n"
    assert sr.stamp_provenance(with_meta) == with_meta


# ── Verdrahtung im Server ────────────────────────────────────────────────────
def test_upload_route_runs_before_any_body_reading_branch():
    """Der Ingest muss vor read_body() liegen, sonst zieht der alte Parser die
    komplette Aufnahme in den RAM (gemessen: das Vierfache der Nutzlast)."""
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    handle_post = src[src.index("def handle_post("):]
    ingest = handle_post.index('parsed.path == "/api/skills/record"')
    csrf = handle_post.index("_check_csrf(handler)")
    # Beide Wege, auf denen handle_post den Body einliest: der Sidecar-Proxy
    # (read_request_body=True) und die gewöhnlichen Routen über read_body().
    first_body_read = min(handle_post.index("_handle_extension_sidecar_proxy("),
                          handle_post.index("read_body(handler)"))
    assert csrf < ingest < first_body_read


def test_permissions_policy_allows_display_capture():
    src = (ROOT / "api" / "helpers.py").read_text(encoding="utf-8")
    assert "display-capture=(self)" in src


def test_record_button_is_hidden_until_the_server_reports_the_feature():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="skillRecordBtn"' in html
    button = html[html.index('id="skillRecordBtn"'):]
    assert 'style="display:none"' in button[:400]


def test_frontend_pins_the_recorder_bitrate():
    """Ohne gesetzte Bitrate erreicht eine 10-Minuten-Aufnahme leicht 190 MB."""
    js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "videoBitsPerSecond: 1500000" in js


def test_frontend_strings_are_translatable():
    js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    block = js[js.index("// ── Record a Skill"):]
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\bt\('([a-z0-9_]+)'", block))
    english = i18n[i18n.index("  en: {"):i18n.index("  it: {")]
    missing = [key for key in used if f"{key}:" not in english]
    assert not missing, f"ohne englischen Text: {missing}"



# ── Ausführbarkeit der Schritte (Umbau 25.07.) ───────────────────────────────
# Der Kern der Nachbesserung: die Synthese soll Schritte liefern, die ein Agent
# aufrufen kann — nicht Prosa. Der Prompt allein garantiert das nicht, deshalb
# prüft der Validator, und deshalb prüfen diese Tests den Validator.

def test_steps_survive_a_clean_answer():
    steps, warnings = sr.validate_steps([
        {"n": 1, "intent": "Board öffnen", "tool": "click",
         "target": {"role": "button", "name": "Kanban"},
         "checkpoint": "Fenstertitel enthält 'Kanban'"},
    ])
    assert len(steps) == 1 and not warnings
    assert steps[0]["tool"] == "click"
    assert steps[0]["target"] == {"role": "button", "name": "Kanban"}


def test_unknown_tool_becomes_manual_instead_of_being_dropped():
    """Ein erfundenes Werkzeug darf nicht still verschwinden — sonst sieht der
    Entwurf vollständiger aus, als er ist."""
    steps, warnings = sr.validate_steps([
        {"tool": "telepathie", "intent": "irgendwas", "checkpoint": "x"}])
    assert steps[0]["tool"] == "manual"
    assert any("nicht aufrufbar" in w for w in warnings)


def test_element_index_is_stripped():
    """element_index gilt nur in der Aufnahmesitzung und wäre später falsch."""
    steps, warnings = sr.validate_steps([
        {"tool": "click", "intent": "x", "checkpoint": "y",
         "target": {"element_index": 7, "role": "button", "name": "OK"}}])
    assert "element_index" not in steps[0]["target"]
    assert steps[0]["target"] == {"role": "button", "name": "OK"}
    assert any("element_index" in w for w in warnings)


def test_click_without_a_target_is_downgraded():
    steps, warnings = sr.validate_steps([
        {"tool": "click", "intent": "irgendwohin klicken", "checkpoint": "x"}])
    assert steps[0]["tool"] == "manual"
    assert any("kein benennbares Ziel" in w for w in warnings)


def test_coordinate_only_target_is_flagged_but_kept():
    """Koordinaten sind erlaubt, aber sie brechen bei anderer Auflösung."""
    steps, warnings = sr.validate_steps([
        {"tool": "click", "intent": "x", "checkpoint": "y",
         "target": {"x": 100, "y": 200}}])
    assert steps[0]["tool"] == "click"
    assert any("nur Koordinaten" in w for w in warnings)


def test_missing_checkpoint_is_flagged():
    steps, warnings = sr.validate_steps([
        {"tool": "type_text", "intent": "Titel eintragen", "value": "Test"}])
    assert steps[0]["checkpoint"] is None
    assert any("Checkpoint" in w for w in warnings)


def test_answer_without_steps_says_so():
    steps, warnings = sr.validate_steps(None)
    assert steps == []
    assert any("keine ausführbaren Schritte" in w for w in warnings)


def test_purely_manual_plan_is_called_out():
    steps, warnings = sr.validate_steps([
        {"tool": "manual", "intent": "Hand anlegen"},
        {"tool": "manual", "intent": "noch mehr Hand"}])
    assert len(steps) == 2
    assert any("Kein einziger Schritt ist automatisch ausführbar" in w for w in warnings)


@pytest.mark.parametrize("text,label", [
    ("Das Passwort: geheim123 eintragen", "Passwort"),
    ("api_key = sk-abcdef123456", "API-Key"),
    ("token: ghp_xxxxxxxxxxxx", "Token"),
])
def test_secret_scan_catches_credentials(text, label):
    assert sr.scan_for_secrets(text), f"{label} nicht erkannt"


def test_secret_scan_catches_private_paths():
    findings = sr.scan_for_secrets("Datei liegt unter /home/manfred/projekt/x.md")
    assert any("Benutzernamen" in f for f in findings)


def test_secret_scan_is_quiet_on_clean_text():
    assert sr.scan_for_secrets("Öffne das Board und lege eine Karte an.") == []


def test_steps_are_appended_to_the_skill_md():
    md = "---\nname: x\ndescription: y\n---\n\n# Titel\n\n## Ablauf\n1. Klicken\n"
    out = sr.render_with_steps(md, [{"n": 1, "tool": "click", "intent": "x"}])
    assert "## Schritte (maschinenlesbar)" in out
    assert '"tool": "click"' in out
    # Zweimal anhängen darf den Block nicht verdoppeln.
    assert sr.render_with_steps(out, [{"n": 1, "tool": "click"}]).count(
        "## Schritte (maschinenlesbar)") == 1


def test_draft_without_steps_stays_plain():
    md = "---\nname: x\ndescription: y\n---\n\nText\n"
    assert sr.render_with_steps(md, []) == md


def test_parse_synthesis_carries_steps_through():
    raw = json.dumps({
        "skill_md": "---\nname: x\ndescription: y\n---\n\nBody",
        "steps": [{"tool": "type_text", "intent": "tippen", "value": "hallo",
                   "checkpoint": "Feld enthält 'hallo'"}],
        "confidence": 0.7})
    result = sr.parse_synthesis(raw)
    assert len(result.steps) == 1
    assert result.steps[0]["value"] == "hallo"


def test_prompt_forbids_element_index_and_demands_checkpoints():
    text = sr.load_prompt()
    assert "element_index" in text and "verboten" in text
    assert "verified: false" in text, "der Prompt muss den False-Green-Fall benennen"
    assert "manual" in text


@needs_ffmpeg
def test_extracted_audio_is_accepted_by_the_real_stt_validator(tmp_path):
    """REGRESSION (Durchstich 25.07.): die Pipeline schrieb `audio.opus`.

    `transcribe_audio` prüft die Dateiendung gegen eine Positivliste, in der
    `.opus` fehlt — die Transkription scheiterte am Namen, nicht am Inhalt. Die
    Unit-Tests sahen es nicht, weil sie den Transkriber injizieren. Deshalb
    prüft dieser Test gegen den ECHTEN Validator der STT.
    """
    video = make_fixture(tmp_path / "ton.webm")
    out = tmp_path / "audio.webm"
    sr.extract_audio(video, out)
    assert out.exists() and out.stat().st_size > 0

    try:
        from tools.transcription_tools import _validate_audio_file
    except Exception:
        pytest.skip("Agent-Checkout nicht im Pfad")
    assert _validate_audio_file(str(out)) is None, "STT lehnt die Endung ab"


def test_pipeline_writes_audio_as_webm():
    """Die Endung ist Vertrag, nicht Geschmack — im Quelltext festgehalten."""
    src = Path(sr.__file__).read_text(encoding="utf-8")
    assert 'directory / "audio.webm"' in src
    assert 'directory / "audio.opus"' not in src


def test_static_recording_still_yields_full_coverage():
    """REGRESSION (Durchstich 25.07.): echte Screencasts haben KEINE Szenenwechsel.

    28 s Texteditor-Bedienung ergaben null Kandidaten über der Schwelle. Die
    frühere Fassung übersprang leere Zeitfenster und lieferte genau ein Bild —
    die Synthese schrieb daraus trotzdem einen Entwurf und meldete `ready`.
    Ohne Kandidaten muss gleichmäßig abgetastet werden.
    """
    picked = sr.pick_frames([], duration=28.0, max_frames=24, min_interval_s=0.5)
    assert len(picked) == 24, f"nur {len(picked)} Bilder aus einer statischen Aufnahme"
    assert picked[0].t == 0.0
    assert picked[-1].t > 0.9 * 28.0
    # Der erste Abstand ist kürzer, weil t=0 immer gesetzt wird; ab da gleichmäßig.
    gaps = [b.t - a.t for a, b in zip(picked[1:], picked[2:])]
    assert max(gaps) - min(gaps) < 0.01, "Abtastung ist nicht gleichmäßig"


def test_scene_scores_still_win_inside_a_window():
    """Wo es Szenenwechsel gibt, sollen sie weiterhin die Auswahl bestimmen."""
    candidates = [(5.0, 0.2), (6.0, 0.9), (7.0, 0.3)]
    picked = sr.pick_frames(candidates, duration=10.0, max_frames=3, min_interval_s=0.5)
    assert 6.0 in [round(f.t, 3) for f in picked], "bester Score im Fenster nicht gewählt"


def test_saved_skill_is_readable_like_the_rest_of_the_catalog(fake_root):
    """mkstemp legt mit 0600 an — ein Skill soll wie eine normale Datei liegen."""
    root, _ = fake_root
    sr.commit_skill(VALID_SKILL, "rechte-test")
    written = root / "skills" / "rechte-test" / "SKILL.md"
    mode = written.stat().st_mode & 0o777
    assert mode & 0o044, f"Skill ist nur für den Besitzer lesbar (mode {mode:o})"


# ── Maschinenlesbare Prüfungen ───────────────────────────────────────────────
#
# Der Runner entscheidet eine `pruefung` ohne Sprachmodell und ohne Rückfrage.
# Genau deshalb darf hier nichts durchrutschen, was er nicht prüfen kann: eine
# falsche Behauptung wäre schlimmer als gar keine.

def test_pruefung_bekannter_art_wird_uebernommen():
    steps, warnungen = sr.validate_steps([{
        "tool": "type_text", "intent": "tippen", "target": {"window": "Editor"},
        "value": "Hallo", "checkpoint": "Der Textbereich endet mit „Hallo“.",
        "pruefung": {"art": "text_endet_mit", "wert": "Hallo"},
    }])
    assert steps[0]["pruefung"] == {"art": "text_endet_mit", "wert": "Hallo"}
    assert not [w for w in warnungen if "Prüf" in w]


def test_dateipruefung_braucht_einen_pfad():
    steps, warnungen = sr.validate_steps([{
        "tool": "press_key", "intent": "speichern", "target": {"window": "Editor"},
        "checkpoint": "Die Datei endet mit „Hallo“.",
        "pruefung": {"art": "datei_endet_mit", "wert": "Hallo"},   # Pfad fehlt
    }])
    assert steps[0]["pruefung"] is None
    assert any("pfad" in w for w in warnungen)


def test_dateipruefung_mit_pfad_bleibt_erhalten():
    steps, _ = sr.validate_steps([{
        "tool": "press_key", "intent": "speichern", "target": {"window": "Editor"},
        "checkpoint": "Die Datei endet mit „Hallo“.",
        "pruefung": {"art": "datei_endet_mit", "wert": "Hallo",
                     "pfad": "/tmp/x.txt"},
    }])
    assert steps[0]["pruefung"]["pfad"] == "/tmp/x.txt"


def test_erfundene_pruefart_wird_verworfen_nicht_uebernommen():
    steps, warnungen = sr.validate_steps([{
        "tool": "click", "intent": "klicken", "target": {"role": "button", "name": "OK"},
        "checkpoint": "Der Knopf leuchtet grün.",
        "pruefung": {"art": "knopf_leuchtet", "wert": "gruen"},
    }])
    assert steps[0]["pruefung"] is None, "unbekannte Prüfart darf nicht durchrutschen"
    assert any("Prüfart" in w for w in warnungen)
    # Der Schritt selbst bleibt brauchbar — nur die Behauptung fällt weg.
    assert steps[0]["tool"] == "click"
    assert steps[0]["checkpoint"] == "Der Knopf leuchtet grün."


def test_ohne_pruefung_bleibt_der_schritt_unveraendert():
    steps, _ = sr.validate_steps([{
        "tool": "click", "intent": "klicken",
        "target": {"role": "button", "name": "OK"},
        "checkpoint": "Ein Dialog ist offen.",
    }])
    assert steps[0]["pruefung"] is None
