#!/bin/sh
# Tests for server/proxy/grafana.conf (the nginx front door).
#
# Runs *inside* an nginx:1.27-alpine container (see tests/run-all.sh) because it
# needs the module set of that image. A mock upstream answers /api/user with 200
# when the request carries a session cookie and 401 otherwise, so both the
# "anonymous is blocked" and the "logged-in user is not blocked" paths are
# covered without touching a real Grafana.
set -u
SUITE_NAME="proxy"
TESTS_DIR="${TESTS_DIR:-/tests}"
. "$TESTS_DIR/common.sh"

CONF=/etc/nginx/conf.d/default.conf
[ -f "$CONF" ] || { echo "ERROR: missing $CONF" >&2; exit 2; }
require_cmd nginx
require_cmd curl

WORK=$(mktemp -d)
cleanup() { [ -n "${NGINX_PID:-}" ] && kill "$NGINX_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# ---- mock upstream --------------------------------------------------------
apk add -q python3 >/dev/null 2>&1
cat > "$WORK/mock.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/user"):
            ok = "grafana_session=valid" in (self.headers.get("Cookie") or "")
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b'{"login":"tester"}' if ok else b'{"message":"Unauthorized"}')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"UPSTREAM")

    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"UPSTREAM-POST")

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8899), H).serve_forever()
PY
python3 "$WORK/mock.py" &
sleep 1

# ---- nginx under test: same file, upstream + port rewritten ----------------
sed 's|server grafana:3000;|server 127.0.0.1:8899;|; s|listen 3000;|listen 8080;|' \
    "$CONF" > "$WORK/site.conf"
cat > "$WORK/nginx.conf" <<NG
events {}
http {
  include /etc/nginx/mime.types;
  include $WORK/site.conf;
}
NG
nginx -c "$WORK/nginx.conf" -g 'daemon off;' > "$WORK/nginx.log" 2>&1 &
NGINX_PID=$!
for _ in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:8080/ && break
    sleep 0.25
done

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
AUTH='Cookie: grafana_session=valid'

section "匿名访问：会泄露通知配置的端点必须被拦"
check "匿名 /api/v1/provisioning/contact-points" \
    "$(code http://127.0.0.1:8080/api/v1/provisioning/contact-points)" 401
check "匿名 .../contact-points/export" \
    "$(code http://127.0.0.1:8080/api/v1/provisioning/contact-points/export)" 401
check "匿名 /api/v1/provisioning/policies" \
    "$(code http://127.0.0.1:8080/api/v1/provisioning/policies)" 401
check "匿名 /apis/notifications.../receivers" \
    "$(code http://127.0.0.1:8080/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers)" 401

section "已登录会话必须放行（不能误伤管理员）"
check "登录后 contact-points" \
    "$(code -H "$AUTH" http://127.0.0.1:8080/api/v1/provisioning/contact-points)" 200
check "登录后 policies" \
    "$(code -H "$AUTH" http://127.0.0.1:8080/api/v1/provisioning/policies)" 200
check "登录后 notifications receivers" \
    "$(code -H "$AUTH" http://127.0.0.1:8080/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers)" 200

section "公开看板依赖的路径不能被拦"
check "匿名 GET /" "$(code http://127.0.0.1:8080/)" 200
check "匿名 /api/dashboards/uid/x" "$(code http://127.0.0.1:8080/api/dashboards/uid/x)" 200
check "匿名 /apis/dashboard.grafana.app" \
    "$(code http://127.0.0.1:8080/apis/dashboard.grafana.app/v1beta1/namespaces/default/dashboards/x)" 200
check "匿名 /apis/folder.grafana.app" \
    "$(code http://127.0.0.1:8080/apis/folder.grafana.app/v1/namespaces/default/folders/x)" 200
check "匿名 /api/folders" "$(code http://127.0.0.1:8080/api/folders)" 200
check "匿名 /api/search" "$(code http://127.0.0.1:8080/api/search)" 200
check "匿名 POST /api/ds/query" \
    "$(code -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/api/ds/query)" 200
check "匿名 /api/health" "$(code http://127.0.0.1:8080/api/health)" 200

section "已登录用户的数据查询也不能被拦"
check "登录后 POST /api/ds/query" \
    "$(code -H "$AUTH" -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/api/ds/query)" 200

section "内部子请求不可被外部直接访问"
check "外部 GET /_grafana_is_authenticated" \
    "$(code http://127.0.0.1:8080/_grafana_is_authenticated)" 404

finish
