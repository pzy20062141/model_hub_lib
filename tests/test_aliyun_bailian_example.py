from __future__ import annotations

import json

import httpx
import pytest

from examples.aliyun_bailian_quota_demo import QUERY, run_demo


@pytest.mark.asyncio
async def test_aliyun_bailian_quota_demo() -> None:
    observed: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        observed.append(payload)
        if payload.get("stream"):
            content = (
                'data: {"id":"chatcmpl_stream","model":"qwen-plus",'
                '"choices":[{"delta":{"content":"我可以回答问题、"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl_stream","model":"qwen-plus",'
                '"choices":[{"delta":{"content":"总结内容并协助完成任务。"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl_stream","model":"qwen-plus",'
                '"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":10,'
                '"total_tokens":22}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                request=request,
                content=content.encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl_bailian_demo",
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "我可以回答问题、总结内容并协助完成任务。",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 10,
                    "total_tokens": 22,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        blocking_answer, streaming_answer = await run_demo(
            api_key="sk-test",
            http_client=http_client,
        )

    assert len(observed) == 2
    assert [request.get("stream", False) for request in observed] == [False, True]
    assert all(request["model"] == "qwen-plus" for request in observed)
    assert all(request["messages"][0]["content"][0]["text"] == QUERY for request in observed)
    assert blocking_answer == "我可以回答问题、总结内容并协助完成任务。"
    assert streaming_answer == "我可以回答问题、总结内容并协助完成任务。"
