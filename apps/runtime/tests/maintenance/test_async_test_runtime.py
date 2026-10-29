import asyncio

from langchain_core.language_models.fake_chat_models import FakeListChatModel


async def test_fake_chat_model_supports_repeated_async_invocation():
    model = FakeListChatModel(responses=["first", "second"])

    first = await asyncio.wait_for(model.ainvoke("first prompt"), timeout=1)
    second = await asyncio.wait_for(model.ainvoke("second prompt"), timeout=1)

    assert first.content == "first"
    assert second.content == "second"
