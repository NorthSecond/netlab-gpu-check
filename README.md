# GPU Cluster Monitor

极简设计：Server 端 Docker Compose（Prometheus + Grafana），Client 端 systemd + Python venv。

```
client/    exporter.py + install.sh + requirements.txt + gpu-exporter.service
server/    docker-compose.yml + prometheus.yml + targets.d/ + grafana/provisioning/
scripts/   embed-client.sh（生成 remote-install.sh）/ check-embedded.sh（校验未过期）
docs/      legacy/（已废弃配置存档）、optional-alerts.md（未启用的备选告警）
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

## 1. 配置 Webhook 通知（告警推送）

Webhook URL 通过 `server/.env` 配置，敏感信息不入 git：

```bash
cp server/.env.example server/.env
$EDITOR server/.env   # 填写 WEBHOOK_URL / WEBHOOK_URL_2
```

`.env` 示例：

```bash
WEBHOOK_URL=https://www.feishu.cn/flow/api/trigger-webhook/xxxxxx
WEBHOOK_URL_2=                       # 可选，第二个接收端
GF_SERVER_ROOT_URL=http://<server-ip>:3000   # 告警通知里的 Source 链接用这个
```

然后重启 Grafana 生效：

```bash
cd server
docker compose restart grafana
```

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

### ⚠️ 访问面提示

`GF_AUTH_ANONYMOUS_ENABLED=true`（匿名可看 Public 看板）会让**未登录**的请求也能
读到一部分 API，其中 `GET /api/v1/provisioning/contact-points` 会返回告警
contact point 的完整配置，**包括 Webhook URL 明文**（等于把可发消息的 token 暴露出去）。
因此：

- 3000 端口只应暴露在内网；需要对外时，在前面加一层带鉴权的反向代理
- Webhook URL 一旦泄露，请在飞书/钉钉侧轮换
- 如需彻底消除，可关闭匿名并改用 Grafana 的 Public Dashboard 分享功能

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

## 6. Webhook 通知格式说明

Grafana 的 Webhook 通知格式与 Prometheus/Alertmanager 不同，示例：

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
