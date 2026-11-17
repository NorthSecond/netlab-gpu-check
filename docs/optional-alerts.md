# 可选告警规则（默认未加载）

`server/grafana/provisioning/alerting/rules.yml` 目前只 provision 两条规则：
`GPU Node Down`（节点抓取失败）和 `GPU Info Missing`（节点可达但完全没有 gpu_info）。

下面这些规则**没有**默认启用。要启用某一条，把对应片段合并进
`rules.yml` 的 `groups[0].rules` 里（保持 `uid` 唯一），然后重启 Grafana：

```bash
cd server && docker compose restart grafana
```

这些规则依赖 exporter 的自检指标（`client/exporter.py` 已上报）：

| 指标 | 含义 |
| --- | --- |
| `gpu_exporter_last_success_timestamp_seconds` | 最近一次采集成功的 Unix 时间戳 |
| `gpu_exporter_collect_errors_total` | 采集失败次数（计数器） |
| `gpu_count` | NVML 当前报告的 GPU 数量 |
| `gpu_up{gpu_index}` | 该卡本轮是否被 NVML 看到（1/0） |

---

## 1. Exporter 卡住/采集失败（推荐）

`up` 只能说明 HTTP 端口活着；NVML 卡住时 exporter 仍会返回**上一次的旧值**，
`up` 依然是 1。这条规则用「最后成功时间」兜住这种情况。

```yaml
      - uid: gpu-exporter-stale
        title: GPU Exporter Stale
        condition: B
        data:
          - refId: A
            relativeTimeRange:
              from: 600
              to: 0
            datasourceUid: prometheus
            model:
              expr: 'time() - gpu_exporter_last_success_timestamp_seconds'
              refId: A
              instant: true
          - refId: B
            relativeTimeRange:
              from: 0
              to: 0
            datasourceUid: __expr__
            model:
              expression: A
              type: threshold
              refId: B
              conditions:
                - evaluator:
                    type: gt
                    params:
                      - 120
        noDataState: Alerting
        execErrState: Error
        for: 2m
        annotations:
          summary: "GPU exporter 采集停滞: {{ $labels.instance }}"
          description: "节点 {{ $labels.instance }} 已超过 2 分钟没有成功采集，指标可能已陈旧。"
        labels:
          severity: warning
```

## 2. 单卡从 NVML 消失（真·掉卡）

`gpu_up == 0` 表示这张卡曾经出现过、但本轮 NVML 没再报告它。注意：为了让时间曲线
不断档，exporter **保留**该卡的其它 series（沿用最后一次的值），所以判断掉卡请用
`gpu_up`，不要用「指标消失」。

```yaml
      - uid: gpu-not-reporting
        title: GPU Not Reporting
        condition: B
        data:
          - refId: A
            relativeTimeRange:
              from: 300
              to: 0
            datasourceUid: prometheus
            model:
              expr: 'gpu_up == 0'
              refId: A
              instant: true
          - refId: B
            relativeTimeRange:
              from: 0
              to: 0
            datasourceUid: __expr__
            model:
              expression: A
              type: threshold
              refId: B
              conditions:
                - evaluator:
                    type: lt
                    params:
                      - 1
        noDataState: OK
        execErrState: Error
        for: 3m
        annotations:
          summary: "GPU 不可见: {{ $labels.instance }} GPU{{ $labels.gpu_index }}"
          description: "该卡已 3 分钟未被 NVML 报告，可能是 Xid 错误或掉卡。"
        labels:
          severity: critical
```

## 3. GPU 温度过高（示例，阈值按机型调整）

```yaml
      - uid: gpu-temperature-high
        title: GPU Temperature High
        condition: B
        data:
          - refId: A
            relativeTimeRange:
              from: 300
              to: 0
            datasourceUid: prometheus
            model:
              expr: 'gpu_temperature_celsius'
              refId: A
              instant: true
          - refId: B
            relativeTimeRange:
              from: 0
              to: 0
            datasourceUid: __expr__
            model:
              expression: A
              type: threshold
              refId: B
              conditions:
                - evaluator:
                    type: gt
                    params:
                      - 85
        noDataState: OK
        execErrState: Error
        for: 5m
        annotations:
          summary: "GPU 温度过高: {{ $labels.instance }} GPU{{ $labels.gpu_index }}"
          description: "温度 {{ $value }}°C 持续 5 分钟超过 85°C。"
        labels:
          severity: warning
```

## 4. Prometheus 自身失联（dead-man）

现在两条现有规则都是 `execErrState: Error`：**Prometheus 挂掉时规则进入 Error 状态，
不会发出任何通知**，等于监控静默死亡。建议加一条恒真告警，并给它设置较短的
`repeat_interval`（例如 1h），这样「收不到心跳」本身就是告警。

```yaml
      - uid: gpu-monitoring-watchdog
        title: GPU Monitoring Watchdog
        condition: A
        data:
          - refId: A
            relativeTimeRange:
              from: 300
              to: 0
            datasourceUid: prometheus
            model:
              expr: 'vector(1)'
              refId: A
              instant: true
        noDataState: Alerting
        execErrState: Alerting
        for: 0m
        annotations:
          summary: "GPU 监控心跳"
          description: "这条告警应当一直处于 firing 状态；不收到它说明 Grafana/Prometheus 出了问题。"
        labels:
          severity: info
```

> 注意：第 1、3 条在 `noDataState` 上的取值和现有规则不同——现有两条故意使用
> `Alerting`，用来在「节点/指标整体消失」时兜底；新加的规则如果 NoData 也报
> `Alerting`，会和它们重复告警，所以这里用 `OK`。
