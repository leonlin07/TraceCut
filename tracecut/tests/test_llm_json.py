from tracecut.providers.llm import extract_json_object


def test_extract_json_from_fence():
    assert extract_json_object('```json\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_from_surrounding_text():
    assert extract_json_object('result: {"value": 3} done') == {"value": 3}
