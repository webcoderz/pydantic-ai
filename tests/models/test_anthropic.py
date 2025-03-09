from __future__ import annotations as _annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timezone
from functools import cached_property
from typing import Any, TypeVar, cast

import pytest
from inline_snapshot import snapshot

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.result import Usage
from pydantic_ai.settings import ModelSettings

from ..conftest import IsDatetime, IsNow, IsStr, try_import
from .mock_async_stream import MockAsyncStream

with try_import() as imports_successful:
    from anthropic import NOT_GIVEN, AsyncAnthropic
    from anthropic.types import (
        ContentBlock,
        InputJSONDelta,
        Message as AnthropicMessage,
        MessageDeltaUsage,
        RawContentBlockDeltaEvent,
        RawContentBlockStartEvent,
        RawContentBlockStopEvent,
        RawMessageDeltaEvent,
        RawMessageStartEvent,
        RawMessageStopEvent,
        RawMessageStreamEvent,
        TextBlock,
        ToolUseBlock,
        Usage as AnthropicUsage,
    )
    from anthropic.types.raw_message_delta_event import Delta

    from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings

pytestmark = [
    pytest.mark.skipif(not imports_successful(), reason='anthropic not installed'),
    pytest.mark.anyio,
]

# Type variable for generic AsyncStream
T = TypeVar('T')


def test_init():
    m = AnthropicModel('claude-3-5-haiku-latest', api_key='foobar')
    assert m.client.api_key == 'foobar'
    assert m.model_name == 'claude-3-5-haiku-latest'
    assert m.system == 'anthropic'


@dataclass
class MockAnthropic:
    messages_: AnthropicMessage | list[AnthropicMessage] | None = None
    stream: list[RawMessageStreamEvent] | list[list[RawMessageStreamEvent]] | None = None
    index = 0
    chat_completion_kwargs: list[dict[str, Any]] = field(default_factory=list)

    @cached_property
    def messages(self) -> Any:
        return type('Messages', (), {'create': self.messages_create})

    @classmethod
    def create_mock(cls, messages_: AnthropicMessage | list[AnthropicMessage]) -> AsyncAnthropic:
        return cast(AsyncAnthropic, cls(messages_=messages_))

    @classmethod
    def create_stream_mock(
        cls, stream: list[RawMessageStreamEvent] | list[list[RawMessageStreamEvent]]
    ) -> AsyncAnthropic:
        return cast(AsyncAnthropic, cls(stream=stream))

    async def messages_create(
        self, *_args: Any, stream: bool = False, **kwargs: Any
    ) -> AnthropicMessage | MockAsyncStream[RawMessageStreamEvent]:
        self.chat_completion_kwargs.append({k: v for k, v in kwargs.items() if v is not NOT_GIVEN})

        if stream:
            assert self.stream is not None, 'you can only use `stream=True` if `stream` is provided'
            # noinspection PyUnresolvedReferences
            if isinstance(self.stream[0], list):
                indexed_stream = cast(list[RawMessageStreamEvent], self.stream[self.index])
                response = MockAsyncStream(iter(indexed_stream))
            else:
                response = MockAsyncStream(iter(cast(list[RawMessageStreamEvent], self.stream)))
        else:
            assert self.messages_ is not None, '`messages` must be provided'
            if isinstance(self.messages_, list):
                response = self.messages_[self.index]
            else:
                response = self.messages_
        self.index += 1
        return response


def completion_message(content: list[ContentBlock], usage: AnthropicUsage) -> AnthropicMessage:
    return AnthropicMessage(
        id='123',
        content=content,
        model='claude-3-5-haiku-123',
        role='assistant',
        stop_reason='end_turn',
        type='message',
        usage=usage,
    )


