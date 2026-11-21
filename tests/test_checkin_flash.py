from datetime import datetime
from threading import Barrier, Event, Lock, Timer
import time
import unittest
from unittest.mock import patch

from checkin_client import CheckinError
from checkin_flash import CLOCK_URL, calibrate_flash_clock, run_flash_burst
from recharge.cmcc import SHANGHAI


class Prepared:
    def __init__(self, outcome=None, *, wait=0.04, delay=0.02, window=3):
        self.ready = True
        self.result = {'flash_state': 'available', 'flash_message': '可参与'}
        self.start_monotonic = time.monotonic() + wait
        self.end_monotonic = self.start_monotonic + window
        self.delay, self.outcome = delay, outcome
        self.guard = Lock()
        self.starts, self.ends = [], []
        self.active, self.peak, self.checks = 0, 0, 0
        self.confirmed = False

    def send(self, *, before_submit):
        before_submit()
        with self.guard:
            index = len(self.starts)
            self.starts.append(time.monotonic())
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if self.outcome:
                return self.outcome(index)
            return {'state': 'retryable', 'code': 'NET_ERROR', 'message': '繁忙'}
        finally:
            with self.guard:
                self.active -= 1
                self.ends.append(time.monotonic())

    @staticmethod
    def local_time():
        return datetime.now(SHANGHAI).isoformat(timespec='milliseconds')

    def reconcile(self):
        self.checks += 1
        return {'flash_state': 'already_claimed' if self.confirmed else 'available'}


