import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "skill_index", Path(__file__).resolve().parent.parent / "yandex_dialogs_skill" / "index.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


@pytest.mark.parametrize("text,expected", [
    ("следующий трек", ("media/control", "NEXT")),
    ("предыдущая песня", ("media/control", "PREV")),
    ("пауза", ("media/control", "PLAY_PAUSE")),
    ("переключи на наушники", ("audio/switch", "HEADPHONES")),
    ("звук на колонку", ("audio/switch", "SPEAKERS")),
    ("выключи спотифай", ("spotify/power", "OFF")),
    ("запусти spotify", ("spotify/power", "ON")),
    ("включи группа крови кино", ("spotify/search", "группа крови кино")),
    ("поставь песню bohemian rhapsody queen", ("spotify/search", "bohemian rhapsody queen")),
])
def test_parse_command(text, expected):
    assert skill.parse_command(text)[:2] == expected


def test_unknown_and_empty():
    assert skill.parse_command("") is None
    assert skill.parse_command("какая погода") is None


def test_handler_publishes(monkeypatch):
    sent = []
    monkeypatch.setattr(skill, "publish", lambda t, p: sent.append((t, p)))
    event = {"version": "1.0", "session": {"new": False},
             "request": {"command": "включи кино звезда по имени солнце"}}
    out = skill.handler(event)
    assert sent == [("alice/pc/spotify/search", "кино звезда по имени солнце")]
    assert out["response"]["end_session"] is True and out["version"] == "1.0"


def test_handler_welcome_and_failure(monkeypatch):
    out = skill.handler({"session": {"new": True}, "request": {"command": ""}})
    assert out["response"]["end_session"] is False

    def boom(*_):
        raise OSError("down")
    monkeypatch.setattr(skill, "publish", boom)
    out = skill.handler({"session": {}, "request": {"command": "пауза"}})
    assert "Не получилось" in out["response"]["text"]