async def test_sync_request_text_response(allow_model_requests: None):
    c = completion_message([TextBlock(text='world', type='text')], AnthropicUsage(input_tokens=5, output_tokens=10))
    mock_client = MockAnthropic.create_mock(c)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m)

    result = await agent.run('hello')
    assert result.data == 'world'
    assert result.usage() == snapshot(Usage(requests=1, request_tokens=5, response_tokens=10, total_tokens=15))

    # reset the index so we get the same response again
    mock_client.index = 0  # type: ignore

    result = await agent.run('hello', message_history=result.new_messages())
    assert result.data == 'world'
    assert result.usage() == snapshot(Usage(requests=1, request_tokens=5, response_tokens=10, total_tokens=15))
    assert result.all_messages() == snapshot(
        [
            ModelRequest(parts=[UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[TextPart(content='world')],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
            ModelRequest(parts=[UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[TextPart(content='world')],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
        ]
    )


async def test_async_request_text_response(allow_model_requests: None):
    c = completion_message(
        [TextBlock(text='world', type='text')],
        usage=AnthropicUsage(input_tokens=3, output_tokens=5),
    )
    mock_client = MockAnthropic.create_mock(c)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m)

    result = await agent.run('hello')
    assert result.data == 'world'
    assert result.usage() == snapshot(Usage(requests=1, request_tokens=3, response_tokens=5, total_tokens=8))


async def test_request_structured_response(allow_model_requests: None):
    c = completion_message(
        [ToolUseBlock(id='123', input={'response': [1, 2, 3]}, name='final_result', type='tool_use')],
        usage=AnthropicUsage(input_tokens=3, output_tokens=5),
    )
    mock_client = MockAnthropic.create_mock(c)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m, result_type=list[int])

    result = await agent.run('hello')
    assert result.data == [1, 2, 3]
    assert result.all_messages() == snapshot(
        [
            ModelRequest(parts=[UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc))]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='final_result',
                        args={'response': [1, 2, 3]},
                        tool_call_id='123',
                    )
                ],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name='final_result',
                        content='Final result processed.',
                        tool_call_id='123',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
        ]
    )


async def test_request_tool_call(allow_model_requests: None):
    responses = [
        completion_message(
            [ToolUseBlock(id='1', input={'loc_name': 'San Francisco'}, name='get_location', type='tool_use')],
            usage=AnthropicUsage(input_tokens=2, output_tokens=1),
        ),
        completion_message(
            [ToolUseBlock(id='2', input={'loc_name': 'London'}, name='get_location', type='tool_use')],
            usage=AnthropicUsage(input_tokens=3, output_tokens=2),
        ),
        completion_message(
            [TextBlock(text='final response', type='text')],
            usage=AnthropicUsage(input_tokens=3, output_tokens=5),
        ),
    ]

    mock_client = MockAnthropic.create_mock(responses)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m, system_prompt='this is the system prompt')

    @agent.tool_plain
    async def get_location(loc_name: str) -> str:
        if loc_name == 'London':
            return json.dumps({'lat': 51, 'lng': 0})
        else:
            raise ModelRetry('Wrong location, please try again')

    result = await agent.run('hello')
    assert result.data == 'final response'
    assert result.all_messages() == snapshot(
        [
            ModelRequest(
                parts=[
                    SystemPromptPart(content='this is the system prompt'),
                    UserPromptPart(content='hello', timestamp=IsNow(tz=timezone.utc)),
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='get_location',
                        args={'loc_name': 'San Francisco'},
                        tool_call_id='1',
                    )
                ],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    RetryPromptPart(
                        content='Wrong location, please try again',
                        tool_name='get_location',
                        tool_call_id='1',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name='get_location',
                        args={'loc_name': 'London'},
                        tool_call_id='2',
                    )
                ],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name='get_location',
                        content='{"lat": 51, "lng": 0}',
                        tool_call_id='2',
                        timestamp=IsNow(tz=timezone.utc),
                    )
                ]
            ),
            ModelResponse(
                parts=[TextPart(content='final response')],
                model_name='claude-3-5-haiku-123',
                timestamp=IsNow(tz=timezone.utc),
            ),
        ]
    )


def get_mock_chat_completion_kwargs(async_anthropic: AsyncAnthropic) -> list[dict[str, Any]]:
    if isinstance(async_anthropic, MockAnthropic):
        return async_anthropic.chat_completion_kwargs
    else:  # pragma: no cover
        raise RuntimeError('Not a MockOpenAI instance')


@pytest.mark.parametrize('parallel_tool_calls', [True, False])
async def test_parallel_tool_calls(allow_model_requests: None, parallel_tool_calls: bool) -> None:
    responses = [
        completion_message(
            [ToolUseBlock(id='1', input={'loc_name': 'San Francisco'}, name='get_location', type='tool_use')],
            usage=AnthropicUsage(input_tokens=2, output_tokens=1),
        ),
        completion_message(
            [TextBlock(text='final response', type='text')],
            usage=AnthropicUsage(input_tokens=3, output_tokens=5),
        ),
    ]

    mock_client = MockAnthropic.create_mock(responses)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m, model_settings=ModelSettings(parallel_tool_calls=parallel_tool_calls))

    @agent.tool_plain
    async def get_location(loc_name: str) -> str:
        if loc_name == 'London':
            return json.dumps({'lat': 51, 'lng': 0})
        else:
            raise ModelRetry('Wrong location, please try again')

    await agent.run('hello')
    assert get_mock_chat_completion_kwargs(mock_client)[0]['tool_choice']['disable_parallel_tool_use'] == (
        not parallel_tool_calls
    )


