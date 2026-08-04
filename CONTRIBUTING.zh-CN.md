# 贡献指南

感谢你帮助改进 Generic Coding Agent。

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m pytest -q
```

## Pull Request

1. 对较大的行为或架构变更，请先创建 Issue。
2. 保持变更的通用性，不要为某个私有仓库或基准案例添加分支、提示词、夹具或回退行为。
3. 为行为变更新增或更新测试。
4. 提交前运行完整离线测试套件。
5. 面向用户的行为变化时，更新文档和 `CHANGELOG.md`。

## 设计要求

- 安全和范围决策应尽可能具有确定性。
- 成功的最终结果必须有已执行证据支持。
- 模型专用行为应放在配置或适配器中，而不是任务逻辑中。
- 新工具必须使用结构化工具接口并声明副作用。
- 兼容性回退必须针对一类有文档记录的失败，并配有回归测试。

提交贡献即表示你同意按 Apache License 2.0 许可这些贡献。
