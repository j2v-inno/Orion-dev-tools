#!/usr/bin/env python3
"""
CAP-style registration workaround.

For every file in a directory:
  1. Resolve the CAP task uid via GET /get-all-tasks (first task containing "CAP", case-sensitive)
  2. POST /register-job-batch-file (start_task=true) -> file_id + S3 upload URL
  3. PUT the file bytes to file_output_upload_url
  4. POST /update-file-status (I -> C) with meta_data

Usage:
  python cap_register.py <directory> [--project-code META] [--workflow-code meta-workflow]

Config via env vars:
  ORION_API_BASE_URL   e.g. https://timetrackerdashboard.innodata.com/be/api
  ORION_API_TOKEN      Sanctum token: {id}|{secret}
"""

import argparse
import os
import random
import sys
import uuid
from pathlib import Path

import requests

TIMEOUT = 60
LEVELS = ["Grad", "UnderGrad", "PhD"]


def api_headers(token: str) -> dict:
    return {
        "api-token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def get_cap_task_uid(base_url: str, token: str, project_code: str, workflow_code: str) -> str:
    resp = requests.get(
        f"{base_url}/get-all-tasks",
        headers=api_headers(token),
        params={"project_code": project_code, "workflow_code": workflow_code},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    tasks = body.get("data") or []
    for task in tasks:
        # Case-sensitive "%CAP%" match on code first, then name
        if "CAP" in str(task.get("code", "")) or "CAP" in str(task.get("name", "")):
            return task["task_uid"]
    raise RuntimeError('No task containing "CAP" (case-sensitive) found in get-all-tasks')


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
        "file_meta_data": {"source": "cap_register_script"},
    }
    resp = requests.post(
        f"{base_url}/register-job-batch-file",
        headers=api_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if resp.status_code == 409:
        raise RuntimeError(f"duplicate file_unique_identifier for {file_path.name}")
    if resp.status_code >= 400:
        raise RuntimeError(f"register failed {resp.status_code}: {resp.text[:500]}")
    data = resp.json().get("data") or {}
    if not data.get("file_output_upload_url"):
        # Fall back to the first entry in output_paths if present
        for entry in data.get("output_paths") or []:
            if entry.get("file_output_upload_url"):
                data["file_output_upload_url"] = entry["file_output_upload_url"]
                break
    if not data.get("file_output_upload_url"):
        raise RuntimeError(f"no file_output_upload_url returned for {file_path.name}; response data: {data}")
    return data


def upload_to_s3(upload_url: str, file_path: Path) -> None:
    with file_path.open("rb") as fh:
        resp = requests.put(upload_url, data=fh, headers={"Content-Type": "application/octet-stream"}, timeout=TIMEOUT * 5)
    if resp.status_code >= 400:
        raise RuntimeError(f"S3 upload failed {resp.status_code}: {resp.text[:200]}")


def complete_file(base_url, token, project_code, task_uid, file_id: int, file_path: Path) -> None:
    payload = {
        "project_code": project_code,
        "task_uid": task_uid,
        "file_id": file_id,
        "previous_file_status": "I",
        "file_status": "C",
        "meta_data": {
            "source": "cap_register_script",
            "file_name": file_path.name,
            "file_size": file_path.stat().st_size,
            "level": random.choice(LEVELS),
        },
    }
    resp = requests.post(
        f"{base_url}/update-file-status",
        headers=api_headers(token),
        json=payload,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"update-file-status failed {resp.status_code}: {resp.text[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CAP-style file registration workaround")
    parser.add_argument("directory", help="Directory containing files to register")
    parser.add_argument("--project-code", default=os.getenv("ORION_PROJECT_CODE", "META"))
    parser.add_argument("--workflow-code", default=os.getenv("ORION_WORKFLOW_CODE", "meta-workflow"))
    parser.add_argument("--job-id", default=None, help="job_unique_identifier (default: generated)")
    args = parser.parse_args()

    base_url = os.getenv("ORION_API_BASE_URL", "").rstrip("/")
    token = os.getenv("ORION_API_TOKEN", "")
    if not base_url or not token:
        print("ERROR: set ORION_API_BASE_URL and ORION_API_TOKEN env vars", file=sys.stderr)
        return 2

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"ERROR: directory does not exist: {directory}", file=sys.stderr)
        return 2

    files = sorted(p for p in directory.iterdir() if p.is_file())
    if not files:
        print(f"No files found in {directory}")
        return 0

    job_uid = args.job_id or f"CAP-RUN-{uuid.uuid4().hex[:8]}"
    print(f"Job: {job_uid} | {len(files)} file(s)")

    task_uid = get_cap_task_uid(base_url, token, args.project_code, args.workflow_code)
    print(f"CAP task_uid: {task_uid}")

    failures = 0
    for file_path in files:
        try:
            print(f"\n[{file_path.name}] registering...")
            data = register_file(base_url, token, args.project_code, args.workflow_code, task_uid, job_uid, file_path)
            file_id = data["file_id"]
            print(f"  file_id={file_id} uploading...")
            upload_to_s3(data["file_output_upload_url"], file_path)
            print("  completing (I -> C)...")
            complete_file(base_url, token, args.project_code, task_uid, file_id, file_path)
            print(f"  OK file_id={file_id}")
        except Exception as exc:
            failures += 1
            print(f"  FAILED {file_path.name}: {exc}", file=sys.stderr)

    print(f"\nDone. {len(files) - failures} succeeded, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
