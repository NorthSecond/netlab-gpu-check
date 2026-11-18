# GPU 集群监控增强 — 实施计划（已定稿）

> 状态：**决策已确认，可开工**。
> 基线（线上实测）：8 节点 / 64 卡、1111 series、301MB / 30 天、抓取耗时 10ms、每节点 exporter 常驻几十 MB。

## 0. 决策记录

| # | 决策项 | 结论 |
| --- | --- | --- |
| 1 | Xid 检测 | **纳入 P1**。读 `/dev/kmsg` 内核日志，覆盖全部 64 卡（含 GeForce） |
| 2 | node_exporter 安装方式 | **用 Ubuntu 发行版包** `prometheus-node-exporter`（节点全是 Ubuntu） |
| 3 | 身份口径 | **以 LXC 容器名为唯一身份**。容器内用户名统一是 `ubuntu`，无区分度 → **不加 `user` 标签**，聚合指标用 `container` |
| 4 | 主机温度 hwmon | **暂不采集** |
| 5 | 记录规则 | **引入**（会启用 `prometheus.yml` 的 `rule_files`） |
| 6 | 告警链路 | **Grafana 告警规则 → contact point `gpu-webhook` → feishu-relay → 飞书** |
| 7 | 执行顺序 | P0 → P1 → P2 → P3 → P4 → P5（每阶段独立 commit + 独立回滚） |

### 明确不做

| 不做 | 原因 |
| --- | --- |
| DCGM / `dcgm-exporter` 常驻 | 40/64 是 GeForce，DCGM 对其仅"有限功能"；无 NVLink；Xid 用 DCGM 会丢事件（见 §9） |
| NVLink / NVSwitch 遥测 | 集群无 NVLink |
| 能耗账单（kWh / 成本） | 暂不需要 |
| 空闲浪费报表（占卡不用排行） | 暂不做，只保留「低利用 + 高显存」单点告警 |
| 主机温度 / hwmon | 暂不需要 |
| 全系统进程 top-N（非 GPU 进程） | `/proc` 全扫成本高，列为 P6 备选 |

## 1. 硬件画像

| 节点 | 卡型 | 数量 | 显存 | ECC 可用 |
| --- | --- | --- | --- | --- |
| S12 | RTX PRO 6000 Blackwell | 8 | 96GB | ✅ |
| S53 / S750 / S98 | RTX 4090 | 各 8 | 24GB | ❌ |
| S38 / S57 | RTX 3090 | 各 8 | 24GB | ❌ |
| S650 | RTX A6000 | 8 | 48GB | ✅ |
| S54 | RTX A6000 ×2 + RTX 5090 ×6 | 8 | 32 / 48GB | 部分 |

异构混布 → 全局 `avg()` 无意义，看板必须支持**按卡型分组**。

## 2. 目标

1. **节点层面**：这台机器 CPU / 内存 / 磁盘 / 网络是不是瓶颈？为什么卡没跑满？
2. **人的层面**：哪个**容器**在跑什么（命令行），各占多少 CPU、内存、显存、几张卡？
3. **异常层面**：哪张卡掉线（Xid）、哪张卡降频（撞温度/功耗墙）、哪张卡占着不用。

---

## 3. P0 — 部署 exporter 新版本（前置）

线上仍是旧版，`gpu_up` / `gpu_count` / `gpu_exporter_last_success_timestamp_seconds` **尚不存在**，所以「单卡消失」「采集停滞」现在无法告警。

```bash
# 每台 GPU 节点（幂等，装完自校验 /metrics 真有 gpu_info）
scp -r client/ me@gpu01:/tmp/ && ssh gpu01 'sudo bash /tmp/client/install.sh'
# 或单文件
ssh gpu01 'sudo bash /tmp/remote-install.sh'
```

**改动**：无代码改动（版本已就绪）。
**验收**：Prometheus 出现 `gpu_up`（64 条）、`gpu_count`（8 条），`make test` 全绿。
**回滚**：`sudo bash install.sh` 重装上一个 tag 即可（服务幂等）。

---

## 4. P1 — exporter 扩展

只改 `client/exporter.py`，全部在**同一个 NVML 采集循环**内完成，不新增任何守护进程。

