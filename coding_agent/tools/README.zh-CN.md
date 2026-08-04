# 工具模块布局

所有 Agent 工具都位于 `coding_agent/tools`。

## 必需结构

- `base.py`
  - 定义通用工具协议：`ToolSpec`、`BaseTool`、`FunctionTool` 和 `ToolRegistry`。
- `registry.py`
  - 声明内置工具规格。
  - 将每个规格绑定到执行器。
  - 暴露 `DEFAULT_TOOL_REGISTRY`、`execute_tool` 和提示模式辅助函数。
- `*_tools.py`
  - 按领域组织具体工具实现。
  - 每个具体函数都必须返回 `ToolResult`。

## 添加工具

1. 在 `file_tools.py`、`shell_tools.py`、`test_tools.py` 或新的 `*_tools.py` 领域文件中实现具体函数。
2. 在 `registry.py` 中添加 `ToolSpec`。
3. 在 `TOOL_EXECUTORS` 中注册执行器。
4. 针对以下内容添加聚焦测试：
   - 提示模式暴露；
   - 参数验证；
   - 读写类别等策略元数据；
   - 结构化 `ToolResult` 输出。

## 规则

- 不要在图节点中增加工具执行分支。
- 不要在注册表之外维护另一套硬编码工具名称集合。
- 工具实现不要返回原始字典。
- 不要静默接受非规范参数名；应返回结构化模式反馈，让 LLM 修复工具调用。
