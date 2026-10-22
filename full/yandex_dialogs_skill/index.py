"""Yandex Dialogs (Alice skill) webhook for Yandex Cloud Functions.

Turns free-form voice commands into MQTT messages for main.py, e.g.
"Алиса, попроси Мой компьютер включить Группа крови Кино".

Entry point: index.handler
Environment variables: MQTT_HOST, MQTT_PORT (1883), MQTT_USER, MQTT_PASSWORD,
MQTT_TLS (0/1), TOPIC_PREFIX (alice/pc).
"""

import base64
import json
import os
import re
import ssl

import paho.mqtt.publish as mqtt_publish

PREFIX = os.environ.get("TOPIC_PREFIX", "alice/pc").rstrip("/")

HELP = ("Скажите, например: включи Группа крови Кино, следующий трек, пауза, "
        "переключи на наушники или на колонку, запусти или выключи спотифай.")

# (regex, topic suffix, payload, spoken reply); first match wins
RULES = [
    (r"\b(следующ|дальше|переключи трек|скип)", "media/control", "NEXT", "Следующий трек"),
    (r"\b(предыдущ|прошл|назад)", "media/control", "PREV", "Предыдущий трек"),
    (r"\b(пауз|стоп|останови|продолж|возобнов)", "media/control", "PLAY_PAUSE", "Готово"),
    (r"\b(наушник)", "audio/switch", "HEADPHONES", "Переключаю на наушники"),
    (r"\b(колонк|станци|динамик)", "audio/switch", "SPEAKERS", "Переключаю на колонку"),
    (r"\b(выключи|закрой|заверши)\b.*\b(спотифай|spotify)", "spotify/power", "OFF", "Закрываю Spotify"),
    (r"\b(запусти|открой|включи)\b\s+(спотифай|spotify)$", "spotify/power", "ON", "Запускаю Spotify"),
]

SEARCH_PREFIX = re.compile(r"^(включи|включить|поставь|поставить|играй|сыграй|найди|play)\s+"
                           r"((песню|трек|композицию)\s+)?")


def parse_command(text: str):
    """Return (topic_suffix, payload, reply) or None."""
    text = (text or "").strip().lower().replace("ё", "е")
    if not text:
        return None
    match = SEARCH_PREFIX.match(text)
    query = text[match.end():].strip() if match else ""
    # "включи <song>": control words inside a song title ("пути назад") must not win,
    # so after a play verb only short phrases ("включи паузу") count as controls.
    control_text = text if not query or len(query.split()) <= 2 else ""
    for pattern, suffix, payload, reply in RULES:
        if control_text and re.search(pattern, control_text):
            return suffix, payload, reply
    if query:
        return "spotify/search", query, f"Включаю {query}"
    return None


def publish(topic: str, payload: str) -> None:
    auth = None
    if os.environ.get("MQTT_USER"):
        auth = {"username": os.environ["MQTT_USER"], "password": os.environ.get("MQTT_PASSWORD", "")}
    tls = {"tls_version": ssl.PROTOCOL_TLS_CLIENT} if os.environ.get("MQTT_TLS", "0") == "1" else None
    mqtt_publish.single(topic, payload, qos=1, hostname=os.environ["MQTT_HOST"],
                        port=int(os.environ.get("MQTT_PORT", "1883")), auth=auth, tls=tls,
                        keepalive=10)


def _unwrap(event):
    """Accept a dict, a JSON string (test console) or an HTTP-invocation envelope."""
    for _ in range(3):
        if isinstance(event, (bytes, str)):
            try:
                event = json.loads(event)
            except ValueError:
                return {}
        elif isinstance(event, dict) and "request" not in event and "body" in event:
            body = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body).decode("utf-8")
            event = body
        else:
            break
    return event if isinstance(event, dict) else {}


def handler(event, context=None):
    event = _unwrap(event)
    request = event.get("request") or {}
    text = request.get("command") or request.get("original_utterance", "")
    end_session = True
    error = None
    parsed = parse_command(text)
    if event.get("session", {}).get("new") and not text:
        reply, end_session = "Компьютер на связи. " + HELP, False
    elif parsed is None:
        reply, end_session = "Не поняла команду. " + HELP, False
    else:
        suffix, payload, reply = parsed
        try:
            publish(f"{PREFIX}/{suffix}", payload)
        except Exception as exc:  # noqa: BLE001 - always answer Alice within the time limit
            error = f"{exc!r} (host={os.environ.get('MQTT_HOST')!r}, port={os.environ.get('MQTT_PORT')!r}, "
            error += f"user={os.environ.get('MQTT_USER')!r}, tls={os.environ.get('MQTT_TLS')!r})"
            print(f"MQTT publish failed: {error}")
            reply = "Не получилось связаться с компьютером"
    result = {
        "version": event.get("version", "1.0"),
        "session": event.get("session", {}),
        "response": {"text": reply, "end_session": end_session},
    }
    if event.get("debug") and error:  # test console only: {"debug": true, ...}
        result["error"] = error
    return result
