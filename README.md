# AXERA tempmon

[![CI](https://github.com/AXERA-TECH/tempmon/actions/workflows/ci.yml/badge.svg)](https://github.com/AXERA-TECH/tempmon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**tempmon** 是面向 AX8860 / AX8860P 的轻量级板卡状态监控组件：

```text
板端 agent（tempmon.c）
  采集 NPU 温度 / 利用率、CPU、OS 内存、CMM、磁盘、网络和 llama-server 状态

中心聚合器（monitor.py）
  轮询多块板卡，发现 llama-server，提供 Web Dashboard 和 JSON API
```

它适合单板调试、多板管理、性能测试采样，以及作为其他工具的只读状态数据源。

> tempmon 只做状态采集，不代理推理请求，也不控制模型启动。模型生命周期管理请使用
> [ax-model-manager](https://github.com/AXERA-TECH/ax-model-manager)。

---

## 架构

```text
Browser / 上位机 / 脚本
        │ HTTP
        ▼
monitor.py 聚合器 :8888
Web Dashboard / JSON API
        │
        │ 轮询 /api/stats
        ▼
板端 tempmon agent :8080
        │
        ▼
OS / CMM / NPU / network / llama-server
```

默认端口：

| 服务 | 端口 | 说明 |
|---|---:|---|
| tempmon agent | `8080` | 板端只读状态 API |
| monitor aggregator | `8888` | 中心 Web Dashboard 与聚合 API |
| llama-server | `18080–18089` | 被发现和展示的推理端口池 |

---

## 快速上手

### 1. 编译并启动板端 agent

在 AX8860 / AX8860P 板端执行：

```bash
gcc -O2 -s -pthread -o tempmon tempmon.c
./tempmon 8080
```

验证：

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/api/stats
```

### 2. 启动中心聚合器

在一台可访问板卡的机器上：

```bash
cp boards.example.json boards.json
# 编辑 boards.json，把示例地址换成真实板卡地址
python3 monitor.py
```

打开：

```text
http://<monitor-host>:8888/
```

### 3. 生产部署

systemd、主动注册、Bearer token、benchmark 采样等完整说明见：

```text
docs/USER_GUIDE.md
```

---

## 主要功能

- NPU 温度、平均利用率、CPU load / 频率
- OS 内存、CMM 内存池、磁盘、网络速率
- llama-server 进程、RSS、监听端口和 slot 状态
- 多板卡聚合、Web Dashboard、历史趋势
- 板卡主动注册，适合 DHCP / IP 变化环境
- 只读 `/api/stats` 与 `/health`
- 可选 Bearer token
- 纯 Python 标准库聚合器，无需 pip 依赖

---

## 文档

- [用户指南](docs/USER_GUIDE.md)：依赖、安装、配置、API、systemd、主动注册、benchmark 采样
- [Agent / 开发指南](docs/skills.md)：项目结构、数据流、API、测试、修改指南、安全规则
- [ax-model-manager](https://github.com/AXERA-TECH/ax-model-manager)：模型生命周期管理
- [LICENSE](LICENSE)

---

## License

MIT License，见 [LICENSE](LICENSE)。
