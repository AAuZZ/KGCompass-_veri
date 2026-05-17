KGCompass 当前在本仓库中的 Verilog/SystemVerilog 方法流程，是一个面向仓库级 RTL 修复的闭环系统。它保留原 KGCompass 的基本思想：先用知识图谱把问题从仓库级搜索空间压缩到少量候选代码实体，再用同一个语言模型进行二次定位和补丁生成，最后在隔离工作副本中执行编译与功能验证。整个流程只修改 `workdirs` 下复制出来的工作仓库，`verilog_repair_cases` 中的案例仓库始终作为只读基线存在，避免实验污染原始 benchmark。

流程入口是 `run_verilog_repair.sh`。脚本读取 `benchmarks/verilog_repair_cases.json` 中的实例配置，包括 `instance_id`、仓库标识、问题描述和验证配置。启动后，脚本会检查源仓库是否存在，创建 `tests/<instance>_<model>` 和 `workdirs/<instance>_<model>` 运行目录，把源仓库复制到工作区，然后依次调用 `kgcompass/fl.py`、`kgcompass/llm_loc.py`、`kgcompass/fix_fl_line.py` 和 `kgcompass/repair.py`。所有定位结果、模型原始输出、补丁、diff、验证日志和最终摘要都会落在 `tests` 对应目录中。

项目中所有调用语言模型的组件统一使用 `MODEL_NAME`。`kgcompass/config.py` 从环境变量读取 `MODEL_NAME`，`llm_loc.py` 和 `repair.py` 都直接使用这个统一模型，不再分别使用定位模型或修复模型覆盖项。这样做是为了保证 KG 二次定位、首次补丁生成和验证反馈修复使用同一个模型行为，便于复现实验和分析失败原因。如果要切换到 DeepSeek V4 Pro，只需要在运行前设置 `MODEL_NAME=deepseek-v4-pro`。对于推理模型，系统会忽略 reasoning 内容，只截取最终回复内容；修复阶段也支持通过 `REPAIR_STREAM=0` 使用非流式调用，以避免推理模型破坏补丁格式。

知识图谱构建由 `kgcompass/fl.py` 和语言解析层完成。Verilog 解析采用轻量扫描与 AST 解析结合的方式：轻量扫描负责仓库文件发现、宏和 include 线索、模块候选和 issue 文本引用；`kgcompass/verilog_ast.py` 基于 `tree-sitter-systemverilog` 抽取更精确的 RTL 结构，包括 module/interface/package/program、端口、信号、参数、连续赋值、always/initial 块、function/task、实例化、generate、条件编译范围、简单状态和转移线索。解析结果会写入 Neo4j，节点保留 KGCompass 兼容标签，同时增加 Verilog 语义属性，例如 `verilog_kind`、`parse_source` 和 `parse_confidence`。

Verilog KG 的关系表达以语义边为主，同时保留 `RELATED` 兼容层。主关系包括 `CONTAINS`、`MENTIONS`、`READS`、`WRITES`、`DRIVES`、`FEEDS`、`CONNECTS`、`INSTANTIATES`、`DEFINES`、`GUARDS`、`TRANSITIONS_TO`、`TESTS` 和 `EXERCISES` 等。`RELATED` 仍会同步写入，用于兼容旧 KGCompass 查询和旧 JSON 消费逻辑，但 Verilog 场景下的路径解释和可视化应优先看真实语义关系。KG 输出会区分可编辑目标和证据实体：always、assign、function/task、instance、module body 属于候选编辑目标；Signal、Port、Parameter、Macro、State、GenerateBlock、Assertion、Testbench 更偏向证据。

`kgcompass/llm_loc.py` 在 KG 初步定位基础上做二次定位。它把问题描述、KG 候选实体、路径关系和 RTL 证据整理成 Verilog 专用提示词，让模型返回更接近实际补丁落点的 JSON 定位结果。与 Python 函数级定位不同，Verilog 定位允许返回 module body、always 块、assign、function、task、实例化语句等 RTL 编辑单元。随后 `kgcompass/fix_fl_line.py` 合并 KG 定位和 LLM 定位，去重、排序并规范化路径，生成 repair 阶段使用的最终定位文件。

修复阶段由 `kgcompass/repair.py` 负责编排，提示词由 `kgcompass/repair_loop.py` 构建。当前修复分为 Generation Agent 和 Debug Agent。Generation Agent 只负责首次生成可应用的最小补丁，它使用 issue、KG 定位、候选 RTL 源码和全局 RTL 规则，目标是先产出格式正确、能落到工作副本上的补丁。Debug Agent 只在补丁已经进入验证之后工作，它不再依赖 KG 定位做主判断，而是以验证失败日志、当前工作副本源码、上一轮尝试记录和失败签名为核心，专门修复 compile、targeted、regression 或 coverage 反馈暴露的问题。

