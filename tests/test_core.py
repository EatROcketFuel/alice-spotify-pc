"""Unit tests for the platform-independent logic (run: python -m pytest)."""
import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


@pytest.fixture()
def cfg(tmp_path):
    data = json.loads((main.BASE_DIR / "config.json").read_text(encoding="utf-8-sig"))
    path = tmp_path / "config.json"
    path.write_text("\ufeff" + json.dumps(data, ensure_ascii=False), encoding="utf-8")  # BOM like Notepad
    return main.load_config(path)


def test_shipped_config_loads_and_merges_defaults(cfg):
    assert cfg["topics"]["spotify_power"] == "alice/pc/spotify/power"
    assert "_comment" not in cfg
    assert cfg["spotify"]["fallback_exe_paths"]           # default kept
    assert cfg["mqtt"]["reconnect_max_delay"] == 60


def test_invalid_json_message(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text('{"spotify": {"exe_path": "C:\\Users"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="backslashes"):
        main.load_config(bad)


@pytest.mark.parametrize("payload,expected", [
    ("NEXT", "NEXT"), ("next", "NEXT"), ("prev", "PREV"), ("Previous", "PREV"),
    ("PLAY_PAUSE", "PLAY_PAUSE"), ("play pause", "PLAY_PAUSE"), ("play-pause", "PLAY_PAUSE"),
    ("pause", "PLAY_PAUSE"), ("volume_up", "VOLUME_UP"), ("пауза", "PLAY_PAUSE"), ("bogus", None),
])
def test_media_resolve(payload, expected):
    assert main.MediaKeys.resolve(payload) == expected


def test_alias(cfg):
    assert main.apply_alias(cfg["payload_aliases"], "audio_switch", "1") == "SPEAKERS"
    assert main.apply_alias(cfg["payload_aliases"], "audio_switch", " FALSE ") == "HEADPHONES"
    assert main.apply_alias(cfg["payload_aliases"], "media_control", "1") == "1"


def test_audio_match_prefers_stereo_over_hands_free():
    devs = [main.AudioDevice("1", "Headset (Yandex Station Hands-Free AG Audio)"),
            main.AudioDevice("2", "Динамики (Yandex Station)"),
            main.AudioDevice("3", "Наушники (WH-1000XM4)")]
    assert main.AudioSwitcher.match(devs, ["Yandex Station"]).id == "2"
    assert main.AudioSwitcher.match(devs, ["нет такого", "наушники"]).id == "3"
    assert main.AudioSwitcher.match(devs, ["Динамики (Yandex Station)"]).id == "2"
    assert main.AudioSwitcher.match(devs, ["AirPods"]) is None


class FakeBackend:
    name = "fake"

    def __init__(self, devices):
        self.devices = devices
        self.calls = []

    def list_devices(self):
        return self.devices

    def set_default(self, device, roles):
        self.calls.append((device.id, roles))


def test_audio_switch_uses_backend(cfg):
    cfg["audio"]["mode"] = "system"
    sw = main.AudioSwitcher(cfg)
    sw._backend = FakeBackend([main.AudioDevice("a", "Динамики (Yandex Station)"),
                               main.AudioDevice("b", "Наушники (USB)", is_default=True)])
    assert sw.switch("speakers").id == "a"
    assert sw._backend.calls == [("a", ["console", "multimedia", "communications"])]
    sw.switch("HEADPHONES")                      # already default -> no call
    assert len(sw._backend.calls) == 1
    with pytest.raises(ValueError):
        sw.switch("TV")


def test_router_routes_and_buttons(cfg, monkeypatch):
    router = main.CommandRouter(cfg)
    seen = []
    for name in main.COMMANDS:
        router.handlers[name] = lambda p, n=name: seen.append((n, p))
    router.handle_message("alice/pc/media/control", "NEXT")
    router.handle_message("alice/pc/media/next", "1")
    router.handle_message("alice/pc/audio/switch", "0")
    router.handle_message("unknown/topic", "x")
    assert seen == [("media_control", "NEXT"), ("media_control", "NEXT"),
                    ("audio_switch", "HEADPHONES")]


def test_button_trigger_on(cfg):
    cfg["button_topics"] = [{"topic": "t/b", "command": "media_control", "payload": "NEXT", "trigger": "on"}]
    router = main.CommandRouter(cfg)
    seen = []
    router.handlers["media_control"] = seen.append
    router.handle_message("t/b", "0")
    router.handle_message("t/b", "true")
    assert seen == ["NEXT"]


def test_errors_are_reported_not_raised(cfg):
    states = []
    router = main.CommandRouter(cfg, publish_state=lambda k, v: states.append((k, v)))
    assert router.execute("spotify_power", "MAYBE") is False
    assert router.execute("media_control", "DANCE") is False
    assert [k for k, _ in states] == ["error", "error"]


def test_diagnose_connection_explains_failures():
    import socket as _socket
    import threading as _threading

    server = _socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept_and_close():
        conn, _ = server.accept()
        conn.close()
    worker = _threading.Thread(target=accept_and_close, daemon=True)
    worker.start()
    assert "does not use TLS" in main.diagnose_connection("127.0.0.1", port, tls=True)
    worker.join(5)
    server.close()
    assert "TCP connection" in main.diagnose_connection("127.0.0.1", port, tls=False, timeout=2)
    assert "DNS lookup" in main.diagnose_connection("no-such-host.invalid", 1883, tls=False)


class FakeProc:
    def __init__(self, pid, name):
        self.pid, self.info = pid, {"name": name}


def test_audio_app_mode_routes_only_spotify(cfg, tmp_path, monkeypatch):
    import psutil
    exe = tmp_path / "SoundVolumeView.exe"
    exe.write_bytes(b"")
    cfg["audio"]["mode"] = "app"
    cfg["audio"]["app_routing"]["soundvolumeview_path"] = str(exe)
    sw = main.AudioSwitcher(cfg)
    sw._backend = FakeBackend([main.AudioDevice("{dev-a}", "Динамики (Яндекс Станция new)"),
                               main.AudioDevice("{dev-b}", "Наушники (WH-1000XM4)", is_default=True)])
    runs = []
    monkeypatch.setattr(main.subprocess, "run",
                        lambda args, **_kw: runs.append(args) or type("R", (), {"returncode": 0})())
    procs = [FakeProc(10, "Spotify.exe"), FakeProc(11, "chrome.exe"), FakeProc(12, "spotify.exe")]
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter(procs))
    cfg["audio"]["devices"] = {"SPEAKERS": ["Яндекс Станция"], "HEADPHONES": ["WH-1000XM4"]}

    assert sw.switch("SPEAKERS").id == "{dev-a}"
    assert sw._backend.calls == []                       # Windows default untouched
    assert runs == [[str(exe), "/SetAppDefault", "{dev-a}", "all", "10"],
                    [str(exe), "/SetAppDefault", "{dev-a}", "all", "12"]]
    assert sw.pending_target is None

    procs.clear()                                        # Spotify not running -> remember
    sw.switch("HEADPHONES")
    assert sw.pending_target == "HEADPHONES"
    procs.append(FakeProc(20, "Spotify.exe"))
    sw.apply_pending()
    assert runs[-1] == [str(exe), "/SetAppDefault", "{dev-b}", "all", "20"]
    assert sw.pending_target is None


def test_audio_app_mode_requires_soundvolumeview(cfg, tmp_path, monkeypatch):
    cfg["audio"]["mode"] = "app"
    cfg["audio"]["app_routing"]["soundvolumeview_path"] = str(tmp_path / "missing.exe")
    cfg["audio"]["devices"] = {"SPEAKERS": ["Speakers"]}
    monkeypatch.setattr(main, "DEFAULT_CONFIG", {"audio": {"app_routing": {
        "soundvolumeview_path": str(tmp_path / "missing2.exe")}}})
    sw = main.AudioSwitcher(cfg)
    sw._backend = FakeBackend([main.AudioDevice("a", "Speakers")])
    with pytest.raises(FileNotFoundError):
        sw.switch("SPEAKERS")


def test_prev_pressed_twice(cfg, monkeypatch):
    router = main.CommandRouter(cfg)
    pressed = []
    monkeypatch.setattr(router.media, "press", pressed.append)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    router.media_control("PREV")
    router.media_control("NEXT")
    assert pressed == ["PREV", "PREV", "NEXT"]
    cfg["spotify"]["prev_presses"] = 1
    router.media_control("PREV")
    assert pressed[-2:] == ["NEXT", "PREV"]


def test_update_config_file_keeps_other_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("\ufeff" + json.dumps({"_comment": "x", "mqtt": {"host": "a", "qos": 1},
                                            "audio": {"devices": {"TV": ["LG"]}}}), encoding="utf-8")
    main.update_config_file(path, {"mqtt": {"host": "b"}, "audio": {"devices": {"SPEAKERS": ["S"]}}})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_comment"] == "x" and data["mqtt"] == {"host": "b", "qos": 1}
    assert data["audio"]["devices"] == {"TV": ["LG"], "SPEAKERS": ["S"]}


def test_guess_device():
    names = ["Наушники (WH-1000XM4)", "Динамики (Яндекс Станция new)", "Динамики (Realtek Audio)",
             "Головной телефон (WH-1000XM4 Hands-Free)"]
    assert main.guess_device(names, main.SPEAKER_HINTS) == "Динамики (Яндекс Станция new)"
    assert main.guess_device(names, main.HEADPHONE_HINTS) == "Наушники (WH-1000XM4)"
    assert main.guess_device(["Speakers (Realtek)"], main.HEADPHONE_HINTS) is None
    assert main.guess_device(names, main.SPEAKER_HINTS,
                             exclude="Динамики (Яндекс Станция new)") == "Динамики (Realtek Audio)"


def test_autostart_toggle(monkeypatch, tmp_path):
    link = tmp_path / "Startup" / "Alice PC Controller.lnk"
    monkeypatch.setattr(main, "startup_shortcut", lambda: link)
    assert not main.autostart_enabled()
    if sys.platform == "win32":
        main.set_autostart(True)
        assert main.autostart_enabled()
    main.set_autostart(False)
    assert not main.autostart_enabled()


def test_settings_window_saves(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    import settings_ui
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mqtt": {"host": "old", "password": "p"}}), encoding="utf-8")
    monkeypatch.setattr(main, "autostart_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(main, "set_autostart", calls.append)
    monkeypatch.setattr(settings_ui, "list_device_names",
                        lambda cfg: (["Наушники (WH-1000XM4)", "Динамики (Яндекс Станция new)"], ""))
    try:
        win = settings_ui.SettingsWindow(path)
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    try:
        for _ in range(30):
            win.root.update()
            if win.devices:
                break
            win.root.after(50)
        assert win.v_speakers.get() == "Динамики (Яндекс Станция new)"
        assert win.v_headphones.get() == "Наушники (WH-1000XM4)"
        win.v_host.set(" broker.example.com ")
        win.v_pass.set(" secret ")
        win.v_port.set("1883")
        win.v_tray.set(True)
        win.v_autostart.set(True)
        win.save()
    finally:
        with contextlib.suppress(tk.TclError):
            win.root.destroy()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mqtt"]["host"] == "broker.example.com" and data["mqtt"]["password"] == "secret"
    assert data["mqtt"]["port"] == 1883 and data["spotify"]["tray_only"] is True
    assert data["audio"]["devices"]["SPEAKERS"] == ["Динамики (Яндекс Станция new)"]
    assert calls == [True]

def test_tray_only_launch_warms_up_and_plays(cfg, monkeypatch):
    cfg["spotify"].update(tray_only=True, play_on_launch=True)
    router = main.CommandRouter(cfg)
    sp, calls = router.spotify_process, []
    monkeypatch.setattr(sp, "launch", lambda: True)
    monkeypatch.setattr(sp, "start_hidden", lambda *a, **k: calls.append("hidden"))
    monkeypatch.setattr(sp, "ensure_ready", lambda *a, **k: calls.append("ready"))
    monkeypatch.setattr(router.media, "press", calls.append)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    router.spotify_power("ON")
    router.media_control("NEXT")
    assert calls == ["hidden", "ready", "PLAY_PAUSE", "ready", "NEXT"]