### P1a 硬件遥测

| 指标 | 类型 | NVML API | 标签 | 覆盖 |
| --- | --- | --- | --- | --- |
| `gpu_power_limit_watts` | gauge | `GetPowerManagementLimit` | `gpu_index` | 全部 |
| `gpu_violation_seconds_total` | counter | `GetViolationStatus` | `gpu_index,reason`（`thermal`/`power`/`sync_boost`/`board_limit`，ns→s） | 全部 |
| `gpu_performance_state` | gauge | `GetPerformanceState` | `gpu_index` | 全部 |
| `gpu_pcie_link_gen` | gauge | `GetCurrPcieLinkGeneration` | `gpu_index` | 全部 |
| `gpu_pcie_link_width` | gauge | `GetCurrPcieLinkWidth` | `gpu_index` | 全部 |
| `gpu_pcie_replay_counter_total` | counter | `GetPcieReplayCounter` | `gpu_index` | 全部 |
| `gpu_encoder_utilization_percent` | gauge | `GetEncoderUtilization` | `gpu_index` | 全部 |
| `gpu_decoder_utilization_percent` | gauge | `GetDecoderUtilization` | `gpu_index` | 全部 |
| `gpu_ecc_mode` | gauge | `GetEccMode` | `gpu_index` | 18 张专业卡 |
| `gpu_ecc_errors_total` | counter | `GetMemoryErrorCounter` | `gpu_index,type(corrected\|uncorrected),scope(volatile\|aggregate)` | 18 张专业卡 |

规则：不支持该 API 的卡**不产生 series**（沿用现有 `_drop()`），而不是报 0——否则会得出"ECC 零错误"的错误结论。

### P1b Xid 事件（替代 DCGM）

- **源**：`/dev/kmsg`（exporter 跑在 LXC 宿主、root；systemd 加固里**故意没开 `PrivateDevices`**）
- **解析**：`NVRM: Xid (PCI:0000:3b:00): 48, ...`，同时匹配 `SXid`
- **映射**：`nvmlDeviceGetPciInfo().busId`（`0000:3B:00.0`）小写规范化后与日志中的 BDF 对齐 → `gpu_index`
- **读取**：非阻塞打开；启动时 `seek(0, 2)`，之后每周期只读新增行
- **降级**：`/dev/kmsg` 不可读 → `journalctl -k -o cat --since @<ts>`；都不可用 → `warn_once` 后仅停用该指标

| 指标 | 类型 | 标签 | 说明 |
| --- | --- | --- | --- |
| `gpu_xid_events_total` | counter | `gpu_index,xid` | 每个 Xid 码独立计数，`increase()` 有意义，**不丢事件** |
| `gpu_last_xid` | gauge | `gpu_index` | 最近一次 Xid 码 |
| `gpu_last_xid_timestamp_seconds` | gauge | `gpu_index` | 最近一次时间 |

### P1c 进程归因（「哪个容器在跑什么，占多少」）

每进程多读 3 个文件：`/proc/<pid>/status`、`/proc/<pid>/stat`、`/proc/<pid>/cmdline`。

| 指标 | 类型 | 标签 | 说明 |
| --- | --- | --- | --- |
| `gpu_process_info` | gauge=1 | `gpu_index,pid,container,exe,cmdline` | `exe` = `/proc/pid/exe` basename；`cmdline` 截断 120 字符，`--token=`/`password=`/`key=` 等**脱敏为 `***`** |
| `gpu_process_cpu_cores` | gauge | `gpu_index,pid,container` | 每周期差分 `/proc/pid/stat` 的 utime+stime；多线程可 >1 |
| `gpu_process_rss_bytes` | gauge | `gpu_index,pid,container` | 常驻内存（当前完全不可见） |
| `gpu_process_threads` | gauge | `gpu_index,pid,container` | 线程数 |
| `gpu_process_uptime_seconds` | gauge | `gpu_index,pid,container` | 由 `stat.starttime` 推算 |
| `gpu_process_memory_bytes` | gauge | `gpu_index,pid,container`（保持兼容） | 显存占用 |

**预聚合层**（pid 指标天然 churn，历史不可用；聚合指标才是有界的）：

