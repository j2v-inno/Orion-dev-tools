# Orion-dev-tools

Orion tools for supporting integration teams.

## Orion Registration Tray Service

A system-tray app that registers files (PDFs + metadata CSV) to the Orion backend.

## Setup

```bat
pip install -r requirements.txt
```

## Run

```bat
python tray_app.py
```

or double-click `run_tray.bat`. On first launch the Settings window opens; fill it in and Save.
Enabling **Start with Windows** adds the app to the HKCU Run key so it boots with the laptop (uses `pythonw.exe`, no console window).

## Settings

- **Instance** – one entry per site. Pick from the dropdown, **Add** to create, **Delete** to remove.
  Each instance holds its own folder, token, project code, workflow code and base URL.
- **Files folder** – folder containing the CSV + the files it describes
- **API token** – masked input (Sanctum `{id}|{secret}`), stored in `%APPDATA%\OrionRegService\config.json`
- **Project code / Workflow code / Base API URL**

The tray menu has a **Site** submenu to switch the active instance; Register and
Simulate BEP Completion both run against it (window title shows which site).
An older single-site `config.json` is migrated on first load into an instance named `default`.

## Folder layout

The watched folder must contain exactly one CSV and the files it references.
Example with 4 files + 1 CSV:

```
files/
  metadata.csv
  Finance_Act_2024.pdf
  ...
```

CSV columns (one row per PDF):

| pdf_filename | pdf_type | file_size | page_count | retrieval_date | language | jurisdiction | source_website_url | legislation_number | legislation_year | issue_number | publication_date |

Every row's `pdf_filename` must match a file in the folder. On Register, each row's file
is registered via `register-job-batch-file`, uploaded to S3, then completed with
`update-file-status` (I → C) carrying that row's metadata.

## Simulate BEP Completion

Fakes a backend process working through the **BEP** task: takes N files, marks them `I → C`
without doing real work. Register unchecked → claims whatever is waiting at BEP
(`get-task-next-file-with-start`). Register checked → registers + uploads files from the
instance folder first. Optional static `key = value` metadata is added on completion.
