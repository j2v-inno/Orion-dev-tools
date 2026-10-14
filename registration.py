#!/usr/bin/env python3
"""
Core registration logic.

Reads the metadata CSV found in the watched folder, then for every row:
  1. resolve the CAP task uid via GET /get-all-tasks
  2. POST /register-job-batch-file (start_task=true) -> file_id + S3 upload URL
  3. PUT the file bytes to file_output_upload_url
  4. POST /update-file-status (I -> C) with the row's metadata

Expected CSV columns:
  pdf_filename, pdf_type, file_size, page_count, retrieval_date, language,
  jurisdiction, source_website_url, legislation_number, legislation_year,
  issue_number, publication_date
"""

import csv
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import requests

TIMEOUT = 60

API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _headers(token: str) -> dict:
    return {**API_HEADERS, "api-token": token}


def get_task_uid(base_url: str, token: str, project_code: str, workflow_code: str, marker: str) -> str:
    resp = requests.get(
        f"{base_url}/get-all-tasks",
        headers=_headers(token),
        params={"project_code": project_code, "workflow_code": workflow_code},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"get-all-tasks failed {resp.status_code}: {resp.text[:1000]}")
    for task in resp.json().get("data") or []:
        if marker in str(task.get("code", "")) or marker in str(task.get("name", "")):
            return task["task_uid"]
    raise RuntimeError(f'No task containing "{marker}" (case-sensitive) found in get-all-tasks')


def get_cap_task_uid(base_url: str, token: str, project_code: str, workflow_code: str) -> str:
    return get_task_uid(base_url, token, project_code, workflow_code, "CAP")


