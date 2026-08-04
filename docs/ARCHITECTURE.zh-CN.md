# 架构

## 概览

Generic Coding Agent 是直接基于 LangGraph 构建的有状态工作流。单个 `AgentState` 在确定性路由函数和节点函数之间传递。LLM 调用负责提出意图、计划、动作、审查和修复建议；运行时代码负责范围约束、工具执行、证据收集、预算和最终决策。

项目不直接导入或声明 LangChain。LangGraph 可能间接安装 `langchain-core`。`coding_agent/graph.py` 从 LangGraph 导入 `StateGraph`、`START`、`END` 和 `MemorySaver`，而 `coding_agent/core/llm_client.py` 自行实现 OpenAI 兼容 HTTP 请求。

## 主工作流

1. `intake` 解析任务、操作模式、不变量和范围。
2. `supervisor` 选择高层执行路径。
3. `repo_scan` 构建有界仓库映射和工作区基线。
4. `task_clarify` 区分可从仓库发现的细节、安全默认值和缺失的核心行为；它会记录假设后继续，或在写入源码前产生可恢复的 `clarification_required` 结果。
5. `context_retrieve` 和 `context_compress` 为下一步选择证据。
6. 只读任务使用 `analyze_repo` 与 `analyze_report`。
7. 写入任务使用 `plan`、`file_plan`、`generate_files`、`act` 和 `tool_exec`。
8. `verify` 执行有依据的验证步骤，并把结果绑定到必需行为。
9. 验证失败会流经 `diagnose`、`failure_owner` 和 `repair`。
10. `deliverable_review` 检查需要语义审查的产物。
11. `report` 计算最终门禁，并持久化人类可读和机器可读结果。

路由代码使用动作、轮次、修复调用、重复失败和上下文预算，防止无约束循环。

## 包职责

| 包 | 职责 |
| --- | --- |
| `contracts` | 用户契约、实现默认值、完整性和需求原子 |
| `scope` | 意图、只读策略、写入范围和范围审计 |
| `workspace` | 仓库映射、基线、产物注册表和接口 |
| `tools` | 结构化文件、Shell、Python 和 pytest 操作 |
| `verification` | 验证计划、声明、测试策略和证据审查 |
| `repair` | 故障解析、归属、读取缓存和修复控制器 |
| `memory` | 项目档案、检索、上下文包、追踪和快照 |
| `safety` | 路径与命令防护 |
| `nodes` | LangGraph 节点实现 |
| `ux` | 终端 UI、报告、语言和 Token 统计 |

## 契约与需求原子

任务被拆解为必需和可选原子，每个原子具有产物、行为、约束或质量等类型。验证会针对这些原子记录声明和执行证据。任何必需原子失败或未经验证，都会阻止无条件成功结果。

这种分离非常重要：模型可以建议检查什么，但运行时证据决定检查是否真正成功。

用户需求与 Agent 实现默认值属于两份独立契约。强用户需求需要精确的提示词证据；传统语言或输出布局等低风险缺失细节会被记录为假设，其通用可运行性检查使用 `implementation:*` 原子。会改变核心行为的缺失信息绝不会被静默默认。

## 工具执行

工具实现共享的结构化结果接口。注册表会归一化参数，并返回机器可读的成功、失败、策略和变更元数据。修改文件的工具执行后，必须重新扫描仓库并产生验证义务。

Shell 访问受命令策略过滤，并限制在工作区契约内。验证支持有界标准输入以及直接执行 `sh relative/script.sh ...` 或 `bash relative/script.sh ...`，但仍会阻止内联 Shell、解释器选项、重定向和命令链。这依然不等同于操作系统沙箱。

## 记忆与持久化

`MemorySaver` 提供进程内 LangGraph 检查点。除此之外，Agent 会在 `.agent_runs/<workspace-key>/` 下写入项目记忆、上下文包、追踪和状态快照。进程重启后，CLI 可以从 Agent 自有快照恢复。澄清答案会重建派生任务契约，同时保留原始提示和澄清历史。

## 验证与最终门禁

验证综合命令结果、pytest 结果解析、产物存在性、接口检查、写入范围证据，以及在确定性检查不足时使用的 LLM 语义审查。最终门禁会区分：

- 运行时执行成功；
- 契约完整性；
- 必需行为证据；
- 写入范围合规性；
- 受控失败和警告。

目标不是保证绝对正确，而是在现有证据不足时避免宣称成功。