@pytest.mark.vcr
async def test_multiple_parallel_tool_calls(allow_model_requests: None):
    async def retrieve_entity_info(name: str) -> str:
        """Get the knowledge about the given entity."""
        data = {
            'alice': "alice is bob's wife",
            'bob': "bob is alice's husband",
            'charlie': "charlie is alice's son",
            'daisy': "daisy is bob's daughter and charlie's younger sister",
        }
        return data[name.lower()]

    system_prompt = """
    Use the `retrieve_entity_info` tool to get information about a specific person.
    If you need to use `retrieve_entity_info` to get information about multiple people, try
    to call them in parallel as much as possible.
    Think step by step and then provide a single most probable concise answer.
    """

    # If we don't provide some value for the API key, the anthropic SDK will raise an error.
    # However, we do want to use the environment variable if present when rewriting VCR cassettes.
    api_key = os.environ.get('ANTHROPIC_API_KEY', 'mock-value')
    agent = Agent(
        AnthropicModel('claude-3-5-haiku-latest', api_key=api_key),
        system_prompt=system_prompt,
        tools=[retrieve_entity_info],
    )

    result = await agent.run('Alice, Bob, Charlie and Daisy are a family. Who is the youngest?')
    assert 'Daisy is the youngest' in result.data

    all_messages = result.all_messages()
    first_response = all_messages[1]
    second_request = all_messages[2]
    assert first_response.parts == [
        TextPart(
            content="I'll retrieve the information about each family member to determine their ages.",
            part_kind='text',
        ),
        ToolCallPart(
            tool_name='retrieve_entity_info', args={'name': 'Alice'}, tool_call_id=IsStr(), part_kind='tool-call'
        ),
        ToolCallPart(
            tool_name='retrieve_entity_info', args={'name': 'Bob'}, tool_call_id=IsStr(), part_kind='tool-call'
        ),
        ToolCallPart(
            tool_name='retrieve_entity_info', args={'name': 'Charlie'}, tool_call_id=IsStr(), part_kind='tool-call'
        ),
        ToolCallPart(
            tool_name='retrieve_entity_info', args={'name': 'Daisy'}, tool_call_id=IsStr(), part_kind='tool-call'
        ),
    ]
    assert second_request.parts == [
        ToolReturnPart(
            tool_name='retrieve_entity_info',
            content="alice is bob's wife",
            tool_call_id=IsStr(),
            timestamp=IsDatetime(),
            part_kind='tool-return',
        ),
        ToolReturnPart(
            tool_name='retrieve_entity_info',
            content="bob is alice's husband",
            tool_call_id=IsStr(),
            timestamp=IsDatetime(),
            part_kind='tool-return',
        ),
        ToolReturnPart(
            tool_name='retrieve_entity_info',
            content="charlie is alice's son",
            tool_call_id=IsStr(),
            timestamp=IsDatetime(),
            part_kind='tool-return',
        ),
        ToolReturnPart(
            tool_name='retrieve_entity_info',
            content="daisy is bob's daughter and charlie's younger sister",
            tool_call_id=IsStr(),
            timestamp=IsDatetime(),
            part_kind='tool-return',
        ),
    ]

    # Ensure the tool call IDs match between the tool calls and the tool returns
    tool_call_part_ids = [part.tool_call_id for part in first_response.parts if part.part_kind == 'tool-call']
    tool_return_part_ids = [part.tool_call_id for part in second_request.parts if part.part_kind == 'tool-return']
    assert len(set(tool_call_part_ids)) == 4  # ensure they are all unique
    assert tool_call_part_ids == tool_return_part_ids


async def test_anthropic_specific_metadata(allow_model_requests: None) -> None:
    c = completion_message([TextBlock(text='world', type='text')], AnthropicUsage(input_tokens=5, output_tokens=10))
    mock_client = MockAnthropic.create_mock(c)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m)

    result = await agent.run('hello', model_settings=AnthropicModelSettings(anthropic_metadata={'user_id': '123'}))
    assert result.data == 'world'
    assert get_mock_chat_completion_kwargs(mock_client)[0]['metadata']['user_id'] == '123'


