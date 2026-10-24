"""Provider-overload resilience of the NVIDIA client, reproduced against the mock.

Measured on the live NVIDIA API on 2026-10-18: nvidia/nemotron-3-ultra-550b-a55b often answers
503 "Service temporarily overloaded" on the free tier, sometimes as an HTTP 503 and sometimes as
an error chunk inside a 200 stream. Kyora retries such an answer like an HTTP 503 (only while no
output reached the caller), then moves the role to its next fallback model for a short cooldown,
and says so in ``model.fallback`` (``temporary: true``). 401/403/404/410 behave as before.
"""
from __future__ import annotations

import logging
import threading
import time
import unittest
from unittest import mock

from kyora.config import KimiCfg, NemotronCfg
from kyora.models import nim_client
from kyora.models.kimi import Kimi, KimiConversation
from kyora.models.nemotron import Nemotron
from kyora.models.nim_client import (
    BUSY_COOLDOWN_S, ModelChain, NIMBodyError, NIMCancelled, NIMClient, NIMError, NIMTimeout, NIMUnreachable,
    call_with_model_fallback, explain_http_failure, is_temporary_failure,
)
from kyora.pipeline.events import EventBus
from tests.mock_nim import MockNIM, iter_stream_chunks, scripted_handler, with_stream_error

ULTRA, SUPER, GLM = "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-super-120b-a12b", "z-ai/glm-5.3"
LAGUNA = "poolside/laguna-xs-2.1"
OVERLOADED = {"error": {"message": "Service temporarily overloaded", "type": "Service Unavailable", "code": 503}}
OVERLOAD_WORDING = ("the provider is temporarily overloaded (common on the free tier): Kyora retried and uses the "
                    "next fallback model; it returns to this model after a short cooldown")
_STATE: dict = {}


def setUpModule() -> None:
    # retries and fallbacks are logged as warnings by design; keep the test output readable
    lg = logging.getLogger("kyora.models")
    _STATE["level"] = lg.level
    lg.setLevel(logging.ERROR)


def tearDownModule() -> None:
    logging.getLogger("kyora.models").setLevel(_STATE.get("level", logging.NOTSET))


