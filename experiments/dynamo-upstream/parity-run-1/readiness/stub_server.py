"""Deterministic, CPU-only protocol responses for the installed client checks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


MODEL = "Qwen/Qwen3-8B"
REASONING = "I will read the local fixture before answering."
ANSWER = "The local protocol check is complete."


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fixture):
        self.fixture = str(fixture)
        self.requests = []
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), StubHandler)


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def event(self, kind, data):
        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()
        time.sleep(0.005)

    def json_reply(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.json_reply({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        with self.server.lock:
            self.server.requests.append({"path": self.path, "raw": raw, "body": body})
        if self.path.split("?", 1)[0] == "/v1/messages/count_tokens":
            self.json_reply({"input_tokens": 100})
            return
        if self.path.split("?", 1)[0] not in ("/v1/messages", "/v1/responses"):
            self.json_reply({"error": {"message": "stub endpoint unavailable"}}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.send_header("x-request-id", "stub-request")
        self.end_headers()
        try:
            if self.path.startswith("/v1/messages"):
                self.anthropic(body)
            else:
                self.emit_responses(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def anthropic(self, body):
        has_result = any(isinstance(message.get("content"), list) and
                         any(block.get("type") == "tool_result" for block in message["content"])
                         for message in body.get("messages", []))
        tools = {tool["name"] for tool in body.get("tools", [])}
        use_tool = not has_result and "Read" in tools
        msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": MODEL,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 100, "output_tokens": 0,
                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        self.event("message_start", {"type": "message_start", "message": msg})
        self.event("content_block_start", {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        self.event("content_block_delta", {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "thinking_delta", "thinking": REASONING}})
        self.event("content_block_delta", {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "signature_delta", "signature": "erased"}})
        self.event("content_block_stop", {"type": "content_block_stop", "index": 0})
        if use_tool:
            block = {"type": "tool_use", "id": "toolu_stub", "name": "Read", "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps({"file_path": self.server.fixture})}
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": ANSWER}
        self.event("content_block_start", {"type": "content_block_start", "index": 1,
                   "content_block": block})
        self.event("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": delta})
        self.event("content_block_stop", {"type": "content_block_stop", "index": 1})
        self.event("message_delta", {"type": "message_delta",
                   "delta": {"stop_reason": "tool_use" if use_tool else "end_turn", "stop_sequence": None},
                   "usage": {"input_tokens": 80, "cache_read_input_tokens": 20,
                             "cache_creation_input_tokens": 0, "output_tokens": 12}})
        self.event("message_stop", {"type": "message_stop"})

    def emit_responses(self, body):
        has_result = any(item.get("type") in ("function_call_output", "custom_tool_call_output")
                         for item in body.get("input", []) if isinstance(item, dict))
        flattened = []
        for tool in body.get("tools", []):
            if tool.get("type") == "namespace":
                flattened.extend((f"{tool['name']}.{nested['name']}", nested)
                                 for nested in tool.get("tools", []))
            else:
                flattened.append((tool.get("name", ""), tool))
        selected = next(((name, tool) for name, tool in flattened
                         if name.split(".")[-1] in ("exec_command", "shell_command", "shell")), None)
        use_tool = not has_result and selected is not None
        response = {"id": "resp_stub", "object": "response", "created_at": 1,
                    "status": "in_progress", "model": MODEL, "output": [], "usage": None}
        self.event("response.created", {"type": "response.created", "response": response})
        reasoning = {"id": "rs_stub", "type": "reasoning",
                     "summary": [{"type": "summary_text", "text": REASONING}]}
        self.event("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                   "item": {"id": "rs_stub", "type": "reasoning", "summary": []}})
        self.event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                   "item": reasoning})
        if use_tool:
            name, tool = selected
            if name.split(".")[-1] == "exec_command":
                args = {"cmd": "cat fixture.txt", "max_output_tokens": 100}
            elif name.split(".")[-1] == "shell_command":
                args = {"command": "cat fixture.txt", "timeout_ms": 1000}
            else:
                args = {"command": ["/bin/cat", "fixture.txt"], "timeout_ms": 1000}
            item = {"id": "fc_stub", "type": "function_call", "call_id": "call_stub",
                    "name": name, "arguments": json.dumps(args), "status": "completed"}
            self.event("response.output_item.added", {"type": "response.output_item.added", "output_index": 1,
                       "item": {**item, "arguments": "", "status": "in_progress"}})
            self.event("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                       "item_id": item["id"], "output_index": 1, "delta": item["arguments"]})
            self.event("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                       "item_id": item["id"], "output_index": 1, "arguments": item["arguments"]})
        else:
            item = {"id": "msg_stub", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": ANSWER, "annotations": []}]}
            self.event("response.output_item.added", {"type": "response.output_item.added", "output_index": 1,
                       "item": {**item, "content": [], "status": "in_progress"}})
            self.event("response.content_part.added", {"type": "response.content_part.added", "item_id": item["id"],
                       "output_index": 1, "content_index": 0,
                       "part": {"type": "output_text", "text": "", "annotations": []}})
            self.event("response.output_text.delta", {"type": "response.output_text.delta", "item_id": item["id"],
                       "output_index": 1, "content_index": 0, "delta": ANSWER})
            self.event("response.output_text.done", {"type": "response.output_text.done", "item_id": item["id"],
                       "output_index": 1, "content_index": 0, "text": ANSWER})
        self.event("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": item})
        response.update(status="completed", output=[reasoning, item],
                        usage={"input_tokens": 100, "output_tokens": 12, "total_tokens": 112,
                               "input_tokens_details": {"cached_tokens": 20},
                               "output_tokens_details": {"reasoning_tokens": 8}})
        self.event("response.completed", {"type": "response.completed", "response": response})
