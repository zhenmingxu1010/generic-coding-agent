# 能力回归矩阵

该矩阵为 Generic Coding Agent 定义了 11 个与模型提供商无关的端到端场景。它通过在一次性夹具项目上执行完整 LLM 工作流，补充离线单元测试套件。

| ID | 能力 | 主要断言 |
| --- | --- | --- |
| T01 | 只读理解 | 生成有证据的报告，且不写入工作区 |
| T02 | 隔离生成 | 创建新交付物，不修改现有文件 |
| T03 | CLI 行为 | 参数确实改变输入和输出行为 |
| T04 | 接口修复 | 现有测试驱动有界实现修复 |
| T05 | 拒绝零测试 | 未收集到测试时不能判定成功 |
| T06 | 防止污染 | 内部测试不会成为用户交付物 |
| T07 | 修复现有项目 | 源码变更通过保留的项目测试 |
| T08 | 从零生成 | 从头生成可运行 CLI、文档和测试 |
| T09 | 短提示默认值 | 单行任务在明确记录假设后继续 |
| T10 | 歧义停止 | 核心行为缺失时在写源码前暂停 |
| T11 | 口语化检查 | 对话式请求路由到只读分析 |

## 用法

先配置 OpenAI 兼容模型，然后渲染一个案例：

```bash
python scripts/render_regression_matrix.py --case T01
```

渲染全部案例：

```bash
python scripts/render_regression_matrix.py --all
```

默认工作区和审计包都创建在 Git 忽略的 `.agent_runs/` 下。需要时可以覆盖位置：

```bash
python scripts/render_regression_matrix.py \
  --all \
  --agent-repo . \
  --audit-dir /tmp/coding-agent-audits \
  --work-root /tmp/coding-agent-regression
```

每个案例都包含人类可读的 `expected_conditions`。T05 是刻意设计的负面案例，其预期结果就是受控失败。
