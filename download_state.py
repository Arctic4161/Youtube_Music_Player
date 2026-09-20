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


def _terminal_record(record: object, request_id: str) -> dict | None:
    if not isinstance(record, dict):
        return None
    try:
        job = job_from_request(record.get("request"))
    except (OSError, TypeError, ValueError):
        return None
    result = record.get("result")
    if not job or job.request_id != request_id or not isinstance(result, dict):
        return None
    if (result.get("request_id") != request_id
            or not isinstance(result.get("status"), str) or result.get("status") not in {
        "success", "error", "cancelled",
    }):
        return None
    return {"request": record["request"], "result": result}


def read_result(state_dir: str, request_id: str) -> dict | None:
    if is_acknowledged(state_dir, request_id):
        return None
    return _terminal_record(_read(_path(state_dir, request_id, "result.json")), request_id)


def terminal_result(state_dir: str, request_id: str) -> dict | None:
    """Read the terminal outcome even after the GUI committed it to its Library."""
    suffix = "ack" if is_acknowledged(state_dir, request_id) else "result.json"
    return _terminal_record(_read(_path(state_dir, request_id, suffix)), request_id)


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
    record = read_result(state_dir, request_id)
    if record is None:
        return
    # Retain the outcome for a separately armed Play request before removing
    # the GUI inbox files. Legacy tombstones still suppress download replay.
    _write(_path(state_dir, request_id, "ack"), {"request_id": request_id, **record})
    for suffix in ("request.json", "result.json"):
        with contextlib.suppress(OSError):
            _path(state_dir, request_id, suffix).unlink()


def active_job(state_dir: str) -> DownloadJob | None:
    job = job_from_request(_read(Path(state_dir) / ".active_download.json"))
    if job and not is_acknowledged(state_dir, job.request_id) and read_result(state_dir, job.request_id) is None:
        return job
    return None



def _create_once(path: Path, value: dict) -> bool:
    """Claim one request exclusively; a failed write leaves a conservative claim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output = path.open("x", encoding="utf-8")
    except FileExistsError:
        return False
    with output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    return True


def _playback_record(request_id: str, command: object) -> dict | None:
    if not isinstance(request_id, str) or not request_id.strip() or not isinstance(command, str):
        return None
    try:
        envelope = json.loads(command)
        if not isinstance(envelope, dict) or envelope.get("command") != "play_download":
            return None
        client_id, sequence = envelope.get("client_id"), envelope.get("sequence")
        if not isinstance(client_id, str) or not client_id.strip():
            return None
        if type(sequence) is not int or sequence < 1:
            return None
        value = json.loads(envelope.get("value"))
        if not isinstance(value, dict):
            return None
        request, playlist = value.get("request"), value.get("playlist")
        if not isinstance(request, dict) or not isinstance(request.get("audio_path"), str):
            return None
        if not request["audio_path"].strip():
            return None
        job = job_from_request(request)
        if job is None or job.request_id != request_id:
            return None
        if not isinstance(playlist, list) or any(
            not isinstance(name, str) or not name.strip() or name in {".", ".."}
            or "/" in name or "\\" in name or ":" in name or "\x00" in name
            for name in playlist
        ):
            return None
    except (OSError, TypeError, ValueError):
        return None
    return {"request_id": request_id, "command": command}


def arm_playback(state_dir: str, request_id: str, command: str) -> None:
    """Persist the exact ordered Play command once, independently of GUI ACK."""
    record = _playback_record(request_id, command)
    if record is None:
        raise ValueError("Invalid durable download playback command")
    path = _path(state_dir, request_id, "playback.json")
    if not _create_once(path, record) and _read(path) != record:
        raise ValueError("A different playback command already owns this request")


def playback_request(state_dir: str, request_id: str) -> dict | None:
    record = _read(_path(state_dir, request_id, "playback.json"))
    if not record or record.get("request_id") != request_id:
        return None
    return _playback_record(request_id, record.get("command"))


def playback_decision(state_dir: str, request_id: str) -> str | None:
    record = _read(_path(state_dir, request_id, "playback-decision.json"))
    if record and record.get("request_id") == request_id:
        outcome = record.get("outcome")
        if isinstance(outcome, str) and outcome in {"started", "cancelled", "failed"}:
            return outcome
    return None


def decide_playback(state_dir: str, request_id: str, outcome: str) -> bool:
    """Only the first service claim or GUI cancellation may decide this request."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("Invalid playback request ID")
    if not isinstance(outcome, str) or outcome not in {"started", "cancelled", "failed"}:
        raise ValueError("Invalid playback decision")
    return _create_once(_path(state_dir, request_id, "playback-decision.json"), {
        "request_id": request_id, "outcome": outcome,
    })


def pending_playbacks(state_dir: str, *, limit: int = 8) -> list[dict]:
    """Return bounded replayable Play commands with no existing decision claim."""
    if limit <= 0:
        return []
    pending = []
    directory = Path(state_dir) / ".download-results"
    paths = sorted(directory.glob("*.playback.json"),
                   key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in paths:
        record = _read(path)
        request_id = (record or {}).get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        # An in-progress or interrupted exclusive write also owns the decision.
        # Never replay it while its JSON is temporarily absent or malformed.
        if _path(state_dir, request_id, "playback-decision.json").exists():
            continue
        if (record := playback_request(state_dir, request_id)) is not None:
            pending.append(record)
            if len(pending) >= limit:
                break
    return pending
