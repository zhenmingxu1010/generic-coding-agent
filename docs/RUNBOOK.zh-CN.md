# 运行手册

## 创建开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

## 配置模型

```bash
cp configs/model.example.yaml configs/model.local.yaml
```

在本地文件中设置 `base_url`、`api_key` 和 `model`。以 `AGENT_LLM_` 开头的环境变量会覆盖文件配置。

```bash
python -m coding_agent.check_llm
```

## 运行模式

只读分析：

```bash
coding-agent --workspace ./project \
  --task "Analyze this repository without modifying it." \
  --thread-id analysis --clean-agent-state
```

在空工作区生成新项目：

```bash
mkdir -p ./scratch-project
coding-agent --workspace ./scratch-project \
  --task "Create a tested Python CLI that converts JSON Lines to CSV." \
  --thread-id generate --clean-agent-state
```

修复现有项目：

```bash
coding-agent --workspace ./project --repair-existing \
  --thread-id repair --clean-agent-state
```

恢复已停止运行：

```bash
coding-agent --workspace ./project --thread-id repair --resume
```

回答待处理的澄清问题并恢复同一运行：

```bash
coding-agent --workspace ./project --thread-id short-task --resume \
  --clarification-answer "Read a text-file path and print its line count."
```

较长答案可以使用 UTF-8 文件：

```bash
coding-agent --workspace ./project --thread-id short-task --resume \
  --clarification-file ./clarification.txt
```

## 检查结果

```bash
python -m json.tool ./project/.agent_runs/repair/final.json
tail -n 20 ./project/.agent_runs/repair/trace.jsonl
```

`final.json` 是权威摘要。请检查 `final.ok`、`stopped_reason`、`final_gate_status`、`requirement_atom_summary`、`verification` 和 `write_scope_audit`。

`clarification_required` 是预期的受控结果，不是运行时崩溃。检查 `clarification_questions`，只回答会改变决策的缺口，并使用相同工作区和任务 ID 恢复。

## 导出审计

```bash
coding-agent-export-audit \
  --workspace ./project \
  --thread-id repair \
  --out ./repair-audit.zip
```

只有源码可以安全共享时才添加 `--include-workspace`。文本审计文件会脱敏常见 Token 模式和本地绝对路径；二进制文件和不常见密钥格式仍需人工检查。发布审计包前检查 `audit_manifest.json`。

## 本地质量检查

```bash
python -m compileall -q coding_agent tests
python -m pytest -q
python -m build
```

## 故障排查

- 连接失败：确认基础 URL 指向 API 根路径（通常以 `/v1` 结尾），并运行 `python -m coding_agent.check_llm`。
- 模型错误：显式设置 `AGENT_LLM_MODEL` 或关闭 `auto_model`。
- 模型内容为空：如果提供商把推理与最终消息分离，在本地配置中关闭思考模式。
- 意外策略阻止：扩大权限前检查 `trace.jsonl` 和 `write_scope_audit`。
- 重复验证失败：检查实际执行命令和失败指纹；理解循环前不要增加预算。
- 直接 Shell 脚本验证可以使用 `sh relative/script.sh ...`，但 `sh -c`、`bash -lc`、重定向和命令链会被刻意拒绝。
