# KGCompass Verilog 本地环境说明

本文记录当前 Windows 本地环境下，KGCompass 的 Verilog/SystemVerilog 仓库级修复流程、依赖、启动方式和运行产物。这里的说明必须和当前代码保持一致：补丁主路径是 line-range `REPLACE`，调试流程分为 `Debug Diagnose` 和 `Debug Patch`，所有 LLM 调用统一使用 `MODEL_NAME`。

## 1. 当前流程

1. `kgcompass/fl.py` 构建 Verilog KG 并完成 fault localization。
2. `kgcompass/llm_loc.py` 基于 KG 做二次定位。
3. `kgcompass/fix_fl_line.py` 合并、去重并规范化定位结果。
4. `kgcompass/repair.py` 负责生成补丁并编排修复循环。
5. `kgcompass/repair_loop.py` 负责 Generation Agent 和 Debug Agent 的提示词、失败反馈和经验回填。
6. `kgcompass/verilog_validation.py` 负责 compile、targeted、regression、coverage 验证。

整个流程只修改 `workdirs/...` 里的工作副本，`verilog_repair_cases/...` 始终保持只读。

## 2. 目录结构

```text
KGCompass/
  benchmarks/
    verilog_repair_cases.json
  kgcompass/
    benchmark.py
    config.py
    fix_fl_line.py
    fl.py
    knowledge_graph.py
    language_factory.py
    llm_loc.py
    repair.py
    repair_loop.py
    verilog_ast.py
    verilog_validation.py
  verilog_repair_cases/
    verilog_demo__spi_flash_ctrl/
  workdirs/
  tests/
  .env.example
  LOCAL_ENV_SETUP.md
  LINUX_DEPLOY_RUN.md
  requirements.txt
  run_verilog_repair.sh
  VERILOG_KG_WORKFLOW.md
  RTL_REPAIR_RULES.md
```

## 3. 已确认环境

- 工作目录: `C:\Users\KAVEN\Desktop\KGCompass`
- Conda 环境: `kgcompass`
- Python: `3.11.15`
- Git Bash: `C:\Program Files\Git\bin\bash.exe`
- Icarus Verilog: `C:\Users\KAVEN\tools\iverilog\bin\iverilog.exe`
- vvp: `C:\Users\KAVEN\tools\iverilog\bin\vvp.exe`
- Neo4j Browser: `http://localhost:7474/browser/`
- tree-sitter: `0.25.2`
- tree-sitter-systemverilog: `0.3.1`

## 4. 环境检查

```powershell
conda run -n kgcompass python -c "import sys; print(sys.executable)"
conda run -n kgcompass python -m py_compile kgcompass/verilog_ast.py kgcompass/verilog_validation.py kgcompass/repair.py kgcompass/repair_loop.py
conda run -n kgcompass iverilog -V
conda run -n kgcompass vvp -V
```

如果需要显式指定工具路径，可在 `.env` 里配置：

```env
IVERILOG_BIN=C:/Users/KAVEN/tools/iverilog/bin/iverilog.exe
VVP_BIN=C:/Users/KAVEN/tools/iverilog/bin/vvp.exe
```

## 5. Neo4j

当前使用本机 Neo4j Desktop DBMS：

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=neo4jpassword
```

启动示例：

```powershell
$neo4jHome='C:\Users\KAVEN\.Neo4jDesktop\relate-data\dbmss\dbms-8a7ac7dd-19f7-47a2-969a-78ae8486d280'
$neo4jBat=Join-Path $neo4jHome 'bin\neo4j.bat'
Start-Process -FilePath $neo4jBat -ArgumentList 'console' -WorkingDirectory $neo4jHome -WindowStyle Hidden
```

检查连通性：

```powershell
Get-NetTCPConnection -LocalPort 7687
```

## 6. `.env` 推荐内容

```env
DEEPSEEK_API_KEY=your_deepseek_key_here
OPENAI_COMPAT_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-v4-pro

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=neo4jpassword

