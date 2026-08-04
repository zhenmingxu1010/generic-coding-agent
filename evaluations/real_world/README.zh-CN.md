# 真实仓库修复评测

此目录是评测层，而不是 Agent 策略。案例专用的仓库名称、提交和测试路径只存在于案例数据中，`coding_agent/` 下没有代码导入此包。

每个案例都会固定一个公开的缺陷提交及其历史修复提交。运行器会：

1. 创建缺陷基线，并且只应用历史测试变更；
2. 要求隐藏测试在缺陷基线上失败；
3. 检查同一测试在历史修复提交上通过；
4. 向 Agent 提供一个不含隐藏测试的全新缺陷检出；
5. 拒绝对受保护测试的修改；
6. 在 Agent 停止后应用隐藏测试，并记录外部结果。

这样可以避免把“环境本来就通过”、LLM 的成功声明或被削弱的测试误算成修复成功。

## 案例模式

`real_world_case_v2` 增加了仅供评测器使用的元数据，但不会向 Agent 暴露历史源码修复：

- `categories` 支持按 `multi-file`、`api-migration`、`ambiguous-issue` 等类别报告；
- `expected_change_shape` 可为 `localized`、`multi_file` 或 `either`；
- `hidden_test_paths` 可以包含多个历史测试路径；
- `test_environment` 可以声明非敏感测试控制，例如关闭历史网络测试。

运行器仍兼容 `real_world_case_v1`。变更形态元数据只是描述，不强制 Agent 复现历史补丁；隐藏行为始终是验收依据。

运行器会把目标解释器的虚拟环境目录放到 `PATH` 前面，提供隔离的 `HOME`，禁用用户 site，并拒绝案例覆盖 `HOME`、`PATH`、Python 导入路径或 `AGENT_*` 配置。

案例还可以声明评测器拥有的 `hidden_files`。它们只会安装到缺陷基线、历史修复对照和 Agent 运行后的验收工作区，在 Agent 工作期间绝不会出现。这可支持原始历史回归中缺失的兼容性检查。

示例：

```bash
python -m evaluations.real_world.runner \
  --case evaluations/real_world/cases/pysnooper-file-output.json \
  --source-repo /path/to/full/PySnooper-clone \
  --work-root /path/to/disposable/evaluation-workspaces \
  --test-python /path/to/target-test-python \
  --agent-python .venv/bin/python \
  --agent-project . \
  --result .agent_runs/real-world/pysnooper-file-output.json
```

源码克隆和目标测试环境由调用方显式提供。网络访问和依赖安装位于基准运行器之外，因此环境失败会被明确暴露，运行也更容易复现。

## 脱敏指标

收集一个或多个原始结果：

```bash
python -m evaluations.real_world.collect_results \
  .agent_runs/real-world/*.json \
  --case-dir evaluations/real_world/cases \
  --run-date YYYY-MM-DD \
  --scope-note "Describe exactly what this sample does and does not prove." \
  --output evaluations/real_world/summary.json
```

`real_world_summary_v2` 会报告：

- 评测器拥有的外部验收；
- Agent 成功声明和最终门禁的真/假阳性、真/假阴性；
- 成功声明的精确率与召回率；
- 受保护测试是否被修改；
- 变更的项目文件、顶层区域和多文件变更；
- 耗时、模型调用和报告的 Token 使用；
- 分类别解决情况。

环境不可达的案例仍会显示，但不会计入解决率和最终门禁比率的分母。
