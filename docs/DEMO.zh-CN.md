# 演示指南

README 中的终端摘要是 [`docs/assets/validated-demo.txt`](assets/validated-demo.txt) 的紧凑渲染。它不包含私有模型配置，只是视觉摘要，不能替代重新运行下列命令。

演示展示真实行为，而不是预先写好的成功记录。请使用一次性工作区和私有模型配置。

## 三分钟演示路径

1. 运行 `python -m coding_agent.check_llm`。
2. 创建空工作区并输入 `写个脚本`。
3. 展示受控的 `clarification_required` 结果和 0 个源码变更。
4. 在同一任务中使用 `统计指定文本文件的行数，并把整数结果打印到终端。` 恢复。
5. 展示生成脚本、直接执行证据、必需原子和 `verified_ok`。
6. 导出审计并检查 `audit_manifest.json`。

可复制命令：

```bash
mkdir -p /tmp/generic-agent-demo

coding-agent \
  --workspace /tmp/generic-agent-demo \
  --task "写个脚本" \
  --thread-id portfolio-demo \
  --clean-agent-state

coding-agent \
  --workspace /tmp/generic-agent-demo \
  --thread-id portfolio-demo \
  --resume \
  --clarification-answer "统计指定文本文件的行数，并把整数结果打印到终端。"

coding-agent-export-audit \
  --workspace /tmp/generic-agent-demo \
  --thread-id portfolio-demo \
  --out /tmp/portfolio-demo-audit.zip \
  --include-workspace
```

## 完整能力演示

渲染一个或全部模型场景：

```bash
python scripts/render_regression_matrix.py --case T09
python scripts/render_regression_matrix.py --all
python scripts/collect_regression_audits.py \
  --audit-dir .agent_runs/regression-audits \
  --strict
```

2026-07-23 的规范记录通过了全部 11 个详细场景验证器。结果和限制见 `docs/VALIDATION.md`。录制公开 GIF 前，请使用干净终端配置，并确认画面中没有端点、密钥、用户名或私有工作区。