| 指标 | 标签 | 用途 |
| --- | --- | --- |
| `gpu_container_memory_bytes` | `container` | 按容器排名 / 历史趋势 |
| `gpu_container_cpu_cores` | `container` | 按容器 CPU |
| `gpu_container_processes` | `container` | 进程数 |
| `gpu_container_gpus` | `container` | 占用显卡数 |

> `node` / `instance` 由 Prometheus 抓取时附加，exporter 不硬编码。

**隐私**：`cmdline` 只出现在 Admin 看板；Public 看板不含任何进程信息。

### P1 验收与回滚

- `tests/exporter/` 新增 mock 用例：Xid 日志解析（含 SXid、未知 BDF）、`/proc` 归因（CPU 差分、RSS、cmdline 脱敏）、新指标存在性、不支持 API 时不产生 series
- 单台先灰度：`curl :9745/metrics` 人工核对，再推全部
- 回滚：`git revert` + 重跑 `install.sh`

---

## 5. P2 — node_exporter（Ubuntu 发行版包）

### 安装方式

```bash
apt-get update
apt-get install -y prometheus-node-exporter
```

- 包由 **`universe`** 仓库提供；若 `apt-get install` 失败，先 `add-apt-repository universe` 再重试（脚本内置该分支）
- 二进制：`/usr/bin/prometheus-node-exporter`
- 单元：`prometheus-node-exporter.service`
- 参数：写在 **`/etc/default/prometheus-node-exporter`** 的 `ARGS=`（Debian/Ubuntu 约定）

### 关键坑（脚本必须处理）

1. **不同 Ubuntu 版本包版本不同**（20.04≈1.0.1、22.04≈1.3.1、24.04≈1.7+），`--collector.disable-defaults` 需要 ≥1.3
   → 脚本先 `node_exporter --version` 解析版本：
   - ≥1.3：白名单（`--collector.disable-defaults` + 指定 collector），~300 series/节点
   - <1.3：退化为黑名单（`--no-collector.xxx`），效果等价、兼容老版本
2. **默认监听地址可能被发行版改成 127.0.0.1**
   → 脚本**显式**写 `--web.listen-address=:9100`，并在装完后**从宿主 LAN IP（不是 localhost）** curl 验证
3. **包升级会重置 `/etc/default/...`**（dpkg conffile 提示）
   → 脚本幂等：每次运行都重写 ARGS 并重启

### 参数模板（白名单版，≥1.3）

```
ARGS="--web.listen-address=:9100 \
  --collector.disable-defaults \
  --collector.cpu --collector.meminfo --collector.loadavg \
  --collector.stat --collector.filesystem --collector.netdev \
  --collector.os --collector.time \
  --collector.filesystem.mount-points-exclude=^/(dev|proc|sys|run|var/lib/docker/.+)($|/)"
```

> hwmon / nvme / edac / mdadm / bcache / zfs / nfs / conntrack 等一律不采。

### 交付物

```
client/install-node-exporter.sh     # 幂等安装 + 版本分档 + 装完校验
tests/install/node_exporter_test.sh # 容器内验证：参数生成、版本分档、幂等
```

抓取间隔 **60s**（节点指标不需要 20s）。

**验收**：8 个 `node-nodes` 目标全 up；`node_cpu_seconds_total`、`node_memory_MemAvailable_bytes`、`node_load1`、`node_filesystem_avail_bytes`、`node_network_receive_bytes_total` 有数据。
**回滚**：`apt-get remove --purge prometheus-node-exporter` + 从 targets 移除。

---

## 6. P3 — Prometheus 配置与记录规则

### 6.1 抓取目标拆分

```
server/targets.d/gpu-nodes.json     # :9745（现有文件改名，内容不变）
server/targets.d/node-nodes.json    # :9100（新增，同一批节点、同一 node 标签）
```

两个 job 共用 `node` 标签 → 看板可 `* on(node)` 关联。

### 6.2 `prometheus.yml`

