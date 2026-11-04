"""Serve OpenAI Responses API requests with a Chat Completions model (Gemini on Vertex AI).

AG Studio's AI assistant speaks the Responses API; Vertex AI offers Chat Completions. This module
converts one request across, calls the model (not streamed), and replays the answer as the
Responses stream events AG Studio's adapter reads: output_item.added, the text/argument deltas,
output_item.done and response.completed. AG Studio's adapter stays unchanged.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator

# JSON Schema keywords Gemini's OpenAI-compatible endpoint accepts; the rest are dropped.
KEEP = {"type", "properties", "required", "items", "enum", "description", "anyOf", "format", "nullable",
        "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "title"}
MAX_NODES = 4000


def simplify(schema, defs: dict | None = None, path: frozenset = frozenset(), budget: list | None = None):
    """Inline $ref/$defs and drop keywords Gemini rejects.

    A reference already being expanded on the current path (a recursive type) becomes a plain object,
    and the whole result is capped at MAX_NODES: a recursive query schema with several branches per
    level otherwise grows exponentially (it once took the server to 2 GB).
    """
    budget = [MAX_NODES] if budget is None else budget
    if not isinstance(schema, dict):
        return schema
    budget[0] -= 1
    defs = {**(defs or {}), **schema.get("$defs", {}), **schema.get("definitions", {})}
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        if name in path or name not in defs or budget[0] <= 0:
            return {"type": "object", "description": schema.get("description") or defs.get(name, {}).get(
                "description") or name}
        return simplify(defs[name], defs, path | {name}, budget)
    if budget[0] <= 0:
        return {"type": schema.get("type", "object") if isinstance(schema.get("type"), str) else "object"}
    out = {}
    for key, value in schema.items():
        if key not in KEEP:
            continue
        if key == "properties":
            out[key] = {k: simplify(v, defs, path, budget) for k, v in value.items()}
        elif key == "items":
            out[key] = simplify(value, defs, path, budget)
        elif key == "anyOf":
            options = [simplify(v, defs, path, budget) for v in value]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) == 1 and len(options) == 2:      # T | null  ->  nullable T
                out.update({**non_null[0], "nullable": True})
            else:
                out[key] = options
        elif key == "type" and isinstance(value, list):         # ["string", "null"]
            types = [t for t in value if t != "null"]
            out["type"] = types[0] if types else "string"
            if "null" in value:
                out["nullable"] = True
        else:
            out[key] = value
    return out


def to_chat(body: dict, model: str) -> dict:
    """A Responses API request body as a Chat Completions request body."""
    messages: list[dict] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for item in items or []:
        kind = item.get("type", "message")
        if kind == "message":
            content = item.get("content")
            text = content if isinstance(content, str) else "".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            role = item.get("role", "user")
            messages.append({"role": "system" if role == "developer" else role, "content": text})
        elif kind == "function_call":
            call = {"id": item["call_id"], "type": "function",
                    "function": {"name": item["name"], "arguments": item.get("arguments") or "{}"}}
            last = messages[-1] if messages else None
            if last and last["role"] == "assistant" and "tool_calls" in last:
                last["tool_calls"].append(call)
            elif last and last["role"] == "assistant" and not last.get("tool_calls"):
                last["tool_calls"] = [call]
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append({"role": "tool", "tool_call_id": item["call_id"],
                             "content": output if isinstance(output, str) else json.dumps(output)})
        # reasoning items are the model's own notes; Chat Completions has no place for them
    chat: dict = {"model": model, "messages": messages}
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": simplify(t.get("parameters") or {"type": "object"})}}
             for t in body.get("tools") or [] if t.get("type", "function") == "function"]
    if tools:
        chat["tools"] = tools
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif choice in ("auto", "none", "required"):
            chat["tool_choice"] = choice
    fmt = (body.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            "name": fmt.get("name", "output"), "schema": simplify(fmt.get("schema") or {})}}
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    return chat


def to_response(chat: dict, model: str) -> dict:
    """A Chat Completions reply as a completed Responses API response object."""
    message = (chat.get("choices") or [{}])[0].get("message") or {}
    output: list[dict] = []
    if message.get("content"):
        output.append({"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for call in message.get("tool_calls") or []:
        output.append({"id": f"fc_{uuid.uuid4().hex}", "type": "function_call", "status": "completed",
                       "call_id": call.get("id") or f"call_{uuid.uuid4().hex}",
                       "name": call["function"]["name"], "arguments": call["function"].get("arguments") or "{}"})
    usage = chat.get("usage") or {}
    return {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": int(time.time()),
            "model": model, "status": "completed", "output": output, "error": None, "incomplete_details": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)}}


def events(response: dict) -> Iterator[bytes]:
    """The Responses stream for a finished response, as server-sent event bytes."""
    seq = 0

    def sse(payload: dict) -> bytes:
        nonlocal seq
        seq += 1
        return f"event: {payload['type']}\ndata: {json.dumps({**payload, 'sequence_number': seq})}\n\n".encode()

    yield sse({"type": "response.created", "response": {**response, "status": "in_progress", "output": []}})
    for index, item in enumerate(response["output"]):
        if item["type"] == "message":
            text = item["content"][0]["text"]
            yield sse({"type": "response.output_item.added", "output_index": index,
                       "item": {**item, "status": "in_progress", "content": []}})
            yield sse({"type": "response.output_text.delta", "item_id": item["id"], "output_index": index,
                       "content_index": 0, "delta": text})
        else:
            yield sse({"type": "response.output_item.added", "output_index": index,
                       "item": {**item, "status": "in_progress", "arguments": ""}})
            yield sse({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                       "output_index": index, "delta": item["arguments"]})
        yield sse({"type": "response.output_item.done", "output_index": index, "item": item})
    yield sse({"type": "response.completed", "response": response})
    yield b"data: [DONE]\n\n"
