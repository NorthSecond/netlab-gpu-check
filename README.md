# GPU Cluster Monitor

极简设计：Server 端 Docker Compose（Prometheus + Grafana），Client 端 systemd + Python venv。

```
client/    exporter.py + install.sh + requirements.txt + gpu-exporter.service
server/    docker-compose.yml + prometheus.yml + targets.d/ + grafana/provisioning/
           proxy/grafana.conf       Grafana 对外入口（nginx，拦截泄漏端点）
           feishu-relay/relay.py    Grafana 告警 -> 飞书自定义机器人的格式转换
scripts/   embed-client.sh（生成 remote-install.sh）/ check-embedded.sh（校验未过期）
tests/     全部回归测试，`make test` 一条命令跑完（见 tests/README.md）
docs/      legacy/（已废弃配置存档）、optional-alerts.md（未启用的备选告警）
```

告警链路：

```
Prometheus ──查询──> Grafana(告警规则) ──webhook──> feishu-relay ──> 飞书群自定义机器人
                                   └──> 浏览器访问统一经 grafana-proxy(:3000)
```

## 0. 首次配置（脱敏后必做）

仓库中真实的运行时配置（包含内网 IP、Grafana 凭据、Webhook）已通过 `.gitignore`
排除，只提交了 `*.example` 模板。首次部署时需要复制并按需修改：

```bash
# 服务端：抓取目标 + Grafana 凭据
mkdir -p server/targets.d
cp server/targets.json.example server/targets.d/targets.json
cp server/.env.example            server/.env

# 用编辑器打开后填写真实值
$EDITOR server/targets.d/targets.json   # 写入要监控的 GPU 节点
$EDITOR server/.env                     # 修改 Grafana 管理员密码、Webhook 等
```

| 文件 | 作用 | 是否提交 |
| --- | --- | --- |
| `server/targets.d/targets.json` | Prometheus file_sd 抓取目标列表 | ❌ 已 ignore |
| `server/targets.json.example` | 抓取目标模板 | ✅ |
| `server/.env` | Grafana 用户名/密码、Webhook、端口覆盖 | ❌ 已 ignore |
| `server/.env.example` | 环境变量模板 | ✅ |
| `server/docker-compose.yml` | Compose 服务定义（读取 `.env`，镜像版本已固定） | ✅ |
| `server/prometheus.yml` | Prometheus 主配置（无敏感信息） | ✅ |

> **容器密码只在首次初始化时生效**：`GF_SECURITY_ADMIN_PASSWORD` 只在 Grafana
> 数据库第一次初始化时写入。之后改 `.env` 里的密码不会生效，请用
> `docker compose exec grafana grafana cli admin reset-admin-password <新密码>`。

## 1. 配置飞书告警推送

> ⚠️ **2026-06-30 起飞书旧接口已下线**：`https://www.feishu.cn/flow/api/trigger-webhook/...`
> （飞书机器人助手 / BotBuilder 触发器）现在一律返回 **HTTP 403 `服务已下线`**。
> 如果告警突然推不出去、Grafana 日志出现
> `Notify for alerts failed ... webhook response status 403 Forbidden`，就是这个原因，
> 与本项目的配置无关。

现在的链路是 **Grafana → feishu-relay → 飞书群自定义机器人**：

1. 在目标群里添加自定义机器人：**群设置 → 群机器人 → 添加机器人 → 自定义机器人**，
   复制 Webhook 地址（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx`）
2. 填进 `server/.env`：

```bash
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx

# 多个群（逗号分隔，会分别投递）
# FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/aaa,https://open.feishu.cn/open-apis/bot/v2/hook/bbb

# 机器人安全设置选「签名校验」时填密钥（与上面的 URL 按顺序对应）
FEISHU_SIGN_SECRET=

# 机器人安全设置选「自定义关键词」时填关键词（与上面的 URL 按顺序对应）
FEISHU_KEYWORD=
```

3. 生效并自测：

```bash
cd server
docker compose up -d feishu-relay

# 健康检查：configured 为 true 表示机器人地址已读到
docker exec feishu-relay python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"

# 手工发一条测试消息进群（不用等真告警）
docker exec feishu-relay python -c "
import json, urllib.request
body = {'status': 'firing', 'title': '[测试] 连通性检查',
        'alerts': [{'status': 'firing', 'labels': {'alertname': 'RelayTest'},
                    'annotations': {'description': '这是手工测试消息，可忽略'}}]}
urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/webhook',
    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}))
print('sent')"