```yaml
global:
  scrape_interval: 20s
  evaluation_interval: 20s

rule_files:
  - /etc/prometheus/rules/*.yml

scrape_configs:
  - job_name: gpu-nodes
    scrape_interval: 20s
    file_sd_configs:
      - files: ['/etc/prometheus/targets/gpu-nodes.json']
        refresh_interval: 1m
    metric_relabel_configs:
      # 丢掉 exporter 自身运行指标（每节点约 50 条，无监控价值）
      - source_labels: [__name__]
        regex: 'python_gc_.*|python_info|process_(cpu_seconds_total|resident_memory_bytes|virtual_memory_bytes|open_fds|max_fds)'
        action: drop

  - job_name: node-nodes
    scrape_interval: 60s
    scrape_timeout: 10s
    file_sd_configs:
      - files: ['/etc/prometheus/targets/node-nodes.json']
        refresh_interval: 1m
```

`docker-compose.yml` 增加挂载：`./rules:/etc/prometheus/rules:ro`。

### 6.3 记录规则 `server/rules/recording.yml`

| 记录规则 | 表达式 |
| --- | --- |
| `node:cpu:usage_ratio` | `1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[2m]))` |
| `node:mem:available_ratio` | `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes` |
| `node:fs:avail_ratio` | `node_filesystem_avail_bytes{fstype!~"tmpfs\|overlay"} / node_filesystem_size_bytes{...}` |
| `gpu:util:avg_by_node` | `avg by (node) (gpu_utilization_percent)` |
| `gpu:mem:used_ratio_by_node` | `sum by (node)(gpu_memory_used_bytes) / sum by (node)(gpu_memory_total_bytes)` |
| `gpu:util:avg_by_model` | `avg by (name) (gpu_utilization_percent * on(gpu_index, instance) group_left(name) gpu_info)` |
| `gpu:container:mem_sum` | `sum by (node, container) (gpu_container_memory_bytes)` |
| `gpu:container:cpu_sum` | `sum by (node, container) (gpu_container_cpu_cores)` |

**验收**：`promtool check rules` 通过；`/-/reload` 后 `/api/v1/rules` 中记录规则 `health=ok`；看板查询明显变快。
**回滚**：注释 `rule_files` + reload。

---

## 7. P4 — 告警（新增 8 条 + 现有 2 条保留）

全部写入 `server/grafana/provisioning/alerting/rules.yml`（folder `Admin`，group `gpu-node-alerts`），接收端统一 `gpu-webhook`（→ relay → 飞书）。

| # | 告警 | 表达式 | for | 级别 |
| --- | --- | --- | --- | --- |
| 1 | **占卡空转** | `avg_over_time(gpu_utilization_percent[10m]) < 5 and avg_over_time(gpu_memory_used_bytes[10m]) > 4e9` | 30m | warning |
| 2 | **单卡消失** | `gpu_up == 0` | 3m | critical |
| 3 | **Xid 报错** | `increase(gpu_xid_events_total[5m]) > 0` | 0m | critical |
| 4 | 降频撞墙 | `increase(gpu_violation_seconds_total{reason=~"thermal\|power"}[10m]) > 60` | 5m | warning |
| 5 | 采集停滞 | `time() - gpu_exporter_last_success_timestamp_seconds > 120` | 5m | warning |
| 6 | 节点内存紧张 | `node:mem:available_ratio < 0.05` | 10m | warning |
| 7 | 节点磁盘将满 | `node:fs:avail_ratio < 0.1` | 10m | warning |
| 8 | 节点 CPU 打满 | `node_load1 > (count by (node)(node_cpu_seconds_total{mode="idle"})) * 1.5` | 15m | info |
| 9 | 监控心跳（dead-man） | `vector(1)`，`repeat_interval: 1h` | 0m | info |

说明：

- **#1** 用 `avg_over_time[10m]` + `for: 30m`，避开模型加载/校验阶段误报；annotation 带 `node` / `gpu_index`，并给 Node Detail 看板链接
- **#3** 里 `xid` 作为标签自动出现在通知里，可直接对着 Xid 码表分流
- **#5** 专治「exporter 卡住但端口还活着」——`up` 看不出来
- **#9** 专治「Prometheus 自己挂了」——现有规则都是 `execErrState: Error`，一挂就彻底静默
- 现有 `GPU Node Down` / `GPU Info Missing` 的 `noDataState: Alerting` **保持不动**（掉卡/失联兜底）
- 可选增强（P4 内可加）：#1 增加一条查询 `topk(1, gpu_process_memory_bytes{gpu_index="$gpu_index"})`，把「嫌疑容器」渲染进 annotation

