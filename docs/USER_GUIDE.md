# tempmon 用户指南

本文是 tempmon 的详细使用说明，包括架构、依赖、安装、配置、API、systemd、主动注册、benchmark 采样和指标来源。

如果只需要快速跑起来，请先看根目录的 [README](../README.md)。

---

## 架构

```text
┌──────────────────────────────┐
│ Browser / 上位机 / 脚本       │
└──────────────┬───────────────┘
               │ HTTP
┌──────────────▼───────────────┐        ┌─────────────────────┐
│ monitor.py 聚合器  :8888     │        │ 板端 tempmon agent   │
│ Web Dashboard / JSON API     │◄──────►│ :8080 /api/stats     │
└──────────────────────────────┘        └──────────┬──────────┘
                                                   │ 只读采集
                                        ┌──────────▼──────────┐
                                        │ OS / CMM / NPU / DDR / net │
                                        └─────────────────────┘
```

默认端口：

| 服务 | 端口 | 说明 |
|---|---:|---|
| tempmon agent | `8080` | 板端只读状态 API |
| monitor aggregator | `8888` | 中心 Web Dashboard 与聚合 API |
| llama-server | `18080–18089` | 被发现和展示的推理端口池 |

---

## 功能

### 板端 agent

- NPU 温度与 CPU load / 频率
- NPU 平均利用率（所有 core/vNPU 取平均）
- DDR 带宽（All DDR USED BW，GB/s）
- OS 内存与 CMM 内存池
- 本地磁盘容量
- 网卡累计流量与实时速率
- llama-server 进程数量、RSS、监听端口
- BSP、芯片类型、agent 版本
- 只读 `/api/stats` 与 `/health`
- 可选 Bearer token
- 可选主动注册，适合板卡 IP 可能变化的网络

### 中心聚合器

- 按配置并行轮询多块板卡
- 短网络抖动时保留最近一次有效数据并显示 `Stale`
- 自动扫描 `18080–18089` 范围内的 llama-server
- 探测 `/health`、`/v1/models`、`/slots`
- 展示模型、slot、上下文和生成状态
- 提供 Web Dashboard
- 支持板卡主动注册并持久化最新 IP
- `/api/all` 默认返回轻量当前状态，避免大响应导致页面超时
- `/api/history` 单独提供趋势历史

---

## 依赖

### 板端 agent

编译：

```text
gcc
make（可选）
pthread
```

运行：

```text
Linux / AX8860 / AX8860P
/proc/ax_proc/*
/sys/class/thermal/*
/sys/class/net/*
```

agent 是单文件 C 程序，不依赖第三方库。

### 中心聚合器

```text
Python 3.10+
```

只使用 Python 标准库，不需要 pip 安装依赖。

可选：

```text
curl（健康检查）
systemd（后台服务）
```

---

## 安装

### 1. 获取代码或预编译包

源码：

```bash
git clone https://github.com/AXERA-TECH/tempmon.git
cd tempmon
```

预编译 aarch64 包：

```text
https://github.com/AXERA-TECH/tempmon/releases
```

下载 `tempmon-<version>-linux-aarch64.tar.gz` 后：

```bash
tar -xzf tempmon-<version>-linux-aarch64.tar.gz
chmod +x tempmon
./tempmon 8080
```

### 2. 编译板端 agent

在 AX8860 板端执行：

```bash
gcc -O2 -s -pthread -o tempmon tempmon.c
```

在 x86 主机上交叉编译静态 aarch64 二进制：

```bash
./scripts/build-aarch64.sh
```

产物：

```text
dist/tempmon-<version>-linux-aarch64.tar.gz
dist/tempmon-<version>-linux-aarch64.tar.gz.sha256
```

构建脚本默认使用 Arm GNU Toolchain `13.3.rel1`。

检查：

```bash
./tempmon 8080
```

默认监听：

```text
0.0.0.0:8080
```

如需指定绑定地址和 token：

```bash
./tempmon 8080 \
  --bind 0.0.0.0 \
  --token <agent-token>
```

也可以用环境变量：

