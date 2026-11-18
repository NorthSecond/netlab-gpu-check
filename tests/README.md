# 测试

一条命令跑完全部回归：

```bash
make test          # 或 ./tests/run-all.sh
```

```
static     16 passed  配置/脚本语法、promtool、embedded 同步、密钥与内网 IP 扫描
relay      27 passed  Grafana -> 飞书 relay 的渲染/加签/关键词/错误/多目标
exporter   25 passed  exporter 行为（mock NVML，无需 GPU）
proxy      17 passed  nginx 前门：匿名被拦、登录放行、公开看板不受影响
install    15 passed  client/install.sh 端到端（容器内假 GPU 节点）
```

单独跑某一套：

```bash
make test-relay
./tests/run-all.sh static proxy     # 任意组合
./tests/run-all.sh --list           # 列出套件
./tests/run-all.sh --no-docker      # 跳过需要 docker 的套件
```

## 环境要求

| 套件 | 需要 |
| --- | --- |
| static | `python3` + `pyyaml`；`promtool` 检查额外需要 `prom/prometheus:v3.11.3` 镜像 |
| relay | `python3`（纯标准库，无需网络，不连真飞书） |
| exporter | `python3` + `prometheus_client`（缺失时自动建 `tests/.venv` 并安装，需要一次网络） |
| proxy | `docker` + `nginx:1.27-alpine` |
| install | `docker` + `alpine:latest`（容器内 `apk add` 需要网络） |

镜像已固定版本，可用 `NGINX_IMAGE` / `ALPINE_IMAGE` / `PYTHON_IMAGE` 覆盖。
宿主已有的 python 可通过 `PYTHON=/path/to/python make test-exporter` 指定。

## 各套件覆盖什么

### `tests/static/checks.sh`

- `remote-install.sh` 是否与 `client/` 分叉（历史 bug：它曾长期装废弃的 `pynvml`）
- provisioning / prometheus / compose 的 YAML+JSON 能否解析
- `client/exporter.py`、`server/feishu-relay/relay.py` 能否编译
- 所有 shell 脚本语法（`bash -n` / `sh -n`）
- `promtool check config`（有镜像时）
- **扫描已跟踪文件里是否出现 webhook token 或内网 IP**（仓库是公开的）
- `server/.env`、`server/targets.d/targets.json` 是否确实被 gitignore

### `tests/relay/relay_test.py`

把 relay 作为**子进程**跑起来，对着 mock 飞书端点验证真实 HTTP 行为：

- 消息渲染（firing / resolved、标签、中文注解、不残留未渲染的模板变量）
- 多条告警只展开前 8 条、其余汇总；超长消息截断
- 加签：`timestamp`/`sign` 与飞书文档算法逐字节一致
- 自定义关键词：消息以关键词开头
- 错误处理：飞书 `code!=0` / HTTP 500 → relay 返回 502（让 Grafana 记为投递失败）；
  未配置地址 → 500，`/healthz` → 503
- 多目标：一成功一失败返回 502 但成功目标确实收到；两个都成功返回 200 且各收 1 条

### `tests/exporter/verify.sh`

用 `tests/exporter/mock/pynvml.py`（可通过 JSON 场景文件控制）替代真实 NVML：

- **旧指标名与标签必须全部保留**（看板依赖；这里用固定清单比对，不依赖 git 历史）
- 自检指标 `gpu_count` / `gpu_up` / `gpu_exporter_last_success_timestamp_seconds` /
  `gpu_exporter_collect_errors_total`
- 进程退出后 series 被移除；GPU 掉卡时 `gpu_up=0` 但 series 保留（曲线不断档）
- 可选指标不支持时只移除该 series，不影响同卡其它指标，也不留陈旧值
- 不支持查询的日志只报一次（不刷 journal）
- SIGTERM 退出码 0；NVML 初始化失败退出码 1（旧版会"装健康"）

### `tests/proxy/proxy_test.sh`

在 `nginx:1.27-alpine` 里加载真实的 `server/proxy/grafana.conf`，上游换成 mock
（带 session cookie 返回 200，否则 401），因此**两条路径都能验证**：

- 匿名访问 `/api/v1/provisioning/*`、`/apis/notifications.alerting.grafana.app/*` → 401
- 登录后同样端点 → 200（不能误伤管理员）
- 公开看板依赖的路径（`/`、`/api/ds/query`、`/apis/dashboard.grafana.app`、
  `/apis/folder.grafana.app`、`/api/folders`、`/api/search`）→ 200
- 内部子请求 `/_grafana_is_authenticated` 外部不可达

### `tests/install/install_test.sh`

在 alpine 容器里造一个假 GPU 节点（stub `nvidia-smi`、stub `systemctl` 会真的启动
exporter、mock `pynvml` 放在 `exporter.py` 旁边以覆盖真实包），然后：

- 正常安装：venv、`requirements.lock`、端口改写、`/metrics` 真有 `gpu_info`
- 幂等重跑：venv 不重建、端口回落默认、不重复创建
- 缺 `nvidia-smi` 时退出码 1 且有可操作提示；未知参数退出码 2

## 约定

- 每个套件以 `tests/common.sh` 的 `finish` 结尾（打印 `NN passed, N failed` 并以
  非 0 退出表示失败），`run-all.sh` 据此汇总。
- 断言用 `check "名字" "$实际" "$期望"`，失败会同时打印期望与实际。
- **不要在测试里连真实飞书、真实 Grafana 或真实 GPU**：relay 用 mock 端点，
  exporter 用 mock NVML，proxy 用 mock 上游，install 用 stub 二进制。
