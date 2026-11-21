"""Timed requests for scheduled flash rounds."""
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextvars import copy_context
from datetime import datetime
from email.utils import parsedate_to_datetime
from feature_runtime import ThreadPoolExecutor, feature_running
from http_transport import requests
from threading import Event, Lock, Timer
import time

from checkin_client import CheckinError
from recharge.cmcc import SHANGHAI

BURST_DEFAULTS = {'burst_count': 40, 'burst_interval': 0.05, 'burst_workers': 8}
PREPARE_SECONDS = 90
FOLLOWUP_SECONDS = 10
CLOCK_URL = 'https://wx.10086.cn/qwhdhub/api/mark/markSeckill/redeem'
CLOCK_SAMPLES = 30


def calibrate_flash_clock(*, session_factory=None, samples=CLOCK_SAMPLES):
    """Estimate server-minus-local clock offset once for a whole flash round."""
    client = (session_factory or (lambda: requests.Session(impersonate='chrome')))()
    client.headers.update({'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    measured = []
    try:
        for _ in range(samples):
            sent = time.time()
            try:
                response = client.options(CLOCK_URL, timeout=(5, 10), allow_redirects=False)
            except requests.RequestsError as error:
                raise CheckinError('连接官方校时服务失败，本轮将使用本机时间',
                                   'CLOCK_NETWORK') from error
            received = time.time()
            raw = response.headers.get('Date')
            if not raw:
                continue
            try:
                server = parsedate_to_datetime(raw).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            # HTTP Date is precise to one second. Its second midpoint and the
            # request RTT midpoint match the reference getTimeDifference logic.
            measured.append((received-sent, server+0.5-(sent+received)/2))
    finally:
        client.close()
    if len(measured) < 3:
        raise CheckinError('官方校时响应不足，本轮将使用本机时间', 'CLOCK_SAMPLES_MISSING')
    latencies = sorted(sample[0] for sample in measured)
    q1, q3 = latencies[len(latencies)//4], latencies[len(latencies)*3//4]
    upper = q3+1.5*(q3-q1)
    retained = [sample for sample in measured if sample[0] <= upper]
    offset = sum(sample[1] for sample in retained)/len(retained)
    latency = sum(sample[0] for sample in retained)/len(retained)
    return {'state': 'ready', 'offset_ms': round(offset*1000, 3),
            'uncertainty_ms': round(max(0.5, latency/2)*1000, 1),
            'latency_ms': round(latency*1000, 1), 'samples': len(retained),
            'at': datetime.now(SHANGHAI).isoformat()}


def _dispatch_timers(times, send, cancelled, halt):
    timers, pending = [], set()

    def dispatch(index, future):
        try:
            future.set_result(send(index))
        except BaseException as error:
            future.set_exception(error)

    try:
        for index, deadline in enumerate(times):
            if cancelled() or halt.is_set():
                break
            future = Future()
            timer = Timer(max(0, deadline - time.monotonic()), copy_context().run,
                          args=(dispatch, index, future))
            timer.start()
            timers.append(timer)
            pending.add(future)
        while pending and not cancelled() and not halt.is_set():
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
    finally:
        halt.set()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            timer.join()
    for future in pending:
        if future.done():
            future.result()


def run_flash_burst(prepared, settings, before_submit, cancelled, *, followup_seconds=FOLLOWUP_SECONDS,
                    result_key='flash', stop_on_result=True, planned_offsets=None, use_timers=False):
    """Queue the configured request count by planned time.

    Each worker leases one prepared HTTP session. ``stop_on_result=False`` is
    the check-in rush mode: every configured request remains scheduled, and no
    extra follow-up is added when ``followup_seconds`` is ``None``.
    """
    original_cancelled = cancelled
    cancelled = lambda: original_cancelled() or not feature_running()
    if not prepared.ready:
        return {**prepared.result, 'state': 'skipped', 'submitted': False,
                'message': prepared.result[result_key+'_message'], 'attempt_count': 0, 'attempts': []}
    count, interval = (settings[k] for k in ('burst_count', 'burst_interval'))
    start, end = prepared.start_monotonic, prepared.end_monotonic
    times = [start + offset for offset in planned_offsets] if planned_offsets is not None else [start + i * interval for i in range(count)]
    if followup_seconds is not None:
        times.append(times[-1] + followup_seconds)
    halt, guard = Event(), Lock()
    records, pending = [], set()
    sent = 0

    def send(index):
        nonlocal sent
        if halt.is_set() or cancelled() or stop_on_result and time.monotonic() >= end:
            return
        record = {'index': index + 1, 'followup': index == count, 'sent_at': None,
                  'planned_offset_ms':round((times[index]-start)*1000)}
        def entering():
            nonlocal sent
            if halt.is_set() or cancelled() or stop_on_result and time.monotonic() >= end:
                raise CheckinError('已停止后续秒杀请求', 'BURST_STOPPED', 409)
            if before_submit is not None:
                before_submit()
            with guard:
                sent += 1
                record['sent_at'] = prepared.local_time()
        try:
            outcome = prepared.send(before_submit=entering)
        except CheckinError as error:
            outcome = {'state': 'uncertain' if error.uncertain else 'failed', 'message': str(error), 'code': error.code}
        record.update(outcome)
        with guard:
            if record['sent_at'] is not None or outcome.get('code') != 'BURST_STOPPED':
                records.append(record)
            if stop_on_result and outcome['state'] != 'retryable':
                halt.set()
        return outcome

    if use_timers:
        _dispatch_timers(times, send, cancelled, halt)
    else:
        workers = settings['burst_workers']
        workers = min(workers, getattr(prepared, 'worker_count', workers) or workers)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='checkin-flash-request')
        cursor = 0
        try:
            while cursor < len(times) or pending:
                if cancelled() or stop_on_result and time.monotonic() >= end:
                    halt.set()
                done = {future for future in pending if future.done()}
                for future in done:
                    if not future.cancelled():
                        future.result()
                pending.difference_update(done)
                if halt.is_set():
                    for future in pending:
                        future.cancel()
                    cursor = len(times)
                now = time.monotonic()
                if cursor < len(times) and now >= times[cursor] and not halt.is_set():
                    pending.add(pool.submit(send, cursor))
                    cursor += 1
                    continue
                if not pending and cursor >= len(times):
                    break
                delay = min(0.1, max(0, times[cursor] - now)) if cursor < len(times) else 0.1
                if pending:
                    wait(pending, timeout=delay, return_when=FIRST_COMPLETED)
                else:
                    halt.wait(delay)
        finally:
            halt.set()
            pool.shutdown(wait=True, cancel_futures=True)
    records.sort(key=lambda record: record['index'])
    winner = next((r for r in records if r['state'] == 'success'), None)
    uncertain = any(r['state'] == 'uncertain' for r in records)
    gwt_limited = result_key == 'flash' and any(r.get('code') == 'GWT_LIMITED' for r in records)
    needs_check = uncertain or gwt_limited or any(
        str(r.get('code', '')).startswith('PRIZE_LIMIT') for r in records)
    result = prepared.result
    checked = False
    if winner is None and needs_check:
        try:
            result = prepared.reconcile()
            checked = True
            if result[result_key+'_state'] == 'already_claimed':
                winner = {'message': '官方状态确认已领取'}
        except CheckinError:
            pass
    if winner:
        state, message = 'success', winner['message']
        result = {**result, result_key+'_state': 'already_claimed'}
    elif uncertain:
        state = 'failed'
        message = ('领取结果待核实，已停止剩余请求，本轮不会重发' if stop_on_result
                   else '领取结果待核实，本轮不会重发')
    elif cancelled():
        state, message = 'stopped', '已停止后续请求，已发送的结果已保留'
    elif records:
        last = next((r for r in records if r['state'] in ('failed', 'terminal')), records[-1])
        state = 'failed' if last['state'] == 'failed' else 'skipped'
        message = last['message']
        if gwt_limited:
            message += '。官方状态未显示已领取' if checked else '。领取状态待核实'
    else:
        state, message = 'skipped', '场次已结束，本轮没有发送请求'
    message = f'已发起 {sent} 次 · '+message
    return {**result, 'state': state, 'message': message, result_key+'_message': message,
            'submitted': bool(sent), 'uncertain': uncertain and winner is None,
            'attempt_count': sent, 'attempts': records}
