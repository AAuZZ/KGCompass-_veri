# KGCompass Verilog Linux 部署与运行

本文说明如何在 Linux 环境中部署并运行当前仓库的 Verilog/SystemVerilog 修复流程。当前流程仍然保持 KGCompass 的主骨架：先构建知识图谱，再做定位，再生成补丁，最后通过验证和调试反馈闭环修复。Linux 版和 Windows 版的核心逻辑一致，差异主要在路径、Neo4j 启动方式和 shell 调用上。

## 1. 前置条件

建议准备以下组件：

- Python 3.10+ 或 3.11+
- Conda / Miniconda / Mambaforge
- Git
- Neo4j 5.x
- APOC / GDS
- Icarus Verilog (`iverilog`, `vvp`)
- Bash
- 可用的 DeepSeek 或兼容 API Key

## 2. 目录结构

建议保持下面的目录布局：

```text
KGCompass/
  benchmarks/
  kgcompass/
  verilog_repair_cases/
  workdirs/
  tests/
  run_verilog_repair.sh
  .env
```

其中 `verilog_repair_cases/` 只放 benchmark 源仓库，`workdirs/` 只放每次实验的工作副本，`tests/` 保存运行产物。

## 3. 创建 conda 环境

```bash
conda create -n kgcompass python=3.11 -y
conda activate kgcompass
pip install -r requirements.txt
```

如果 `requirements.txt` 已经安装完整，可以直接复用现有环境。

## 4. Icarus Verilog

Ubuntu / Debian：

```bash
sudo apt-get update
sudo apt-get install -y iverilog
```

验证版本：

```bash
iverilog -V
vvp -V
```

## 5. Neo4j

Linux 下推荐直接使用 Neo4j Server / tarball / systemd。示例：

```bash
neo4j console
```

或者：

```bash
sudo systemctl start neo4j
```

验证连接：

```bash
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4jpassword "RETURN 1 AS ok"
```

如果使用 APOC / GDS，请确保插件已启用。

## 6. `.env` 推荐内容

```env
MODEL_NAME=deepseek-v4-pro
OPENAI_COMPAT_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=your_key_here

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=neo4jpassword

BENCHMARK_NAME=verilog-local
LOCAL_BENCHMARK_PATH=benchmarks/verilog_repair_cases.json
VERILOG_SOURCE_REPOS_DIR=verilog_repair_cases
VERILOG_WORK_ROOT=workdirs

VERILOG_GENERATION_MAX_ATTEMPTS=3
VERILOG_DEBUG_MAX_ATTEMPTS=3
VERILOG_REPAIR_MAX_ATTEMPTS=3

REPAIR_STREAM=0
LOC_STREAM=0
PYTHONUNBUFFERED=1
```

说明：

- `MODEL_NAME` 是唯一模型入口。
- `REPAIR_STREAM=0` 和 `LOC_STREAM=0` 更适合推理模型。
- 生成与调试默认各 3 次。

## 7. 运行方式

单个轻量实例：

```bash
export FORCE_RERUN=1
export RUN_MODEL_NAME=deepseek-v4pro
./run_verilog_repair.sh verilog_demo__uart_idle-0001
```

复杂实例：

```bash
export FORCE_RERUN=1
export RUN_MODEL_NAME=deepseek-v4pro
./run_verilog_repair.sh verilog_demo__spi_flash_ctrl-0001
```

如果 Python 不在 PATH 中：

```bash
export PYTHON_BIN=/path/to/conda/envs/kgcompass/bin/python
```

## 8. 运行产物

每次实验会生成：

```text
tests/<instance>_<model>/
  run.log
  repair_progress.log
  kg_locations/
  llm_locations/
  final_locations/
  validation_attempts/
  repair_summary.json
  validation_summary.json
```

其中：

- `run.log` 是总运行日志
- `repair_progress.log` 是 generation/debug 关键进度日志
- `validation_attempts/` 保存每轮补丁和验证结果
- `repair_summary.json` 是最终是否修复成功

## 9. 调试流程

当前调试流程是两段式：

1. `Debug Diagnose` 只输出结构化诊断 JSON，不输出补丁。
2. 系统根据诊断请求扩展 RTL 源码上下文。
3. `Debug Patch` 基于验证失败证据、扩展后的源码和 `Bug KG Digest` 输出 line-range `REPLACE` 补丁。

调试上下文里会包含：

- 验证失败证据
- 压缩版 `Bug KG Digest`
- 压缩版 `Debug KG Context`
- 当前工作副本源码窗口
- 之前的尝试记录

## 10. 常见问题

### 10.1 Neo4j 没启动

表现：

- `bolt://localhost:7687` 不可达
- KG 构建失败

处理：

```bash
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4jpassword "RETURN 1"
```

### 10.2 iverilog 不支持某些语法

表现：

- targeted / regression 编译失败
- 语法报错或端口报错

处理：

- 先确认 `iverilog -V`
- 尽量使用 benchmark 中已经验证过的 SystemVerilog 子集

### 10.3 LLM 输出格式不稳

处理：

- 确认 `MODEL_NAME` 设置正确
- 保持 `REPAIR_STREAM=0`
- 必要时打开 `KGCOMPASS_VERBOSE_PROMPTS=1`

### 10.4 路径过长

Linux 下通常不严重，但仍建议：

- 不要把仓库嵌套在过深目录
- 不要把 `workdirs/` 放在超长路径下

## 11. 验证顺序

建议先做静态检查：

```bash
python -m py_compile kgcompass/repair.py kgcompass/repair_loop.py kgcompass/llm_loc.py kgcompass/verilog_validation.py
```

然后跑轻量实例：

```bash
./run_verilog_repair.sh verilog_demo__uart_idle-0001
```

最后再跑复杂实例：

```bash
./run_verilog_repair.sh verilog_demo__spi_flash_ctrl-0001
```

## 12. 运行原则

- `verilog_repair_cases/` 始终只读。
- 补丁只允许落到 `workdirs/`。
- 生成和调试默认各 3 次。
- Debug 阶段会注入压缩版 `Debug KG Context` 和 `Bug KG Digest`。
- 最终成功标准仍然是 compile、targeted、regression、coverage 全部通过。
