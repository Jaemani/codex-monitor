#!/usr/bin/env python3
"""Opt-in Rust outage/restart check with a fake owner and no model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import websockets.asyncio.server

ROOT = Path(__file__).resolve().parents[2]


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def run(args):
    binary = args.binary.resolve()
    report = {'result': 'RUNNING', 'surface': 'fake owner; no model', 'checks': {}, 'outage_seconds_requested': args.seconds}
    started = time.monotonic()
    process = None
    log = None
    temp = tempfile.TemporaryDirectory(prefix='cm-rust-outage-')
    root = Path(temp.name)
    peer = None
    submissions = {}
    attempts = {}
    peer_errors = []
    forbidden = []

    def save():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        args.report.write_text(json.dumps(report, indent=2) + '\n')

    def cli(*argv):
        p = subprocess.run([str(binary), '--state', str(root), *argv], capture_output=True, text=True, timeout=10)
        if p.returncode:
            raise RuntimeError(p.stderr[:300])
        return json.loads(p.stdout)

    async def wait(predicate, timeout=80):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError('expected state did not arrive')
            await asyncio.sleep(.1)

    def stop():
        nonlocal process
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process = None

    async def start():
        nonlocal process
        process = subprocess.Popen([str(binary), '--state', str(root), 'serve'], stdout=log, stderr=log)
        await wait(lambda: cli('status')['receiver']['ready'], 15)

    async def handle_requests(ws):
        async for raw in ws:
            request = json.loads(raw)
            method = request.get('method')
            if 'id' not in request:
                continue
            if method == 'initialize':
                value = {'userAgent': 'outage-fixture'}
            elif method == 'account/read':
                value = {'account': {'type': 'chatgpt'}, 'requiresOpenaiAuth': True}
            elif method == 'thread/read':
                value = {'thread': {'id': 'fixture', 'status': {'type': 'idle'}}}
            elif method == 'thread/loaded/list':
                value = {'data': ['fixture']}
            elif method == 'thread/queue/list':
                value = {'data': list(submissions.values())}
            elif method == 'thread/turns/list':
                value = {'data': []}
            elif method == 'thread/queue/add':
                key = request['params']['clientUserMessageId']
                attempts[key] = attempts.get(key, 0) + 1
                if key in submissions:
                    raise AssertionError('duplicate native submission')
                submissions[key] = {'id': 'submission-' + str(len(submissions)), 'clientUserMessageId': key}
                value = {'queuedSubmission': submissions[key]}
            else:
                forbidden.append(method)
                value = {}
            await ws.send(json.dumps({'id': request['id'], 'result': value}))

    async def handler(ws):
        try:
            await handle_requests(ws)
        except websockets.exceptions.ConnectionClosed:
            pass  # Receiver SIGKILL deliberately closes without a handshake.
        except Exception as error:
            peer_errors.append(str(error))

    save()
    try:
        cli('init', '--port', str(port()))
        cli('source', 'test')
        native_port = port()
        cli('bind', 'route', '--thread', 'fixture', '--source', 'test', '--endpoint', f'ws://127.0.0.1:{native_port}')
        log = root.joinpath('receiver.log').open('w')
        await start()
        receipt = cli('send', '--to', 'route', '--source', 'test', '--id', 'outage', '--data', '{"message":"fixture"}')
        outage_start = time.monotonic()
        restarted = False
        while time.monotonic() - outage_start < args.seconds:
            if not restarted and time.monotonic() - outage_start >= args.seconds / 2:
                stop()
                await start()
                restarted = True
            event = cli('event', receipt['delivery_id'])
            assert event['state'] == 'pending', event['state']
            assert event['attempts'] == 0, 'unavailable-owner probe spent a delivery attempt'
            assert cli('status')['receiver']['ready']
            await asyncio.sleep(min(5, max(.1, args.seconds - (time.monotonic() - outage_start))))
        report['outage_seconds_observed'] = round(time.monotonic() - outage_start, 3)
        report['checks']['backlog_preserved_without_spending_delivery_attempts'] = True
        report['checks']['receiver_sigkill_and_restart_during_outage'] = restarted
        peer = await websockets.asyncio.server.serve(handler, '127.0.0.1', native_port)
        await wait(lambda: cli('event', receipt['delivery_id'])['state'] == 'accepted')
        assert len(submissions) == 1
        report['checks']['recovered_delivery_once'] = True
        duplicate = cli('send', '--to', 'route', '--source', 'test', '--id', 'outage', '--data', '{"message":"fixture"}')
        assert duplicate['duplicate'] and duplicate['delivery_id'] == receipt['delivery_id']
        stop()
        await start()
        await asyncio.sleep(2)
        assert len(submissions) == 1 and cli('event', receipt['delivery_id'])['state'] == 'accepted'
        report['checks']['accepted_receipt_survives_restart_without_replay'] = True
        assert not peer_errors, peer_errors
        assert all(count == 1 for count in attempts.values()), attempts
        assert not forbidden, forbidden
        report['checks']['no_start_resume_turn_or_interrupt_calls'] = True
        report['result'] = 'PASS'
    except Exception as error:
        report['result'] = 'FAIL'
        report['error'] = str(error)
    finally:
        stop()
        if peer:
            peer.close()
            await peer.wait_closed()
        if log:
            log.close()
        temp.cleanup()
        report['checks']['temporary_state_removed'] = not root.exists()
        save()
    print(json.dumps(report), flush=True)
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True, action='store_true')
    p.add_argument('--binary', type=Path, default=ROOT / 'rust/target/release/codex-monitor-rs')
    p.add_argument('--seconds', type=float, default=120)
    p.add_argument('--report', required=True, type=Path)
    args = p.parse_args()
    if not 2 <= args.seconds <= 3500:
        p.error('seconds must be between 2 and 3500 (below the one-hour delivery TTL)')
    if args.report.resolve().is_relative_to(ROOT):
        p.error('raw report must be outside repository')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(run(args)))
