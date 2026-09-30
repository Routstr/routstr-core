import asyncio
import json
import socket
import subprocess
import time
import httpx

BASE='http://127.0.0.1:18090'

def snapshot():
    code="import sqlite3,json,time; c=sqlite3.connect('/tmp/reserved-main.db'); c.row_factory=sqlite3.Row; print(json.dumps({'time':time.time(),'keys':[dict(r) for r in c.execute(\"select hashed_key,balance,reserved_balance,reserved_at from api_keys where hashed_key like 'main-%'\")],'rows':[dict(r) for r in c.execute(\"select * from reservation_releases where key_hash like 'main-%'\")]}))"
    return json.loads(subprocess.check_output(['podman','exec','reserved-router-main','/.venv/bin/python','-c',code],text=True))

async def consume(mode):
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream('POST',BASE+'/v1/chat/completions',headers={'Authorization':'Bearer sk-main-'+mode},json={'model':'gpt-4o-mini','messages':[{'role':'user','content':mode}],'stream':True,'max_tokens':10}) as r:
                print('STREAM',mode,r.status_code,flush=True)
                async for _ in r.aiter_bytes(): pass
        print('ENDED',mode,flush=True)
    except asyncio.CancelledError:
        print('CLIENT_DISCONNECTED',mode,flush=True)
        raise
    except Exception as e:
        print('CLIENT_ERROR',mode,type(e).__name__,str(e),flush=True)

async def report(label):
    print(label,json.dumps(snapshot()),flush=True)
    async with httpx.AsyncClient(timeout=5) as c:
        for mode in ['silent-disconnect','endless-disconnect','keepalive','flood','header']:
            # Only attempt payout while reserved: avoid requiring a real mint.
            if next(k for k in snapshot()['keys'] if k['hashed_key']=='main-'+mode)['reserved_balance']:
                r=await c.post(BASE+'/v1/wallet/refund',headers={'Authorization':'Bearer sk-main-'+mode})
                print('REFUND',mode,r.status_code,r.text,flush=True)
        print('UPSTREAM_EVENTS',json.dumps((await c.get('http://127.0.0.1:18091/events')).json()),flush=True)

async def main():
    modes=['finite','silent','silent-disconnect','endless-disconnect','keepalive','header']
    tasks={m:asyncio.create_task(consume(m)) for m in modes}
    # Real client with a small receive buffer, never draining the HTTP response.
    sock=socket.socket(); sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024); sock.connect(('127.0.0.1',18090))
    body=json.dumps({'model':'gpt-4o-mini','messages':[{'role':'user','content':'flood'}],'stream':True,'max_tokens':10}).encode()
    sock.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer sk-main-flood\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    await asyncio.sleep(1)
    for m in ['silent-disconnect','endless-disconnect']:
        tasks[m].cancel()
    await asyncio.gather(tasks['silent-disconnect'],tasks['endless-disconnect'],return_exceptions=True)
    await asyncio.sleep(9)
    await report('AT_10_SECONDS')
    await asyncio.sleep(60)
    await report('AFTER_SWEEP')
    sock.close()
    tasks['keepalive'].cancel()
    await asyncio.gather(tasks['keepalive'],return_exceptions=True)
    await asyncio.sleep(8)
    await report('AFTER_ALL_CLIENTS_CLOSED')
    for task in tasks.values(): task.cancel()
    await asyncio.gather(*tasks.values(),return_exceptions=True)

asyncio.run(main())
