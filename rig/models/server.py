#!/usr/bin/env python3
"""The scripted model server: the vendors' public model APIs, answered from a
script that rides in the prompt, so real engines run with no account and no
test code in the product.

    python3 rig/models/server.py --bind 198.18.0.1 --port 18099 --log calls.jsonl

It speaks Anthropic Messages (``/v1/messages``), OpenAI Responses
(``/v1/responses``) and OpenAI Chat Completions (``/v1/chat/completions``). A
scenario puts the model's whole behaviour in its message::

    [[rig {"steps": [{"tool": "shell", "input": {"command": "echo hi"}},
                     {"sleep": 2, "text": "All done."}]}]]

The step is the count of tool results after that message, so the server keeps
no state and a retried or resumed call answers the same. ``tool`` is ``shell``
(whatever the engine calls its shell) or a name from the request's tool list
(an exact name, or the part after ``__`` or a dot, so ``delegate`` finds
``mcp__delegation-mcp__delegate``). A tool the engine has not loaded yet is searched
for first, as a model does, and that search is no step. No marker, no tool list, or
past the last step: a short plain answer. A sub-agent's prompt carries its own marker.
A text step's ``woken`` is its answer in the turn the engine starts by itself when a
background command ended, after ``woken_sleep`` seconds of silence. A script's ``after_cut`` is what it does once the platform has
told it its last turn was cut off: steps of their own, counted from that message, as a
model does what the words ask, so nothing here repeats a call on its own.

Each call is logged with its tool list and, under ``offered``, the MCP servers
whose tools the request names anywhere (a tool the engine has not loaded yet is
named in its prompt only), so a scenario can tell when a session gained a tool.

``--inject model-error`` answers every model call 401, so a run can prove that a
failure shows as a failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAIN_ANSWER = "Scripted answer."
MARKER = "[[rig "
# A tool of an MCP server, as the engines name it.
MCP_TOOL = re.compile(r"mcp__([A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*?)__([A-Za-z0-9_-]+)")
MODEL_LIST = ("scripted",)
SEARCH_TOOL = "ToolSearch"
MAX_SEARCHES = 3
# What an engine's own turn opens with when a background command ended.
WOKEN_BY = "<task-notification>"
# What the platform's words after a restart say, on every engine.
CUT_WORDS = "your last turn was cut off"
RIDER = "Before the cut you had called:"
_DECODER = json.JSONDecoder()


def find_script(text: str) -> dict | None:
    """The script of the first whole marker in ``text``, or None: a later one sits
    inside it, in the prompt a step hands to a sub-agent. One cut short is another
    chat's title, in the line about the sessions beside this one."""
    at = text.find(MARKER)
    while at >= 0:
        try:
            script, _ = _DECODER.raw_decode(text[at + len(MARKER):])
        except ValueError:
            at = text.find(MARKER, at + len(MARKER))
            continue
        return script if isinstance(script, dict) else None
    return None


def offered_servers(body: dict) -> dict[str, int]:
    """Each MCP server whose tools the request names, with how many."""
    tools: dict[str, set[str]] = {}
    for server, tool in MCP_TOOL.findall(json.dumps(body)):
        tools.setdefault(server, set()).add(tool)
    return {server: len(names) for server, names in sorted(tools.items())}


def resolve_tool(want: str, names: list[str]) -> str | None:
    """The request's tool a step means: exact, then by its last part."""
    if want in names:
        return want
    for name in names:
        if name.endswith(f"__{want}") or name.endswith(f".{want}"):
            return name
    return None


def shell_call(names: list[str], command: str) -> tuple[str, dict] | None:
    """The engine's own shell tool and its arguments for ``command``."""
    if "Bash" in names:
        return "Bash", {"command": command, "description": "A step of the rig's script"}
    if "exec_command" in names:
        return "exec_command", {"cmd": command}
    if "shell_command" in names:
        return "shell_command", {"command": command}
    if "shell" in names:
        return "shell", {"command": ["bash", "-lc", command]}
    return None


def plan_answer(script: dict | None, results: int, names: list[str], searched: int = 0) -> dict:
    """What to answer: ``{"text": ...}`` or ``{"tool": ..., "input": ...}``,
    each with an optional ``sleep`` in seconds."""
    steps = (script or {}).get("steps") or []
    if not script or results >= len(steps) or not names:
        step = steps[results] if script and names and results < len(steps) else {}
        return {"text": step.get("text", PLAIN_ANSWER), "sleep": float(step.get("sleep", 0))}
    step = steps[results]
    sleep = float(step.get("sleep", 0))
    want = step.get("tool")
    if not want:
        answer = {"text": step.get("text", PLAIN_ANSWER), "sleep": sleep}
        if step.get("woken"):
            return {**answer, "woken": step["woken"], "woken_sleep": float(step.get("woken_sleep", 0))}
        return answer
    tool_input = dict(step.get("input") or {})
    if want == "shell":
        call = shell_call(names, str(tool_input.get("command", "")))
        if call is None:
            return {"text": f"rig: this request has no shell among {names}", "sleep": 0}
        return {"tool": call[0], "input": call[1], "say": step.get("say", ""), "sleep": sleep}
    name = resolve_tool(want, names)
    if name is None and SEARCH_TOOL in names and searched < MAX_SEARCHES:
        return {"tool": SEARCH_TOOL, "input": {"query": want, "max_results": 5}, "say": "", "sleep": 0}
    if name is None:
        return {"text": f"rig: no tool {want} among {names}", "sleep": 0}
    return {"tool": name, "input": tool_input, "say": step.get("say", ""), "sleep": sleep}


