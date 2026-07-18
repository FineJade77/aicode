from __future__ import annotations

from app.models.provider import CompletionRequest, StreamEvent, ToolCallRequest, Usage


class FakeProvider:
    provider_name = "fake"

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self.turns = list(turns)
        self.calls: list[CompletionRequest] = []

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request: CompletionRequest):
        self.calls.append(request)
        for event in self.turns.pop(0):
            yield event


def text_turn(text: str, input_tokens: int = 10, output_tokens: int = 5) -> list[StreamEvent]:
    return [
        StreamEvent(type="text_delta", text=text),
        StreamEvent(type="done", usage=Usage(input_tokens, output_tokens), model="fake-model"),
    ]


def tool_turn(name: str, arguments: dict, call_id: str = "tc_1", text: str = "") -> list[StreamEvent]:
    events = []
    if text:
        events.append(StreamEvent(type="text_delta", text=text))
    events.append(StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=call_id, name=name, arguments=arguments)))
    events.append(StreamEvent(type="done", usage=Usage(10, 5), model="fake-model"))
    return events
