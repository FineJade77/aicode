import json

from app.agent.context_budget import CONTEXT_BUDGETS, budgeted_observations_for_model, encoded_chars


def test_budgeted_observations_compacts_large_read_file_text() -> None:
    text = "# src/app.py\n" + "a" * 25_000 + "\nTAIL"
    observations = [
        {
            "tool": "read_file",
            "args": {"path": "src/app.py"},
            "success": True,
            "text": text,
            "data": {"path": "src/app.py", "workspace": "main"},
        }
    ]

    compacted, stats = budgeted_observations_for_model(observations, "coder")

    assert stats["compacted"] is True
    assert compacted[0]["tool"] == "read_file"
    assert compacted[0]["data"]["path"] == "src/app.py"
    assert compacted[0]["text"].startswith("# src/app.py")
    assert compacted[0]["text"].endswith("TAIL")
    assert "[CONTEXT COMPACTED:" in compacted[0]["text"]
    assert compacted[0]["context_compacted"]["text"]["original_chars"] == len(text)


def test_budgeted_observations_enforces_total_budget() -> None:
    observations = []
    for index in range(30):
        observations.append(
            {
                "tool": "search_text",
                "args": {"query": f"needle-{index}"},
                "success": True,
                "text": f"search result {index}\n" + "x" * 6_000,
                "data": {"matches": [f"src/file_{index}.py:1:needle"]},
            }
        )

    compacted, stats = budgeted_observations_for_model(observations, "planner")

    assert stats["compacted"] is True
    assert stats["total_budget_compactions"] > 0
    assert len(compacted) == len(observations)
    assert encoded_chars(compacted) <= CONTEXT_BUDGETS["planner"].total_chars
    assert all(observation["tool"] == "search_text" for observation in compacted)
    assert any("[CONTEXT COMPACTED:" in observation["text"] for observation in compacted)


def test_budget_metadata_is_json_serializable() -> None:
    compacted, stats = budgeted_observations_for_model(
        [{"tool": "read_file", "text": "hello", "data": {"path": "README.md"}}],
        "coder",
    )

    encoded = json.dumps({"observations": compacted, "context_budget": stats}, ensure_ascii=False, sort_keys=True)

    assert "README.md" in encoded
    assert stats["purpose"] == "coder"