async def test_stream_structured(allow_model_requests: None):
    """Test streaming structured responses with Anthropic's API.

    This test simulates how Anthropic streams tool calls:
    1. Message start
    2. Tool block start with initial data
    3. Tool block delta with additional data
    4. Tool block stop
    5. Update usage
    6. Message stop
    """
    stream: list[RawMessageStreamEvent] = [
        RawMessageStartEvent(
            type='message_start',
            message=AnthropicMessage(
                id='msg_123',
                model='claude-3-5-haiku-latest',
                role='assistant',
                type='message',
                content=[],
                stop_reason=None,
                usage=AnthropicUsage(input_tokens=20, output_tokens=0),
            ),
        ),
        # Start tool block with initial data
        RawContentBlockStartEvent(
            type='content_block_start',
            index=0,
            content_block=ToolUseBlock(type='tool_use', id='tool_1', name='my_tool', input={'first': 'One'}),
        ),
        # Add more data through an incomplete JSON delta
        RawContentBlockDeltaEvent(
            type='content_block_delta',
            index=0,
            delta=InputJSONDelta(type='input_json_delta', partial_json='{"second":'),
        ),
        RawContentBlockDeltaEvent(
            type='content_block_delta',
            index=0,
            delta=InputJSONDelta(type='input_json_delta', partial_json='"Two"}'),
        ),
        # Mark tool block as complete
        RawContentBlockStopEvent(type='content_block_stop', index=0),
        # Update the top-level message with usage
        RawMessageDeltaEvent(
            type='message_delta',
            delta=Delta(
                stop_reason='end_turn',
            ),
            usage=MessageDeltaUsage(
                output_tokens=5,
            ),
        ),
        # Mark message as complete
        RawMessageStopEvent(type='message_stop'),
    ]

    done_stream: list[RawMessageStreamEvent] = [
        RawMessageStartEvent(
            type='message_start',
            message=AnthropicMessage(
                id='msg_123',
                model='claude-3-5-haiku-latest',
                role='assistant',
                type='message',
                content=[],
                stop_reason=None,
                usage=AnthropicUsage(input_tokens=0, output_tokens=0),
            ),
        ),
        # Text block with final data
        RawContentBlockStartEvent(
            type='content_block_start',
            index=0,
            content_block=TextBlock(type='text', text='FINAL_PAYLOAD'),
        ),
        RawContentBlockStopEvent(type='content_block_stop', index=0),
        RawMessageStopEvent(type='message_stop'),
    ]

    mock_client = MockAnthropic.create_stream_mock([stream, done_stream])
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m)

    tool_called = False

    @agent.tool_plain
    async def my_tool(first: str, second: str) -> int:
        nonlocal tool_called
        tool_called = True
        return len(first) + len(second)

    async with agent.run_stream('') as result:
        assert not result.is_complete
        chunks = [c async for c in result.stream(debounce_by=None)]

        # The tool output doesn't echo any content to the stream, so we only get the final payload once when
        # the block starts and once when it ends.
        assert chunks == snapshot(
            [
                'FINAL_PAYLOAD',
                'FINAL_PAYLOAD',
            ]
        )
        assert result.is_complete
        assert result.usage() == snapshot(Usage(requests=2, request_tokens=20, response_tokens=5, total_tokens=25))
        assert tool_called


@pytest.mark.vcr()
async def test_image_url_input(allow_model_requests: None, anthropic_api_key: str):
    m = AnthropicModel('claude-3-5-haiku-latest', api_key=anthropic_api_key)
    agent = Agent(m)

    result = await agent.run(
        [
            'What is this vegetable?',
            ImageUrl(url='https://t3.ftcdn.net/jpg/00/85/79/92/360_F_85799278_0BBGV9OAdQDTLnKwAPBCcg1J7QtiieJY.jpg'),
        ]
    )
    assert result.data == snapshot("""\
This is a potato. It's a yellow-skinned potato with a somewhat oblong or oval shape. The surface is covered in small eyes or dimples, which is typical of potato skin. The color is a golden-yellow, and the potato appears to be clean and fresh, photographed against a white background.

Potatoes are root vegetables that are staple foods in many cuisines around the world. They can be prepared in numerous ways such as boiling, baking, roasting, frying, or mashing. This particular potato looks like it could be a Yukon Gold or a similar yellow-fleshed variety.\
""")


@pytest.mark.parametrize('media_type', ('audio/wav', 'audio/mpeg'))
async def test_audio_as_binary_content_input(allow_model_requests: None, media_type: str):
    c = completion_message([TextBlock(text='world', type='text')], AnthropicUsage(input_tokens=5, output_tokens=10))
    mock_client = MockAnthropic.create_mock(c)
    m = AnthropicModel('claude-3-5-haiku-latest', anthropic_client=mock_client)
    agent = Agent(m)

    base64_content = b'//uQZ'

    with pytest.raises(RuntimeError, match='Only images are supported for binary content'):
        await agent.run(['hello', BinaryContent(data=base64_content, media_type=media_type)])