class FlashBurstTests(unittest.TestCase):
    def test_clock_calibration_samples_once_and_uses_request_midpoints(self):
        class Response:
            headers = {'Date': 'Thu, 29 Oct 2026 04:00:00 GMT'}
        class Session:
            def __init__(self):
                self.headers, self.calls, self.closed = {}, [], False
            def options(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()
            def close(self):
                self.closed = True
        session = Session()
        server = datetime(2026, 10, 29, 12, 0, 0, tzinfo=SHANGHAI).timestamp()
        timeline = [value for _ in range(30) for value in (server+0.1, server+0.2)]
        with patch('checkin_flash.time.time', side_effect=timeline):
            result = calibrate_flash_clock(session_factory=lambda: session)
        self.assertEqual((result['state'], result['samples']), ('ready', 30))
        self.assertAlmostEqual(result['offset_ms'], 350, places=3)
        self.assertEqual(len(session.calls), 30)
        self.assertTrue(all(call[0] == CLOCK_URL for call in session.calls))
        self.assertTrue(session.closed)

    def run_burst(self, prepared, *, count=6, workers=2, interval=0.01, cancelled=None):
        return run_flash_burst(prepared, {'burst_count': count, 'burst_workers': workers,
                'burst_interval': interval}, lambda: None, cancelled or (lambda: False), followup_seconds=0.04)

    def test_not_ready_never_sends(self):
        prepared = Prepared()
        prepared.ready = False
        result = self.run_burst(prepared)
        self.assertFalse(result['submitted'])
        self.assertEqual(prepared.starts, [])

    def test_timed_overlap_bounded_workers_and_no_dropped_queue(self):
        prepared = Prepared(delay=0.06)
        result = self.run_burst(prepared)
        self.assertEqual(result['attempt_count'], 7)  # Main burst plus delayed follow-up.
        self.assertEqual(prepared.peak, 2)
        self.assertLess(prepared.starts[1], prepared.ends[0])
        self.assertGreaterEqual(min(prepared.starts), prepared.start_monotonic)
        self.assertGreaterEqual(prepared.starts[-1], prepared.start_monotonic+0.09)
        self.assertTrue(result['attempts'][-1]['followup'])
        self.assertEqual(prepared.active, 0)

    def test_success_cancels_backlog_and_followup(self):
        prepared = Prepared(lambda i: {'state': 'success', 'code': 'SUCCESS', 'message': '领取成功'}, delay=0.05)
        result = self.run_burst(prepared, count=30, workers=1, interval=0.001)
        self.assertEqual((result['state'], result['attempt_count']), ('success', 1))
        self.assertEqual(result['flash_state'], 'already_claimed')

    def test_sold_out_stops_without_delayed_retry(self):
        prepared = Prepared(lambda i: {'state': 'terminal', 'code': 'PRIZE_NO_STOCK', 'message': '已抢完'})
        result = self.run_burst(prepared, workers=1)
        self.assertEqual(result['attempt_count'], 1)
        self.assertIn('已抢完', result['message'])
        self.assertEqual(prepared.checks, 0)

    def test_inflight_success_is_retained_after_other_request_fails(self):
        def outcome(index):
            if index == 0:
                raise CheckinError('超时', uncertain=True)
            return {'state': 'success', 'message': '领取成功'}
        prepared = Prepared(outcome, delay=0.05)
        result = self.run_burst(prepared, workers=2, count=20)
        self.assertEqual((result['state'], result['attempt_count']), ('success', 2))
        self.assertEqual(prepared.checks, 0)

    def test_uncertain_write_only_checks_state_and_never_retries(self):
        def timeout(index):
            raise CheckinError('结果不明', uncertain=True)
        prepared = Prepared(timeout)
        result = self.run_burst(prepared, workers=1)
        self.assertEqual((result['state'], result['attempt_count'], prepared.checks), ('failed', 1, 1))
        prepared = Prepared(timeout)
        prepared.confirmed = True
        result = self.run_burst(prepared, workers=1)
        self.assertEqual((result['state'], result['attempt_count'], prepared.checks), ('success', 1, 1))

    def test_official_limit_is_confirmed_by_read_only_state(self):
        prepared = Prepared(lambda i: {'state': 'terminal', 'code': 'PRIZE_LIMIT_MONTH', 'message': '已达上限'})
        prepared.confirmed = True
        result = self.run_burst(prepared, workers=1)
        self.assertEqual((result['state'], result['attempt_count'], prepared.checks), ('success', 1, 1))

    def test_gwt_limit_does_not_reduce_explicit_fixed_request_count(self):
        def limited(_):
            return {'state': 'terminal', 'code': 'GWT_LIMITED', 'message': '官方未允许领取（GWT_LIMITED）'}

        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                prepared = Prepared(limited)
                prepared.confirmed = confirmed
                result = run_flash_burst(prepared,
                    {'burst_count': 10, 'burst_workers': 10, 'burst_interval': 0.01},
                    lambda: None, lambda: False, followup_seconds=None, stop_on_result=False)
                self.assertEqual(result['attempt_count'], 10)
                self.assertEqual(prepared.checks, 1)
                self.assertEqual([attempt['index'] for attempt in result['attempts']], list(range(1, 11)))
                self.assertTrue(all(attempt['code'] == 'GWT_LIMITED' for attempt in result['attempts']))
                self.assertFalse(any(attempt['followup'] for attempt in result['attempts']))
                self.assertEqual(result['state'], 'success' if confirmed else 'skipped')
                self.assertIn('官方状态确认已领取' if confirmed else '官方状态未显示已领取', result['message'])

    def test_stop_during_wait_sends_nothing(self):
        prepared, stop = Prepared(wait=0.2), Event()
        timer = Timer(0.03, stop.set)
        timer.start()
        self.addCleanup(timer.join)
        result = self.run_burst(prepared, cancelled=stop.is_set)
        self.assertEqual((result['state'], result['attempt_count']), ('stopped', 0))

    def test_timers_do_not_wait_for_an_earlier_request_to_return(self):
        ready = Barrier(4, timeout=2)

        def reply(_index):
            ready.wait()
            return {'state': 'terminal', 'code': 'NOT_AVAILABLE', 'message': '暂不可领取'}

        prepared = Prepared(reply, delay=0)
        result = run_flash_burst(prepared,
            {'burst_count': 4, 'burst_workers': 1, 'burst_interval': 0.05},
            lambda: None, lambda: False, followup_seconds=None, stop_on_result=False, use_timers=True)
        self.assertEqual(prepared.peak, 4)
        self.assertEqual(result['attempt_count'], 4)
        self.assertEqual([item['planned_offset_ms'] for item in result['attempts']], [0, 50, 100, 150])

    def test_official_end_prevents_remaining_requests(self):
        prepared = Prepared(delay=0.002, window=0.04)
        result = self.run_burst(prepared, count=30)
        self.assertGreater(result['attempt_count'], 0)
        self.assertLess(result['attempt_count'], 30)
        self.assertTrue(all(t < prepared.end_monotonic for t in prepared.starts))
        self.assertFalse(any(record['followup'] for record in result['attempts']))

    def test_fixed_reservation_ignores_official_end_and_sends_every_request(self):
        prepared = Prepared(wait=0.01, delay=0.002, window=0.01)
        result = run_flash_burst(prepared,
            {'burst_count': 20, 'burst_workers': 5, 'burst_interval': 0.001},
            lambda: None, lambda: False, followup_seconds=None, stop_on_result=False)
        self.assertEqual(result['attempt_count'], 20)
        self.assertEqual([attempt['index'] for attempt in result['attempts']], list(range(1, 21)))
        self.assertGreater(prepared.starts[-1], prepared.end_monotonic)


if __name__ == '__main__':
    unittest.main()