```bash
TEMPMON_BIND=0.0.0.0 \
TEMPMON_TOKEN=<agent-token> \
./tempmon 8080
```

### 3. 安装为 systemd 服务

将二进制放到：

```text
/usr/local/bin/tempmon
```

创建 `/etc/systemd/system/tempmon.service`：

```ini
[Unit]
Description=AXERA tempmon board agent
After=network.target

[Service]
Environment=TEMPMON_BIND=0.0.0.0
# Environment=TEMPMON_TOKEN=<agent-token>
ExecStart=/usr/local/bin/tempmon 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启用：

```bash
systemctl daemon-reload
systemctl enable --now tempmon.service
```

验证：

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/api/stats
```

### 4. 启动中心聚合器

```bash
cp boards.example.json boards.json
```

编辑 `boards.json`，将示例地址替换为真实板卡地址：

```json
{
  "boards": [
    {
      "name": "board-a",
      "ip": "<board-a-ip>",
      "port": 8080,
      "token": "<optional-agent-token>"
    },
    {
      "name": "board-b",
      "ip": "<board-b-ip>",
      "port": 8080,
      "llama_port_min": 18080,
      "llama_port_max": 18089
    },
    {
      "name": "board-c",
      "ip": "<board-c-ip>",
      "port": 8080,
      "llama_ports": ["18080-18089", 18500]
    }
  ]
}
```

启动：

```bash
python3 monitor.py
```

打开：

```text
http://<monitor-host>:8888/
```

### 5. 聚合器 systemd 示例

`~/.config/systemd/user/monitor.service`：

```ini
[Unit]
Description=AXERA tempmon aggregator
After=network-online.target

[Service]
WorkingDirectory=<tempmon-repository>
ExecStart=/usr/bin/python3 <tempmon-repository>/monitor.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now monitor.service
```

---

## 配置

### 板卡配置

`boards.json` 字段：

| 字段 | 说明 |
|---|---|
| `name` | 聚合器中显示的板卡名，必须唯一且保持稳定 |
| `ip` | 默认轮询地址 |
| `port` | 板端 tempmon agent 端口 |
| `token` | 可选，板端 agent 的 Bearer token |
| `llama_port_min/max` | 可选，覆盖 llama-server 端口扫描范围 |
| `llama_ports` | 可选，端口列表或 `"lo-hi"` 区间；可覆盖 18080–18089 之外的服务（如 axdiffusion 的 18500） |

`boards.json` 是本地运行时配置，包含真实环境信息，不要提交到 Git。

### 聚合器环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TEMPMON_CONFIG` | `<repo>/boards.json` | 板卡配置文件 |
| `TEMPMON_REGISTRATIONS` | `<repo>/registrations.json` | 主动注册状态文件 |
| `TEMPMON_MONITOR_PORT` | `8888` | 聚合器监听端口 |
| `TEMPMON_REPORT_TOKEN` | 空 | 主动注册接口 token，优先于 token 文件 |

配置文件每轮轮询都会自动重载，新增板卡通常不需要重启聚合器。

---

## 使用方式

### 1. 查看板卡状态

```bash
curl http://<board-ip>:8080/health
curl http://<board-ip>:8080/api/stats
```

如果启用 token：

```bash
curl -H "Authorization: Bearer <agent-token>" \
  http://<board-ip>:8080/api/stats
```

### 2. 打开 Web Dashboard

浏览器访问：

```text
http://<monitor-host>:8888/
```

### 3. 查询聚合 API

```bash
curl http://<monitor-host>:8888/health
curl http://<monitor-host>:8888/api/all
curl http://<monitor-host>:8888/api/history?limit=300
curl http://<monitor-host>:8888/api/boards
```

`/api/all` 默认不包含 history，适合浏览器每秒轮询。如需在单个响应中携带历史：

```bash
curl 'http://<monitor-host>:8888/api/all?history=60'
```

### 4. 板卡主动注册

如果中心服务器地址固定，而板卡可能因 DHCP 或重启改变 IP，可以让板端 agent 定期上报：

