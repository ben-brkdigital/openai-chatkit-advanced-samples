# backend/app/chat.py
"""ChatKit server for BRK Digital Sales Assistant."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Annotated, Any, AsyncIterator, Final
from uuid import uuid4

from agents import Agent, RunContextWrapper, Runner, function_tool
from chatkit.agents import (
    AgentContext,
    ClientToolCall,
    ThreadItemConverter,
    stream_agent_response,
)
from chatkit.server import ChatKitServer, ThreadItemDoneEvent
from chatkit.types import (
    Attachment,
    ClientToolCallItem,
    HiddenContextItem,
    ThreadItem,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)
from openai.types.responses import ResponseInputContentParam
from pydantic import ConfigDict, Field

# import your updated instructions and model
from .constants import INSTRUCTIONS, MODEL
from .facts import Fact, fact_store
from .memory_store import MemoryStore

logging.basicConfig(level=logging.INFO)

# Utility helpers
def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"

def _is_tool_completion_item(item: Any) -> bool:
    return isinstance(item, ClientToolCallItem)


# Agent context
class BrkAgentContext(AgentContext):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    store: Annotated[MemoryStore, Field(exclude=True)]
    request_context: dict[str, Any]


# Optional simple fact recorder (keeps example minimal)
@function_tool(description_override="Record a fact shared by the user (e.g. name or use case).")
async def save_fact(
    ctx: RunContextWrapper[BrkAgentContext],
    fact: str,
) -> dict[str, str] | None:
    try:
        saved = await fact_store.create(text=fact)
        confirmed = await fact_store.mark_saved(saved.id)
        if confirmed is None:
            raise ValueError("Failed to save fact")
        await ctx.context.stream(
            ThreadItemDoneEvent(
                item=HiddenContextItem(
                    id=_gen_id("msg"),
                    thread_id=ctx.context.thread.id,
                    created_at=datetime.now(),
                    content=f'<FACT_SAVED id="{confirmed.id}">{confirmed.text}</FACT_SAVED>',
                ),
            )
        )
        ctx.context.client_tool_call = ClientToolCall(
            name="record_fact",
            arguments={"fact_id": confirmed.id, "fact_text": confirmed.text},
        )
        return {"fact_id": confirmed.id, "status": "saved"}
    except Exception:
        logging.exception("Failed to save fact")
        return None


def _user_message_text(item: UserMessageItem) -> str:
    """Extract raw user message text."""
    parts: list[str] = []
    for part in item.content:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


# The BRK ChatKit server itself
class BrkAssistantServer(ChatKitServer[dict[str, Any]]):
    """ChatKit server hosting BRK Digital Sales Assistant."""

    def __init__(self) -> None:
        self.store: MemoryStore = MemoryStore()
        super().__init__(self.store)

        tools = [save_fact]  # remove if no tools are needed

        self.assistant = Agent[BrkAgentContext](
            model=MODEL,
            name="BRK Digital Assistant",
            instructions=INSTRUCTIONS,
            tools=tools,  # type: ignore[arg-type]
        )

        self._thread_item_converter = self._init_thread_item_converter()

    async def respond(
        self,
        thread: ThreadMetadata,
        item: UserMessageItem | None,
        context: dict[str, Any],
    ) -> AsyncIterator[ThreadStreamEvent]:
        agent_context = BrkAgentContext(
            thread=thread,
            store=self.store,
            request_context=context,
        )

        target_item: ThreadItem | None = item or await self._latest_thread_item(thread, context)
        if target_item is None or _is_tool_completion_item(target_item):
            return

        agent_input = await self._to_agent_input(thread, target_item)
        if agent_input is None:
            return

        result = Runner.run_streamed(self.assistant, agent_input, context=agent_context)
        async for event in stream_agent_response(agent_context, result):
            yield event

    async def to_message_content(self, _input: Attachment) -> ResponseInputContentParam:
        raise RuntimeError("File attachments not supported.")

    def _init_thread_item_converter(self) -> Any | None:
        converter_cls = ThreadItemConverter
        if converter_cls is None or not callable(converter_cls):
            return None

        for kwargs in (
            {"to_message_content": self.to_message_content},
            {"message_content_converter": self.to_message_content},
            {},
        ):
            try:
                return converter_cls(**kwargs)
            except TypeError:
                continue
        return None

    async def _latest_thread_item(
        self, thread: ThreadMetadata, context: dict[str, Any]
    ) -> ThreadItem | None:
        try:
            items = await self.store.load_thread_items(thread.id, None, 1, "desc", context)
        except Exception:
            return None
        return items.data[0] if getattr(items, "data", None) else None

    async def _to_agent_input(
        self, thread: ThreadMetadata, item: ThreadItem
    ) -> Any | None:
        if _is_tool_completion_item(item):
            return None

        converter = getattr(self, "_thread_item_converter", None)
        if converter is not None:
            for attr in ("to_input_item", "convert", "convert_item", "convert_thread_item"):
                method = getattr(converter, attr, None)
                if method is None:
                    continue
                call_args = [item]
                call_kwargs = {}
                try:
                    signature = inspect.signature(method)
                except (TypeError, ValueError):
                    signature = None
                if signature is not None:
                    params = [
                        p for p in signature.parameters.values()
                        if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                    ]
                    if len(params) >= 2:
                        next_param = params[1]
                        if next_param.kind in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        ):
                            call_args.append(thread)
                        else:
                            call_kwargs[next_param.name] = thread
                result = method(*call_args, **call_kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result
        if isinstance(item, UserMessageItem):
            return _user_message_text(item)
        return None


# Factory for FastAPI
def create_chatkit_server() -> ChatKitServer | None:
    return BrkAssistantServer()
