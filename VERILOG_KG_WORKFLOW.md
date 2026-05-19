# KGCompass Verilog 工作流程

1. 运行入口
   - 组件：`run_verilog_repair.sh`
   - 输入：实例 `instance_id`、本地环境变量、`benchmarks/verilog_repair_cases.json`
   - 输出：本次实验目录、日志目录、工作区副本
   - 作用：准备可写工作区，确保原始案例仓库不被修改

2. 仓库与任务加载
   - 组件：`kgcompass/benchmark.py`、`kgcompass/fl.py`
   - 输入：实例信息、仓库路径、问题描述
   - 输出：任务元数据、issue 文本、候选仓库文件
   - 作用：读取案例信息，建立本次修复任务的基础上下文

3. Verilog 解析
   - 组件：`kgcompass/language_factory.py`、`kgcompass/verilog_ast.py`
   - 输入：`src/rtl`、`src/include`、`tb` 下的 Verilog/SystemVerilog 文件
   - 输出：`Class`、`Method`、`Signal`、`Port`、`Parameter`、`Macro`、`State`、`Branch`、`Condition`、`GenerateBlock`、`Assertion`、`Testbench` 等实体，以及它们之间的边
   - 作用：把 RTL 代码拆成可检索的结构单元和信号关系单元

4. 知识图谱写入
   - 组件：`kgcompass/knowledge_graph.py`
   - 输入：解析器返回的实体与关系
   - 输出：Neo4j 中的节点、语义边和 `RELATED` 兼容边
   - 作用：构建仓库级知识图谱，支持后续路径检索与解释

5. KG 定位
   - 组件：`kgcompass/fl.py`
   - 输入：issue 文本、知识图谱、候选文件
   - 输出：`related_entities`、`edit_targets`、`evidence_entities`、`fault_anchor_entities`
   - 作用：从图里找出最相关的 RTL 目标与证据实体

6. 子图整理
   - 组件：`kgcompass/verilog_kg_digest.py`、`kgcompass/verilog_kg_slice.py`
   - 输入：定位结果、issue 文本、仓库路径
   - 输出：`Bug KG Digest`、`fault neighborhood`、候选文件集合、候选源代码上下文
   - 作用：把定位到的图结构整理成适合 LLM 阅读的子图摘要

7. 生成 Agent
   - 组件：`kgcompass/repair_loop.py` 中的 `build_generation_prompt()`
   - 输入：问题描述、KG 定位摘要、候选实体路径、候选文件全文或片段、全局 RTL 规则
   - 输出：首轮补丁
   - 作用：基于知识图谱和源码上下文生成第一次可应用的修复补丁

8. 补丁应用
   - 组件：`kgcompass/repair.py`
   - 输入：LLM 补丁文本、工作区副本源码
   - 输出：应用后的工作区、diff、补丁应用结果
   - 作用：把 LLM 输出转换成实际文件修改，失败则进入反馈链路

9. 验证
   - 组件：`kgcompass/verilog_validation.py`
   - 输入：应用后的工作区、验证配置
   - 输出：编译结果、targeted 结果、regression 结果、coverage 结果
   - 作用：检查补丁是否能编译、是否复现目标问题、是否破坏回归、是否满足覆盖要求

10. 失败分析
   - 组件：`kgcompass/repair_loop.py` 中的 `FailureAnalyzer`
   - 输入：验证结果、补丁应用结果、历史尝试
   - 输出：固定签名的失败分析 `FailureAnalysis`
   - 作用：把失败归一成稳定类别，供下一轮提示词使用

11. Debug Agent
   - 组件：`kgcompass/repair_loop.py` 中的 `build_debug_diagnosis_prompt()`、`build_debug_patch_prompt()`
   - 输入：失败分析、当前工作区源码、局部故障上下文、历史尝试、经验记录
   - 输出：修正后的补丁建议
   - 作用：围绕失败证据继续修补，直到验证通过或达到轮数上限

12. 经验累计
   - 组件：`kgcompass/experience_store.py`
   - 输入：每轮尝试、失败签名、成功补丁、验证摘要
   - 输出：可检索经验片段
   - 作用：让后续实例复用相似失败的修复经验

13. 结果落盘
   - 组件：`kgcompass/repair.py`
   - 输入：最终补丁、验证报告、实验过程日志
   - 输出：`patch`、`diff`、`validation_summary.json`、`repair_summary.json`
   - 作用：保存最终修复结果和实验轨迹
