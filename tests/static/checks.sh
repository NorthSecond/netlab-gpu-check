#!/bin/bash
# Static checks: nothing here starts a service or touches the running stack.
#
# Catches the failure modes that have actually bitten this repo: a stale
# remote-install.sh, an invalid prometheus/compose config, unparseable
# provisioning files, and internal IPs or webhook tokens leaking into a public
# repository.
set -u
SUITE_NAME="static"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/tests/common.sh"

cd "$REPO" || exit 2

section "remote-install.sh 与 client/ 未分叉"
if [ -x "$REPO/scripts/check-embedded.sh" ]; then
    if "$REPO/scripts/check-embedded.sh" >/dev/null 2>&1; then
        ok "embedded copies up to date"
    else
        bad "remote-install.sh is stale" "run scripts/embed-client.sh"
    fi
else
    bad "scripts/check-embedded.sh missing"
fi

section "配置可解析"
if command -v python3 >/dev/null 2>&1; then
    if python3 - "$REPO" <<'PY'
import glob
import json
import sys

import yaml

repo = sys.argv[1]
errors = []
for path in glob.glob("%s/server/grafana/provisioning/**/*.yml" % repo, recursive=True):
    try:
        yaml.safe_load(open(path))
    except Exception as exc:  # noqa: BLE001
        errors.append("%s: %s" % (path, exc))
for path in glob.glob("%s/server/grafana/provisioning/dashboards/*/*.json" % repo):
    try:
        json.load(open(path))
    except Exception as exc:  # noqa: BLE001
        errors.append("%s: %s" % (path, exc))
for path in ("%s/server/prometheus.yml" % repo, "%s/server/docker-compose.yml" % repo):
    try:
        yaml.safe_load(open(path))
    except Exception as exc:  # noqa: BLE001
        errors.append("%s: %s" % (path, exc))
print("\n".join(errors))
sys.exit(1 if errors else 0)
PY
    then
        ok "provisioning / prometheus / compose YAML+JSON 均可解析"
    else
        bad "有配置文件无法解析"
    fi
    python3 -m py_compile client/exporter.py 2>/dev/null \
        && ok "client/exporter.py 编译通过" || bad "client/exporter.py 语法错误"
    python3 -m py_compile server/feishu-relay/relay.py 2>/dev/null \
        && ok "server/feishu-relay/relay.py 编译通过" || bad "relay.py 语法错误"
else
    skip "python3 不可用，跳过解析检查"
fi

section "shell 语法"
for f in client/install.sh remote-install.sh scripts/*.sh tests/run-all.sh tests/static/checks.sh; do
    [ -f "$f" ] || continue
    bash -n "$f" && ok "$f" || bad "$f syntax"
done
sh -n tests/proxy/proxy_test.sh && ok "tests/proxy/proxy_test.sh" || bad "proxy_test.sh syntax"
sh -n tests/install/install_test.sh && ok "tests/install/install_test.sh" || bad "install_test.sh syntax"

section "prometheus 配置（需要 docker，可选）"
if command -v docker >/dev/null 2>&1 && docker image inspect prom/prometheus:v3.11.3 >/dev/null 2>&1; then
    out=$(docker run --rm \
        -v "$REPO/server/prometheus.yml:/tmp/p.yml:ro" \
        -v "$REPO/server/targets.d:/etc/prometheus/targets:ro" \
        --entrypoint /bin/promtool prom/prometheus:v3.11.3 check config /tmp/p.yml 2>&1)
    case "$out" in
        *SUCCESS*) ok "promtool check config" ;;
        *) bad "promtool 校验失败" "$(echo "$out" | tail -2)" ;;
    esac
else
    skip "prom/prometheus:v3.11.3 镜像不可用，跳过 promtool"
fi

section "仓库内不得出现凭据或内网地址"
SECRET_PATTERN='trigger-webhook/[a-f0-9]{16,}|open-apis/bot/v2/hook/[A-Za-z0-9-]{10,}|172\.18\.[0-9]+\.[0-9]+'
HITS=$(git ls-files -z 2>/dev/null | xargs -0 grep -lnE "$SECRET_PATTERN" 2>/dev/null)
if [ -z "$HITS" ]; then
    ok "无 webhook token / 内网 IP"
else
    bad "发现疑似敏感内容" "$(echo "$HITS" | tr '\n' ' ')"
fi

section "不应被提交的运行时文件"
for f in server/.env server/targets.d/targets.json; do
    if [ -e "$f" ]; then
        if git check-ignore -q "$f"; then ok "$f 已被 gitignore"; else bad "$f 未被忽略"; fi
    else
        skip "$f 不存在（未部署）"
    fi
done

finish