# 没收到消息就看 relay 日志，里面会原样打印飞书的拒绝原因
docker logs --tail 20 feishu-relay
```

为什么需要 relay：飞书自定义机器人只接受

```json
{"msg_type": "text", "content": {"text": "..."}}
```

而 Grafana 的 webhook 通道只发自己的 JSON（`{"receiver":..., "alerts":[...]}`），
且 Grafana **没有原生飞书 contact point 类型**。`feishu-relay` 负责这两者之间的转换，
并同时支持加签、关键词、多群分发。

## 2. 启动 Server（当前机器）

```bash
cd server
docker compose up -d
```

- Prometheus: <http://<server-ip>:9090>
- Grafana:    <http://<server-ip>:3000> （凭据来自 `server/.env`）

如果 9090 / 3000 端口冲突，可以在 `server/.env` 中设置 `PROMETHEUS_PORT` /
`GRAFANA_PORT` 覆盖。

镜像版本已固定（`prom/prometheus:v3.11.3`、`grafana/grafana:13.0.1`），
避免重启时被 `latest` 静默升级。升级时改 tag 后 `docker compose up -d`。

### 访问面与匿名访问

`GF_AUTH_ANONYMOUS_ENABLED=true`，这样 "GPU Cluster (Public)" 看板不用登录也能看。
代价是：Grafana 的 Viewer 角色（包括匿名访客）**被设计成可以读 contact points**，
而 contact point 里原来就放着飞书 webhook 地址明文。现在做了两层防护：

1. **数据层**：contact point 里不再有飞书地址，只指向内部 relay
   （`http://feishu-relay:8080/webhook`），真正的 token 只在 `server/.env` 里
2. **接入层**：`grafana-proxy`（nginx）把
   `/api/v1/provisioning/*`、`/api/v1/notifications/*`、
   `/apis/notifications.alerting.grafana.app/*` 收敛为"必须已登录"，
   匿名访问返回 401；其余路径（看板、`/api/ds/query`、`/apis/dashboard.grafana.app`）
   照常放行

因此 3000 端口现在是 nginx，Grafana 本身**不再监听 0.0.0.0**，只在
`127.0.0.1:3001` 留了一个应急直连口（`ssh -L 3001:127.0.0.1:3001 <server>` 后用
<http://127.0.0.1:3001> 直达，绕过代理，用于排查代理问题）。

如果哪天需要把 Grafana 完全关到登录之后，把 `.env` 里的
`GF_AUTH_ANONYMOUS_ENABLED` 设为 `false` 即可（Public 看板届时需要登录）。

## 3. 部署 Client（GPU 节点）

方式 A：复制目录

```bash
scp -r client/ user@gpu01:/tmp/
ssh user@gpu01
sudo bash /tmp/client/install.sh            # 默认监听 :9745
sudo bash /tmp/client/install.sh --port 9800
```

方式 B：单文件（由 `scripts/embed-client.sh` 从 `client/` 生成，内容与目录一致）

```bash
scp remote-install.sh user@gpu01:/tmp/
ssh user@gpu01 'sudo bash /tmp/remote-install.sh'
```

`install.sh` 会做前置检查（python3 / venv / nvidia-smi）、装 `requirements.txt`
里锁定的依赖、安装 systemd 服务，并在结束时**校验 `/metrics` 真的上报了
`gpu_info`**，失败就打印 `journalctl` 尾部并以非 0 退出。

> 修改 `client/` 下任何文件后，记得运行 `./scripts/embed-client.sh` 重新生成
> `remote-install.sh`；`./scripts/check-embedded.sh` 会检查两者是否已分叉。

验证：

```bash
curl http://localhost:9745/metrics | grep gpu_info
systemctl status gpu-exporter
journalctl -u gpu-exporter -f
```

## 4. 添加新机器到监控

在 `server/targets.d/` 里改 `targets.json`（或新增任意 `*.json`），保存后
Prometheus 会在 1 分钟内热加载：

```json
[
  {
    "targets": ["gpu01.lab:9745"],
    "labels": {"node": "gpu01"}
  },
  {
    "targets": ["10.0.0.10:9745"],
    "labels": {"node": "gpu03"}
  }
]
```

之所以挂载**目录**而不是单个文件：`vim`、`sed -i`、`ansible copy` 这类编辑都会
替换文件 inode，单文件 bind mount 下容器会一直读到旧内容（静默失效）。目录挂载
没有这个问题，加节点不需要重启容器。