```bash
export TEMPMON_BOARD_NAME=board-a
export TEMPMON_REPORT_URL=http://<monitor-host>:8888/api/report
export TEMPMON_REPORT_TOKEN=<report-token>
export TEMPMON_REPORT_INTERVAL=10
./tempmon 8080
```

也可以使用命令行参数：

```bash
./tempmon 8080 \
  --board-name board-a \
  --report-url http://<monitor-host>:8888/api/report \
  --report-token <report-token> \
  --report-interval 10
```

安全规则：

- `POST /api/report` 必须携带 Bearer token；
- 板卡名必须已经存在于 `boards.json`；
- 聚合器使用 TCP 连接的源地址作为新 IP，不信任请求 body 中声明的 IP；
- 地址变化会写入 `registrations.json`；
- `registrations.json` 是本地运行时状态，不要提交到 Git。

聚合器默认从以下文件读取主动注册 token：

```text
~/.config/tempmon/report.token
```

建议权限：

```bash
chmod 600 ~/.config/tempmon/report.token
```

### 5. Benchmark 采样

`bench_sample.py` 可以在运行任意 benchmark 命令时采样某个板卡状态：

```bash
python3 bench_sample.py \
  --api http://<monitor-host>:8888/api/all \
  --board board-a \
  --label pp4096-tg256 \
  --json report.json \
  --csv report.csv \
  --markdown report.md \
  -- ./bench.sh
```

输出包含：

```text
CMM 峰值 / 均值
OS 内存峰值 / 余量
NPU 最高温度
网络速率
llama RSS
推理在线与处理状态
benchmark 退出码和耗时
```

---

## API

### 板端 agent

```http
GET /health
GET /api/stats
```

### 中心聚合器

```http
GET /health
GET /api/all
GET /api/history?limit=300
GET /api/boards
```

---

## 测试

```bash
python3 -m unittest discover -v
```

当前覆盖：

- API 状态探测
- 板卡配置范围检查
- 主动注册与持久化
- 短网络故障 stale 逻辑
- history 环形缓存
- Web 页面 JavaScript 语法
- benchmark 采样和报告导出

---

## 指标来源

| 指标 | 来源 |
|---|---|
| NPU 温度 | `/sys/class/thermal/thermal_zone*/temp` |
| CPU load | `/proc/loadavg` |
| OS memory | `/proc/meminfo` |
| CMM | `/proc/ax_proc/mem_cmm_info` |
| 磁盘容量 | `/proc/mounts` + `statvfs` |
| CPU 频率 | `cpu*/cpufreq/scaling_cur_freq` |
| BSP / 芯片 | `/proc/ax_proc/version`、`/proc/ax_proc/chip_type` |
| NPU TDP | `/proc/ax_proc/tdp` |
| NPU clock | `/sys/kernel/debug/clk/npu*_clk_nn_glb/clk_rate` |
| NPU 平均利用率 | `/proc/ax_proc/npu/top`，采样周期使用 `/proc/ax_proc/npu/top_period_ms`，建议 `1000 ms` |
| DDR 带宽 | `/proc/ax_proc/bw/bw` 的 `All DDR USED BW`；需要 `insmod /soc/ko/ax_perf_monitor.ko`，agent 启动时会自动尝试加载；KB 按 1024 字节换算为 GB/s |
| llama 进程 | `/proc/<pid>/comm`、`/proc/<pid>/status` |
| 监听端口 | `/proc/net/tcp`、`/proc/net/tcp6` |

---

## 本地文件

| 文件 | 是否提交 | 说明 |
|---|---|---|
| `tempmon.c` | 是 | 板端 agent 源码 |
| `monitor.py` | 是 | 中心聚合器 |
| `bench_sample.py` | 是 | benchmark 采样工具 |
| `test_*.py` | 是 | 单元测试 |
| `boards.example.json` | 是 | 脱敏示例配置 |
| `boards.json` | 否 | 本地真实板卡配置 |
| `registrations.json` | 否 | 本地主动注册状态 |
| `*.token` | 否 | 本地 token |

---

## License

MIT License，见 [LICENSE](LICENSE)。
