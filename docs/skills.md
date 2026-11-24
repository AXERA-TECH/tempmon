# tempmon Skills / Agent 操作指南

本文给 coding agent、维护者和二次开发者提供一份可直接执行的项目上下文。目标是让 agent
在不读完整源码的情况下，也能安全地修改、测试和扩展 tempmon。

更偏用户的安装和配置说明见 [用户指南](USER_GUIDE.md)。

---

## 1. 项目定位

tempmon 是 **AX8860 / AX8860P 板卡状态监控组件**，包含两部分：

```text
板端 agent：tempmon.c
  单文件 C 程序，只读采集 OS / CMM / NPU / 网络磁盘 / llama-server 状态

中心聚合器：monitor.py
  Python 标准库实现的 HTTP 服务，轮询多块板卡，发现 llama-server，
  提供 Web Dashboard 和 JSON API
```

它不是推理数据面，不代理 prompt，不控制模型启动。模型生命周期管理属于
[ax-model-manager](https://github.com/AXERA-TECH/ax-model-manager)。

默认端口：

| 服务 | 端口 | 说明 |
|---|---:|---|
| tempmon agent | 8080 | 板端只读状态 API |
| monitor aggregator | 8888 | Web Dashboard + 聚合 API |
| llama-server | 18080–18089 | 被发现和展示的推理端口池 |

---

## 2. 数据流

```text
Browser / 上位机
    │ HTTP
    ▼
monitor.py :8888
    │  每秒轮询 /api/stats
    ▼
tempmon agent :8080
    │ 读取
    ▼
/proc/ax_proc/*
/sys/class/thermal/*
/sys/class/net/*
/proc/<pid>/*
```

关键约束：

1. agent 是只读的，不写设备配置，不启动/停止模型。
2. 聚合器只读 agent，不修改 agent 配置。
3. 聚合器会主动探测板端 `18080–18089` 的 llama-server，但不代理推理请求。
4. `boards.json`、`registrations.json`、token 都是本地运行时数据，不提交 Git。

---

## 3. 仓库结构

```text
tempmon/
├── tempmon.c              板端 agent，单文件 C
├── monitor.py             中心聚合器，Python 标准库
├── bench_sample.py        benchmark 运行时资源采样工具
├── test_monitor.py        聚合器 / API / 页面测试
├── test_bench_sample.py   benchmark 采样测试
├── boards.example.json    脱敏配置示例
├── README.md              项目简介 + 快速上手
├── docs/
│   ├── USER_GUIDE.md      用户安装 / 配置 / API 说明
│   └── skills.md          本文，agent / 维护者上下文
├── VERSION                当前版本
├── LICENSE                MIT
└── .github/workflows/ci.yml
```

---

## 4. 板端 agent

### 4.1 编译

```bash
gcc -O2 -Wall -Wextra -Werror -pthread -o tempmon tempmon.c
```

发布构建可使用：

```bash
gcc -O2 -s -pthread -o tempmon tempmon.c
```

要求：

```text
Linux / AX8860 / AX8860P
pthread
/proc/ax_proc/*
/sys/class/thermal/*
```

### 4.2 启动

```bash
./tempmon 8080
```

参数：

| 参数 | 说明 |
|---|---|
| 位置参数 `PORT` | 监听端口，默认 `8080` |
| `--bind ADDRESS` | 绑定地址，默认 `0.0.0.0` |
| `--token TOKEN` | 可选 Bearer token |
| `--board-name NAME` | 主动注册使用的板卡名 |
| `--report-url URL` | 主动注册 URL |
| `--report-token TOKEN` | 主动注册 token |
| `--report-interval N` | 主动注册间隔秒，3–3600 |

等价环境变量：

```text
TEMPMON_PORT
TEMPMON_BIND
TEMPMON_TOKEN
TEMPMON_BOARD_NAME
TEMPMON_REPORT_URL
TEMPMON_REPORT_TOKEN
TEMPMON_REPORT_INTERVAL
```

### 4.3 HTTP 接口

```http
GET /health
GET /api/stats
GET /api/temp
```

`/api/temp` 是 `/api/stats` 的兼容别名。

启用 token 后需要：

```http
Authorization: Bearer <agent-token>
```

### 4.4 `/api/stats` 结构

聚合器依赖以下字段：

```json
{
  "time": 1790000000,
  "agent": {
    "version": "2.2.0",
    "bsp_version": "...",
    "chip_type": "..."
  },
  "load": {"1": 0.1, "5": 0.1, "15": 0.1},
  "mem": {"total_kb": 1, "avail_kb": 1, "used_kb": 1},
  "cmm": {"total_kb": 1, "used_kb": 1},
  "net": {
    "iface": "end0",
    "rx_bytes": 0,
    "tx_bytes": 0,
    "rx_mbps": 0.0,
    "tx_mbps": 0.0
  },
  "disks": [],
  "cpu_cores": 16,
  "cpu_freqs": [1650],
  "npu": {
    "tdp_freq_mhz": 800,
    "clock_mhz": [400, 200, 200, 200],
    "util_avg": 0.0
  },
  "llama": {
    "process_count": 0,
    "rss_kb": 0,
    "listening_port_count": 0,
    "processes": [],
    "listening_ports": []
  },
  "zones": [
    {"name": "thermal_zone0", "type": "thermal_npu0", "milli": 40000, "c": 40.0}
  ]
}
```

### 4.5 指标来源

| 指标 | 来源 |
|---|---|
| NPU 温度 | `/sys/class/thermal/thermal_zone*/temp` |
| CPU load | `/proc/loadavg` |
| OS memory | `/proc/meminfo` |
| CMM | `/proc/ax_proc/mem_cmm_info` |
| 磁盘 | `/proc/mounts` + `statvfs` |
| CPU 频率 | `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` |
| BSP / 芯片 | `/proc/ax_proc/version`、`/proc/ax_proc/chip_type` |
| NPU TDP | `/proc/ax_proc/tdp` |
| NPU clock | `/sys/kernel/debug/clk/npu*_clk_nn_glb/clk_rate` |
| NPU 平均利用率 | `/proc/ax_proc/npu/top` |
| llama 进程 | `/proc/<pid>/comm`、`/proc/<pid>/status` |
| 监听端口 | `/proc/net/tcp`、`/proc/net/tcp6` |

NPU 利用率建议：

```bash
echo 1 > /proc/ax_proc/npu/enable
echo 1000 > /proc/ax_proc/npu/top_period_ms
cat /proc/ax_proc/npu/top
```

`npu.util_avg` 是所有 cluster/core/vNPU 行的平均值；接口不可用时为 `null`。

---

## 5. 中心聚合器

### 5.1 启动

```bash
python3 monitor.py
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TEMPMON_CONFIG` | `<repo>/boards.json` | 板卡配置 |
| `TEMPMON_REGISTRATIONS` | `<repo>/registrations.json` | 注册状态 |
| `TEMPMON_MONITOR_PORT` | `8888` | 聚合器端口 |
| `TEMPMON_REPORT_TOKEN` | 空 | 主动注册 token |

### 5.2 板卡配置

```json
{
  "boards": [
    {
      "name": "board-a",
      "ip": "<board-ip>",
      "port": 8080,
      "token": "<optional-token>",
      "llama_port_min": 18080,
      "llama_port_max": 18089,
      "llama_ports": ["18080-18089", 18500]
    }
  ]
}
```

规则：

- `name` 必须唯一且稳定；
- `token` 可选；
- `llama_port_min/max` 可覆盖默认端口扫描范围；
- `llama_ports` 可显式列出端口或 `"lo-hi"` 区间，用于 18080–18089 之外的服务（如 axdiffusion 的 18500）；
- 配置文件每轮轮询自动重载；
- `boards.json` 不提交 Git。

### 5.3 聚合 API

```http
GET /health
GET /api/all
GET /api/all?history=60
GET /api/history?limit=300
GET /api/boards
POST /api/report
```

`/api/all` 默认不返回 history，适合浏览器每秒轮询。`/api/history` 返回历史。
`/api/boards` 返回当前配置。

### 5.4 聚合状态

每块板卡包含：

```text
name
ip
port
agent
data
inference
stale
data_age_ms
```

`inference` 包含：

```text
online_count
port_range
servers[]
last_full_scan_at
next_full_scan_at
```

每个 llama-server：

```text
port
state              ready / generating / loading / unhealthy / offline / unknown-service
is_llama
health_status
model
slot_state         idle / processing
context_used
context_total
error
```

### 5.5 主动注册

```http
POST /api/report
Authorization: Bearer <report-token>
```

规则：

- 板卡名必须已存在于 `boards.json`；
- 聚合器使用 TCP 源地址作为新 IP；
- 不信任 body 中声明的 IP；
- 地址变化写入 `registrations.json`；
- `registrations.json` 不提交 Git。

### 5.6 stale 策略

短网络抖动时，聚合器保留最近一次有效数据，并标记：

```text
stale=true
agent.state=stale
data_age_ms=<ms>
```

超过 `STALE_DATA_SECONDS` 后不再保留旧数据。

### 5.7 llama-server 发现

聚合器会探测：

```text
/health
/v1/models
/slots
```

扫描范围默认：

```text
18080–18089
```

发现后展示：

```text
model
slot_state
context_used / context_total
state
```

聚合器不代理 `/v1/chat/completions` 等推理请求。

---

## 6. 本地开发与测试

### 6.1 运行测试

```bash
python3 -m unittest discover -v
```

当前覆盖：

```text
llama-server 探测
端口范围保护
history 环形缓存
stale 逻辑
主动注册 / 持久化
Web UI JavaScript 语法
benchmark 采样与报告
```

### 6.2 编译检查

```bash
gcc -O2 -Wall -Wextra -Werror -pthread -o /tmp/tempmon tempmon.c
```

### 6.3 CI

GitHub Actions：

```text
.github/workflows/ci.yml
```

CI 执行：

```text
1. 校验 VERSION 与 tempmon.c 的 AGENT_VERSION 一致
2. 校验 LICENSE
3. Python 单元测试
4. 本机 C agent 编译
5. 生成 source artifact
6. 冲突标记 / 空白错误检查
7. 内网地址 / 凭据字面量扫描
8. 聚合器 API / Web UI smoke test
9. 使用 Arm GNU Toolchain 13.3.rel1 交叉编译静态 aarch64 agent
10. 上传 tempmon-aarch64 artifact
```

发布：

```text
推送到 v* tag 时，.github/workflows/release.yml 会构建
tempmon-<version>-linux-aarch64.tar.gz，并上传到 GitHub Release。
```

---

## 7. 常见修改指南

### 7.1 新增一个板端指标

1. 在 `tempmon.c` 增加读取函数；
2. 在 `build_json()` 中追加 JSON 字段；
3. 更新 `/api/stats` 文档；
4. 更新 `docs/USER_GUIDE.md` 指标来源；
5. 增加测试或板端手测；
6. 更新 `AGENT_VERSION` / `VERSION`（如果是发布变更）。

注意：

- 不要写入设备配置；
- 不要读取可能阻塞的调试节点；
- 字段缺失时返回 `null` 或合理默认值；
- 不要伪造 NPU utilization。

### 7.2 新增聚合 API 字段

1. 在 `monitor.py` 的 snapshot / history 结构中添加字段；
2. 更新 `make_history_sample()`；
3. 更新测试；
4. 更新 `docs/skills.md` 和 `docs/USER_GUIDE.md`；
5. 确认 `/api/all` 默认响应不过大。

### 7.3 修改 Web UI

Web UI 是 `monitor.py` 内嵌 HTML 字符串。

修改后至少执行：

```bash
python3 -m unittest discover -v
```

CI 会检查：

```text
HTML 内 JavaScript 语法
关键 UI 字符串
聚合器 smoke test
```

### 7.4 新增依赖

聚合器当前只使用 Python 标准库。除非有充分理由，不要引入 pip 依赖。

如果必须引入：

1. 更新 `docs/USER_GUIDE.md`；
2. 更新 CI 安装步骤；
3. 说明为什么标准库不够用；
4. 保持 agent 侧仍然零第三方依赖。

---

## 8. 安全与脱敏规则

必须遵守：

```text
1. 不提交真实内网 IP、密码、token、内部路径。
2. boards.json / registrations.json / *.token 不提交 Git。
3. 不在日志中打印 token。
4. 不把 agent token 发给 llama-server。
5. 不代理 prompt / response。
6. 不伪造 NPU utilization、ViT 耗时或后端没有的指标。
7. 公开文档只使用 <board-ip>、<agent-token> 等占位符。
8. 测试使用 192.0.2.0/24 文档地址或 fixture。
```

CI 会扫描：

```text
内网地址
口令类字面量 / 常见弱口令
冲突标记
空白错误
```

---

## 9. 与 ax-model-manager 的关系

`ax-model-manager` 是模型生命周期管理平面：

```text
模型发现 / manifest / profile
start / stop / restart / switch
jobs / logs / audit
```

tempmon 是它的板端资源数据源之一：

```text
ax-model-manager → http://<board-ip>:8080/api/stats
```

部署顺序：

```text
1. 安装 tempmon agent
2. 确认 http://<board-ip>:8080/health
3. 安装 ax-model-manager
4. 配置 Manager 读取板端资源
```

注意：

```text
tempmon 不包含在 ax-model-manager 仓库中。
ax-model-manager 也不包含 tempmon 源码。
```

---

## 10. Agent 检查清单

修改本项目后，至少确认：

```text
[ ] python3 -m unittest discover -v 通过
[ ] gcc -O2 -Wall -Wextra -Werror -pthread 编译通过
[ ] 没有真实 IP / token / 内部路径
[ ] VERSION 与 AGENT_VERSION 一致
[ ] README / USER_GUIDE / skills 文档同步
[ ] /health 与 /api/stats 可访问
[ ] /api/all 与 /api/history 可访问
[ ] Web UI 可加载
[ ] 主动注册不会覆盖错误配置
```

---

## 11. License

MIT License，见 [LICENSE](../LICENSE)。
