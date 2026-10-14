#!/usr/bin/env python3
"""Orion registration tray service."""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray
from PIL import Image, ImageDraw

import registration

APP_NAME = "OrionRegService"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "OrionRegService"
CONFIG_PATH = CONFIG_DIR / "config.json"

INSTANCE_FIELDS = ("folder", "api_token", "project_code", "workflow_code", "base_url")

DEFAULT_INSTANCE = {
    "folder": "",
    "api_token": "",
    "project_code": "META",
    "workflow_code": "meta-workflow",
    "base_url": "",
}

config = {
    "active": "default",
    "instances": {"default": dict(DEFAULT_INSTANCE)},
    "start_with_windows": True,
}
running = False
icon: pystray.Icon | None = None
root: tk.Tk | None = None


LOG_PATH = CONFIG_DIR / "registration.log"


def log_to_file(msg: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _migrate(old: dict) -> dict:
    """Pre-instances flat config -> instances dict, first run only."""
    flat = {k: old[k] for k in INSTANCE_FIELDS if k in old}
    name = old.get("active") or "default"
    inst = dict(DEFAULT_INSTANCE, **flat)
    return {
        "active": name,
        "instances": {name: inst},
        "start_with_windows": old.get("start_with_windows", True),
    }


def load_config() -> None:
    global config
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data.get("instances"), dict) or not data["instances"]:
        data = _migrate(data)
    for key, value in data.items():
        if key == "instances":
            config["instances"] = {
                str(n): dict(DEFAULT_INSTANCE, **{k: i.get(k, "") for k in INSTANCE_FIELDS})
                for n, i in value.items()
            }
        else:
            config[key] = value
    if config["active"] not in config["instances"]:
        config["active"] = next(iter(config["instances"]))


def instance() -> dict:
    """The currently selected site."""
    return config["instances"][config["active"]]


def save_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def set_startup(enabled: bool) -> None:
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    with key:
        if enabled:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}"'
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                exe = pythonw if pythonw.exists() else Path(sys.executable)
                command = f'"{exe}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def _resource(*parts: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base.joinpath(*parts))


def make_icon_image() -> Image.Image:
    return Image.open(_resource("assets", "logo.png")).convert("RGBA")