**验收**：临时加假目标 → 触发 #2/#5 → relay 收到请求（飞书未配置时 relay 返回 500，属预期）→ 移除目标后自动恢复。
**回滚**：`git revert` + `docker compose restart grafana`。

---

## 8. P5 — 看板

### 8.1 新建 `Admin/Node Detail`（uid `node-detail`，变量 `$node`）

| 行 | 面板 |
| --- | --- |
| 1 节点资源 | CPU 使用率（by mode 堆叠）、负载 vs 核数、内存 used/available、各挂载点磁盘、网络 收/发 |
| 2 GPU 一屏 | 每卡：利用率 / 显存 / 温度 / 功耗 vs 上限 / performance state（bar gauge + timeseries） |
| 3 **进程表** | `container` / `exe` / `cmdline` / GPU / 显存 / CPU 核 / RSS / 线程 / 运行时长 |
| 4 容器聚合 | 本节点按 `container` 的显存、CPU 核、进程数、占卡数 |
| 5 事件 | 近 24h：Xid 计数（by xid）、降频累计、`gpu_up` 变化 |

### 8.2 改造 `Admin/GPU Cluster Overview`

- 新增**按卡型分组**表：3090 / 4090 / 5090 / A6000 / PRO 6000 各一行（卡数、平均利用率、显存%、功耗、撞墙数）
- 「All Users Ranking」→ 改为**按容器**，列：显存、CPU 核、进程数、占卡数
- 节点汇总表增加 `CPU%`、`内存可用%`、`磁盘可用%` 三列（join node_exporter）
- 新增「异常摘要」：当前 firing 告警数、掉卡数、24h Xid 计数

### 8.3 `Public` 看板

保持不变（不含容器名、命令行、节点资源细节）。

**验收**：两个看板全部 panel 查询返回 200（登录态 API 脚本化验证）；`make test` 的静态套件校验新看板 JSON 合法。
**回滚**：`git revert` + `docker compose restart grafana`。

---

## 9. 为什么不上 DCGM（决策留档）

| 维度 | 事实 |
| --- | --- |
| 支持范围 | NVIDIA 官方：非数据中心卡（GeForce/Quadro）**只有"有限功能"**；安装文档写明 DCGM 面向数据中心驱动测试，其他驱动会缺失功能 |
| 你们的卡 | 40/64 为 GeForce → ECC / 行重映射字段不存在；能吃到增益的只有 18 张专业卡 |
| NVLink | 集群无 → DCGM 整组 NVLink 字段作废 |
| Xid | `DCGM_FI_DEV_XID_ERRORS` 是「最后一次的 code」gauge，不能 `rate()`，两次抓取之间的突发会丢；要计数需启用默认注释掉的 `DCGM_EXP_XID_ERRORS_TOTAL`，且 exporter 重启归零 |
| 成本 | `nv-hostengine` + `dcgm-exporter` 每节点 150–400MB 内存；要求宿主 ≥16GB、CPU 核数 ≥ GPU 数；DCGM 版本需与驱动分支匹配；新架构（5090 / PRO 6000 Blackwell）字段支持可能滞后 |

**再评估条件**：换到数据中心卡（A100/H100/L40S）；需要 `dcgmi diag` 做故障定位（临时装即可，无需常驻）；需要 NVLink 遥测；需要 DCGM Health 预置检查组。

---

## 10. 性能与容量预算

| 项 | series 增量 | 磁盘 / 30d | 采集端开销 |
| --- | --- | --- | --- |
| P1a 硬件遥测 | ≈600（ECC 仅 18 卡） | +180MB | NVML +11 次/卡/周期 → 每台 **<0.02% 单核** |
| P1b Xid | ≈0（无事件时无 series） | ≈0 | 每周期一次增量读 → **<0.1%** |
| P1c 进程原始指标 | ≈310（churn） | +100MB | 3 次 `/proc` 读 × ~52 进程 → **<0.5%** |
| P1c 聚合指标 | ≈420（有界） | +130MB | 纯内存计算 |
| P2 node_exporter | ≈2400（300 × 8） | +700MB | 每节点常驻 15–25MB、**<0.5% CPU** |
| P3 relabel 丢弃 | −400 | −110MB | — |
| **合计** | **1111 → ≈4400** | **301MB → ≈1.2GB** | **每台增加 <1% CPU** |

