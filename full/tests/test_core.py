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


@pytest.mark.parametrize("payload,expected", [
    ("Smells Like Teen Spirit - Nirvana", ("Smells Like Teen Spirit", "Nirvana")),
    ("Группа крови — Кино", ("Группа крови", "Кино")),
    ("Song – Artist", ("Song", "Artist")),
    ("Jay-Z", (None, None)),
    ("  Numb  ", (None, None)),
])
def test_parse_search_payload(payload, expected):
    song, artist, _raw = main.parse_search_payload(payload)
    assert (song, artist) == expected


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
    assert router.execute("spotify_search", "1") is False
    assert [k for k, _ in states] == ["error", "error", "error"]


def test_pick_device(cfg):
    ctl = main.SpotifyController(cfg, main.SpotifyProcess(cfg))
    devices = [{"id": "phone", "name": "Pixel", "type": "Smartphone", "is_active": True},
               {"id": "pc2", "name": "LAPTOP", "type": "Computer", "is_active": False},
               {"id": "pc", "name": "GAMING-PC", "type": "Computer", "is_active": False}]
    cfg["spotify"]["device_name"] = "gaming-pc"
    assert ctl.pick_device(devices)["id"] == "pc"
    cfg["spotify"]["device_name"] = "nope"
    assert ctl.pick_device(devices)["id"] == "pc2"
    assert ctl.pick_device(devices[:1]) is None


class FakeSpotify:
    def __init__(self):
        self.queries, self.played = [], []

    def search(self, q, type, limit, market):
        self.queries.append(q)
        hit = {"name": "Кукушка", "uri": "spotify:track:x", "artists": [{"name": "Кино"}]}
        return {"tracks": {"items": [hit] if len(self.queries) == 2 else []}}

    def devices(self):
        return {"devices": [{"id": "d1", "name": "PC", "type": "Computer", "is_active": True}]}

    def start_playback(self, device_id, uris):
        self.played.append((device_id, uris))


def test_play_search_fallback_order(cfg):
    cfg["spotify"]["search_mode"] = "web_api"
    ctl = main.SpotifyController(cfg, main.SpotifyProcess(cfg))
    ctl._client = FakeSpotify()
    title = ctl.play("Кино - Кукушка")       # artist first -> second query matches
    assert title == "Кино — Кукушка"
    assert ctl._client.queries[0] == 'track:"Кино" artist:"Кукушка"'
    assert ctl._client.played == [("d1", ["spotify:track:x"])]


def test_background_oauth_never_prompts(cfg, tmp_path):
    cfg["spotify"]["client_id"] = "id"
    cfg["spotify"]["client_secret"] = "secret"
    cfg["spotify"]["cache_path"] = str(tmp_path / "cache")
    oauth = main.build_spotify_oauth(cfg, interactive=False)
    with pytest.raises(main.SpotifyAuthRequired):
        oauth.get_access_token(as_dict=False)


def test_oauth_requires_keys(cfg):
    cfg["spotify"]["client_id"] = ""
    with pytest.raises(main.SpotifyAuthRequired):
        main.build_spotify_oauth(cfg, interactive=False)


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


