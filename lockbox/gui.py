import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .engine import (
    MIN_PASSWORD, decrypt_path, encrypt_path, generate_password, human_size, is_locked, strength,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    Root = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    Root = tk.Tk

DARK = {
    "BG": "#101319", "PANEL": "#171b23", "PANEL_2": "#202531", "BORDER": "#2b3140",
    "TEXT": "#e8eaf0", "MUTED": "#8b93a5", "ACCENT": "#2dd4bf", "ACCENT_HOVER": "#5eead4",
    "ACCENT_TEXT": "#04211d", "GOOD": "#4ade80", "WARN": "#fbbf24", "BAD": "#f87171",
    "SELECT": "#2a3347",
}
LIGHT = {
    "BG": "#f3f5f9", "PANEL": "#ffffff", "PANEL_2": "#e9edf4", "BORDER": "#d3d9e4",
    "TEXT": "#151a23", "MUTED": "#5b6579", "ACCENT": "#0d9488", "ACCENT_HOVER": "#0f766e",
    "ACCENT_TEXT": "#ffffff", "GOOD": "#15803d", "WARN": "#b45309", "BAD": "#b91c1c",
    "SELECT": "#d9e6fb",
}
BG = PANEL = PANEL_2 = BORDER = TEXT = MUTED = ACCENT = ACCENT_HOVER = ""
ACCENT_TEXT = GOOD = WARN = BAD = SELECT = ""
DARK_MODE = True

FONT = "Segoe UI"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Lockbox"
SETTINGS = APP_DIR / "settings.json"
ASSETS = Path(__file__).parent / "assets"
STRENGTH_NAMES = ["Too weak", "Weak", "Okay", "Good", "Strong"]

TIPS = {
    "hide": "The locked file gets a random name, so nobody can tell what is inside from the file name.",
    "delete": "Deletes the original once the locked copy has been opened and checked.",
    "shred": "Overwrites the original before deleting it. SSDs and backups may still keep older copies.",
    "key": "Optional. Locking also uses the contents of this file, so you need both the password and the file to open it again.",
}


def detect_dark():
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def apply_theme(choice):
    global DARK_MODE
    DARK_MODE = detect_dark() if choice == "auto" else choice == "dark"
    globals().update(DARK if DARK_MODE else LIGHT)


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data))
    except OSError:
        pass


def folder_size(path):
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class Tip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _):
        if self.window:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry("+%d+%d" % (x, y))
        tk.Label(
            self.window, text=self.text, bg=PANEL_2, fg=TEXT, font=(FONT, 9), justify="left",
            wraplength=320, padx=10, pady=6, highlightthickness=1, highlightbackground=BORDER,
        ).pack()

    def hide(self, _):
        if self.window:
            self.window.destroy()
            self.window = None


