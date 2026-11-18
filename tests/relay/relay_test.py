#!/usr/bin/env python3
"""Tests for server/feishu-relay/relay.py.

Runs the relay as a subprocess against a mock Feishu endpoint, so it exercises
the real HTTP behaviour: message rendering, truncation, the optional 加签
signature and keyword, error propagation (a Feishu rejection must surface as
5xx so Grafana marks the notification failed) and multi-target fan-out.

No network access and no Feishu credentials required.
"""

import base64
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RELAY = os.path.join(REPO, "server", "feishu-relay", "relay.py")

PASS = 0
FAIL = 0
SKIP = 0
RELAY_PORT = 0
FEISHU_PORT = 0
received = []
FEISHU_MODE = {"mode": "ok"}


def ok(name):
    global PASS
    print("  PASS  %s" % name, flush=True)
    PASS += 1


def bad(name, detail=""):
    global FAIL
    print("  FAIL  %s  %s" % (name, detail), flush=True)
    FAIL += 1


def check(name, condition, detail=""):
    ok(name) if condition else bad(name, detail)


def section(title):
    print("\n== %s " % title, flush=True)


class MockFeishu(BaseHTTPRequestHandler):
    """Stands in for https://open.feishu.cn/open-apis/bot/v2/hook/<token>."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            received.append(json.loads(body))
        except ValueError:
            received.append({"__raw__": body.decode("utf-8", "replace")})

        if FEISHU_MODE["mode"] == "reject":
            payload = json.dumps({"code": 19021, "msg": "sign match fail"}).encode()
            self.send_response(200)
        elif FEISHU_MODE["mode"] == "http500":
            payload = b"boom"
            self.send_response(500)
        else:
            payload = json.dumps({"code": 0, "msg": "success"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def is_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(start):
    for port in range(start, start + 60):
        if is_free(port):
            return port
    raise RuntimeError("no free port near %d" % start)


def start_relay(**env_overrides):
    env = dict(os.environ)
    env.update({
        "LISTEN_PORT": str(RELAY_PORT),
        "FEISHU_WEBHOOK_URL": "http://127.0.0.1:%d/open-apis/bot/v2/hook/test" % FEISHU_PORT,
        "HTTP_TIMEOUT": "5",
    })
    env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, RELAY],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", RELAY_PORT), timeout=0.2):
                return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("relay exited early: %s" % proc.stdout.read())
            time.sleep(0.05)
    raise RuntimeError("relay did not start")


def stop(proc):
    proc.send_signal(signal.SIGINT)
    try:
        out = proc.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    return out or ""


def post(payload, path="/webhook"):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (RELAY_PORT, path),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


GRAFANA_FIRING = {
    "receiver": "gpu-webhook",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {"alertname": "GPU Node Down", "instance": "10.0.0.10:9745",
                   "job": "gpu-nodes", "node": "S12", "severity": "critical"},
        "annotations": {"summary": "GPU 节点离线: 10.0.0.10:9745",
                        "description": "节点 10.0.0.10:9745 已超过 1 分钟无法连接，GPU 监控中断。"},
        "startsAt": "2026-11-17T15:18:15Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "19fab50",
    }],
    "groupLabels": {},
    "commonLabels": {"alertname": "GPU Node Down"},
    "commonAnnotations": {},
    "externalURL": "http://grafana:3000",
    "version": "1",
    "title": "[FIRING:1] GPU Node Down",
    "state": "alerting",
    "message": "GPU 节点离线",
}


def clone(obj):
    return json.loads(json.dumps(obj))


def main():
    global RELAY_PORT, FEISHU_PORT
    if not os.path.isfile(RELAY):
        print("ERROR: relay not found at %s" % RELAY, file=sys.stderr)
        return 2

    RELAY_PORT = int(os.environ.get("RELAY_TEST_PORT") or pick_port(18800))
    FEISHU_PORT = pick_port(RELAY_PORT + 1)
    if not (is_free(RELAY_PORT) and is_free(FEISHU_PORT)):
        print("ERROR: ports %d/%d busy" % (RELAY_PORT, FEISHU_PORT), file=sys.stderr)
        return 2

    feishu = ThreadingHTTPServer(("127.0.0.1", FEISHU_PORT), MockFeishu)
    threading.Thread(target=feishu.serve_forever, daemon=True).start()

    # ------------------------------------------------------------ rendering
    section("消息渲染")
    proc = start_relay()
    code, body = post(GRAFANA_FIRING)
    check("firing 转发返回 200", code == 200, str(body))
    check("飞书收到 1 条", len(received) == 1, str(len(received)))
    msg = received[0]
    check("msg_type 为 text", msg.get("msg_type") == "text", str(list(msg)))
    text = msg["content"]["text"]
    check("含【告警触发】", "【告警触发】" in text, text[:60])
    check("含 alertname", "GPU Node Down" in text)
    check("含 node 标签", "S12" in text)
    check("含中文 description", "GPU 监控中断" in text)
    check("无未渲染模板变量", "{{" not in text and "$labels" not in text)

    received.clear()
    resolved = clone(GRAFANA_FIRING)
    resolved["status"] = "resolved"
    resolved["alerts"][0]["status"] = "resolved"
    resolved["alerts"][0]["endsAt"] = "2026-11-17T15:19:15Z"
    resolved["title"] = "[RESOLVED] GPU Node Down"
    post(resolved)
    text = received[0]["content"]["text"]
    check("resolved 用恢复文案", "【告警恢复】" in text and "✅" in text, text[:60])

    section("多条与超长消息")
    received.clear()
    many = clone(GRAFANA_FIRING)
    many["alerts"] = []
    for i in range(20):
        alert = clone(GRAFANA_FIRING["alerts"][0])
        alert["labels"]["node"] = "S%d" % i
        many["alerts"].append(alert)
    post(many)
    text = received[0]["content"]["text"]
    check("只展开前 8 条", text.count("🔥") == 8, str(text.count("🔥")))
    check("其余被汇总", "另有 12 条" in text, text[-60:])

    received.clear()
    huge = clone(GRAFANA_FIRING)
    huge["alerts"][0]["annotations"]["description"] = "x" * 5000
    post(huge)
    text = received[0]["content"]["text"]
    check("超长被截断", len(text) <= 3000 and text.endswith("（已截断）"), str(len(text)))
    stop(proc)

    # -------------------------------------------------------------- signing
    section("加签（机器人安全设置）")
    secret = "SIGN_SECRET_ABC"
    received.clear()
    proc = start_relay(FEISHU_SIGN_SECRET=secret)
    post(GRAFANA_FIRING)
    msg = received[0]
    ts, sign = msg.get("timestamp"), msg.get("sign")
    expected = base64.b64encode(
        hmac.new(("%s\n%s" % (ts, secret)).encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    check("带 timestamp", bool(ts), str(ts))
    check("签名与飞书文档算法一致", sign == expected, "%s vs %s" % (sign, expected))
    stop(proc)

    section("自定义关键词")
    received.clear()
    proc = start_relay(FEISHU_KEYWORD="GPU监控")
    post(GRAFANA_FIRING)
    check("消息以关键词开头", received[0]["content"]["text"].startswith("GPU监控"))
    stop(proc)

    # --------------------------------------------------------------- errors
    section("错误处理")
    FEISHU_MODE["mode"] = "reject"
    proc = start_relay()
    code, body = post(GRAFANA_FIRING)
    check("飞书 code!=0 -> relay 502", code == 502, "%s %s" % (code, body))
    check("拒绝原因透传", "sign match fail" in json.dumps(body, ensure_ascii=False), str(body))
    stop(proc)

    FEISHU_MODE["mode"] = "http500"
    proc = start_relay()
    code, _ = post(GRAFANA_FIRING)
    check("飞书 HTTP 500 -> relay 502", code == 502, str(code))
    stop(proc)
    FEISHU_MODE["mode"] = "ok"

    proc = start_relay()
    code, _ = post(GRAFANA_FIRING, path="/nope")
    check("未知路径 404", code == 404, str(code))
    code, _ = post(GRAFANA_FIRING)
    check("正常路径不受影响", code == 200, str(code))
    stop(proc)

    section("未配置机器人地址")
    proc = start_relay(FEISHU_WEBHOOK_URL="")
    code, _ = post(GRAFANA_FIRING)
    check("POST 返回 500（Grafana 会记为失败）", code == 500, str(code))
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/healthz" % RELAY_PORT, timeout=5
        ) as resp:
            hcode = resp.status
    except urllib.error.HTTPError as exc:
        hcode = exc.code
    check("/healthz 返回 503", hcode == 503, str(hcode))
    stop(proc)

    section("多目标（两个群）")
    received.clear()
    good = "http://127.0.0.1:%d/hook-a" % FEISHU_PORT
    dead = "http://127.0.0.1:1/hook-b"
    proc = start_relay(FEISHU_WEBHOOK_URL="%s,%s" % (good, dead))
    code, _ = post(GRAFANA_FIRING)
    check("一成功一失败 -> 502", code == 502, str(code))
    check("成功目标确实收到", len(received) == 1, str(len(received)))
    stop(proc)

    received.clear()
    proc = start_relay(FEISHU_WEBHOOK_URL="%s,%s" % (good, good))
    code, _ = post(GRAFANA_FIRING)
    check("两目标都成功 -> 200", code == 200, str(code))
    check("各收到 1 条", len(received) == 2, str(len(received)))
    out = stop(proc)
    check("启动日志报告目标数", "2 Feishu target(s)" in out, out[:160])

    feishu.shutdown()
    print("\nrelay: %d passed, %d failed, %d skipped" % (PASS, FAIL, SKIP))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