BENCHMARK_NAME=verilog-local
LOCAL_BENCHMARK_PATH=benchmarks/verilog_repair_cases.json
VERILOG_SOURCE_REPOS_DIR=verilog_repair_cases
VERILOG_WORK_ROOT=workdirs
VERILOG_REPAIR_MAX_ATTEMPTS=3
VERILOG_GENERATION_MAX_ATTEMPTS=3
VERILOG_DEBUG_MAX_ATTEMPTS=3
VERILOG_ALLOW_UNVALIDATED=0

REPAIR_STREAM=0
LOC_STREAM=0
PYTHONUNBUFFERED=1

IVERILOG_BIN=C:/Users/KAVEN/tools/iverilog/bin/iverilog.exe
VVP_BIN=C:/Users/KAVEN/tools/iverilog/bin/vvp.exe
JAVA_HOME=C:/Users/KAVEN/anaconda3/envs/kgcompass/Library
```

说明：

- `MODEL_NAME` 是唯一模型入口。
- `REPAIR_STREAM=0` 和 `LOC_STREAM=0` 更适合推理模型，避免输出格式被打断。
- 生成和调试默认最多各 3 次。

## 7. Benchmark

当前保留的 Verilog benchmark 是：

- `verilog_demo__spi_flash_ctrl`

包含 3 个实例：

- `verilog_demo__spi_flash_ctrl-0001`
- `verilog_demo__spi_flash_ctrl-0002`
- `verilog_demo__spi_flash_ctrl-0003`

## 8. 运行方式

建议这样启动：

```powershell
$env:PYTHON_BIN='C:/Users/KAVEN/anaconda3/envs/kgcompass/python.exe'
$env:FORCE_RERUN='1'
& 'C:/Program Files/Git/bin/bash.exe' -lc 'cd /c/Users/KAVEN/Desktop/KGCompass && ./run_verilog_repair.sh verilog_demo__spi_flash_ctrl-0001'
```

运行产物默认写到：

```text
tests/<instance>_<model>/
```

工作副本在：

```text
workdirs/<instance>_<model>/repos/<repo_name>/
```

## 9. 产物说明

每次实验通常会生成：

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

说明：

- `run.log`：总运行日志
- `repair_progress.log`：generation/debug 阶段进度日志
- `validation_attempts/`：每轮补丁和验证结果
- `repair_summary.json`：最终是否修复成功

## 10. 调试循环

当前调试流程是分阶段的：

1. Generation Agent 先尝试生成可直接应用的首版补丁。
2. 如果补丁通过验证，则结束。
3. 如果失败，失败信息进入 Debug Agent。
4. Debug Agent 读取验证失败摘要、当前工作副本源码和 Debug KG Context，再生成新补丁。
5. 生成与调试都默认最多 3 次；超过次数仍失败就结束实验。

Debug Agent 看到的上下文包括：

- 验证失败证据
- 当前候选文件源码
- 压缩版 Debug KG Context
- 之前尝试的失败签名

Debug KG Context 只是跨文件传播的结构索引，不替代验证结果本身。

## 11. 补丁格式

Verilog 主路径使用 line-range `REPLACE`，不再以 `SEARCH/REPLACE` 作为主补丁格式。

模型输出需要包含：

```text
### file_path
- start_line: N
- end_line: M
<<<<<<< REPLACE
替换后的代码
>>>>>>> REPLACE
```

应用器会直接拿工作副本中的真实源码，按行区间替换目标块。

## 12. RTL 规则

`RTL_REPAIR_RULES.md` 已经整理成一条一条的全局规则，作为生成和调试阶段的统一约束。

## 13. 维护原则

- 只保留 Verilog 相关 benchmark。
- 所有补丁只允许落在 `workdirs/`。
- 文档必须和代码保持一致。
- 如果补丁格式、模型变量名、尝试次数或验证阶段改了，这个文件要同步更新。