# --- Anthropic Messages ---------------------------------------------------------


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _after_a_cut(script: dict, at: int, texts: list[str]) -> tuple[dict, int]:
    """The script from the message that told of the cut, when it says what to do
    after one and such a message came after its own (``at``)."""
    if script.get("after_cut"):
        for i in range(len(texts) - 1, at, -1):
            if CUT_WORDS in texts[i]:
                return {**script, "steps": script["after_cut"]}, i
    return script, at


def read_messages(body: dict) -> tuple[dict | None, int, list[str], int]:
    """(script, tool results after its message, tool names, searches among them)
    of a Messages call. A search is not a step of the script."""
    names = [t.get("name", "") for t in body.get("tools") or []]
    msgs = body.get("messages") or []
    texts = [_block_text(m.get("content")) if m.get("role") == "user" else "" for m in msgs]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") != "user":
            continue
        script = find_script(_block_text(msgs[i].get("content")))
        if script is None:
            continue
        script, i = _after_a_cut(script, i, texts)
        results = searched = 0
        for later in msgs[i:]:
            content = later.get("content")
            if isinstance(content, list):
                results += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_result")
                searched += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                                and b.get("name") == SEARCH_TOOL)
        return script, results - searched, names, searched
    return None, 0, names, 0


def messages_blocks(answer: dict) -> tuple[list[dict], str]:
    if "tool" in answer:
        blocks: list[dict] = []
        if answer.get("say"):
            blocks.append({"type": "text", "text": answer["say"]})
        blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20],
                       "name": answer["tool"], "input": answer["input"]})
        return blocks, "tool_use"
    return [{"type": "text", "text": answer["text"]}], "end_turn"


_USAGE = {"input_tokens": 12, "output_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def messages_events(body: dict, answer: dict) -> list[tuple[str, dict]]:
    """A Messages stream, event by event."""
    blocks, stop = messages_blocks(answer)
    message = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
               "model": body.get("model", "scripted"), "content": [], "stop_reason": None,
               "stop_sequence": None, "usage": _USAGE}
    events: list[tuple[str, dict]] = [("message_start", {"type": "message_start", "message": message})]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
                   ("content_block_stop", {"type": "content_block_stop", "index": i})]
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": 5}}),
               ("message_stop", {"type": "message_stop"})]
    return events


def messages_whole(body: dict, answer: dict) -> dict:
    blocks, stop = messages_blocks(answer)
    return {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
            "model": body.get("model", "scripted"), "content": blocks, "stop_reason": stop,
            "stop_sequence": None, "usage": _USAGE}


# --- OpenAI Responses -----------------------------------------------------------


def read_responses(body: dict) -> tuple[dict | None, int, list[str], int]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        if tool.get("name"):
            names.append(tool["name"])
        for sub in tool.get("tools") or []:  # a namespace of tools
            if sub.get("name"):
                names.append(f"{tool.get('name', '')}__{sub['name']}")
    items = body.get("input") or []
    if isinstance(items, str):
        return find_script(items), 0, names, 0
    for extra in items:  # a lite request (the 6.x rows) declares its tools in an input item
        if isinstance(extra, dict) and extra.get("type") == "additional_tools":
            for tool in extra.get("tools") or []:
                if tool.get("name") and not tool.get("tools"):
                    names.append(tool["name"])
                for sub in tool.get("tools") or []:
                    if sub.get("name"):
                        names.append(f"{tool.get('name', '')}__{sub['name']}")

    def _text(item: dict) -> str:
        if item.get("role") != "user":
            return ""
        return " ".join(p.get("text", "") for p in item.get("content") or [] if isinstance(p, dict))
    texts = [_text(item) for item in items]
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if item.get("role") != "user":
            continue
        script = find_script(texts[i])
        if script is None:
            continue
        script, i = _after_a_cut(script, i, texts)
        results = sum(1 for later in items[i:] if str(later.get("type", "")).endswith("_call_output"))
        return script, results, names, 0
    return None, 0, names, 0