def show_settings() -> None:
    win = tk.Toplevel(root)
    win.title("Orion Reg Service - Settings")
    try:
        win.iconbitmap(_resource("assets", "logo.ico"))
    except Exception:
        pass
    win.resizable(False, False)
    pad = {"padx": 8, "pady": 4}

    picked = tk.StringVar(value=config["active"])
    folder_var = tk.StringVar(value=instance()["folder"])
    token_var = tk.StringVar(value=instance()["api_token"])
    project_var = tk.StringVar(value=instance()["project_code"])
    workflow_var = tk.StringVar(value=instance()["workflow_code"])
    baseurl_var = tk.StringVar(value=instance()["base_url"])
    startup_var = tk.BooleanVar(value=config["start_with_windows"])

    def load(name: str) -> None:
        inst = config["instances"].get(name) or dict(DEFAULT_INSTANCE)
        folder_var.set(inst["folder"])
        token_var.set(inst["api_token"])
        project_var.set(inst["project_code"])
        workflow_var.set(inst["workflow_code"])
        baseurl_var.set(inst["base_url"])

    def browse():
        path = filedialog.askdirectory(title="Select files folder")
        if path:
            folder_var.set(path)

    def add_instance():
        name = _ask_name(win)
        if not name:
            return
        if name in config["instances"]:
            messagebox.showwarning("Exists", f'Instance "{name}" already exists.', parent=win)
            return
        config["instances"][name] = dict(DEFAULT_INSTANCE)
        picked.set(name)
        load(name)

    def delete_instance():
        if len(config["instances"]) <= 1:
            messagebox.showwarning("Keep one", "At least one instance is required.", parent=win)
            return
        name = picked.get()
        if not messagebox.askyesno("Delete", f'Delete instance "{name}"?', parent=win):
            return
        del config["instances"][name]
        if config["active"] == name:
            config["active"] = next(iter(config["instances"]))
        picked.set(config["active"])
        load(config["active"])

    row = 0
    tk.Label(win, text="Instance:").grid(row=row, column=0, sticky="e", **pad)
    combo = ttk.Combobox(win, textvariable=picked, values=list(config["instances"]), width=30, state="readonly")
    combo.grid(row=row, column=1, sticky="w", **pad)
    combo.bind("<<ComboboxSelected>>", lambda _e: load(picked.get()))
    tk.Button(win, text="Add", command=add_instance).grid(row=row, column=2, sticky="w", **pad)
    tk.Button(win, text="Delete", command=delete_instance).grid(row=row, column=3, sticky="w", **pad)

    row += 1
    tk.Label(win, text="Files folder:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=folder_var, width=50).grid(row=row, column=1, columnspan=3, **pad)
    tk.Button(win, text="Browse...", command=browse).grid(row=row, column=4, **pad)

    row += 1
    tk.Label(win, text="API token:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=token_var, width=50, show="*").grid(row=row, column=1, columnspan=3, sticky="we", **pad)

    row += 1
    tk.Label(win, text="Project code:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=project_var, width=50).grid(row=row, column=1, columnspan=3, sticky="we", **pad)

    row += 1
    tk.Label(win, text="Workflow code:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=workflow_var, width=50).grid(row=row, column=1, columnspan=3, sticky="we", **pad)

    row += 1
    tk.Label(win, text="Base API URL:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=baseurl_var, width=50).grid(row=row, column=1, columnspan=3, sticky="we", **pad)

    row += 1
    tk.Checkbutton(win, text="Start with Windows", variable=startup_var).grid(row=row, column=1, sticky="w", **pad)

    def save():
        name = picked.get().strip()
        if not name:
            messagebox.showwarning("Missing info", "Instance name is required.", parent=win)
            return
        inst = config["instances"].get(name, dict(DEFAULT_INSTANCE))
        inst["folder"] = folder_var.get().strip()
        inst["api_token"] = token_var.get().strip()
        inst["project_code"] = project_var.get().strip()
        inst["workflow_code"] = workflow_var.get().strip()
        inst["base_url"] = baseurl_var.get().strip().rstrip("/")
        if not inst["folder"] or not inst["api_token"] or not inst["base_url"]:
            messagebox.showwarning("Missing info", "Folder, API token and Base API URL are required.", parent=win)
            return
        config["instances"][name] = inst
        config["active"] = name
        config["start_with_windows"] = bool(startup_var.get())
        save_config()
        try:
            set_startup(config["start_with_windows"])
        except Exception as exc:
            messagebox.showwarning("Startup", f"Could not update startup entry: {exc}", parent=win)
        win.destroy()

    row += 1
    tk.Button(win, text="Save", width=12, command=save).grid(row=row, column=1, sticky="e", **pad)

    win.grab_set()
    win.wait_window()


def _ask_name(parent) -> str:
    from tkinter import simpledialog
    return (simpledialog.askstring("New instance", "Instance name:", parent=parent) or "").strip()


def _require_config() -> dict | None:
    inst = instance()
    if not inst["folder"] or not inst["api_token"] or not inst["base_url"]:
        messagebox.showwarning(
            "Not configured",
            f'Instance "{config["active"]}" is missing folder, API token or Base API URL. Open Settings first.',
        )
        return None
    return inst


def show_register_progress() -> None:
    inst = _require_config()
    if inst is None:
        return

    def work(log):
        failures = registration.run_registration(
            Path(inst["folder"]),
            inst["base_url"],
            inst["api_token"],
            inst["project_code"],
            inst["workflow_code"],
            log=log,
        )
        log("FAILED" if failures else "SUCCESS")

    run_with_progress(f'Registration progress [{config["active"]}]', work)


def show_simulate_dialog() -> None:
    inst = _require_config()
    if inst is None:
        return

    win = tk.Toplevel(root)
    win.title("Simulate BEP completion")
    try:
        win.iconbitmap(_resource("assets", "logo.ico"))
    except Exception:
        pass
    win.resizable(False, False)
    pad = {"padx": 8, "pady": 4}

    count_var = tk.StringVar(value="1")
    register_var = tk.BooleanVar(value=True)
    add_meta_var = tk.BooleanVar(value=False)
    meta_var = tk.StringVar(value="review_result=approved\nlevel=Grad")

    row = 0
    tk.Label(win, text="Number of files:").grid(row=row, column=0, sticky="e", **pad)
    tk.Entry(win, textvariable=count_var, width=10).grid(row=row, column=1, sticky="w", **pad)

    row += 1
    tk.Checkbutton(win, text="Register new files first (unchecked = complete files already waiting)",
                   variable=register_var).grid(row=row, column=0, columnspan=2, sticky="w", **pad)

    row += 1
    tk.Checkbutton(win, text="Add metadata on completion (same for all files)",
                   variable=add_meta_var, command=lambda: meta_box.configure(state="normal" if add_meta_var.get() else "disabled")
                   ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)

    row += 1
    meta_box = tk.Text(win, width=50, height=6, wrap="none")
    meta_box.grid(row=row, column=0, columnspan=2, sticky="we", **pad)
    meta_box.insert("1.0", meta_var.get())
    meta_box.configure(state="disabled")

    def start():
        try:
            count = int(count_var.get().strip())
        except ValueError:
            messagebox.showwarning("Bad input", "Number of files must be a whole number.", parent=win)
            return
        if count < 1:
            messagebox.showwarning("Bad input", "Number of files must be at least 1.", parent=win)
            return
        meta = registration.parse_metadata(meta_box.get("1.0", "end")) if add_meta_var.get() else {}
        register = bool(register_var.get())
        site = config["active"]
        win.destroy()

        def work(log):
            failures = registration.run_simulate(
                inst["base_url"],
                inst["api_token"],
                inst["project_code"],
                inst["workflow_code"],
                count,
                register,
                meta,
                folder=Path(inst["folder"]),
                log=log,
            )
            log("FAILED" if failures else "SUCCESS")

        run_with_progress(f"Simulate BEP completion [{site}]", work)

    row += 1
    tk.Button(win, text="Start", width=12, command=start).grid(row=row, column=1, sticky="e", **pad)
    win.grab_set()
    win.wait_window()


def run_with_progress(title: str, work) -> None:
    """Open a log window and run `work(log)` on a background thread."""
    global running
    if running:
        messagebox.showinfo("Busy", "Already running.")
        return

    win = tk.Toplevel(root)
    win.title(title)
    try:
        win.iconbitmap(_resource("assets", "logo.ico"))
    except Exception:
        pass
    win.geometry("700x420")
    txt = tk.Text(win, wrap="word", state="disabled")
    txt.pack(fill="both", expand=True, padx=6, pady=6)

    def log(msg: str) -> None:
        log_to_file(msg)
        def append():
            txt.configure(state="normal")
            txt.insert("end", msg + "\n")
            txt.see("end")
            txt.configure(state="disabled")
        root.after(0, append)

    def runner():
        global running
        running = True
        try:
            work(log)
        except Exception as exc:
            log(f"ERROR: {exc}")
        finally:
            running = False

    threading.Thread(target=runner, daemon=True).start()


def exit_app(icon_obj=None, item=None) -> None:
    try:
        icon.stop()
    finally:
        root.after(0, root.destroy)


def switch_instance(name: str, icon_obj=None, item=None) -> None:
    config["active"] = name
    save_config()
    try:
        icon.update_menu()
    except Exception:
        pass


def main() -> None:
    global icon, root
    load_config()

    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(_resource("assets", "logo.ico"))
    except Exception:
        pass

    menu = pystray.Menu(
        pystray.MenuItem("Settings", lambda: root.after(0, show_settings)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Site",
            pystray.Menu(
                *(
                    pystray.MenuItem(
                        name,
                        partial(switch_instance, name),
                        checked=lambda item=None, n=name: n == config["active"],
                        radio=True,
                    )
                    for name in config["instances"]
                ),
            ),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Register", lambda: root.after(0, show_register_progress)),
        pystray.MenuItem("Simulate BEP Completion", lambda: root.after(0, show_simulate_dialog)),
        pystray.MenuItem("Exit", exit_app),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, menu)
    icon.run_detached()

    if len(config["instances"]) == 1 and not instance()["base_url"]:
        root.after(300, show_settings)

    root.mainloop()


if __name__ == "__main__":
    # smallest check that breaks if config loading/migration breaks
    old = {
        "folder": "C:/files", "api_token": "1|x", "project_code": "META",
        "workflow_code": "meta-workflow", "base_url": "https://be/api/", "start_with_windows": False,
    }
    migrated = _migrate(old)
    assert migrated["active"] == "default"
    assert migrated["start_with_windows"] is False
    assert migrated["instances"]["default"]["base_url"] == "https://be/api/"
    assert set(INSTANCE_FIELDS) <= set(DEFAULT_INSTANCE)
    print("ok")
    main()