def register_file(base_url, token, project_code, workflow_code, task_uid, job_uid, file_path: Path) -> dict:
    payload = {
        "project_code": project_code,
        "workflow_code": workflow_code,
        "first_task_uid": task_uid,
        "job_unique_identifier": job_uid,
        "file_name": file_path.name,
        "file_path": f"cap/{file_path.name}",
        "file_unique_identifier": f"CAP-{file_path.stem}-{uuid.uuid4().hex[:8]}",
        "start_task": True,
        "file_meta_data": {"source": "orion_reg_service"},
    }
    resp = requests.post(
        f"{base_url}/register-job-batch-file",
        headers=_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if resp.status_code == 409:
        raise RuntimeError(f"duplicate file_unique_identifier for {file_path.name}")
    if resp.status_code >= 400:
        raise RuntimeError(f"register failed {resp.status_code}: {resp.text[:500]}")
    data = resp.json().get("data") or {}
    if not data.get("file_output_upload_url"):
        for entry in data.get("output_paths") or []:
            if entry.get("file_output_upload_url"):
                data["file_output_upload_url"] = entry["file_output_upload_url"]
                break
    if not data.get("file_output_upload_url"):
        raise RuntimeError(f"no file_output_upload_url returned for {file_path.name}")
    return data


def claim_next_file(base_url, token, project_code, task_uid, meta_data: dict | None = None) -> dict:
    """POST /get-task-next-file-with-start -> claims one Y file (Y -> I)."""
    payload = {"project_code": project_code, "task_uid": task_uid}
    if meta_data:
        payload["meta_data"] = meta_data
    resp = requests.post(
        f"{base_url}/get-task-next-file-with-start",
        headers=_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"claim failed {resp.status_code}: {resp.text[:500]}")
    return (resp.json().get("data") or {}).get("file") or {}


def parse_metadata(text: str) -> dict:
    """Parse `key = value` lines; blank lines and # comments ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def upload_to_s3(upload_url: str, file_path: Path) -> None:
    with file_path.open("rb") as fh:
        resp = requests.put(
            upload_url,
            data=fh,
            headers={"Content-Type": "application/octet-stream"},
            timeout=TIMEOUT * 5,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"S3 upload failed {resp.status_code}: {resp.text[:200]}")


def complete_file(base_url, token, project_code, task_uid, file_id: int, meta_data: dict) -> None:
    payload = {
        "project_code": project_code,
        "task_uid": task_uid,
        "file_id": file_id,
        "previous_file_status": "I",
        "file_status": "C",
        "meta_data": {"source": "orion_reg_service", **meta_data},
    }
    resp = requests.post(
        f"{base_url}/update-file-status",
        headers=_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"update-file-status failed {resp.status_code}: {resp.text[:500]}")


def _norm_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def move_to_registered(path: Path, run_folder: Path) -> Path:
    run_folder.mkdir(parents=True, exist_ok=True)
    dest = run_folder / path.name
    if dest.exists():
        dest = run_folder / f"{path.stem}_{uuid.uuid4().hex[:6]}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


def find_csv(folder: Path) -> Path:
    csvs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    if not csvs:
        raise RuntimeError(f"No CSV metadata file found in {folder}")
    return csvs[0]


def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RuntimeError(f"CSV has no header row: {csv_path}")
        rows = []
        for raw in reader:
            row = {_norm_key(k): (v or "").strip() for k, v in raw.items() if k}
            rows.append(row)
        return rows


def run_registration(folder: Path, base_url: str, token: str, project_code: str,
                     workflow_code: str, log=print, job_id: str | None = None) -> int:
    """Returns number of failed files."""
    folder = Path(folder)
    if not folder.is_dir():
        raise RuntimeError(f"Folder does not exist: {folder}")

    csv_path = find_csv(folder)
    rows = load_rows(csv_path)
    log(f"Site: {base_url} | token: {len(token)} chars")
    log(f"Metadata CSV: {csv_path.name} ({len(rows)} row(s))")

    if not rows:
        log("CSV has no data rows.")
        return 0

    job_uid = job_id or f"CAP-RUN-{uuid.uuid4().hex[:8]}"
    log(f"Job: {job_uid}")

    task_uid = get_cap_task_uid(base_url, token, project_code, workflow_code)
    log(f"CAP task_uid: {task_uid}")

    files_in_folder = {p.name: p for p in folder.iterdir() if p.is_file()}
    failures = 0
    matched = set()
    run_folder = folder / "registered" / datetime.now().strftime("%Y-%m-%d %H-%M-%S")

    for row in rows:
        name = row.get("pdf_filename") or row.get("filename") or ""
        file_path = files_in_folder.get(name)
        if file_path is None:
            # case-insensitive fallback
            lowered = {p.name.lower(): p for p in files_in_folder.values()}
            file_path = lowered.get(name.lower())
        if file_path is None:
            log(f"SKIP: file not found for row '{name}'")
            failures += 1
            continue

        matched.add(file_path.name)
        meta_data = {k: v for k, v in row.items() if k not in ("pdf_filename", "filename")}
        meta_data["file_name"] = file_path.name
        meta_data["file_size_bytes"] = file_path.stat().st_size

        try:
            log(f"[{file_path.name}] registering...")
            data = register_file(base_url, token, project_code, workflow_code, task_uid, job_uid, file_path)
            file_id = data["file_id"]
            log(f"  file_id={file_id}, uploading to S3...")
            upload_to_s3(data["file_output_upload_url"], file_path)
            log("  completing (I -> C) with metadata...")
            complete_file(base_url, token, project_code, task_uid, file_id, meta_data)
            log(f"  OK file_id={file_id}")
            moved = move_to_registered(file_path, run_folder)
            log(f"  moved to {moved}")
        except Exception as exc:
            failures += 1
            log(f"  FAILED {file_path.name}: {exc}")

    unmatched = {p.name for p in files_in_folder.values()} - matched - {csv_path.name}
    for name in sorted(unmatched):
        log(f"WARNING: file in folder has no CSV row: {name}")

    if failures == 0:
        moved_csv = move_to_registered(csv_path, run_folder)
        log(f"CSV moved to {moved_csv}")
    else:
        log("CSV left in place because some files failed.")

    log(f"Done. {len(rows) - failures} succeeded, {failures} failed.")
    return failures


def run_simulate(base_url, token, project_code, workflow_code, count: int,
                 register: bool, meta_data: dict, folder: Path | None = None,
                 log=print) -> int:
    """Complete `count` BEP-task files (I -> C) without doing real work.

    register=True  -> register + upload files from `folder` first, then complete.
    register=False -> claim whatever is waiting at the task, then complete.
    Returns number of failures.
    """
    task_uid = get_task_uid(base_url, token, project_code, workflow_code, "BEP")
    log(f"Site: {base_url} | token: {len(token)} chars")
    log(f"BEP task_uid: {task_uid}")

    job_uid = f"CAP-RUN-{uuid.uuid4().hex[:8]}"
    pending = None
    if register:
        if folder is None or not Path(folder).is_dir():
            raise RuntimeError(f"Folder does not exist: {folder}")
        pending = sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() != ".csv")
        if len(pending) < count:
            raise RuntimeError(f"Need {count} file(s) to register, only {len(pending)} found in {folder}")
        log(f"Job: {job_uid} | registering {count} file(s)")

    failures = 0
    for i in range(count):
        try:
            if register:
                path = pending[i]
                log(f"[{i + 1}/{count}] {path.name} registering...")
                data = register_file(base_url, token, project_code, workflow_code, task_uid, job_uid, path)
                upload_to_s3(data["file_output_upload_url"], path)
            else:
                log(f"[{i + 1}/{count}] claiming...")
                data = claim_next_file(base_url, token, project_code, task_uid)
                path = Path(str(data.get("file_name") or data.get("id")))
                log(f"  claimed file_id={data.get('id')} {path.name}")

            extra = {**meta_data, "file_name": path.name}
            if register:
                extra["file_size_bytes"] = pending[i].stat().st_size
            log(f"  completing (I -> C) for file_id={data['id']}...")
            complete_file(base_url, token, project_code, task_uid, data["id"], extra)
            log("  OK")
        except Exception as exc:
            failures += 1
            log(f"  FAILED #{i + 1}: {exc}")

    log(f"Done. {count - failures} succeeded, {failures} failed.")
    return failures


if __name__ == "__main__":
    # smallest check that breaks if the metadata parser breaks
    assert parse_metadata("a=1\n\n# skip\nb = two \nnope\nc=3=4") == {"a": "1", "b": "two", "c": "3=4"}
    print("ok")
