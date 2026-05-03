# GPU Cluster Monitor

极简设计：Server 端 Docker Compose，Client 端 systemd + Python venv。

## 0. 首次配置（脱敏后必做）

仓库中真实的运行时配置（包含内网 IP、Grafana 凭据等）已通过 `.gitignore`
排除，只提交了 `*.example` 模板。首次部署时需要复制并按需修改：

```bash
# 服务端：抓取目标 + Grafana 凭据
cp server/targets.json.example       server/targets.json
cp server/.env.example                server/.env

# 用编辑器打开后填写真实值
$EDITOR server/targets.json   # 写入要监控的 GPU 节点
$EDITOR server/.env           # 修改 Grafana 管理员密码等
```

| 文件 | 作用 | 是否提交 |
| --- | --- | --- |
| `server/targets.json` | Prometheus file_sd 抓取目标列表 | ❌ 已 ignore |
| `server/targets.json.example` | 抓取目标模板 | ✅ |
| `server/.env` | Grafana 用户名/密码、端口覆盖 | ❌ 已 ignore |
| `server/.env.example` | 环境变量模板 | ✅ |
| `server/docker-compose.yml` | Compose 服务定义（读取 `.env`） | ✅ |
| `server/prometheus.yml` | Prometheus 主配置（无敏感信息） | ✅ |

## 1. 启动 Server（当前机器）

```bash
cd server
docker compose up -d
```

- Prometheus: <http://localhost:9090>
- Grafana:    <http://localhost:3000> （凭据来自 `server/.env`）

如果 9090 / 3000 端口冲突，可以在 `server/.env` 中设置 `PROMETHEUS_PORT` /
`GRAFANA_PORT` 覆盖。

## 2. 部署 Client（GPU 节点）

把 `client/` 目录复制到目标机器：

```bash
scp -r client/ user@gpu01:/tmp/
ssh user@gpu01
sudo bash /tmp/client/install.sh
```

安装完成后 exporter 在 9745 端口暴露指标：

```bash
curl http://localhost:9745/metrics
```

## 3. 添加新机器到监控

编辑 `server/targets.json`，加入新节点，保存后 Prometheus 自动热加载（1 分钟内生效）：

```json
[
  {
    "targets": ["gpu01.lab:9745"],
    "labels": {"node": "gpu01"}
  },
  {
    "targets": ["gpu02.lab:9745"],
    "labels": {"node": "gpu02"}
  }
]
```

> 不要把真实节点写进 `targets.json.example`——那是给新克隆者看的模板。

## 4. 查看看板

浏览器打开 `http://<server-ip>:3000`，看板 “GPU Cluster Overview” 已自动加载。

## 5. Client 维护

```bash
# 查看状态
systemctl status gpu-exporter

# 查看日志
journalctl -u gpu-exporter -f

# 卸载
sudo systemctl stop gpu-exporter
sudo systemctl disable gpu-exporter
sudo rm -rf /opt/gpu-exporter /etc/systemd/system/gpu-exporter.service
sudo systemctl daemon-reload
```