Debug Agent 现在采用受控两阶段流程。每一轮 debug 先进入 Debug Diagnose 阶段，模型只输出诊断 JSON，不允许生成补丁；这个 JSON 需要说明失败假设、observed signal、期望行为、跨文件信号传播链、需要查看的 RTL 文件和候选编辑文件。系统随后执行 Context Expansion，从当前候选工作副本读取 `need_files` 中的源码，小文件全文注入，大文件围绕 observed signal、候选信号和传播链关键词切窗口。最后进入 Debug Patch 阶段，模型只能基于诊断结果、扩展后的当前源码和验证失败证据输出 line-range REPLACE 补丁。这样 debug agent 可以主动请求跨文件上下文，但不能自由读写文件，补丁应用仍然由系统控制。

为了让 debug agent 更稳定地理解跨文件信号传播，当前流程还会从前阶段 `final_locations` 的 `related_entities` 中抽取一段压缩的 Debug KG Context。它不是完整 KG，也不会重新做 KG ranking，而是把 direct drivers、top-level wiring、config/register evidence、editable targets、evidence entities 和与 observed signal 相关的 KG path hints 整理成一个短的结构索引。Debug Diagnose 和 Debug Patch 都会看到这段上下文；验证失败信息仍然是最高优先级，KG Debug Context 只负责提示模型 `regs -> top -> shifter -> observed signal` 这类跨文件 RTL 结构链路。

Verilog 补丁格式使用 line-range REPLACE，而不是旧的 SEARCH/REPLACE 主路径。模型必须输出文件路径、起止行号和替换文本，应用器直接读取工作副本中的真实源码并替换该闭区间行号。格式固定为 `### file`、`- start_line: N`、`- end_line: M`、`<<<<<<< REPLACE`、替换代码、`>>>>>>> REPLACE`。这种方式避免让模型反复抄写原始代码块，也减少 Verilog 中 begin/end、端口列表和缩进变化导致的搜索匹配失败。旧 SEARCH/REPLACE 仍保留兼容入口，但 Verilog 主流程要求只输出 REPLACE。

全局 RTL 约束写在 `RTL_REPAIR_RULES.md` 中，并会注入 generation 和 debug 提示词。该文件现在是一条一条的规则，强调最小编辑、保持端口和位宽、保持时序风格、不要修改 testbench、闭合信号传播链、targeted failure 优先修复 observed signal 的直接 driver。对于类似 SPI CPOL/CPHA 的 bug，提示词会要求模型明确区分寄存器解码、顶层连线、shifter 直接驱动、idle 行为和 active transaction 行为，避免只做局部看似合理但不能通过 targeted test 的修改。

验证层由 `kgcompass/verilog_validation.py` 实现。它按照 benchmark 配置依次执行 compile、targeted、regression 和 coverage。compile 只检查 RTL 与 testbench 是否能通过 Icarus Verilog 编译；targeted test 是专门复现当前 issue 的功能测试，baseline 通常应失败，candidate 必须通过；regression test 用于保护已有功能；coverage 当前采用 testbench 自报 functional coverage bin 的方式，不依赖额外仿真覆盖工具。验证报告会记录命令、返回码、stdout/stderr 摘要、失败行、刺激窗口、observed signal 和 timing signature。

反馈闭环的关键是失败归因。`FailureAnalyzer` 会把失败归一成固定签名，例如 `patch_apply_failed`、`compile_failed`、`targeted_failed`、`regression_failed`、`coverage_shortfall` 和 `repeated_no_progress`。Generation 阶段默认最多尝试 3 次，主要处理格式和可应用性问题；Debug 阶段默认最多尝试 3 次，只在补丁通过应用并进入验证后启动。targeted 失败时，debug diagnose 和 debug patch 都会显式使用 RTL Signal Propagation Checklist，要求模型闭合“配置来源、顶层连线、直接 driver、idle/not-active 输出、立即观察窗口”这条链，再输出最小替换补丁。

当前 benchmark 包括轻量的 `verilog_demo__uart_idle-0001` 和复杂的 `verilog_demo__spi_flash_ctrl` 系列实例。前者用于快速 smoke 验证整条链路；后者包含寄存器控制、顶层连线、子模块 driver、FIFO/IRQ/状态机和 testbench 的多文件联动，用于评估 KG 定位、二次定位、补丁生成和验证反馈是否真正有效。复杂案例中 targeted 失败不是验证过严，而是说明候选补丁没有修复 issue 的核心行为；只有 compile、targeted、regression 和 coverage 全部通过，系统才接受最终补丁。
