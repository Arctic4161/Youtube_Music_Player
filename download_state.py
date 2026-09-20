"""Durable Android download results, independent of either process lifetime."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from pathlib import Path

from media_identity import download_audio_path, stable_media_id
from playback_logic import DownloadJob


def _path(state_dir: str, request_id: str, suffix: str) -> Path:
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    directory = Path(state_dir) / ".download-results"
    if suffix == "ack":
        directory /= "acknowledged"
    return directory / f"{key}.{suffix}"


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def job_from_request(request: object) -> DownloadJob | None:
    if not isinstance(request, dict):
        return None
    required = ("request_id", "url", "title", "download_dir")
    if any(not isinstance(request.get(key), str) or not request[key].strip() for key in required):
        return None
    video_id = stable_media_id(request["url"], request.get("video_id"))
    audio_path = request.get("audio_path") or download_audio_path(
        request["download_dir"], request["title"], video_id
    )
    # Older checkpoints did not include audio_path or playlist_id.
    return DownloadJob(
        request_id=request["request_id"], url=request["url"], title=request["title"],
        video_id=video_id, thumbnail_url=str(request.get("thumbnail_url") or ""),
        download_dir=request["download_dir"], audio_path=str(audio_path),
        playlist_id=str(request["playlist_id"]) if request.get("playlist_id") else None,
    )


def is_acknowledged(state_dir: str, request_id: str) -> bool:
    return _path(state_dir, request_id, "ack").is_file()


def remember_request(state_dir: str, request: dict) -> None:
    job = job_from_request(request)
    if job is None:
        raise ValueError("Invalid durable download request")
    if not is_acknowledged(state_dir, job.request_id):
        _write(_path(state_dir, job.request_id, "request.json"), json.loads(job.service_payload()))


def read_result(state_dir: str, request_id: str) -> dict | None:
    if is_acknowledged(state_dir, request_id):
        return None
    record = _read(_path(state_dir, request_id, "result.json"))
    if not record or not job_from_request(record.get("request")):
        return None
    result = record.get("result")
    if not isinstance(result, dict) or result.get("request_id") != request_id:
        return None
    if result.get("status") not in {"success", "error", "cancelled"}:
        return None
    if record["request"]["request_id"] != request_id:
        return None
    return record


def record_result(state_dir: str, result: dict) -> bool:
    request_id = str(result.get("request_id") or "")
    if not request_id or is_acknowledged(state_dir, request_id):
        return False
    request = _read(_path(state_dir, request_id, "request.json"))
    if not job_from_request(request):
        return False
    # A late duplicate must not replace an already committed terminal outcome.
    if read_result(state_dir, request_id) is None:
        _write(_path(state_dir, request_id, "result.json"), {"request": request, "result": result})
    return True


def pending_results(state_dir: str, *, limit: int = 1) -> list[dict]:
    directory = Path(state_dir) / ".download-results"
    results = []
    for path in directory.glob("*.result.json"):
        record = _read(path)
        result = (record or {}).get("result")
        request_id = str(result.get("request_id") or "") if isinstance(result, dict) else ""
        if request_id and (record := read_result(state_dir, request_id)) is not None:
            results.append(record)
            if len(results) >= limit:
                break
    return results


def acknowledge_result(state_dir: str, request_id: str) -> None:
    if read_result(state_dir, request_id) is None:
        return
    # Keep a small tombstone so a late Android start Intent cannot rerun this ID.
    _write(_path(state_dir, request_id, "ack"), {"request_id": request_id})
    for suffix in ("request.json", "result.json"):
        with contextlib.suppress(OSError):
            _path(state_dir, request_id, suffix).unlink()


def active_job(state_dir: str) -> DownloadJob | None:
    job = job_from_request(_read(Path(state_dir) / ".active_download.json"))
    if job and not is_acknowledged(state_dir, job.request_id) and read_result(state_dir, job.request_id) is None:
        return job
    return None