> 不要把真实节点写进 `targets.json.example`——那是给新克隆者看的模板。

## 5. 查看看板与告警

浏览器打开 `http://<server-ip>:3000`：

- 看板 "GPU Cluster Overview"（Admin 目录）与 "GPU Cluster (Public)"（Public 目录）自动加载
- 告警规则在 **Alerting → Alert rules**
- 通知渠道在 **Alerting → Contact points**

## 6. 通知格式说明

**Grafana → feishu-relay**（Grafana 的 webhook 载荷，与 Prometheus/Alertmanager 不同）：

```json
{
  "receiver": "gpu-webhook",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "GPU Node Down",
        "instance": "gpu01.lab:9745",
        "severity": "critical"
      },
      "annotations": {
        "summary": "GPU 节点离线: gpu01.lab:9745",
        "description": "节点 gpu01.lab:9745 已超过 1 分钟无法连接，GPU 监控中断。"
      },
      "startsAt": "2024-01-01T00:00:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "valueString": "[ var='B' labels={instance=gpu01.lab:9745} value=0 ]",
      "generatorURL": "http://grafana:3000/alerting/grafana/gpu-node-down/view",
      "fingerprint": "..."
    }
  ],
  "groupLabels": {},
  "commonLabels": {},
  "commonAnnotations": {},
  "externalURL": "http://grafana:3000",
  "version": "1",
  "orgId": 1,
  "title": "[FIRING:1] GPU Node Down",
  "state": "alerting",
  "message": "..."
}
```

**feishu-relay → 飞书**：relay 把上面的载荷渲染成飞书自定义机器人的信封：

```json
{
  "msg_type": "text",
  "content": {
    "text": "【告警触发】 [FIRING:1] GPU Node Down\n🔥 GPU Node Down（S12 / 10.0.0.10:9745 / gpu-nodes / critical）\n    GPU 节点离线: 10.0.0.10:9745\n    节点 ... 已超过 1 分钟无法连接，GPU 监控中断。\n    开始: 2026-11-17T15:18:15Z"
  },
  "timestamp": "...",
  "sign": "..."
}
```

- 每次最多展开 8 条告警，其余汇总成"另有 N 条告警未展开"
- 超长消息截断到 3000 字符（飞书对文本长度有限制）
- `timestamp`/`sign` 仅在配置了 `FEISHU_SIGN_SECRET` 时出现
- 需要改文案/排版，直接改 `server/feishu-relay/relay.py` 里的 `render_message()`

## 7. 告警规则

预置规则位于 `server/grafana/provisioning/alerting/rules.yml`：

| 告警名称 | 触发条件 | 严重程度 |
| --- | --- | --- |
| GPU Node Down | 节点无法抓取（`up < 1` 或指标整体缺失） | critical |
| GPU Info Missing | 节点不再上报 `gpu_info` | warning |

两条规则都使用 `noDataState: Alerting`，用于兜住「指标整体消失」的情况——这是
掉卡/失联能被通知到的前提，**不要改成 `NoData`**。

`docs/optional-alerts.md` 里还有几条**未启用**的备选规则（exporter 采集停滞、
单卡从 NVML 消失、温度过高、监控自身心跳），按需合并进 `rules.yml`。

## 8. Client 维护

```bash
# 升级（重跑 install.sh 即可，幂等：不会重建 venv）
sudo bash install.sh
cat /opt/gpu-exporter/requirements.lock   # 实际安装的版本

# 查看状态 / 日志
systemctl status gpu-exporter
journalctl -u gpu-exporter -f

# 卸载
sudo systemctl stop gpu-exporter
sudo systemctl disable gpu-exporter
sudo rm -rf /opt/gpu-exporter /etc/systemd/system/gpu-exporter.service
sudo systemctl daemon-reload
```

### exporter 上报的指标

| 指标 | 说明 |
| --- | --- |
| `gpu_info{gpu_index,uuid,name,memory_total}` | GPU 静态信息 |
| `gpu_up{gpu_index}` | 该卡本轮是否被 NVML 看到（1/0），用于判断掉卡 |
| `gpu_utilization_percent` / `gpu_memory_utilization_percent` | 利用率 |
| `gpu_memory_{used,free,total}_bytes` | 显存 |
| `gpu_temperature_celsius` / `gpu_power_draw_watts` / `gpu_fan_speed_percent` | 温度/功耗/风扇（不支持的卡上该 series 不出现） |
| `gpu_clock_{sm,memory}_mhz` | 频率 |
| `gpu_process_info{gpu_index,pid,container,command}` / `gpu_process_memory_bytes{...}` | 进程（LXC 名从 cgroup 解析）；compute 与 graphics 进程都统计 |
| `gpu_count` | NVML 当前报告的总卡数 |
| `gpu_exporter_last_success_timestamp_seconds` | 最近一次采集成功时间戳 |
| `gpu_exporter_collect_errors_total` | 采集失败次数 |