class App:
    def __init__(self, start_paths=()):
        self.saved = load_settings()
        apply_theme(self.saved.get("theme", "auto"))
        self.prepare_windows()
        self.root = Root()
        self.root.title("Lockbox")
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        width, height = self.saved.get("size", [820, 900])
        self.root.geometry("%dx%d" % (self.px(min(width, 1400)), self.px(min(height, 1200))))
        self.root.minsize(self.px(720), self.px(800))
        self.root.configure(bg=BG)
        self.load_icon()

        self.items = {}
        self.events = queue.Queue()
        self.size_queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.last_folder = None
        self.last_tick = 0.0

        saved = self.saved
        self.password = tk.StringVar()
        self.confirm = tk.StringVar()
        self.reveal = tk.BooleanVar(value=False)
        self.keyfile = tk.StringVar()
        self.mode = tk.StringVar(value=saved.get("mode", "copy"))
        self.folder = tk.StringVar(value=saved.get("folder", ""))
        self.hide_name = tk.BooleanVar(value=saved.get("hide_name", False))
        self.delete_original = tk.BooleanVar(value=False)
        self.shred = tk.BooleanVar(value=False)
        self.theme = tk.StringVar(value=saved.get("theme", "auto"))

        self.style()
        self.build()
        for variable in (self.password, self.confirm, self.keyfile):
            variable.trace_add("write", lambda *_: self.refresh())
        self.mode.trace_add("write", lambda *_: self.mode_changed())
        self.delete_original.trace_add("write", lambda *_: self.mode_changed())
        self.reveal.trace_add("write", lambda *_: self.reveal_changed())
        self.mode_changed()
        self.reveal_changed()

        self.root.bind("<Delete>", self.delete_key)
        self.root.bind("<Control-o>", lambda _: self.add_files())
        self.root.bind("<Escape>", lambda _: self.cancel.set() if self.running else None)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.size_loop, daemon=True).start()
        self.root.after(80, self.poll)
        self.root.update_idletasks()
        self.dark_title_bar()
        if start_paths:
            self.add(start_paths)

    def px(self, value):
        return int(value * getattr(self, "scale", 1.0))

    def prepare_windows(self):
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("lockbox.file.encryptor")
        except Exception:
            pass

    def dark_title_bar(self):
        if sys.platform != "win32":
            return
        try:
            handle = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1 if DARK_MODE else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, 20, ctypes.byref(value), ctypes.sizeof(value))
            self.root.withdraw()
            self.root.deiconify()
        except Exception:
            pass

    def load_icon(self):
        try:
            self.icon = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            self.root.iconphoto(True, self.icon)
            self.small_icon = self.icon.subsample(4, 4)
        except tk.TclError:
            self.icon = None
            self.small_icon = None

    def style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=(FONT, 10), borderwidth=0, focuscolor=BG)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", font=(FONT, 20, "bold"))
        style.configure("Heading.TLabel", font=(FONT, 10, "bold"))

        style.configure("TButton", background=PANEL_2, foreground=TEXT, padding=(self.px(14), self.px(8)), relief="flat")
        style.map("TButton", background=[("active", BORDER), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT, font=(FONT, 10, "bold"))
        style.map(
            "Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", PANEL_2)],
            foreground=[("disabled", MUTED)],
        )
        style.configure("Small.TButton", padding=(self.px(10), self.px(5)))
        style.configure("Link.TButton", background=BG, foreground=MUTED, padding=(self.px(8), self.px(4)))
        style.map("Link.TButton", foreground=[("active", TEXT)], background=[("active", BG)])

        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(
                name, background=PANEL, foreground=TEXT, indicatorbackground=BORDER,
                indicatorforeground=ACCENT_TEXT, upperbordercolor=BORDER, lowerbordercolor=BORDER,
                indicatormargin=(0, 0, 6, 0),
            )
            style.map(
                name,
                background=[("active", PANEL)],
                indicatorbackground=[("selected", ACCENT), ("disabled", PANEL), ("active", MUTED)],
                indicatorforeground=[("selected", ACCENT_TEXT)],
                foreground=[("disabled", MUTED)],
            )

        style.configure(
            "TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, padding=6,
            bordercolor=BORDER, lightcolor=PANEL_2, darkcolor=PANEL_2,
        )
        style.map(
            "TEntry", fieldbackground=[("disabled", PANEL)], foreground=[("disabled", MUTED)],
            bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
        )
        style.configure(
            "Key.TEntry", fieldbackground=PANEL_2, foreground=TEXT, padding=6,
            bordercolor=BORDER, lightcolor=PANEL_2, darkcolor=PANEL_2,
        )
        style.map("Key.TEntry", fieldbackground=[("disabled", PANEL_2)], foreground=[("disabled", TEXT)])

        style.configure(
            "Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
            rowheight=self.px(30), borderwidth=0,
        )
        style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, font=(FONT, 9, "bold"), relief="flat", padding=6)
        style.map("Treeview.Heading", background=[("active", PANEL_2)])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure(
            "Vertical.TScrollbar", background=PANEL_2, troughcolor=PANEL, arrowcolor=MUTED,
            bordercolor=PANEL, lightcolor=PANEL_2, darkcolor=PANEL_2,
        )
        style.map("Vertical.TScrollbar", background=[("active", BORDER)])

    def build(self):
        outer = ttk.Frame(self.root, padding=(self.px(22), self.px(14), self.px(22), self.px(16)))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        if self.small_icon:
            tk.Label(header, image=self.small_icon, bg=BG).pack(side="left", padx=(0, self.px(12)))
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="Lockbox", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Lock files with a password so only you can open them", style="Muted.TLabel").pack(anchor="w")
        self.settings_button = ttk.Button(header, text="Settings", style="Link.TButton", command=self.open_menu)
        self.settings_button.pack(side="right", anchor="n")
        self.build_menu()

        self.zone = tk.Canvas(outer, height=self.px(92), bg=BG, highlightthickness=0)
        self.zone.grid(row=1, column=0, sticky="ew", pady=(self.px(14), self.px(12)))
        self.zone.bind("<Configure>", self.draw_zone)
        self.zone_hot = False
        self.zone_buttons = ttk.Frame(self.zone)
        ttk.Button(self.zone_buttons, text="Browse files", command=self.add_files).pack(side="left")
        ttk.Button(self.zone_buttons, text="Browse folder", command=self.add_folder).pack(side="left", padx=(8, 0))
        if DND_FILES:
            for widget in (self.root, self.zone):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.dropped)
                widget.dnd_bind("<<DropEnter>>", lambda e: self.set_hot(True))
                widget.dnd_bind("<<DropLeave>>", lambda e: self.set_hot(False))

        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, self.px(6)))
        self.count_label = ttk.Label(bar, text="Files", style="Heading.TLabel")
        self.count_label.pack(side="left")
        ttk.Button(bar, text="Clear all", style="Link.TButton", command=self.clear).pack(side="right")
        ttk.Button(bar, text="Remove selected", style="Link.TButton", command=self.remove_selected).pack(side="right")

        listing = ttk.Frame(outer, style="Panel.TFrame")
        listing.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(listing, columns=("type", "size", "status"), show="tree headings", selectmode="extended", height=4)
        for column, label, width in (("#0", "File", 330), ("type", "Type", 90), ("size", "Size", 80), ("status", "Status", 230)):
            self.tree.heading(column, text=label, anchor="w")
            self.tree.column(column, width=self.px(width), anchor="w", stretch=column == "#0")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for tag, color in (("locked", WARN), ("plain", TEXT), ("done", GOOD), ("bad", BAD), ("muted", MUTED)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Button-3>", self.context_menu)
        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL_2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT, bd=0)
        self.menu.add_command(label="Remove from list", command=self.remove_selected)
        self.empty = tk.Label(
            listing, text="Nothing here yet\nAdd files or folders to lock them, or locked files to open them",
            bg=PANEL, fg=MUTED, font=(FONT, 10), justify="center",
        )
        self.empty.place(relx=0.5, rely=0.58, anchor="center")

        secret = ttk.Frame(outer, style="Panel.TFrame", padding=(self.px(16), self.px(12)))
        secret.grid(row=4, column=0, sticky="ew", pady=(self.px(12), 0))
        secret.columnconfigure(1, weight=1)
        ttk.Label(secret, text="Password", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.password_entry = ttk.Entry(secret, textvariable=self.password, show="•")
        self.password_entry.grid(row=0, column=1, sticky="ew", padx=8)
        row_buttons = ttk.Frame(secret, style="Panel.TFrame")
        row_buttons.grid(row=0, column=2)
        ttk.Checkbutton(row_buttons, text="Show", variable=self.reveal).pack(side="left", padx=(0, 8))
        ttk.Button(row_buttons, text="Generate", style="Small.TButton", command=self.generate).pack(side="left")

        self.confirm_label = ttk.Label(secret, text="Repeat it", style="Panel.TLabel")
        self.confirm_entry = ttk.Entry(secret, textvariable=self.confirm, show="•")
        self.meter = tk.Canvas(secret, height=self.px(6), bg=PANEL, highlightthickness=0)
        self.meter.bind("<Configure>", lambda _: self.draw_meter())
        self.hint = ttk.Label(secret, text="", style="PanelMuted.TLabel")

        ttk.Label(secret, text="Key file", style="Panel.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))
        key_entry = ttk.Entry(secret, textvariable=self.keyfile, state="disabled", style="Key.TEntry")
        key_entry.grid(row=4, column=1, sticky="ew", padx=8, pady=(10, 0))
        key_buttons = ttk.Frame(secret, style="Panel.TFrame")
        key_buttons.grid(row=4, column=2, pady=(10, 0))
        ttk.Button(key_buttons, text="Browse", style="Small.TButton", command=self.pick_key).pack(side="left")
        ttk.Button(key_buttons, text="Clear", style="Small.TButton", command=lambda: self.keyfile.set("")).pack(side="left", padx=(6, 0))
        Tip(key_entry, TIPS["key"])
        ttk.Label(
            secret, text="There is no way to get your files back if you forget the password.",
            style="PanelMuted.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        options = ttk.Frame(outer, style="Panel.TFrame", padding=(self.px(16), self.px(12)))
        options.grid(row=5, column=0, sticky="ew", pady=(self.px(12), 0))
        options.columnconfigure(1, weight=1)
        ttk.Radiobutton(options, text="Save next to the original", variable=self.mode, value="copy").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(options, text="Save in a folder:", variable=self.mode, value="folder").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.folder_entry = ttk.Entry(options, textvariable=self.folder)
        self.folder_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 0))
        self.folder_button = ttk.Button(options, text="Browse", style="Small.TButton", command=self.pick_folder)
        self.folder_button.grid(row=1, column=2, pady=(4, 0))
        extras = ttk.Frame(options, style="Panel.TFrame")
        extras.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        hide_box = ttk.Checkbutton(extras, text="Hide the file name", variable=self.hide_name)
        hide_box.grid(row=0, column=0, padx=(0, 18), sticky="w")
        delete_box = ttk.Checkbutton(extras, text="Delete the original", variable=self.delete_original)
        delete_box.grid(row=0, column=1, padx=(0, 18), sticky="w")
        self.shred_box = ttk.Checkbutton(extras, text="Overwrite it first", variable=self.shred)
        self.shred_box.grid(row=0, column=2, sticky="w")
        Tip(hide_box, TIPS["hide"])
        Tip(delete_box, TIPS["delete"])
        Tip(self.shred_box, TIPS["shred"])

        footer = ttk.Frame(outer)
        footer.grid(row=6, column=0, sticky="ew", pady=(self.px(14), 0))
        self.progress = tk.Canvas(footer, height=self.px(6), bg=PANEL_2, highlightthickness=0)
        self.progress.pack(fill="x", pady=(0, self.px(10)))
        self.progress.bind("<Configure>", lambda _: self.draw_progress())
        self.fraction = 0.0
        row = ttk.Frame(footer)
        row.pack(fill="x")
        self.status = ttk.Label(row, text="Add some files to get started.", style="Muted.TLabel")
        self.status.pack(side="left")
        self.unlock_button = ttk.Button(row, text="Unlock", command=lambda: self.start("unlock"), state="disabled")
        self.unlock_button.pack(side="right")
        self.lock_button = ttk.Button(row, text="Lock", style="Accent.TButton", command=lambda: self.start("lock"), state="disabled")
        self.lock_button.pack(side="right", padx=(0, 8))
        self.show_button = ttk.Button(row, text="Show in folder", command=self.show_folder)
        self.cancel_button = ttk.Button(row, text="Cancel", command=self.cancel.set)

    def build_menu(self):
        self.app_menu = tk.Menu(self.root, tearoff=0, bg=PANEL_2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT, bd=0)
        themes = tk.Menu(self.app_menu, tearoff=0, bg=PANEL_2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT, bd=0)
        for label, value in (("Match Windows", "auto"), ("Dark", "dark"), ("Light", "light")):
            themes.add_radiobutton(label=label, value=value, variable=self.theme, command=self.theme_changed)
        self.app_menu.add_cascade(label="Theme", menu=themes)
        self.app_menu.add_separator()
        self.app_menu.add_command(label="About Lockbox", command=self.about)

    def open_menu(self):
        button = self.settings_button
        self.app_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def theme_changed(self):
        self.saved["theme"] = self.theme.get()
        save_settings(self.saved)
        messagebox.showinfo("Lockbox", "The new theme is used the next time you open Lockbox.")

    def about(self):
        text = (
            "Lockbox %s\n\nFiles are encrypted with AES-256-GCM. The key is made from your password "
            "with Argon2id, so guessing it is slow even for a fast computer.\n\n"
            "If you forget the password, the files can't be recovered.\n\nBuilt by Unkn0wngb" % __version__
        )
        messagebox.showinfo("About Lockbox", text)

    def draw_zone(self, _=None):
        canvas = self.zone
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        color = ACCENT if self.zone_hot else BORDER
        canvas.create_rectangle(2, 2, width - 2, height - 2, outline=color, dash=(6, 5), width=2, fill=PANEL_2 if self.zone_hot else PANEL)
        hint = "Drop files or folders here" if DND_FILES else "Add files or folders to begin"
        canvas.create_text(width / 2, height * 0.30, text=hint, fill=TEXT, font=(FONT, 12, "bold"))
        canvas.create_window(width / 2, height * 0.68, window=self.zone_buttons)

    def set_hot(self, value):
        self.zone_hot = value
        self.draw_zone()

    def draw_progress(self):
        self.progress.delete("all")
        if self.fraction > 0:
            width = self.progress.winfo_width() * min(self.fraction, 1.0)
            self.progress.create_rectangle(0, 0, width, self.progress.winfo_height(), fill=ACCENT, width=0)

    def draw_meter(self):
        self.meter.delete("all")
        score = strength(self.password.get())
        width = self.meter.winfo_width()
        height = self.meter.winfo_height()
        gap = self.px(4)
        piece = (width - gap * 3) / 4
        colors = [BAD, BAD, WARN, GOOD, GOOD]
        for index in range(4):
            filled = self.password.get() and index < max(score, 1)
            fill = colors[score] if filled else PANEL_2
            self.meter.create_rectangle(index * (piece + gap), 0, index * (piece + gap) + piece, height, fill=fill, width=0)

    def reveal_changed(self):
        mark = "" if self.reveal.get() else "•"
        self.password_entry.configure(show=mark)
        self.confirm_entry.configure(show=mark)

    def generate(self):
        value = generate_password()
        self.password.set(value)
        self.confirm.set(value)
        self.reveal.set(True)
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.configure(text="New password made and copied. Keep it somewhere safe.", foreground=MUTED)

    def pick_key(self):
        picked = filedialog.askopenfilename(title="Choose a key file")
        if picked:
            self.keyfile.set(picked)

    def pick_folder(self):
        picked = filedialog.askdirectory(title="Choose where to save")
        if picked:
            self.folder.set(picked)

    def dropped(self, event):
        self.set_hot(False)
        self.add(self.root.tk.splitlist(event.data))

    def add(self, entries):
        if self.running:
            self.status.configure(text="Wait for the current run to finish before adding files.", foreground=WARN)
            return
        known = {item["path"] for item in self.items.values()}
        for entry in entries:
            path = Path(entry)
            if path in known or not path.exists():
                continue
            known.add(path)
            folder = path.is_dir()
            locked = not folder and is_locked(path)
            try:
                size = 0 if folder else path.stat().st_size
            except OSError:
                continue
            kind = "Folder" if folder else ("Locked" if locked else "File")
            iid = self.tree.insert(
                "", "end", text="  " + path.name,
                values=(kind, "..." if folder else human_size(size), "Ready to unlock" if locked else "Ready to lock"),
                tags=("locked" if locked else "plain",),
            )
            self.items[iid] = {"path": path, "locked": locked, "folder": folder, "size": size, "state": "new"}
            if folder:
                self.size_queue.put(iid)
        self.refresh()

    def add_files(self):
        picked = filedialog.askopenfilenames(title="Choose files")
        if picked:
            self.add(picked)

    def add_folder(self):
        picked = filedialog.askdirectory(title="Choose a folder to lock")
        if picked:
            self.add([picked])

    def size_loop(self):
        while True:
            iid = self.size_queue.get()
            item = self.items.get(iid)
            if item is not None:
                self.events.put(("size", iid, folder_size(item["path"])))

    def delete_key(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return
        self.remove_selected()

    def remove_selected(self):
        if self.running:
            return
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.tree.delete(iid)
        self.refresh()

    def clear(self):
        if self.running:
            return
        for iid in list(self.items):
            self.tree.delete(iid)
        self.items.clear()
        self.fraction = 0
        self.draw_progress()
        self.show_button.pack_forget()
        self.refresh()

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def pending(self, locked):
        return [
            iid for iid in self.tree.get_children()
            if self.items[iid]["state"] != "done" and self.items[iid]["locked"] == locked
        ]

    def refresh(self):
        total = len(self.items)
        self.count_label.configure(text="Files (%d)" % total if total else "Files")
        if total:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.58, anchor="center")

        to_lock = self.pending(False)
        to_unlock = self.pending(True)
        password = self.password.get()
        if to_lock:
            self.confirm_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.confirm_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
            self.meter.grid(row=2, column=1, sticky="ew", padx=8, pady=(8, 0))
            self.hint.grid(row=3, column=1, sticky="w", padx=8, pady=(4, 0))
        else:
            for widget in (self.confirm_label, self.confirm_entry, self.meter, self.hint):
                widget.grid_remove()
        self.draw_meter()

        problem = ""
        if to_lock and password:
            if len(password) < MIN_PASSWORD:
                problem = "Use at least %d characters" % MIN_PASSWORD
            elif self.confirm.get() != password:
                problem = "The two passwords don't match yet"
        if to_lock and password and not problem:
            self.hint.configure(text=STRENGTH_NAMES[strength(password)], foreground=MUTED)
        else:
            self.hint.configure(text=problem, foreground=WARN)

        can_lock = bool(to_lock) and bool(password) and not problem
        can_unlock = bool(to_unlock) and bool(password)
        if not self.running:
            self.lock_button.configure(
                state="normal" if can_lock else "disabled",
                text="Lock %d" % len(to_lock) if to_lock else "Lock",
            )
            self.unlock_button.configure(
                state="normal" if can_unlock else "disabled",
                text="Unlock %d" % len(to_unlock) if to_unlock else "Unlock",
            )
            if not total:
                self.status.configure(text="Add some files to get started.", foreground=MUTED)
            elif self.status.cget("text").startswith(("Add some", "Wait for", "Enter a password", "Ready")):
                self.status.configure(text="Enter a password to continue." if not password else "Ready.", foreground=MUTED)

    def mode_changed(self):
        folder_mode = self.mode.get() == "folder"
        self.folder_entry.configure(state="normal" if folder_mode else "disabled")
        self.folder_button.configure(state="normal" if folder_mode else "disabled")
        self.shred_box.configure(state="normal" if self.delete_original.get() else "disabled")
        if not self.delete_original.get():
            self.shred.set(False)

    def start(self, action):
        targets = self.pending(action == "unlock")
        if not targets or self.running:
            return
        output = None
        if self.mode.get() == "folder":
            output = self.folder.get().strip()
            if not output:
                messagebox.showinfo("Lockbox", "Choose a folder to save the files in.")
                return
        delete = self.delete_original.get()
        if delete and action == "lock":
            ask = "The original files will be deleted once the locked copies check out. Continue?"
            if not messagebox.askokcancel("Delete originals", ask, icon="warning"):
                return
        self.running = True
        self.cancel.clear()
        self.fraction = 0
        self.draw_progress()
        self.lock_button.pack_forget()
        self.unlock_button.pack_forget()
        self.show_button.pack_forget()
        self.cancel_button.pack(side="right")
        self.status.configure(text="Working...", foreground=MUTED)
        settings = {
            "action": action, "password": self.password.get(), "keyfile": self.keyfile.get() or None,
            "output": output, "hide": self.hide_name.get(), "delete": delete, "shred": self.shred.get(),
        }
        threading.Thread(target=self.work, args=(targets, settings), daemon=True).start()

    def work(self, targets, settings):
        done = failed = 0
        count = len(targets)
        for index, iid in enumerate(targets):
            if self.cancel.is_set():
                break
            item = self.items.get(iid)
            if item is None:
                continue
            self.events.put(("start", iid, index, count))

            def progress(finished, total, index=index):
                now = time.monotonic()
                if now - self.last_tick > 0.05 or finished >= total:
                    self.last_tick = now
                    fraction = finished / total if total else 1
                    self.events.put(("progress", (index + fraction) / count))

            if settings["action"] == "lock":
                result = encrypt_path(
                    item["path"], settings["password"], keyfile=settings["keyfile"], output_dir=settings["output"],
                    hide_name=settings["hide"], delete_original=settings["delete"], shred=settings["shred"],
                    progress=progress, cancel=self.cancel,
                )
            else:
                result = decrypt_path(
                    item["path"], settings["password"], keyfile=settings["keyfile"], output_dir=settings["output"],
                    delete_original=settings["delete"], shred=settings["shred"], progress=progress, cancel=self.cancel,
                )
            if result.error:
                failed += 1
            else:
                done += 1
            self.events.put(("done", iid, result))
        self.events.put(("finished", settings["action"], done, failed, self.cancel.is_set()))

    def poll(self):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def handle(self, event):
        kind = event[0]
        if kind == "size":
            _, iid, size = event
            if iid in self.items:
                self.items[iid]["size"] = size
                self.tree.set(iid, "size", human_size(size))
        elif kind == "start":
            _, iid, index, count = event
            if iid in self.items:
                self.tree.set(iid, "status", "Working...")
            self.status.configure(text="%d of %d..." % (index + 1, count), foreground=MUTED)
        elif kind == "progress":
            self.fraction = event[1]
            self.draw_progress()
        elif kind == "done":
            _, iid, result = event
            item = self.items.get(iid)
            if item is None:
                return
            if result.error:
                self.tree.item(iid, tags=("bad",))
                self.tree.set(iid, "status", result.error)
            else:
                item["state"] = "done"
                self.last_folder = result.output.parent
                self.tree.item(iid, tags=("done",))
                self.tree.set(iid, "status", "Locked as " + result.output.name if result.action == "lock" else "Saved as " + result.output.name)
        elif kind == "finished":
            _, action, done, failed, stopped = event
            self.running = False
            self.fraction = 1.0 if not stopped else self.fraction
            self.draw_progress()
            self.cancel_button.pack_forget()
            self.unlock_button.pack(side="right")
            self.lock_button.pack(side="right", padx=(0, 8))
            self.refresh()
            word = "locked" if action == "lock" else "unlocked"
            parts = ["%d %s" % (done, word)]
            if failed:
                parts.append("%d failed" % failed)
            if stopped:
                parts.append("stopped early")
            self.status.configure(text=", ".join(parts), foreground=BAD if failed else (GOOD if done else MUTED))
            if done and self.last_folder:
                self.show_button.pack(side="right", padx=(0, 8))

    def show_folder(self):
        if not self.last_folder:
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(self.last_folder))
            else:
                subprocess.Popen(["xdg-open", str(self.last_folder)])
        except OSError:
            pass

    def close(self):
        self.saved.update({
            "mode": self.mode.get(),
            "folder": self.folder.get(),
            "hide_name": self.hide_name.get(),
            "theme": self.theme.get(),
            "size": [int(self.root.winfo_width() / self.scale), int(self.root.winfo_height() / self.scale)],
        })
        save_settings(self.saved)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    App(paths).run()


if __name__ == "__main__":
    main()

_credit = "Built by Unkn0wngb"