抓取间隔保持 20s（GPU）/ 60s（节点），**不放宽也不缩短**。

---

## 11. 交付物清单

| 文件 | 动作 | 阶段 |
| --- | --- | --- |
| `client/exporter.py` | 改（P1a/b/c） | P1 |
| `tests/exporter/*` | 改（Xid / `/proc` / 新指标用例） | P1 |
| `client/install-node-exporter.sh` | 新 | P2 |
| `tests/install/node_exporter_test.sh` | 新 | P2 |
| `server/prometheus.yml` | 改（两 job + rule_files + relabel） | P3 |
| `server/rules/recording.yml` | 新 | P3 |
| `server/docker-compose.yml` | 改（挂载 rules） | P3 |
| `server/targets.json.example` | 改（补 :9100 示例） | P3 |
| `tests/static/checks.sh` | 改（校验新规则/看板/示例） | P3–P5 |
| `server/grafana/provisioning/alerting/rules.yml` | 改（+8 条） | P4 |
| `server/grafana/provisioning/dashboards/admin/node-detail.json` | 新 | P5 |
| `server/grafana/provisioning/dashboards/admin/gpu-overview.json` | 改 | P5 |
| `README.md` | 改（指标表 / node_exporter 部署 / 看板说明） | P5 |

每个阶段一个独立 commit，`make test` 全绿后推送。

---

## 12. 风险与回滚

| 风险 | 应对 |
| --- | --- |
| 新指标异常影响既有采集 | 每项独立 `try/except` + `_drop`；**单台灰度**后再推全量；`make test` 覆盖 |
| `/dev/kmsg` 在部分 LXC 不可读 | 自动降级 `journalctl -k`；再不行仅停用 Xid 指标并 `warn_once` |
| Ubuntu 包版本差异导致参数不生效 | 版本探测 + 白/黑名单双档；装完用 `ps` 确认实际生效参数 + LAN IP 探测 |
| 发行版把监听地址改回 127.0.0.1 | 显式覆盖 ARGS 并从 **LAN IP** 验证 |
| 告警过噪（尤其 #1） | 先 `warning` + `for: 30m`，观察一周再调阈值；支持 silence |
| 记录规则写错致 Prometheus 起不来 | `promtool check rules` 纳入 `make test`；先 `/-/reload` 验证 |
| series 增长超预期 | relabel 兜底；必要时拉长进程指标抓取间隔或丢弃 `cmdline` 标签 |

---

## 13. 验收总表

| 阶段 | 验收标准 |
| --- | --- |
| P0 | 8 目标出现 `gpu_up`(64) / `gpu_count`(8)；`make test` 全绿 |
| P1 | mock 用例覆盖新指标、Xid 解析、`/proc` 归因；单台真机 `curl :9745/metrics` 核对通过 |
| P2 | 8 个 node 目标 up；5 类关键指标有数据；从 LAN IP 可访问 9100 |
| P3 | `promtool check config` + `check rules` 通过；记录规则 `health=ok` |
| P4 | 假目标触发 #2/#5 → relay 收到 → 移除后自动恢复；规则数量与文件一致 |
| P5 | 两个看板全部 panel 查询 200；JSON/YAML 全部解析通过 |

---

## 14. 工作量估算

| 阶段 | 内容 | 估时 |
| --- | --- | --- |
| P0 | 8 台部署 + 验证 | 0.5h |
| P1 | exporter 扩展 + mock 用例 + 灰度 | 2–3h |
| P2 | 安装脚本 + 8 台部署 + 验证 | 1.5–2h |
| P3 | 配置 + 记录规则 + reload 验证 | 1h |
| P4 | 8 条告警 + 触发验证 | 1h |
| P5 | Node Detail 新看板 + Overview 改造 + 验证 | 2–3h |
| **合计** | | **约 1 个工作日（含验证）** |
