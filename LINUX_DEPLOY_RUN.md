# KGCompass Verilog Linux 部署与运行指南

本文档说明如何在 Linux 环境下部署并运行当前仓库中的 Verilog/SystemVerilog 仓库级修复流程。该流程仍然保持现有结构：先构建 KG，再做 LLM 定位，随后生成补丁，并在隔离工作副本中执行验证。

## 1. 环境前提

建议准备以下工具：

- Python 3.10+ 或 3.11+
- Conda 或 Miniconda
- Git
- Neo4j 5.x
- APOC / GDS 插件
- Icarus Verilog (`iverilog` / `vvp`)
- Bash

如果使用推理模型或 OpenAI 兼容接口，还需要可用的 API Key 和 Base URL。

## 2. 关键差异点

Linux 下与 Windows 版最重要的差异有：

1. 不需要 Git Bash，直接使用系统 `bash` 即可。
2. 路径分隔符是 `/`，但仓库脚本和 Python 代码已经做了兼容处理。
3. Neo4j 一般通过 systemd、tarball 或 Docker 启动，不依赖 Neo4j Desktop。
4. `iverilog` / `vvp` 通常可直接通过包管理器安装。
5. 路径长度限制比 Windows 宽松，但仍建议避免过深的嵌套临时目录。
6. 如果使用 WSL，不建议再套一层 Windows 路径调用；优先纯 Linux 运行。

## 3. 推荐目录结构

建议保持以下目录：

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

`verilog_repair_cases/` 只作为只读 benchmark 源仓库，`workdirs/` 用于每次实验的临时工作副本，`tests/` 保存运行产物。

## 4. Python 环境

创建并激活 conda 环境：

```bash
conda create -n kgcompass python=3.11 -y
conda activate kgcompass
```

安装依赖：

```bash
pip install -r requirements.txt
```

如果仓库已经配置好 tree-sitter、Neo4j、openai、datasets、torch 等依赖，建议直接使用 requirements 一次性安装，不要拆成多轮手工装包。

## 5. Icarus Verilog

Ubuntu / Debian 可直接：

```bash
sudo apt-get update
sudo apt-get install -y iverilog
```

检查版本：

```bash
iverilog -V
vvp -V
```

如果系统包版本过旧，也可以从发行版软件源之外单独安装，但对当前流程来说，能编译 SystemVerilog 常用语法即可。

## 6. Neo4j

建议安装 Neo4j Server 版，并启用 APOC / GDS 插件。

启动方式示例：

```bash
neo4j console
```

或通过 systemd：

```bash
sudo systemctl start neo4j
```

连接检查：

```bash
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4jpassword "RETURN 1 AS ok"
```

如果你已经配置了用户名和密码，请把它们写入 `.env`。

## 7. `.env` 配置

建议至少包含：

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

- `MODEL_NAME` 是唯一的模型入口。
- `REPAIR_STREAM=0` 和 `LOC_STREAM=0` 对推理模型更稳。
- `PYTHONUNBUFFERED=1` 能让日志更及时输出。

## 8. 启动流程

单次运行示例：

```bash
export FORCE_RERUN=1
export RUN_MODEL_NAME=deepseek-v4pro
./run_verilog_repair.sh verilog_demo__uart_idle-0001
```

复杂仓库示例：

```bash
export FORCE_RERUN=1
export RUN_MODEL_NAME=deepseek-v4pro
./run_verilog_repair.sh verilog_demo__spi_flash_ctrl-0001
```

如果你的 Python 不在 PATH 中，可以设置：

```bash
export PYTHON_BIN=/path/to/conda/envs/kgcompass/bin/python
```

## 9. 运行产物

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

- `run.log`：总运行日志
- `repair_progress.log`：generation/debug 关键进度日志
- `validation_attempts/`：每一轮补丁、验证和差异产物
- `repair_summary.json`：最终是否修复成功

## 10. 常见问题

### 10.1 Neo4j 没启动

表现：

- `bolt://localhost:7687` 不可达
- KG 构建阶段报连接失败

处理：

```bash
cypher-shell -a bolt://localhost:7687 -u neo4j -p neo4jpassword "RETURN 1"
```

如果失败，先启动 Neo4j，再确认 APOC/GDS 是否可用。

### 10.2 iverilog 不支持某些语法

表现：

- targeted 或 regression 编译失败
- 报语法或端口错误

处理：

- 检查 `iverilog -V`
- 尽量使用当前 benchmark 中已有的 SystemVerilog 子集

### 10.3 远程 API 输出格式不稳定

表现：

- LLM localization JSON 无法解析
- patch 被压扁成一行

处理：

- 确认 `MODEL_NAME` 设置正确
- 保持 `REPAIR_STREAM=0`
- 必要时打开 `KGCOMPASS_VERBOSE_PROMPTS=1` 观察 prompt

### 10.4 路径过长

Linux 一般不严重，但仍建议：

- 不要把仓库嵌套在过深的目录
- 不要把 `workdirs` 放到超长路径里

## 11. 验证检查

建议先做静态检查：

```bash
python -m py_compile kgcompass/repair.py kgcompass/repair_loop.py kgcompass/llm_loc.py kgcompass/verilog_validation.py
```

再跑轻量案例：

```bash
./run_verilog_repair.sh verilog_demo__uart_idle-0001
```

最后再跑复杂案例：

```bash
./run_verilog_repair.sh verilog_demo__spi_flash_ctrl-0001
```

## 12. 运行原则

- 源仓库 `verilog_repair_cases/` 必须保持只读。
- 所有补丁只允许落到 `workdirs/`。
- generation/debug 轮数默认都是 3。
- debug 阶段会注入压缩版 Debug KG Context，用于跨文件信号传播导航。
- 最终成功标准仍然是 compile、targeted、regression、coverage 全部通过。