采集周期 5s（Prometheus 抓取 20s），比抓取更频繁可避免每次抓到同一份旧快照。
exporter 在 NVML 初始化失败时会**非 0 退出**，由 systemd 重试，而不是假装健康地
返回空指标。

## 9. 已废弃的配置

`docs/legacy/` 下是迁移到 Grafana Unified Alerting 之前的东西，**不会被任何组件加载**：

- `docs/legacy/prometheus-gpu-alerts.yml`：旧的 Prometheus 告警规则
  （`prometheus.yml` 已没有 `rule_files`，改了也没用）
- `docs/legacy/alertmanager.yml`：compose 里没有 alertmanager 服务

留着只为考古；要恢复旧方案请自行把服务加回 compose。

## 10. 故障排查

### 收不到飞书通知

```bash
# 1) 告警有没有触发？（Grafana 视角）
docker logs grafana --since 30m 2>&1 | grep -E "Sending alerts|Notify for alerts failed"

# 2) 投递失败的原因（relay 视角，会原样带上飞书的返回）
docker logs --tail 50 feishu-relay
```

按日志对症：

| 日志特征 | 原因 | 处理 |
| --- | --- | --- |
| `webhook response status 403 Forbidden` + 飞书返回 `服务已下线` | 还在用飞书 BotBuilder 触发器（2026-06-30 已下线） | 换成群自定义机器人，按第 1 节迁移 |
| relay 返回 `500 ... FEISHU_WEBHOOK_URL is empty` | `.env` 没填机器人地址 | 填 `FEISHU_WEBHOOK_URL` 后 `docker compose up -d feishu-relay` |
| relay 返回 `502 ... feishu code=19021 sign match fail` | 机器人开了「签名校验」但密钥不对/没配 | 填 `FEISHU_SIGN_SECRET`（与 URL 顺序对应） |
| relay 返回 `502 ... feishu code=19024` 之类 | 消息里缺少「自定义关键词」 | 填 `FEISHU_KEYWORD` |
| relay 返回 `502 ... connection refused/timeout` | relay 出不了网 | 检查服务器到 `open.feishu.cn:443` 的出网/DNS |
| `Sending alerts` 都没有 | 规则没触发或 Prometheus 没数据 | 查 `server/targets.d/`、`curl :9745/metrics` |

> 顺带一个容易踩的点：Grafana 的文件化 provisioning **不会删除**从 yaml 里移除的
> contact point。删掉一个 receiver 后它仍会继续被投递（若它指向失效地址，整条通知都会
> 判失败）。正确的删除方式是在 `contact-points.yml` 里加 `deleteContactPoints:`
> 指令并重启，生效后再把该指令注释掉（本仓库已经这么清掉过两个历史遗留 receiver）。

### Grafana 打不开 / 登录后告警页面异常

对外入口是 `grafana-proxy`，Grafana 本体只在 `127.0.0.1:3001`。排查时先直连应急通道：

```bash
ssh -L 3001:127.0.0.1:3001 <server>     # 本地浏览器打开 http://127.0.0.1:3001
docker logs grafana-proxy               # nginx 侧错误
```

如果确认是代理的问题，把 `docker-compose.yml` 里 grafana 的 `ports` 改回
`"${GRAFANA_PORT:-3000}:3000"` 并 `docker compose up -d grafana`，
再 `docker compose stop grafana-proxy` 即可回退。

## 11. 测试

改动本项目后（尤其是 exporter、relay、代理、provisioning）跑一次：

```bash
make test
```

```
static     16 passed   配置/脚本语法、promtool、embedded 同步、密钥与内网 IP 扫描
relay      27 passed   Grafana -> 飞书 relay 的渲染/加签/关键词/错误/多目标
exporter   25 passed   exporter 行为（mock NVML，不需要 GPU）
proxy      17 passed   nginx 前门：匿名被拦、登录放行、公开看板不受影响
install    15 passed   client/install.sh 端到端（容器内假 GPU 节点）
```

全部跑在 mock/容器里，不会连真实飞书、真实 Grafana 或真实 GPU。详见 `tests/README.md`。