class Clock:
    """A settable monotonic clock for cooldowns."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _client(mock_server: MockNIM, **kw) -> NIMClient:
    kw.setdefault("backoff_s", (0.01, 0.02))
    kw.setdefault("timeout_s", 20.0)
    return NIMClient(mock_server.url, "nvapi-test-key-123456", **kw)


def _msgs() -> list[dict]:
    return [{"role": "user", "content": "x"}]


class MockStreamErrorTests(unittest.TestCase):
    """The mock emits what the gateway sends; the error event placement is what the client tests rely on."""

    def test_first_event_by_default_and_after_n_delta_chunks(self):
        req = {"model": "m"}
        first = list(with_stream_error(iter_stream_chunks(req, {"content": "abcd"}), {"stream_error": OVERLOADED}))
        self.assertEqual(first, [OVERLOADED])
        spec = {"content": "abcdef", "chunks": 3, "stream_error": OVERLOADED, "after_chunks": 2}
        items = list(with_stream_error(iter_stream_chunks(req, spec), spec))
        self.assertEqual(items[-1], OVERLOADED)
        contents = [c["choices"][0]["delta"].get("content") for c in items[:-1]]
        self.assertEqual(contents, ["", "ab", "cd"])  # the role chunk, then exactly two content chunks

    def test_bare_error_is_wrapped_and_too_few_chunks_end_with_the_error(self):
        spec = {"content": "ab", "chunks": 1, "stream_error": {"message": "overloaded"}, "after_chunks": 5}
        items = list(with_stream_error(iter_stream_chunks({"model": "m"}, spec), spec))
        self.assertEqual(items[-1], {"error": {"message": "overloaded"}})
        self.assertNotIn("[DONE]", items)


class ClientRetryTests(unittest.TestCase):
    def test_error_chunk_before_output_is_retried_then_succeeds(self):
        handler = scripted_handler([
            (lambda req: True, {"stream_error": OVERLOADED, "once": True}),
            (lambda req: True, {"content": "recovered after overload", "chunks": 3}),
        ])
        deltas = []
        with MockNIM(handler) as server:
            res = _client(server).chat(ULTRA, _msgs(), on_delta=deltas.append)
            self.assertEqual(len(server.requests), 2)
        self.assertEqual(res.content, "recovered after overload")
        self.assertEqual("".join(d.text for d in deltas if d.kind == "content"), "recovered after overload")

    def test_retried_exactly_like_http_503(self):
        def delays_for(spec: dict) -> tuple[list[float], NIMError, int]:
            with MockNIM(lambda req: dict(spec)) as server:
                client = _client(server, max_attempts=3, backoff_s=(0.01, 0.02))
                delays: list[float] = []
                client._sleep = lambda seconds, cancel: delays.append(seconds)
                with self.assertRaises(NIMError) as cm:
                    client.chat(ULTRA, _msgs())
                return delays, cm.exception, len(server.requests)

        body_delays, body_exc, body_n = delays_for({"stream_error": OVERLOADED})
        http_delays, http_exc, http_n = delays_for({"status": 503, "error_body": OVERLOADED})
        self.assertEqual(body_delays, http_delays)
        self.assertEqual(body_delays, [0.01, 0.02])
        self.assertEqual((body_n, http_n), (3, 3))
        self.assertIsInstance(body_exc, NIMBodyError)
        self.assertNotIsInstance(body_exc, NIMUnreachable)
        self.assertEqual((body_exc.status, http_exc.status), (503, 503))
        self.assertEqual((body_exc.attempts, http_exc.attempts), (3, 3))
        self.assertFalse(body_exc.output_started)
        self.assertIn("Service temporarily overloaded", str(body_exc))
        self.assertIn("Service Unavailable", body_exc.body)

    def test_error_after_output_is_raised_without_retry(self):
        spec = {"content": "abcdefghijkl", "chunks": 4, "stream_error": OVERLOADED, "after_chunks": 2}
        deltas = []
        with MockNIM(lambda req: dict(spec)) as server:
            with self.assertRaises(NIMBodyError) as cm:
                _client(server).chat(ULTRA, _msgs(), on_delta=deltas.append)
            self.assertEqual(len(server.requests), 1)  # a retry would deliver "abcdef" twice
        self.assertTrue(cm.exception.output_started)
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.attempts, 1)
        self.assertEqual([d.text for d in deltas], ["abc", "def"])

    def test_error_after_reasoning_counts_as_output(self):
        spec = {"reasoning": "thinking hard", "reasoning_chunks": 2, "content": "x", "stream_error": OVERLOADED,
                "after_chunks": 1}
        with MockNIM(lambda req: dict(spec)) as server:
            with self.assertRaises(NIMBodyError) as cm:
                _client(server).chat(ULTRA, _msgs())
            self.assertEqual(len(server.requests), 1)
        self.assertTrue(cm.exception.output_started)

    def test_numeric_string_code_becomes_the_status(self):
        err = {"error": {"message": "Service temporarily overloaded", "code": "503"}}
        with MockNIM(lambda req: {"stream_error": err}) as server:
            with self.assertRaises(NIMError) as cm:
                _client(server, max_attempts=2).chat(ULTRA, _msgs())
            self.assertEqual(len(server.requests), 2)
        self.assertEqual(cm.exception.status, 503)

    def test_client_side_codes_inside_a_200_are_not_retried(self):
        err = {"error": {"message": "prompt is too long", "type": "BadRequestError", "code": 400}}
        with MockNIM(lambda req: {"stream_error": err}) as server:
            with self.assertRaises(NIMBodyError) as cm:
                _client(server).chat(ULTRA, _msgs())
            self.assertEqual(len(server.requests), 1)
        self.assertEqual(cm.exception.status, 400)

    def test_without_a_code_only_overload_wording_is_retried(self):
        with MockNIM(lambda req: {"stream_error": {"error": {"message": "Service temporarily overloaded"}}}) as server:
            with self.assertRaises(NIMBodyError) as cm:
                _client(server, max_attempts=3).chat(ULTRA, _msgs())
            self.assertEqual(len(server.requests), 3)
        self.assertIsNone(cm.exception.status)
        with MockNIM(lambda req: {"stream_error": {"error": {"message": "something odd happened"}}}) as server:
            with self.assertRaises(NIMBodyError):
                _client(server, max_attempts=3).chat(ULTRA, _msgs())
            self.assertEqual(len(server.requests), 1)

    def test_error_body_of_a_non_stream_200_is_retried_too(self):
        handler = scripted_handler([
            (lambda req: True, {"stream_error": OVERLOADED, "once": True}),
            (lambda req: True, {"content": "pong"}),
        ])
        with MockNIM(handler) as server:
            res = _client(server).chat(ULTRA, _msgs(), stream=False)
            self.assertEqual(len(server.requests), 2)
        self.assertEqual(res.content, "pong")

    def test_cancel_is_honoured_during_the_backoff(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        with MockNIM(lambda req: {"stream_error": OVERLOADED}) as server:
            timer.start()
            t0 = time.monotonic()
            try:
                with self.assertRaises(NIMCancelled):
                    _client(server, backoff_s=(5.0,)).chat(ULTRA, _msgs(), cancel=cancel)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - t0, 3.0)
            self.assertEqual(len(server.requests), 1)


class ChainBusyTests(unittest.TestCase):
    def test_busy_model_moves_behind_the_healthy_ones_until_its_cooldown_ends(self):
        clock = Clock()
        chain = ModelChain(ULTRA, [GLM, LAGUNA], clock=clock)
        chain.mark_busy(ULTRA, 120, "overloaded")
        self.assertEqual(chain.candidates(), [GLM, LAGUNA, ULTRA])  # never removed
        self.assertEqual((chain.active, chain.fallback_active), (GLM, True))
        self.assertEqual(chain.to_dict()["busy"], {ULTRA: 120.0})
        self.assertEqual(chain.busy_reason(ULTRA), "overloaded")
        clock.advance(119.5)
        self.assertEqual(chain.candidates(), [GLM, LAGUNA, ULTRA])
        self.assertEqual(chain.busy(), {ULTRA: 0.5})
        clock.advance(0.5)
        self.assertEqual(chain.candidates(), [ULTRA, GLM, LAGUNA])  # back in first place
        self.assertEqual((chain.active, chain.fallback_active), (ULTRA, False))
        self.assertEqual(chain.to_dict()["busy"], {})
        self.assertIsNone(chain.busy_reason(ULTRA))

    def test_a_model_that_sends_nothing_in_time_is_skipped_like_an_overloaded_one(self):
        # live 2026-10-23: GLM-5.3 took 26 s for a 1-token chat and the agent probe ran out of time on it
        chain = ModelChain(GLM, [ULTRA])
        bus = EventBus(None)
        self.addCleanup(bus.close)

        def call(model):
            if model == GLM:
                raise NIMTimeout(f"{model} at integrate.api.nvidia.com did not answer within 20 s (timed out)")
            return f"answer from {model}"
        self.assertEqual(call_with_model_fallback(chain, bus, call, role="agent"), f"answer from {ULTRA}")
        self.assertIn(GLM, chain.busy())
        [fb] = [e.data for e in bus.replay(0) if e.type == "model.fallback"]
        self.assertEqual((fb["from_model"], fb["to_model"], fb["temporary"], fb["status"]), (GLM, ULTRA, True, None))
        self.assertIn("did not answer in time", fb["reason"])
        unreachable = ModelChain(GLM, [ULTRA])  # the endpoint itself: no other model is closer
        with self.assertRaises(NIMUnreachable):
            call_with_model_fallback(unreachable, None, lambda m: (_ for _ in ()).throw(
                NIMUnreachable("integrate.api.nvidia.com unreachable: [Errno -3] DNS")), role="agent")
        self.assertEqual(unreachable.busy(), {})

    def test_several_busy_models_soonest_free_first(self):
        clock = Clock()
        chain = ModelChain(ULTRA, [GLM, LAGUNA], clock=clock)
        chain.mark_busy(GLM, 120)
        clock.advance(10)
        chain.mark_busy(ULTRA, 120)
        self.assertEqual(chain.candidates(), [LAGUNA, GLM, ULTRA])
        chain.mark_busy(LAGUNA, 300)
        self.assertEqual(chain.candidates(), [GLM, ULTRA, LAGUNA])  # all busy: every model is still tried
        chain.clear_busy(ULTRA)
        self.assertEqual(chain.candidates(), [ULTRA, GLM, LAGUNA])

    def test_a_sticky_cooldown_outlasts_an_answer_and_a_shorter_mark(self):
        # live 2026-10-24: GLM-5.3, stopped for reasoning although thinking was off, answered one turn as the
        # stand-in for a rate-limited model, lost its cooldown that way and ran away again
        clock = Clock()
        chain = ModelChain(GLM, [ULTRA], clock=clock)
        chain.mark_busy(GLM, 1800, "kept reasoning", sticky=True)
        chain.clear_busy(GLM)  # it answered: a sticky cooldown runs its time
        self.assertEqual(chain.candidates(), [ULTRA, GLM])
        self.assertEqual(chain.sidelined(), {GLM})
        chain.mark_busy(GLM, 120, "overloaded")  # a shorter mark never shortens it
        self.assertEqual(chain.busy(), {GLM: 1800.0})
        self.assertEqual(chain.busy_reason(GLM), "kept reasoning")
        self.assertEqual(chain.to_dict()["sidelined"], [GLM])
        clock.advance(1800)
        self.assertEqual((chain.candidates(), chain.sidelined()), ([GLM, ULTRA], set()))

    def test_a_plain_cooldown_ends_with_an_answer_and_a_longer_sticky_mark_replaces_it(self):
        clock = Clock()
        chain = ModelChain(ULTRA, [GLM], clock=clock)
        chain.mark_busy(ULTRA, 120, "overloaded")
        self.assertEqual(chain.sidelined(), set())
        chain.mark_busy(ULTRA, 1800, "kept reasoning", sticky=True)
        self.assertEqual((chain.busy(), chain.sidelined()), ({ULTRA: 1800.0}, {ULTRA}))
        clock.advance(1790)
        chain.mark_busy(ULTRA, 120, "overloaded")  # extends the sticky cooldown: still sticky
        self.assertEqual((chain.busy(), chain.sidelined()), ({ULTRA: 120.0}, {ULTRA}))
        clock.advance(120)
        chain.mark_busy(ULTRA, 120, "overloaded")  # the sticky one has ended: a plain mark
        chain.clear_busy(ULTRA)
        self.assertEqual((chain.busy(), chain.candidates()), ({}, [ULTRA, GLM]))

    def test_skip_sidelined_leaves_the_sidelined_model_out_unless_nothing_else_is_left(self):
        chain = ModelChain(GLM, [ULTRA])
        chain.mark_busy(GLM, 1800, "kept reasoning", sticky=True)
        called = []

        def rate_limited(model):
            called.append(model)
            raise NIMError(f"HTTP 429 from integrate.api.nvidia.com for {model}", status=429,
                           body='{"status":429,"title":"Too Many Requests"}')
        with self.assertRaises(NIMError):
            call_with_model_fallback(chain, None, rate_limited, role="agent", skip_sidelined=True)
        self.assertEqual(called, [ULTRA], "the caller waits for ULTRA; GLM does not stand in for it")
        called.clear()
        with self.assertRaises(NIMError):  # without the flag (a caller's last try) it is still tried, last
            call_with_model_fallback(chain, None, rate_limited, role="agent")
        self.assertEqual(called, [ULTRA, GLM])
        only = ModelChain(GLM, [])
        only.mark_busy(GLM, 1800, "kept reasoning", sticky=True)
        self.assertEqual(call_with_model_fallback(only, None, lambda m: m, role="agent", skip_sidelined=True), GLM,
                         "a chain with only sidelined models still answers")

    def test_skip_sidelined_still_moves_on_from_a_gone_model_and_puts_sidelined_ones_last(self):
        clock = Clock()
        chain = ModelChain(GLM, [ULTRA, LAGUNA], clock=clock)
        chain.mark_busy(GLM, 1800, "kept reasoning", sticky=True)
        clock.advance(1750)  # 50 s left: sooner free than a model just marked busy for 120 s
        chain.mark_busy(LAGUNA, 120, "overloaded")
        self.assertEqual(chain.candidates(), [ULTRA, GLM, LAGUNA])
        called = []

        def call(model):
            called.append(model)
            if model == ULTRA:
                raise NIMError(f"HTTP 404 for {model}", status=404, body="Not found for account")
            if model == LAGUNA:
                raise NIMTimeout(f"{model} did not answer within 20 s")
            return f"answer from {model}"
        with self.assertRaises(NIMTimeout):  # LAGUNA timed out: wait for it, GLM does not stand in
            call_with_model_fallback(chain, None, call, role="agent", skip_sidelined=True)
        self.assertEqual(called, [ULTRA, LAGUNA], "sidelined GLM moved behind LAGUNA")
        self.assertIn(ULTRA, chain.gone())
        called.clear()
        chain.mark_gone(LAGUNA, "gone too")
        self.assertEqual(call_with_model_fallback(chain, None, call, role="agent", skip_sidelined=True),
                         f"answer from {GLM}", "only the sidelined model is left")

    def test_gone_stays_excluded_and_beats_busy(self):
        clock = Clock()
        chain = ModelChain(ULTRA, [GLM, LAGUNA], clock=clock)
        chain.mark_gone(GLM, "retired")
        chain.mark_busy(GLM, 120)
        chain.mark_busy(ULTRA, 120)
        self.assertEqual(chain.candidates(), [LAGUNA, ULTRA])
        chain.mark_gone(ULTRA, "retired too")
        self.assertEqual(chain.candidates(), [LAGUNA])
        self.assertEqual(chain.to_dict()["busy"], {})
        self.assertEqual(set(chain.to_dict()["gone"]), {ULTRA, GLM})
        chain.mark_gone(LAGUNA, "gone")
        self.assertEqual(chain.candidates(), [ULTRA])  # never empty, as before

    def test_default_clock_is_time_monotonic(self):
        now = {"t": 50.0}
        fake_time = mock.Mock(monotonic=lambda: now["t"])
        with mock.patch.object(nim_client, "time", fake_time):
            chain = ModelChain(ULTRA, [GLM])
            chain.mark_busy(ULTRA, BUSY_COOLDOWN_S)
            self.assertEqual(chain.candidates(), [GLM, ULTRA])
            now["t"] += BUSY_COOLDOWN_S
            self.assertEqual(chain.candidates(), [ULTRA, GLM])
        self.assertEqual(BUSY_COOLDOWN_S, 120)

    def test_thread_safe_under_concurrent_marks(self):
        chain = ModelChain(ULTRA, [GLM, LAGUNA, SUPER])
        models = [ULTRA, GLM, LAGUNA, SUPER]
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(300):
                    m = models[(n + i) % len(models)]
                    if i % 3 == 0:
                        chain.mark_busy(m, 0.001 * (i % 5), "x")
                    elif i % 3 == 1:
                        chain.clear_busy(m)
                    got = chain.candidates()
                    if sorted(got) != sorted(models):
                        raise AssertionError(f"lost a model: {got}")
                    chain.to_dict()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])


class FallbackRuleTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus(None)
        self.addCleanup(self.bus.close)
        self.clock = Clock()
        self.chain = ModelChain(ULTRA, [GLM, LAGUNA], clock=self.clock)
        self.calls: list[str] = []

    def events(self, type_: str) -> list[dict]:
        return [e.data for e in self.bus.replay(0) if e.type == type_]

    def run_chain(self, failures: dict, **kw):
        def call(model: str):
            self.calls.append(model)
            exc = failures.get(model)
            if exc is not None:
                raise exc
            return f"answer from {model}"
        return call_with_model_fallback(self.chain, self.bus, call, role="nemotron", purpose="design", **kw)

    def test_overload_moves_on_marks_busy_and_publishes_temporary(self):
        exc = NIMError("HTTP 503", status=503, body='{"error":{"message":"Service temporarily overloaded"}}',
                       attempts=4)
        self.assertEqual(self.run_chain({ULTRA: exc}), f"answer from {GLM}")
        fb = self.events("model.fallback")
        self.assertEqual(len(fb), 1)
        self.assertEqual((fb[0]["kind"], fb[0]["from_model"], fb[0]["to_model"], fb[0]["status"], fb[0]["temporary"]),
                         ("model", ULTRA, GLM, 503, True))
        self.assertEqual(fb[0]["cooldown_s"], BUSY_COOLDOWN_S)
        self.assertEqual(fb[0]["reason"], OVERLOAD_WORDING)
        self.assertEqual((fb[0]["requested"], fb[0]["purpose"]), (ULTRA, "design"))
        self.assertEqual(set(self.chain.busy()), {ULTRA})
        self.assertEqual(self.chain.gone(), {})
        self.run_chain({})
        self.assertEqual(self.calls, [ULTRA, GLM, GLM])  # the busy model waits for its cooldown
        self.clock.advance(BUSY_COOLDOWN_S)
        self.assertEqual(self.run_chain({}), f"answer from {ULTRA}")
        self.assertEqual(self.calls[-1], ULTRA)

    def test_temporary_statuses_and_wording_without_a_status(self):
        for exc in (NIMError("x", status=500), NIMError("x", status=502), NIMError("x", status=504),
                    NIMBodyError("stream error: Service Unavailable", body='{"error":{"type":"Service Unavailable"}}'),
                    NIMError("model temporarily unavailable")):
            with self.subTest(exc=str(exc), status=exc.status):
                self.assertTrue(is_temporary_failure(exc))
                chain = ModelChain(ULTRA, [GLM])

                def call(model: str, exc=exc) -> str:
                    if model == ULTRA:
                        raise exc
                    return model

                self.assertEqual(call_with_model_fallback(chain, None, call, role="kimi"), GLM)
                self.assertIn(ULTRA, chain.busy())

    def test_what_another_model_cannot_fix_never_changes_the_model(self):
        cases = [NIMError("HTTP 401", status=401, body="Service temporarily unavailable"),
                 NIMError("HTTP 403", status=403, body='{"detail": "Authorization failed"}'),
                 NIMError("HTTP 429", status=429, body="insufficient credit balance"), NIMError("HTTP 400", status=400),
                 NIMError("HTTP 501", status=501), NIMUnreachable("host unreachable: Service Unavailable"),
                 NIMCancelled()]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__, status=exc.status):
                self.calls.clear()
                with self.assertRaises(type(exc)):
                    self.run_chain({ULTRA: exc})
                self.assertEqual(self.calls, [ULTRA])
                self.assertEqual((self.chain.busy(), self.chain.gone()), ({}, {}))
        self.assertEqual(self.events("model.fallback"), [])

    def test_a_rate_limited_model_is_skipped_like_an_overloaded_one(self):
        # NVIDIA's free tier limits each model: live 2026-10-23 Nemotron-3-Ultra answered 429 while GLM-5.3 answered
        limited = NIMError("HTTP 429", status=429, body='{"status":429,"title":"Too Many Requests"}')
        self.assertEqual(self.run_chain({ULTRA: limited}), f"answer from {GLM}")
        [fb] = self.events("model.fallback")
        self.assertEqual((fb["status"], fb["temporary"]), (429, True))
        self.assertIn("next fallback model", fb["reason"])
        self.assertIn(ULTRA, self.chain.busy())
        self.clock.advance(BUSY_COOLDOWN_S)
        self.assertEqual(self.chain.candidates()[0], ULTRA, "it returns after the cooldown")

    def test_gone_is_unchanged_and_says_temporary_false(self):
        gone = NIMError("HTTP 410", status=410, body="reached its end of life on 2026-10-15T08:00:00Z")
        self.assertEqual(self.run_chain({ULTRA: gone}), f"answer from {GLM}")
        fb = self.events("model.fallback")
        self.assertEqual((fb[0]["status"], fb[0]["temporary"]), (410, False))
        self.assertNotIn("cooldown_s", fb[0])
        self.assertIn(ULTRA, self.chain.gone())
        self.assertEqual(self.chain.busy(), {})
        self.clock.advance(10 * BUSY_COOLDOWN_S)
        self.assertEqual(self.chain.candidates(), [GLM, LAGUNA])  # gone is for good

    def test_last_candidate_overloaded_raises_and_is_still_marked_busy(self):
        busy = NIMError("HTTP 503", status=503, attempts=4)
        with self.assertRaises(NIMError):
            self.run_chain({ULTRA: busy, GLM: busy, LAGUNA: busy})
        self.assertEqual(self.calls, [ULTRA, GLM, LAGUNA])
        self.assertEqual(len(self.events("model.fallback")), 2)  # no model left to move to after the last
        self.assertEqual(set(self.chain.busy()), {ULTRA, GLM, LAGUNA})
        self.assertEqual(self.chain.candidates(), [ULTRA, GLM, LAGUNA])  # all cooling: soonest free first
        self.calls.clear()
        self.clock.advance(1)
        self.assertEqual(self.run_chain({}), f"answer from {ULTRA}")  # a cooling model that answers is healthy again
        self.assertNotIn(ULTRA, self.chain.busy())

    def test_cooldown_is_configurable(self):
        self.run_chain({ULTRA: NIMError("x", status=503)}, busy_cooldown_s=5)
        self.assertEqual(self.events("model.fallback")[0]["cooldown_s"], 5.0)
        self.clock.advance(5)
        self.assertEqual(self.chain.active, ULTRA)


class ExplainTests(unittest.TestCase):
    def test_overload_wording_only_claims_what_happened(self):
        body = '{"error":{"message":"Service temporarily overloaded","code":503}}'
        self.assertEqual(explain_http_failure(503, body, ULTRA, fallback=True, attempts=4), OVERLOAD_WORDING)
        self.assertNotIn("retried", explain_http_failure(503, body, ULTRA, fallback=True, attempts=1))
        self.assertNotIn("retried", explain_http_failure(503, body, ULTRA, fallback=True))
        last = explain_http_failure(503, body, ULTRA, fallback=False, attempts=4)
        self.assertIn("no fallback model is left to try", last)
        unknown = explain_http_failure(503, body, ULTRA, attempts=4)
        self.assertIn("temporarily overloaded (common on the free tier): Kyora retried", unknown)
        self.assertIn("with fallback models configured", unknown)
        self.assertIn("temporarily overloaded", explain_http_failure(None, body, ULTRA))
        self.assertIn("HTTP 502", explain_http_failure(502, "bad gateway", ULTRA, fallback=True))
        self.assertEqual(explain_http_failure(501, "", ULTRA), "the provider had a server error: retry later")
        self.assertEqual(explain_http_failure(None, "", ULTRA), "")
        self.assertIn("rate limited", explain_http_failure(429, "overloaded", ULTRA))


class RoleRecoveryTests(unittest.TestCase):
    """Kimi and Nemotron recover from an overloaded model through their chains, visibly."""

    def setUp(self):
        self.bus = EventBus(None)
        self.addCleanup(self.bus.close)
        self.overloaded = {ULTRA}
        self.server = MockNIM(self.handle).start()
        self.addCleanup(self.server.stop)
        self.client = NIMClient(self.server.url, "nvapi-good-key", key_label="kimi key", backoff_s=(0.01,),
                                max_attempts=2)

    def handle(self, req: dict) -> dict:
        if req.get("model") in self.overloaded:
            return {"stream_error": OVERLOADED} if req.get("stream") else {"status": 503, "error_body": OVERLOADED}
        return {"content": f"ok from {req.get('model')}", "usage": {"prompt_tokens": 3, "completion_tokens": 3}}

    def events(self, type_: str) -> list[dict]:
        return [e.data for e in self.bus.replay(0) if e.type == type_]

    def models_sent(self) -> list[str]:
        return [r.get("model") for r in self.server.requests]

    def test_kimi_moves_to_its_fallback_and_back_after_the_cooldown(self):
        clock = Clock()
        k = Kimi(self.client, ULTRA, KimiCfg(reasoning_effort="max", judge_reasoning_effort="max",
                                             effort_field="top_level"), self.bus, fallback_models=[GLM])
        k.chain = ModelChain(ULTRA, [GLM], clock=clock)
        conv = KimiConversation("sys")
        res = k.think(conv, purpose="plan", publish_deltas=False)
        self.assertEqual(res.content, f"ok from {GLM}")
        self.assertEqual(self.models_sent(), [ULTRA, ULTRA, GLM])  # the client's retry, then the fallback
        self.assertEqual(len([m for m in conv.messages() if m.get("role") == "assistant"]), 1)
        fb = self.events("model.fallback")
        self.assertEqual(len(fb), 1)
        self.assertEqual((fb[0]["role"], fb[0]["from_model"], fb[0]["to_model"], fb[0]["status"], fb[0]["temporary"]),
                         ("kimi", ULTRA, GLM, 503, True))
        self.assertEqual(fb[0]["reason"], OVERLOAD_WORDING)
        err = self.events("model.error")[-1]
        self.assertEqual((err["model"], err["status"]), (ULTRA, 503))
        self.assertIn("temporarily overloaded", err["hint"])
        metrics = self.events("model.metrics")[-1]
        self.assertEqual((metrics["model"], metrics["requested_model"], metrics["fallback"]), (GLM, ULTRA, True))
        self.assertEqual(k.model, GLM)
        self.assertNotIn("reasoning_effort", self.server.requests[-1])  # Kimi's field is not sent to GLM

        k.think(KimiConversation("sys"), purpose="review", publish_deltas=False)
        self.assertEqual(self.models_sent()[3:], [GLM])  # no 503 round trip while the model cools down
        self.overloaded.clear()
        clock.advance(BUSY_COOLDOWN_S)
        self.assertEqual(k.model, ULTRA)
        res = k.think(KimiConversation("sys"), purpose="review", publish_deltas=False)
        self.assertEqual(res.content, f"ok from {ULTRA}")
        self.assertEqual(self.models_sent()[4:], [ULTRA])

    def test_nemotron_recovers_from_an_http_503(self):
        n = Nemotron(self.client, ULTRA, self.bus, NemotronCfg(thinking_for_design=False), fallback_models=[SUPER])
        res = n.generate(system="s", messages=[{"role": "user", "content": "x"}], thinking=False, purpose="design",
                         publish_deltas=False)
        self.assertEqual(res.content, f"ok from {SUPER}")
        self.assertEqual(self.models_sent(), [ULTRA, ULTRA, SUPER])
        fb = self.events("model.fallback")
        self.assertEqual((fb[0]["role"], fb[0]["to_model"], fb[0]["temporary"]), ("nemotron", SUPER, True))
        self.assertIn(ULTRA, n.chain.to_dict()["busy"])
        self.assertEqual(n.model, SUPER)
        metrics = self.events("model.metrics")[-1]
        self.assertEqual((metrics["model"], metrics["requested_model"]), (SUPER, ULTRA))

    def test_without_fallbacks_the_overload_is_raised_with_its_explanation(self):
        n = Nemotron(self.client, ULTRA, self.bus, NemotronCfg(thinking_for_design=False))
        with self.assertRaises(NIMError) as cm:
            n.generate(system="s", messages=[{"role": "user", "content": "x"}], thinking=False, purpose="design",
                       publish_deltas=False)
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(self.models_sent(), [ULTRA, ULTRA])
        self.assertEqual(self.events("model.fallback"), [])
        hint = self.events("model.error")[-1]["hint"]
        self.assertIn("temporarily overloaded (common on the free tier): Kyora retried", hint)


if __name__ == "__main__":
    unittest.main()
