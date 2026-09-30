"""Loopback-only streaming fixture; no router monkeypatches."""
import asyncio
import json
import time
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
events = []

@app.get('/events')
async def history():
    return events

@app.get('/v1/models')
async def models():
    return {'object': 'list', 'data': [{'id': 'gpt-4o-mini', 'object': 'model', 'created': 1, 'owned_by': 'repro'}]}

@app.post('/v1/chat/completions')
async def completions(request: Request):
    body = await request.json()
    mode = body.get('messages', [{}])[0].get('content', 'finite')
    events.append({'event': 'start', 'mode': mode, 'time': time.time()})
    if mode.startswith('header'):
        await asyncio.sleep(3600)
    async def stream():
        count = 0
        try:
            while True:
                if mode.startswith('keepalive'):
                    yield ': ping\n\n'
                else:
                    chunk = {'id': 'repro', 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': 'gpt-4o-mini', 'choices': [{'index': 0, 'delta': {'content': 'x' * (65536 if mode.startswith('flood') else 1)}, 'finish_reason': None}]}
                    yield 'data: ' + json.dumps(chunk) + '\n\n'
                count += 1
                if mode == 'finite' and count >= 3:
                    yield 'data: ' + json.dumps({'id': 'repro', 'object': 'chat.completion.chunk', 'model': 'gpt-4o-mini', 'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': count, 'total_tokens': count + 1}}) + '\n\n'
                    yield 'data: [DONE]\n\n'
                    return
                await asyncio.sleep(3600 if mode.startswith('silent') else (0.001 if mode.startswith('flood') else 0.5))
        finally:
            event = {'event': 'close', 'mode': mode, 'chunks': count, 'time': time.time()}
            events.append(event)
            print(json.dumps(event), flush=True)
    return StreamingResponse(stream(), media_type='text/event-stream')