def responses_events(answer: dict, top_level: set[str] | None = None) -> list[tuple[str, dict]]:
    """A Responses stream, event by event. A tool of a namespace is called by its
    own name with the namespace beside it, as the vendor's API does."""
    rid = "resp_" + uuid.uuid4().hex[:20]
    events: list[tuple[str, dict]] = [("response.created", {"type": "response.created", "response": {
        "id": rid, "object": "response", "status": "in_progress", "output": []}})]
    output: list[dict] = []
    if "tool" in answer:
        call_id = "call_" + uuid.uuid4().hex[:20]
        item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:20], "call_id": call_id,
                "name": answer["tool"], "arguments": json.dumps(answer["input"]), "status": "completed"}
        namespace, _, last = answer["tool"].rpartition("__")
        if namespace and answer["tool"] not in (top_level or set()):
            item["namespace"], item["name"] = namespace, last
        events.append(("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": item}))
        events.append(("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}))
        output.append(item)
    else:
        iid = "msg_" + uuid.uuid4().hex[:20]
        item = {"type": "message", "id": iid, "role": "assistant", "status": "in_progress", "content": []}
        events.append(("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": item}))
        events.append(("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0,
                                                      "item_id": iid, "content_index": 0, "delta": answer["text"]}))
        done = {**item, "status": "completed",
                "content": [{"type": "output_text", "text": answer["text"], "annotations": []}]}
        events.append(("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": done}))
        output.append(done)
    events.append(("response.completed", {"type": "response.completed", "response": {
        "id": rid, "object": "response", "status": "completed", "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}}}}))
    return events


# --- OpenAI Chat Completions ----------------------------------------------------


def read_chat(body: dict) -> tuple[dict | None, int, list[str], int]:
    names = [(t.get("function") or {}).get("name", "") for t in body.get("tools") or []]
    msgs = body.get("messages") or []
    texts = [_block_text(m.get("content")) if m.get("role") == "user" else "" for m in msgs]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") != "user":
            continue
        script = find_script(_block_text(msgs[i].get("content")))
        if script is None:
            continue
        script, i = _after_a_cut(script, i, texts)
        return script, sum(1 for later in msgs[i:] if later.get("role") == "tool"), names, 0
    return None, 0, names, 0


def chat_chunks(answer: dict) -> list[dict]:
    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": "chatcmpl-rig", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": "scripted", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    if "tool" in answer:
        chunks = []
        if answer.get("say"):
            chunks.append(chunk({"role": "assistant", "content": answer["say"]}))
        chunks.append(chunk({"role": "assistant", "tool_calls": [{
            "index": 0, "id": "call_" + uuid.uuid4().hex[:20], "type": "function",
            "function": {"name": answer["tool"], "arguments": json.dumps(answer["input"])}}]}))
        chunks.append(chunk({}, "tool_calls"))
    else:
        chunks = [chunk({"role": "assistant", "content": answer["text"]}), chunk({}, "stop")]
    chunks.append({"id": "chatcmpl-rig", "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": "scripted", "choices": [],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
    return chunks


# --- the server -----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    inject = ""
    log_file = None
    log_lock = threading.Lock()

    def log_message(self, format, *args):
        pass

    def _record(self, **fields) -> None:
        if self.log_file is None:
            return
        with self.log_lock:
            self.log_file.write(json.dumps({"t": round(time.time(), 2), **fields}) + "\n")
            self.log_file.flush()

    def _json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.close_connection = True
        self.end_headers()

    def _named_events(self, events: list[tuple[str, dict]]) -> None:
        self._start_stream()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "rig"} for m in MODEL_LIST]})
        else:
            self._json({})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self._json({"input_tokens": 100})
            return
        if self.inject == "model-error":
            self._record(api=path, injected=True)
            self._json({"type": "error", "error": {"type": "authentication_error", "message": "rig: injected"}}, 401)
            return
        try:
            self._answer(path, body)
        except (BrokenPipeError, ConnectionResetError):
            self._record(api=path, gone=True)

    def _answer(self, path: str, body: dict) -> None:
        if path.endswith("/messages"):
            reader = read_messages
        elif path.endswith("/responses"):
            reader = read_responses
        else:
            reader = read_chat
        script, results, names, searched = reader(body)
        answer = plan_answer(script, results, names, searched)
        if answer.get("woken") and WOKEN_BY in json.dumps((body.get("messages") or [{}])[-1]):
            answer = {**answer, "text": answer["woken"], "sleep": answer["woken_sleep"]}
        told = json.dumps(body)
        rider = told[told.rfind(RIDER):][:300] if RIDER in told else ""
        self._record(api=path, model=body.get("model"), stream=bool(body.get("stream")), marker=script is not None,
                     step=results, answer=answer.get("tool") or "text", tools=names, tag=(script or {}).get("tag", ""),
                     offered=offered_servers(body), told_of_the_cut=CUT_WORDS in told, rider=rider)
        if answer.get("sleep"):
            time.sleep(answer["sleep"])
        if path.endswith("/messages"):
            if body.get("stream"):
                self._named_events(messages_events(body, answer))
            else:
                self._json(messages_whole(body, answer))
        elif path.endswith("/responses"):
            self._named_events(responses_events(answer, {t.get("name") for t in body.get("tools") or []}))
        else:
            self._start_stream()
            for chunk in chat_chunks(answer):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The rig's scripted model server")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18099)
    parser.add_argument("--log", default="")
    parser.add_argument("--inject", default="", choices=["", "model-error"])
    args = parser.parse_args(argv)
    Handler.inject = args.inject
    Handler.log_file = open(args.log, "a", buffering=1) if args.log else None
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