def _embed(title, *artists, release="", playable=True):
    data = {"props": {"pageProps": {"state": {"data": {"entity": {
        "name": title, "artists": [{"name": a} for a in artists],
        "releaseDate": {"isoString": release + "T00:00:00Z"} if release else None,
        "isPlayable": playable}}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def _fake_search(monkeypatch, tracks):
    """tracks: list of (id, title, artists) in search-result order."""
    links = "".join(f'<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopen.spotify.com%2Ftrack%2F{t[0]}">x</a>'
                    for t in tracks)
    calls = []

    def fake_get(url, timeout=10):
        calls.append(url)
        if "duckduckgo" in url:
            return links
        for i, title, artists, *extra in tracks:
            if i in url:
                return _embed(title, *artists, **(extra[0] if extra else {}))
        raise OSError("unexpected url")
    monkeypatch.setattr(main, "http_get", fake_get)
    return calls


def test_keyless_search_prefers_original(monkeypatch):
    a, b, c = "A" * 22, "B" * 22, "C" * 22
    calls = _fake_search(monkeypatch, [(a, "Группа крови - Acoustic Version", ["Kino"]),
                                       (b, "Группа крови", ["Tribute Band"]),
                                       (c, "Группа крови", ["Kino"])])
    assert main.keyless_find_track("Группа крови - Кино") == (c, "Kino — Группа крови")
    assert "%D0%93" in calls[0]
    assert main.keyless_find_track("Кино - Группа крови")[0] == c          # artist first
    assert main.keyless_find_track("группа крови кино")[0] == c            # no separator
    assert main.keyless_find_track("группа крови кино акустика")[0] == a   # asked for it
    calls.clear()
    assert main.keyless_find_track(f"https://open.spotify.com/track/{a}?si=x")[0] == a
    assert calls == []                               # direct links need no network


def test_keyless_search_unknown_artist_keeps_search_order(monkeypatch):
    a, b = "A" * 22, "B" * 22
    _fake_search(monkeypatch, [(a, "Лесник", ["Король и Шут"]), (b, "Лесник (Remix)", ["DJ X"])])
    assert main.keyless_find_track("лесник")[0] == a
    assert main.keyless_find_track("Лесник - Король и Шут")[0] == a


def test_keyless_play_opens_uri(cfg, monkeypatch):
    opened = []
    monkeypatch.setattr(main, "keyless_find_track", lambda p: ("C" * 22, "Группа крови"))
    monkeypatch.setattr(main, "open_uri", opened.append)
    router = main.CommandRouter(cfg)
    monkeypatch.setattr(router.spotify_process, "is_running", lambda: True)
    cfg["spotify"]["search_mode"] = "keyless"
    assert router.spotify.play("группа крови кино") == "Группа крови"
    assert opened == ["spotify:track:" + "C" * 22]


def test_keyless_play_falls_back_to_search_page(cfg, monkeypatch):
    opened = []

    def nothing(_payload):
        raise LookupError("nothing")
    monkeypatch.setattr(main, "keyless_find_track", nothing)
    monkeypatch.setattr(main, "open_uri", opened.append)
    cfg["spotify"]["search_mode"] = "keyless"
    ctl = main.SpotifyController(cfg, main.SpotifyProcess(cfg))
    monkeypatch.setattr(ctl.process, "is_running", lambda: True)
    assert ctl.play("Кукушка - Кино") == "поиск «Кукушка Кино»"
    assert opened == ["spotify:search:%D0%9A%D1%83%D0%BA%D1%83%D1%88%D0%BA%D0%B0%20%D0%9A%D0%B8%D0%BD%D0%BE"]


def test_keyless_search_prefers_earliest_playable_release(monkeypatch):
    a, b, c = "A" * 22, "B" * 22, "C" * 22
    _fake_search(monkeypatch, [
        (a, "Сентябрь.", ["Стигмата (STIGMATA)"], {"release": "2013-03-28"}),  # acoustic album
        (b, "Сентябрь", ["Stigmata"], {"release": "2005-01-01", "playable": False}),
        (c, "Сентябрь", ["Stigmata"], {"release": "2007-09-12"}),
    ])
    assert main.keyless_find_track("сентябрь стигмата")[0] == c


def test_dialogs_skill_parse_command():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dialogs_skill", Path(__file__).parent.parent / "yandex_dialogs_skill" / "index.py")
    skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skill)
    parse = skill.parse_command
    assert parse("включить сентябрь стигмата") == ("spotify/search", "сентябрь стигмата",
                                                   "Включаю сентябрь стигмата")
    assert parse("включи пути назад группа")[0] == "spotify/search"
    assert parse("следующий трек")[1] == "NEXT"
    assert parse("включи следующий трек")[1] == "NEXT"
    assert parse("назад")[1] == "PREV"
    assert parse("на колонку")[1] == "SPEAKERS"
    assert parse("запусти спотифай")[1] == "ON"
    assert parse("включи спотифай")[1] == "ON"
    assert parse("выключи спотифай")[1] == "OFF"
    assert parse("") is None and parse("как дела") is None


def test_dialogs_skill_accepts_string_and_http_events(monkeypatch):
    import base64
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dialogs_skill2", Path(__file__).parent.parent / "yandex_dialogs_skill" / "index.py")
    skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skill)
    sent = []
    monkeypatch.setattr(skill, "publish", lambda topic, payload: sent.append((topic, payload)))
    raw = json.dumps({"version": "1.0", "session": {"new": False},
                      "request": {"command": "включи кино"}}, ensure_ascii=False)
    for event in (raw, json.loads(raw), {"body": raw},
                  {"body": base64.b64encode(raw.encode()).decode(), "isBase64Encoded": True}):
        assert skill.handler(event)["response"]["text"] == "Включаю кино"
    assert len(sent) == 4 and sent[0] == ("alice/pc/spotify/search", "кино")


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
