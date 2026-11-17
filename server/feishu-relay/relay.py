#!/usr/bin/env python3
"""Grafana -> Feishu (Lark) notification relay.

Why this exists
---------------
Grafana has no native Feishu/Lark contact point, and a Feishu custom-bot webhook
accepts only Feishu's own envelope:

    {"msg_type": "text", "content": {"text": "..."}}

Grafana's webhook contact point sends its own JSON ({"receiver": ..., "alerts":
[...]}), which Feishu rejects. This relay receives the Grafana payload, renders
a readable message and forwards it to one or more Feishu custom bots.

It also replaces the old workflow: the Feishu "BotBuilder" trigger URLs
(https://www.feishu.cn/flow/api/trigger-webhook/...) were shut down by Feishu on
2026-06-30 and now answer HTTP 403 {"服务已下线"}. Custom bots live at
https://open.feishu.cn/open-apis/bot/v2/hook/<token> instead.

Configuration (environment)
---------------------------
FEISHU_WEBHOOK_URL   One or more custom-bot webhook URLs (comma separated).
FEISHU_SIGN_SECRET   Optional per-URL signature secrets ("加签"), same order;
                     use an empty slot for bots without signing.
FEISHU_KEYWORD       Optional per-URL keyword ("自定义关键词"), same order. The
                     message is prefixed with it, as Feishu requires the keyword
                     to appear in the text.
LISTEN_PORT          Default 8080.
MAX_ALERTS_PER_MSG   Default 8; the rest are summarised.

Endpoints
---------
POST /webhook   Grafana contact point target.
GET  /healthz   Liveness probe (200 when at least one bot URL is configured).
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "10"))
MAX_ALERTS_PER_MSG = int(os.environ.get("MAX_ALERTS_PER_MSG", "8"))
# Feishu rejects very long text messages; keep well below the limit.
MAX_TEXT_CHARS = 3000


def _split(value):
    return [part.strip() for part in (value or "").split(",")]


def load_targets():
    """Pair every webhook URL with its optional secret and keyword."""
    urls = [u for u in _split(os.environ.get("FEISHU_WEBHOOK_URL", "")) if u]
    secrets = _split(os.environ.get("FEISHU_SIGN_SECRET", ""))
    keywords = _split(os.environ.get("FEISHU_KEYWORD", ""))

    def pick(values, index):
        return values[index] if index < len(values) else ""

    return [
        {"url": url, "secret": pick(secrets, i), "keyword": pick(keywords, i)}
        for i, url in enumerate(urls)
    ]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _sign(secret):
    """Feishu custom-bot signature: base64(HmacSHA256(key=f"{ts}\\n{secret}", ""))"""
    timestamp = str(int(time.time()))
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return timestamp, base64.b64encode(digest).decode("utf-8")


def _icon(status):
    return "🔥" if status == "firing" else "✅"


def _alert_block(alert):
    status = alert.get("status") or "firing"
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}

    name = labels.get("alertname") or "Alert"
    targets = []
    for key in ("node", "instance", "gpu_index", "job", "severity"):
        if labels.get(key):
            field = "GPU" + labels[key] if key == "gpu_index" else labels[key]
            targets.append(field)
    header = "%s %s%s" % (
        _icon(status),
        name,
        ("（%s）" % " / ".join(targets)) if targets else "",
    )

    lines = [header]
    summary = annotations.get("summary")
    description = annotations.get("description")
    if summary and summary != name:
        lines.append("    %s" % summary)
    if description:
        lines.append("    %s" % description)
    if status == "firing" and alert.get("startsAt"):
        lines.append("    开始: %s" % alert["startsAt"])
    if status != "firing" and alert.get("endsAt") and not alert["endsAt"].startswith("0001"):
        lines.append("    恢复: %s" % alert["endsAt"])
    return "\n".join(lines)


def render_message(payload):
    """Turn a Grafana webhook payload into a Feishu text message."""
    status = payload.get("status") or "firing"
    title = payload.get("title") or ""
    alerts = payload.get("alerts") or []

    headline = "%s %s" % (
        "【告警触发】" if status == "firing" else "【告警恢复】",
        title or ("%d 条告警" % len(alerts)),
    )
    blocks = [headline]

    for alert in alerts[:MAX_ALERTS_PER_MSG]:
        blocks.append(_alert_block(alert))
    remaining = len(alerts) - MAX_ALERTS_PER_MSG
    if remaining > 0:
        blocks.append("… 另有 %d 条告警未展开" % remaining)

    if not alerts:
        # Grafana may send a test notification without an alerts list.
        message = payload.get("message")
        if message:
            blocks.append(str(message))

    text = "\n".join(blocks)
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 12] + "\n…（已截断）"
    return text


def send_to_feishu(target, text):
    """POST one message to one custom bot. Raises RuntimeError on failure."""
    if target["keyword"]:
        text = "%s\n%s" % (target["keyword"], text)

    body = {"msg_type": "text", "content": {"text": text}}
    if target["secret"]:
        timestamp, sign = _sign(target["secret"])
        body["timestamp"] = timestamp
        body["sign"] = sign

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        target["url"],
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail))
    except Exception as exc:  # noqa: BLE001 - surface any transport failure
        raise RuntimeError(str(exc))

    # Feishu answers 200 with {"code":0,...}; a non-zero code is a rejection.
    try:
        parsed = json.loads(raw)
    except ValueError:
        return
    code = parsed.get("code")
    if code not in (0, None):
        raise RuntimeError("feishu code=%s msg=%s" % (code, parsed.get("msg")))


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-relay/1"

    def _respond(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/healthz", "/health"):
            targets = load_targets()
            if not targets:
                self._respond(503, {"ok": False, "reason": "FEISHU_WEBHOOK_URL is empty"})
            else:
                self._respond(200, {"ok": True, "targets": len(targets)})
            return
        self._respond(404, {"ok": False, "reason": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] not in ("/webhook", "/", "/grafana"):
            self._respond(404, {"ok": False, "reason": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            self._respond(400, {"ok": False, "reason": "invalid json: %s" % exc})
            return

        targets = load_targets()
        if not targets:
            # Answer 5xx so Grafana records the notification as failed instead of
            # silently dropping it.
            self._respond(500, {"ok": False, "reason": "FEISHU_WEBHOOK_URL is empty"})
            return

        text = render_message(payload)

        failures = []
        for index, target in enumerate(targets):
            try:
                send_to_feishu(target, text)
            except RuntimeError as exc:
                failures.append("target[%d]: %s" % (index, exc))
                log("feishu send failed %s" % failures[-1])

        if failures:
            self._respond(502, {"ok": False, "reason": "; ".join(failures)})
            return

        log("forwarded %s to %d target(s)" % (payload.get("title") or "-", len(targets)))
        self._respond(200, {"ok": True})

    def log_message(self, fmt, *args):  # keep the journal readable
        log("%s - %s" % (self.address_string(), fmt % args))


def main():
    targets = load_targets()
    if not targets:
        log("warning: FEISHU_WEBHOOK_URL is empty - the relay will reject all alerts")
    else:
        log("relay configured with %d Feishu target(s)" % len(targets))
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log("listening on :%d" % LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
