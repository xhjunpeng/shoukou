"""Local Responses fixture for the actual desktop-bundled Codex engine.

No real account, API key, model request, or user conversation is used. The
native engine still dispatches tools and lifecycle hooks itself.
"""
import http.server
import copy
import json
import os
from pathlib import Path
import subprocess
import threading

ENGINE = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')


def native_run(home, repo, outputs, prompt='Edit the fixture as requested.', timeout=60, sandbox='workspace-write', model='fixture-model'):
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            output = outputs[min(len(requests) - 1, len(outputs) - 1)]
            if callable(output):
                output = output(body)
            output = copy.deepcopy(output)
            for index, item in enumerate(output):
                item['id'] = f'item_fixture_{len(requests)}_{index}'
                if item['type'] in ('function_call', 'custom_tool_call'):
                    item['call_id'] = f'call_fixture_{len(requests)}_{index}'
            rid = 'resp_fixture_' + str(len(requests))
            response = {'id': rid, 'object': 'response', 'status': 'completed',
                        'output': output, 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            def event(name, value):
                self.wfile.write(('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.flush()
            event('response.created', {'type': 'response.created', 'response': {'id': rid, 'status': 'in_progress', 'output': []}})
            for index, item in enumerate(output):
                event('response.output_item.added', {'type': 'response.output_item.added', 'output_index': index, 'item': item})
                event('response.output_item.done', {'type': 'response.output_item.done', 'output_index': index, 'item': item})
            event('response.completed', {'type': 'response.completed', 'response': response})

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args = [str(ENGINE), '--no-daemon', '--dangerously-bypass-hook-trust',
            '-a', 'never', '-s', sandbox, '-C', str(repo),
            '-c', 'model_provider="fixture"', '-c', 'model=' + json.dumps(model),
            '-c', 'model_providers.fixture={name="Local validation only",base_url="http://127.0.0.1:' + str(server.server_port) + '/v1",wire_api="responses",requires_openai_auth=false}',
            '-c', 'features.shell_snapshot=false', '-c', 'features.memories=false',
            '-c', 'features.plugins=false', '-c', 'features.apps=false',
            'exec', '--ephemeral', '--ignore-user-config', '--json', '--skip-git-repo-check', prompt]
    env = dict(os.environ, CODEX_HOME=str(home), DO_NOT_TRACK='1')
    # Explicit fixture provider never reads user credentials or inherited API keys.
    for key in list(env):
        if key.endswith(('API_KEY', 'AUTH_TOKEN')) or key.startswith('OPENAI_'):
            env.pop(key, None)
    try:
        result = subprocess.run(args, env=env, cwd=repo, capture_output=True, text=True, timeout=timeout)
        return {'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                'request_count': len(requests), 'requests': requests,
                'engine': str(ENGINE)}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def message(text='Fixture finished.'):
    return [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}]


def command(cmd):
    return [{'id': 'fc_fixture', 'type': 'function_call', 'call_id': 'call_fixture',
             'name': 'exec_command', 'arguments': json.dumps({'cmd': cmd, 'yield_time_ms': 1000, 'max_output_tokens': 1000})}]
