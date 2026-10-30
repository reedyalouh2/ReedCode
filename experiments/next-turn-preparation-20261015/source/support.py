"""Streaming and template helpers used by the recorded sessions."""

import hashlib
import json
from pathlib import Path

TOOLS = json.loads(Path(__file__).with_name("tools.json").read_text())

SYSTEM = ('You are a coding agent working in /workspace. Inspect the repository, make the requested '
          'changes, and run relevant tests. Use the available tools for all file and command operations. '
          'The container has no network access. Report what you changed and what you tested.')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Stream:
    """Keep reasoning and structured calls in the assistant message."""
    def __init__(self):
        self.message = {'role': 'assistant', 'content': None}
        self.calls = {}
        self.finish = self.usage = self.completion_id = self.engine_data = None
        self.output_ids = []
        self.first_generated_ns = self.first_semantic_ns = None
        self.done = False

    def add(self, payload, received):
        if self.done:
            raise ValueError('data after DONE')
        if payload == '[DONE]':
            self.done = True
            return
        event = json.loads(payload)
        if event.get('error'):
            raise ValueError('server stream error: ' + json.dumps(event['error']))
        if event.get('id'):
            if self.completion_id is not None and self.completion_id != event['id']:
                raise ValueError('completion ID changed')
            self.completion_id = event['id']
        if event.get('usage'):
            self.usage = event['usage']
        nvext = event.get('nvext') or {}
        ids = nvext.get('completion_token_ids') or []
        if ids:
            self.first_generated_ns = self.first_generated_ns or received
            self.output_ids.extend(ids)
        if nvext.get('engine_data'):
            self.engine_data = nvext['engine_data']
        choices = event.get('choices') or []
        if len(choices) > 1 or any(choice.get('index') != 0 for choice in choices):
            raise ValueError('expected one completion')
        for choice in choices:
            delta = choice.get('delta') or {}
            visible = any(delta.get(key) for key in ('content', 'reasoning', 'reasoning_content', 'refusal', 'tool_calls'))
            if self.finish is not None and visible:
                raise ValueError('output after finish reason')
            if visible:
                self.first_semantic_ns = self.first_semantic_ns or received
            for key in ('content', 'reasoning', 'reasoning_content', 'refusal'):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError('unsupported content delta')
                    self.message[key] = (self.message.get(key) or '') + value
            if delta.get('function_call'):
                raise ValueError('legacy function_call is unsupported')
            for fragment in delta.get('tool_calls') or []:
                index = fragment['index']
                if not isinstance(index, int) or index < 0:
                    raise ValueError('invalid tool index')
                call = self.calls.setdefault(index, {'id': None, 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if fragment.get('type', 'function') != 'function':
                    raise ValueError('unsupported tool type')
                if fragment.get('id'):
                    if call['id'] not in (None, fragment['id']):
                        raise ValueError('tool ID changed')
                    call['id'] = fragment['id']
                for key in ('name', 'arguments'):
                    value = (fragment.get('function') or {}).get(key)
                    if value:
                        call['function'][key] += value
            if choice.get('finish_reason') is not None:
                if self.finish is not None:
                    raise ValueError('duplicate finish reason')
                self.finish = choice['finish_reason']

    def result(self):
        if self.calls:
            if sorted(self.calls) != list(range(len(self.calls))):
                raise ValueError('missing tool call index')
            self.message['tool_calls'] = [self.calls[index] for index in sorted(self.calls)]
        if not self.done or self.finish is None:
            raise ValueError('incomplete stream')
        return {'message': self.message, 'finish_reason': self.finish, 'usage': self.usage,
                'completion_id': self.completion_id, 'engine_data': self.engine_data,
                'output_ids': self.output_ids, 'first_generated_monotonic_ns': self.first_generated_ns,
                'first_semantic_monotonic_ns': self.first_semantic_ns}


class TokenBudget:
    def __init__(self, directory):
        from jinja2.sandbox import ImmutableSandboxedEnvironment
        from tokenizers import Tokenizer
        directory = Path(directory)
        config = json.loads((directory / 'tokenizer_config.json').read_text())
        self.hashes = {name: sha((directory / name).read_bytes()) for name in ('tokenizer.json', 'tokenizer_config.json')}
        if self.hashes != {'tokenizer.json': 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4',
                           'tokenizer_config.json': 'd5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101'}:
            raise ValueError('tokenizer files do not match the pinned Qwen3-8B revision')
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        env.filters['tojson'] = lambda value, **kwargs: json.dumps(value, ensure_ascii=False, **kwargs)
        self.template = env.from_string(config['chat_template'])
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))

    def ids(self, body):
        # The first server call must confirm this renderer before long histories proceed.
        rendered = self.template.render(messages=body['messages'], tools=body['tools'],
                                        add_generation_prompt=True, enable_thinking=True)
        return self.tokenizer.encode(rendered, add_special_tokens=False).ids

    def count(self, body):
        return len(self.ids(body))


def observation(result, limit):
    text = 'exit_code: ' + str(result.get('exit_code')) + '\nstdout:\n' + result.get('stdout', '') + '\nstderr:\n' + result.get('stderr', '')
    if len(text) <= limit:
        return text, False
    head = limit // 2
    return text[:head] + '\n[tool output truncated]\n' + text[-(limit - head):], True
