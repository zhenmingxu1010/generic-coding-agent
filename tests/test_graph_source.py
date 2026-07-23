from pathlib import Path

from coding_agent.nodes.act import ACT_SYSTEM
from coding_agent.nodes.repair import REPAIR_SYSTEM


def test_graph_uses_langgraph_stategraph():
    text = Path("coding_agent/graph.py").read_text(encoding="utf-8")
    assert "from langgraph.graph import StateGraph" in text
    assert "builder = StateGraph" in text
    assert "compile(checkpointer=" in text


def test_graph_context_route_can_go_to_verify():
    text = Path("coding_agent/graph.py").read_text(encoding="utf-8")
    assert '"verify": "verify"' in text


def test_graph_has_strategy_reflection_node():
    text = Path("coding_agent/graph.py").read_text(encoding="utf-8")
    assert "strategy_reflection" in text
    assert '"strategy_reflection": "strategy_reflection"' in text


def test_repair_prompts_require_minimal_compatibility_preserving_changes():
    for prompt in (ACT_SYSTEM, REPAIR_SYSTEM):
        assert "special-case branches" in prompt
        assert "compatibility constraint" in prompt
