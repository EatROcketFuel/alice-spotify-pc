"""Settings window for Alice PC Controller (tkinter, ships with Python).

Open it from the tray menu ("Настройки...") or with ``python main.py --settings``.
Saving writes config.json; the running tray app notices the change and restarts.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import main

LOG_LEVELS = ("INFO", "DEBUG", "WARNING")


def test_mqtt(host: str, port: int, username: str, password: str, tls: bool,
              timeout: float = 8.0) -> tuple[bool, str]:
    """Connect once with the given credentials; returns (ok, human-readable result)."""
    import paho.mqtt.client as mqtt

    done = threading.Event()
    result: dict = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    main.configure_mqtt_client(client, {"username": username, "password": password, "tls": tls})

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        result["rc"] = reason_code
        done.set()

    client.on_connect = on_connect
    try:
        client.connect(host, port, keepalive=30)
    except Exception:  # noqa: BLE001 - explained by diagnose_connection
        return False, main.diagnose_connection(host, port, tls)
    client.loop_start()
    try:
        if not done.wait(timeout):
            return False, main.diagnose_connection(host, port, tls)
        rc = result["rc"]
        if rc.is_failure:
            hint = " - проверьте логин и пароль" if "author" in str(rc).lower() else ""
            return False, f"Брокер отказал: {rc}{hint}"
        return True, "Подключение успешно"
    finally:
        client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()


def list_device_names(cfg: dict) -> tuple[list[str], str]:
    """Active playback devices (names) and an error text if listing failed."""
    with contextlib.suppress(Exception):
        import comtypes
        comtypes.CoInitialize()  # COM must be initialised in every thread that uses pycaw
    try:
        return [d.name for d in main.AudioSwitcher(cfg).list_devices()], ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def resolve_name(names: list[str], wanted: str) -> str | None:
    devices = [main.AudioDevice(n, n) for n in names]
    found = main.AudioSwitcher.match(devices, [wanted]) if wanted.strip() else None
    return found.name if found else None


class SettingsWindow:
    def __init__(self, config_path: Path, first_run: bool = False) -> None:
        self.config_path = config_path
        self.cfg = (main.load_config(config_path) if config_path.exists()
                    else main.deep_merge(main.DEFAULT_CONFIG, {}))
        self.saved = False
        self.devices: list[str] = []
        self.events: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"{main.APP_NAME} - настройки")
        self.root.minsize(600, 470)
        with contextlib.suppress(tk.TclError):
            ttk.Style().theme_use("vista")

        if first_run or not self.cfg["mqtt"].get("host"):
            ttk.Label(self.root, padding=(12, 10, 12, 0), wraplength=560, text=(
                "Добро пожаловать! Заполните данные брокера WQTT, выберите колонку и "
                "наушники и нажмите «Сохранить».")).pack(anchor="w")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._build_mqtt(self._tab(notebook, "Подключение"))
        self._build_audio(self._tab(notebook, "Звук"))
        self._build_spotify(self._tab(notebook, "Spotify"))
        self._build_general(self._tab(notebook, "Общие"))

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, foreground="#666",
                  text="После сохранения программа в трее сама перезапустится.").pack(side="left")
        ttk.Button(bottom, text="Отмена", command=self.root.destroy).pack(side="right")
        ttk.Button(bottom, text="Сохранить", command=self.save).pack(side="right", padx=6)

        self.root.after(100, self._poll)
        self.refresh_devices()

    # -- layout helpers ------------------------------------------------------ #
    @staticmethod
    def _tab(notebook: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(notebook, padding=12)
        frame.columnconfigure(1, weight=1)
        notebook.add(frame, text=title)
        return frame

    @staticmethod
    def _row(parent, row: int, label: str, widget, **grid) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        widget.grid(row=row, column=1, pady=4, **{"sticky": "ew", **grid})

    @staticmethod
    def _hint(parent, row: int, text: str) -> ttk.Label:
        label = ttk.Label(parent, text=text, foreground="#666", wraplength=520)
        label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        return label

    # -- tabs ---------------------------------------------------------------- #
    def _build_mqtt(self, tab: ttk.Frame) -> None:
        m = self.cfg["mqtt"]
        self.v_host = tk.StringVar(value=m.get("host", ""))
        self.v_port = tk.StringVar(value=str(m.get("port", 1883)))
        self.v_user = tk.StringVar(value=m.get("username", ""))
        self.v_pass = tk.StringVar(value=m.get("password", ""))
        self.v_tls = tk.BooleanVar(value=bool(m.get("tls")))
        self.v_show = tk.BooleanVar(value=False)
        self._hint(tab, 0, "Данные из кабинета WQTT (раздел с параметрами брокера). "
                           "Пароль - от MQTT, а не от входа на сайт.")
        self._row(tab, 1, "Сервер", ttk.Entry(tab, textvariable=self.v_host))
        self._row(tab, 2, "Порт", ttk.Entry(tab, textvariable=self.v_port, width=10), sticky="w")
        self._row(tab, 3, "Логин", ttk.Entry(tab, textvariable=self.v_user))
        self.e_pass = ttk.Entry(tab, textvariable=self.v_pass, show="•")
        self._row(tab, 4, "Пароль", self.e_pass)
        ttk.Checkbutton(tab, text="показать", variable=self.v_show, command=lambda: self.e_pass.configure(
            show="" if self.v_show.get() else "•")).grid(row=4, column=2, padx=6)
        ttk.Checkbutton(tab, text="TLS (для WQTT обычно выключено)",
                        variable=self.v_tls).grid(row=5, column=1, sticky="w", pady=4)
        self.b_test = ttk.Button(tab, text="Проверить подключение", command=self.check_mqtt)
        self.b_test.grid(row=6, column=1, sticky="w", pady=(10, 4))
        self.l_mqtt = ttk.Label(tab, text="", wraplength=520)
        self.l_mqtt.grid(row=7, column=0, columnspan=3, sticky="w")

    def _build_audio(self, tab: ttk.Frame) -> None:
        a = self.cfg["audio"]
        devices = {k.upper(): ([v] if isinstance(v, str) else list(v))
                   for k, v in a.get("devices", {}).items()}
        self.v_mode = tk.StringVar(value=str(a.get("mode", "app")).lower())
        self.v_speakers = tk.StringVar(value=(devices.get("SPEAKERS") or [""])[0])
        self.v_headphones = tk.StringVar(value=(devices.get("HEADPHONES") or [""])[0])
        ttk.Radiobutton(tab, text="Переключать только Spotify (системный звук не трогать)",
                        value="app", variable=self.v_mode).grid(row=0, column=0, columnspan=3,
                                                                sticky="w")
        ttk.Radiobutton(tab, text="Переключать звук всей Windows", value="system",
                        variable=self.v_mode).grid(row=1, column=0, columnspan=3, sticky="w",
                                                   pady=(0, 8))
        self.c_speakers = ttk.Combobox(tab, textvariable=self.v_speakers)
        self.c_headphones = ttk.Combobox(tab, textvariable=self.v_headphones)
        self._row(tab, 2, "Колонка («включить»)", self.c_speakers)
        self.l_speakers = self._hint(tab, 3, "")
        self._row(tab, 4, "Наушники («выключить»)", self.c_headphones)
        self.l_headphones = self._hint(tab, 5, "")
        buttons = ttk.Frame(tab)
        buttons.grid(row=6, column=1, sticky="w", pady=6)
        ttk.Button(buttons, text="Обновить список", command=self.refresh_devices).pack(side="left")
        ttk.Button(buttons, text="Определить автоматически",
                   command=lambda: self.autodetect(force=True)).pack(side="left", padx=6)
        self.l_devices = self._hint(tab, 7, "Ищу аудиоустройства...")
        tool = main.resolve_path(a["app_routing"]["soundvolumeview_path"])
        self._hint(tab, 8, "SoundVolumeView: " + ("найден" if tool.exists() else
                                                  f"не найден ({tool}) - нужен для режима «только Spotify»"))
        for var in (self.v_speakers, self.v_headphones):
            var.trace_add("write", lambda *_: self._update_matches())

    def _build_spotify(self, tab: ttk.Frame) -> None:
        sp = self.cfg["spotify"]
        self.v_exe = tk.StringVar(value=sp.get("exe_path", ""))
        self.v_tray = tk.BooleanVar(value=bool(sp.get("tray_only")))
        self.v_play = tk.BooleanVar(value=bool(sp.get("play_on_launch")))
        self.v_prev = tk.StringVar(value=str(sp.get("prev_presses", 2)))
        exe = ttk.Frame(tab)
        exe.columnconfigure(0, weight=1)
        ttk.Entry(exe, textvariable=self.v_exe).grid(row=0, column=0, sticky="ew")
        ttk.Button(exe, text="Обзор...", command=self.browse_exe).grid(row=0, column=1, padx=(6, 0))
        self._row(tab, 0, "Spotify.exe", exe)
        self.l_exe = self._hint(tab, 1, "")
        self.v_exe.trace_add("write", lambda *_: self._update_exe())
        self._update_exe()
        ttk.Checkbutton(tab, variable=self.v_tray, text=(
            "Только в трее: прятать окно Spotify, когда программа его запускает "
            "или включает трек")).grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
        self._hint(tab, 3, "Вернуть окно: значок Spotify в трее или меню этой программы → "
                           "«Показать Spotify».")
        self._row(tab, 5, "Нажатий «назад»", ttk.Spinbox(tab, from_=1, to=3, width=5,
                                                       textvariable=self.v_prev), sticky="w")
        ttk.Checkbutton(tab, variable=self.v_play,
                        text="Сразу включать музыку, когда Алиса запускает Spotify").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=4)
        self._hint(tab, 6, "2 - сразу на предыдущий трек (первое нажатие Spotify перематывает в начало).")

    def _build_general(self, tab: ttk.Frame) -> None:
        self.autostart_was = main.autostart_enabled()
        self.v_autostart = tk.BooleanVar(value=self.autostart_was)
        self.v_notify = tk.BooleanVar(value=bool(self.cfg["tray"].get("notifications", True)))
        self.v_level = tk.StringVar(value=str(self.cfg["logging"].get("level", "INFO")).upper())
        ttk.Checkbutton(tab, text="Запускать вместе с Windows (в трее, без окна)",
                        variable=self.v_autostart).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(tab, text="Уведомления в трее", variable=self.v_notify).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=4)
        self._row(tab, 2, "Подробность лога", ttk.Combobox(tab, textvariable=self.v_level, width=10,
                                                           state="readonly", values=LOG_LEVELS),
                  sticky="w")
        self._hint(tab, 3, f"Файл настроек: {self.config_path}")

    # -- actions --------------------------------------------------------------- #
    def _poll(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                kind, *data = self.events.get_nowait()
                if kind == "mqtt":
                    ok, text = data
                    self.l_mqtt.configure(text=("✔ " if ok else "✖ ") + text,
                                          foreground="#1a7f37" if ok else "#c62828")
                    self.b_test.state(["!disabled"])
                elif kind == "devices":
                    self._devices_loaded(*data)
        self.root.after(100, self._poll)

    def check_mqtt(self) -> None:
        try:
            port = int(self.v_port.get().strip())
        except ValueError:
            self.l_mqtt.configure(text="✖ Порт должен быть числом", foreground="#c62828")
            return
        args = (self.v_host.get().strip(), port, self.v_user.get().strip(),
                self.v_pass.get().strip(), self.v_tls.get())
        self.b_test.state(["disabled"])
        self.l_mqtt.configure(text="Подключаюсь...", foreground="")
        threading.Thread(target=lambda: self.events.put(("mqtt", *test_mqtt(*args))),
                         daemon=True).start()

    def refresh_devices(self) -> None:
        self.l_devices.configure(text="Ищу аудиоустройства...")
        threading.Thread(target=lambda: self.events.put(("devices", *list_device_names(self.cfg))),
                         daemon=True).start()

    def _devices_loaded(self, names: list[str], error: str) -> None:
        self.devices = names
        self.c_speakers.configure(values=names)
        self.c_headphones.configure(values=names)
        if error:
            self.l_devices.configure(text=f"Не удалось получить список устройств: {error}")
        else:
            self.l_devices.configure(text=f"Найдено устройств вывода: {len(names)}. Подключите "
                                          "колонку и наушники и нажмите «Обновить список», "
                                          "если какого-то нет.")
        self.autodetect(force=False)
        self._update_matches()

    def autodetect(self, force: bool) -> None:
        """Fill in the speaker / headphones from device names (Станция, JBL, Наушники...)."""
        for var, hints, other in ((self.v_speakers, main.SPEAKER_HINTS, self.v_headphones),
                                  (self.v_headphones, main.HEADPHONE_HINTS, self.v_speakers)):
            if force or not resolve_name(self.devices, var.get()):
                other_name = resolve_name(self.devices, other.get()) or ""
                guess = main.guess_device(self.devices, hints, exclude=other_name)
                if guess:
                    var.set(guess)

    def _update_matches(self) -> None:
        for var, label in ((self.v_speakers, self.l_speakers),
                           (self.v_headphones, self.l_headphones)):
            name = resolve_name(self.devices, var.get())
            if name:
                label.configure(text=f"✔ {name}", foreground="#1a7f37")
            elif self.devices:
                label.configure(text="✖ сейчас не подключено", foreground="#c62828")

    def _update_exe(self) -> None:
        path = main.resolve_path(self.v_exe.get()) if self.v_exe.get().strip() else None
        ok = bool(path and path.exists())
        self.l_exe.configure(text="✔ найден" if ok else "✖ файл не найден",
                             foreground="#1a7f37" if ok else "#c62828")

    def browse_exe(self) -> None:
        current = main.resolve_path(self.v_exe.get()) if self.v_exe.get().strip() else None
        path = filedialog.askopenfilename(
            parent=self.root, title="Где находится Spotify.exe",
            initialdir=str(current.parent) if current and current.parent.exists() else None,
            filetypes=[("Spotify.exe", "*.exe")])
        if path:
            self.v_exe.set(str(Path(path)))

    def collect(self) -> dict:
        port = int(self.v_port.get().strip())
        devices = {}
        for key, var in (("SPEAKERS", self.v_speakers), ("HEADPHONES", self.v_headphones)):
            if var.get().strip():
                devices[key] = [var.get().strip()]
        return {
            "mqtt": {"host": self.v_host.get().strip(), "port": port,
                     "username": self.v_user.get().strip(), "password": self.v_pass.get().strip(),
                     "tls": self.v_tls.get()},
            "audio": {"mode": self.v_mode.get(), "devices": devices},
            "spotify": {"exe_path": self.v_exe.get().strip(), "tray_only": self.v_tray.get(),
                        "play_on_launch": self.v_play.get(),
                        "prev_presses": max(1, int(self.v_prev.get() or 2))},
            "tray": {"notifications": self.v_notify.get()},
            "logging": {"level": self.v_level.get()},
        }

    def save(self) -> None:
        try:
            updates = self.collect()
        except ValueError:
            messagebox.showerror(main.APP_NAME, "Порт и число нажатий должны быть числами.",
                                 parent=self.root)
            return
        if not updates["mqtt"]["host"]:
            messagebox.showerror(main.APP_NAME, "Укажите сервер MQTT.", parent=self.root)
            return
        try:
            main.update_config_file(self.config_path, updates)
        except (OSError, ValueError) as exc:
            messagebox.showerror(main.APP_NAME, f"Не удалось сохранить config.json:\n{exc}",
                                 parent=self.root)
            return
        if self.v_autostart.get() != self.autostart_was:
            try:
                main.set_autostart(self.v_autostart.get())
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning(main.APP_NAME, f"Настройки сохранены, но автозапуск "
                                                      f"изменить не удалось:\n{exc}", parent=self.root)
        self.saved = True
        self.root.destroy()


def open_settings(config_path: Path, first_run: bool = False) -> bool:
    """Show the window (blocks until closed). Returns True if the user saved."""
    window = SettingsWindow(config_path, first_run=first_run)
    window.root.mainloop()
    return window.saved
