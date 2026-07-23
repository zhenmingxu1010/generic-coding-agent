from coding_agent.core.utils import extract_json_object


def test_extract_json_from_fence():
    obj = extract_json_object('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_extract_json_from_mixed_text():
    obj = extract_json_object('prefix {"tool": "read_file", "args": {"path": "x.py"}} suffix')
    assert obj["tool"] == "read_file"


def test_extract_json_repairs_invalid_backslash_escape():
    obj = extract_json_object('{"action": {"tool": "search_text", "args": {"pattern": "\\d+"}}}')
    assert obj["action"]["args"]["pattern"] == r"\d+"


def test_extract_json_repairs_literal_newline_inside_string():
    text = '{"action": {"tool": "write_file", "args": {"path": "x.py", "content": "print(1)\nprint(2)"}}}'
    obj = extract_json_object(text)
    assert "print(2)" in obj["action"]["args"]["content"]
