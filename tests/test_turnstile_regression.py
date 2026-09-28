"""Regression coverage for Turnstile waiting and interactive completion (issue #79)."""

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import registration_browser

_REAL_RECT = {"x": 10.0, "y": 20.0, "w": 300.0, "h": 65.0}


class Cancelled(Exception):
    pass


class _FakeCdpPage:
    """Minimal DrissionPage stub answering the CDP calls the probe chain uses."""

    def __init__(self, document=None, box=None, frames=None, js_payload=None):
        self.document = document if document is not None else {"root": {"nodeName": "HTML"}}
        self.box = box
        self.frames = frames if frames is not None else {"frameTree": {"frame": {"id": "main", "url": "about:blank"}}}
        self.js_payload = js_payload if js_payload is not None else json.dumps({"rect": None, "candidates": []})

    def run_js(self, *_args):
        return self.js_payload

    def run_cdp(self, command, **kwargs):
        if command == "Page.getFrameTree":
            return self.frames
        if command == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 800}}
        if command == "DOM.getDocument":
            return self.document
        if command == "DOM.getBoxModel":
            if self.box is None:
                raise RuntimeError("no box")
            return self.box
        if command == "DOM.getFrameOwner":
            return {}
        raise AssertionError("unexpected cdp command: %s" % command)


def _state(status, token=""):
    return {
        "state": status,
        "present": status != registration_browser.TURNSTILE_ABSENT,
        "token": token,
        "token_length": len(token),
        "widget_present": status in {
            registration_browser.TURNSTILE_WAITING,
            registration_browser.TURNSTILE_SOLVED,
            registration_browser.TURNSTILE_FAILED,
        },
        "iframe_present": status == registration_browser.TURNSTILE_WAITING,
        "script_present": status != registration_browser.TURNSTILE_ABSENT,
        "visible": status == registration_browser.TURNSTILE_WAITING,
    }


class TurnstileRegressionTests(unittest.TestCase):
    def test_existing_short_token_is_accepted_immediately(self):
        with patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_SOLVED, "ok"),
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(registration_browser, "page", object()):
            token = registration_browser.getTurnstileToken(timeout=5)

        self.assertEqual(token, "ok")

    def test_loading_can_finish_automatically(self):
        clock = {"now": 0.0}
        states = [
            _state(registration_browser.TURNSTILE_LOADING),
            _state(registration_browser.TURNSTILE_LOADING),
            _state(registration_browser.TURNSTILE_SOLVED, "ready"),
        ]

        def now():
            return clock["now"]

        def sleep(_seconds, _cancel=None):
            clock["now"] += 1.0

        with patch.object(
            registration_browser, "_read_turnstile_state", side_effect=states
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(
            registration_browser, "sleep_with_cancel", side_effect=sleep, create=True
        ), patch.object(
            registration_browser.time, "time", side_effect=now
        ), patch.object(
            registration_browser, "page", object()
        ):
            token = registration_browser.getTurnstileToken(timeout=10)

        self.assertEqual(token, "ready")

    def test_interactive_wait_can_finish_after_user_completion(self):
        clock = {"now": 0.0}
        logs = []
        states = [
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_SOLVED, "ready"),
        ]

        def now():
            return clock["now"]

        def sleep(_seconds, _cancel=None):
            clock["now"] += 2.0

        with patch.object(
            registration_browser, "_read_turnstile_state", side_effect=states
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(
            registration_browser, "sleep_with_cancel", side_effect=sleep, create=True
        ), patch.object(
            registration_browser.time, "time", side_effect=now
        ), patch.object(
            registration_browser, "page", object()
        ):
            token = registration_browser.getTurnstileToken(
                timeout=20,
                log_callback=logs.append,
            )

        self.assertEqual(token, "ready")
        self.assertTrue(any("请在当前浏览器窗口完成验证" in item for item in logs))

    def test_absent_challenge_returns_to_page_re_evaluation(self):
        with patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_ABSENT),
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(registration_browser, "page", object()):
            token = registration_browser.getTurnstileToken(timeout=5)

        self.assertEqual(token, "")

    def test_challenge_disappearing_while_waiting_returns_for_re_evaluation(self):
        clock = {"now": 0.0}
        states = [
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_ABSENT),
        ]

        def now():
            return clock["now"]

        def sleep(_seconds, _cancel=None):
            clock["now"] += 1.0

        with patch.object(
            registration_browser, "_read_turnstile_state", side_effect=states
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(
            registration_browser, "sleep_with_cancel", side_effect=sleep, create=True
        ), patch.object(
            registration_browser.time, "time", side_effect=now
        ), patch.object(
            registration_browser, "page", object()
        ):
            token = registration_browser.getTurnstileToken(timeout=5)

        self.assertEqual(token, "")

    def test_explicit_failed_state_stops_immediately(self):
        with patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_FAILED),
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(registration_browser, "page", object()):
            with self.assertRaisesRegex(Exception, "Cloudflare 人机验证失败"):
                registration_browser.getTurnstileToken(timeout=5)

    def test_interactive_wait_times_out_deterministically(self):
        clock = {"now": 0.0}

        def now():
            return clock["now"]

        def sleep(seconds, _cancel=None):
            clock["now"] += max(float(seconds), 1.0)

        with patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_WAITING),
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(
            registration_browser, "sleep_with_cancel", side_effect=sleep, create=True
        ), patch.object(
            registration_browser.time, "time", side_effect=now
        ), patch.object(
            registration_browser, "page", object()
        ):
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=2)

    def test_turnstile_wait_honors_cancel(self):
        with patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_WAITING),
        ), patch.object(
            registration_browser, "raise_if_cancelled", side_effect=Cancelled(), create=True
        ), patch.object(registration_browser, "page", object()):
            with self.assertRaises(Cancelled):
                registration_browser.getTurnstileToken(timeout=5)

    def test_state_reader_distinguishes_interactive_widget(self):
        class FakePage:
            def run_js(self, *_args):
                return json.dumps(
                    {
                        "state": "WAITING",
                        "token": "",
                        "widget_present": True,
                        "iframe_present": True,
                        "script_present": True,
                        "visible": True,
                    }
                )

        with patch.object(registration_browser, "page", FakePage()):
            state = registration_browser._read_turnstile_state()

        self.assertEqual(state["state"], registration_browser.TURNSTILE_WAITING)
        self.assertTrue(state["widget_present"])
        self.assertTrue(state["iframe_present"])
        self.assertTrue(state["visible"])
        self.assertEqual(state["token_length"], 0)

    def test_final_sso_page_uses_same_turnstile_waiter(self):
        class FakePage:
            def __init__(self):
                self.states = iter(["final-page", "not-final-page"])

            def run_js(self, *_args):
                return next(self.states, "not-final-page")

            def cookies(self, **_kwargs):
                return [{"name": "sso", "value": "sso-token"}]

        waiter = Mock(return_value="ready")
        with patch.object(registration_browser, "page", FakePage()), patch.object(
            registration_browser, "refresh_active_page", return_value=None
        ), patch.object(
            registration_browser, "raise_if_cancelled", return_value=None, create=True
        ), patch.object(
            registration_browser,
            "_read_turnstile_state",
            return_value=_state(registration_browser.TURNSTILE_WAITING),
        ), patch.object(
            registration_browser, "getTurnstileToken", waiter
        ):
            token = registration_browser.wait_for_sso_cookie(timeout=2)

        self.assertEqual(token, "sso-token")
        waiter.assert_called_once()

    def test_script_only_is_not_treated_as_an_active_challenge(self):
        source = Path(registration_browser.__file__).read_text(encoding="utf-8")
        reader = source[
            source.index("def _read_turnstile_state("):
            source.index("def _wait_for_turnstile(")
        ]
        self.assertIn("else if (widget) state = 'LOADING';", reader)
        self.assertNotIn("else if (scriptPresent) state = 'LOADING';", reader)

    def test_registration_path_contains_no_legacy_turnstile_interference(self):
        source = Path(registration_browser.__file__).read_text(encoding="utf-8")
        profile = source[
            source.index("def fill_profile_and_submit("):
            source.index("def wait_for_sso_cookie(")
        ]
        sso = source[source.index("def wait_for_sso_cookie("):]

        self.assertNotIn("turnstile.reset", source)
        self.assertNotIn("MouseEvent.prototype", source)
        self.assertNotIn("二次复用 Turnstile", source)
        self.assertNotIn("token.length >= 80", source)
        self.assertNotIn("nativeSetter.call(cfInput", source)

        self.assertIn("_read_turnstile_state()", profile)
        self.assertIn("getTurnstileToken(", profile)
        self.assertIn("_read_turnstile_state()", sso)
        self.assertIn("getTurnstileToken(", sso)

        self.assertNotIn("cf-turnstile-response", profile)
        self.assertNotIn("cf-turnstile-response", sso)
        self.assertNotIn("wait-cloudflare", profile)
        self.assertNotIn("final-page-wait-cf", sso)
        self.assertNotIn('script[src*="turnstile"]', profile)
        self.assertNotIn('script[src*="turnstile"]', sso)

    def _patch_wait(self, stack, states, clock, click, config_value=True, rect=_REAL_RECT):
        def now():
            return clock["now"]

        def sleep(seconds, _cancel=None):
            clock["now"] += max(float(seconds), 1.0)

        stack.enter_context(patch.object(registration_browser, "_read_turnstile_state", side_effect=states))
        stack.enter_context(
            patch.object(
                registration_browser,
                "_turnstile_widget_probe",
                return_value=(_REAL_RECT if rect is _REAL_RECT else rect, []),
            )
        )
        stack.enter_context(patch.object(registration_browser, "_click_turnstile_widget", side_effect=click))
        stack.enter_context(
            patch.object(
                registration_browser,
                "config",
                {"turnstile_autoclick_enabled": config_value},
                create=True,
            )
        )
        stack.enter_context(patch.object(registration_browser, "raise_if_cancelled", return_value=None, create=True))
        stack.enter_context(patch.object(registration_browser, "sleep_with_cancel", side_effect=sleep, create=True))
        stack.enter_context(patch.object(registration_browser.time, "time", side_effect=now))
        stack.enter_context(patch.object(registration_browser, "page", object()))

    def test_autoclick_clicks_visible_widget_once_then_picks_up_token(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        states = [
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_WAITING),
            _state(registration_browser.TURNSTILE_SOLVED, "ready"),
        ]
        with ExitStack() as stack:
            self._patch_wait(stack, states, clock, click)
            token = registration_browser.getTurnstileToken(timeout=20)

        self.assertEqual(token, "ready")
        self.assertEqual(click.call_count, 1)

    def test_autoclick_skipped_before_grace_period(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        with ExitStack() as stack:
            self._patch_wait(
                stack, lambda: _state(registration_browser.TURNSTILE_WAITING), clock, click
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=2)

        click.assert_not_called()

    def test_autoclick_skipped_while_widget_is_loading(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        with ExitStack() as stack:
            self._patch_wait(
                stack, lambda: _state(registration_browser.TURNSTILE_LOADING), clock, click
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=10)

        click.assert_not_called()

    def test_autoclick_disabled_config_never_clicks(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        with ExitStack() as stack:
            self._patch_wait(
                stack,
                lambda: _state(registration_browser.TURNSTILE_WAITING),
                clock,
                click,
                config_value=False,
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=20)

        click.assert_not_called()

    def test_autoclick_attempts_are_capped_per_wait(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        with ExitStack() as stack:
            self._patch_wait(
                stack, lambda: _state(registration_browser.TURNSTILE_WAITING), clock, click
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=40)

        self.assertEqual(
            click.call_count, registration_browser.TURNSTILE_AUTOCLICK_MAX_ATTEMPTS
        )

    def test_autoclick_skips_when_no_clickable_widget_rect(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        with ExitStack() as stack:
            self._patch_wait(
                stack,
                lambda: _state(registration_browser.TURNSTILE_WAITING),
                clock,
                click,
                rect=None,
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=20)

        click.assert_not_called()

    def test_widget_rect_probe_rejects_missing_and_tiny_widgets(self):
        class FakePage:
            def __init__(self, payload):
                self.payload = payload

            def run_js(self, *_args):
                return self.payload

        with patch.object(registration_browser, "page", FakePage("")):
            self.assertIsNone(registration_browser._turnstile_widget_rect())
        with patch.object(
            registration_browser,
            "page",
            FakePage(json.dumps({"rect": None, "candidates": []})),
        ):
            self.assertIsNone(registration_browser._turnstile_widget_rect())
        with patch.object(
            registration_browser,
            "page",
            FakePage(json.dumps({"rect": {"x": 0, "y": 0, "w": 10, "h": 10}, "candidates": []})),
        ):
            self.assertIsNone(registration_browser._turnstile_widget_rect())
        with patch.object(
            registration_browser,
            "page",
            FakePage(json.dumps({"rect": {"x": 5, "y": 6, "w": 300, "h": 65}, "candidates": []})),
        ):
            self.assertEqual(
                registration_browser._turnstile_widget_rect(),
                {"x": 5.0, "y": 6.0, "w": 300.0, "h": 65.0},
            )

    def test_probe_returns_diagnostics_for_hidden_widget(self):
        payload = {
            "rect": None,
            "candidates": [
                {
                    "tag": "iframe",
                    "src": "https://challenges.cloudflare.com/turnstile/v0/api.js",
                    "w": 0,
                    "h": 0,
                    "visible": False,
                }
            ],
        }

        class FakePage:
            def run_js(self, *_args):
                return json.dumps(payload)

        with patch.object(registration_browser, "page", FakePage()):
            rect, diagnostics = registration_browser._turnstile_widget_probe()

        self.assertIsNone(rect)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("0x0(hidden)", registration_browser._format_turnstile_candidates(diagnostics))

    def test_autoclick_logs_diagnostics_once_when_nothing_is_clickable(self):
        clock = {"now": 0.0}
        click = Mock(return_value=True)
        logs = []
        diagnostics = [
            {"tag": "iframe", "src": "https://challenges.cloudflare.com/x", "w": 0, "h": 0, "visible": False}
        ]
        with ExitStack() as stack:
            self._patch_wait(
                stack, lambda: _state(registration_browser.TURNSTILE_WAITING), clock, click
            )
            stack.enter_context(
                patch.object(
                    registration_browser,
                    "_turnstile_widget_probe",
                    return_value=(None, diagnostics),
                )
            )
            with self.assertRaisesRegex(Exception, "Turnstile 验证超时"):
                registration_browser.getTurnstileToken(timeout=20, log_callback=logs.append)

        click.assert_not_called()
        reported = [item for item in logs if "未找到可点击的 Cloudflare 组件" in item]
        self.assertEqual(len(reported), 1)
        self.assertIn("0x0(hidden)", reported[0])

    def test_turnstile_state_reader_stays_read_only(self):
        source = Path(registration_browser.__file__).read_text(encoding="utf-8")
        reader = source[
            source.index("def _read_turnstile_state("):
            source.index("def _wait_for_turnstile(")
        ]
        self.assertNotIn("run_cdp", reader)
        self.assertNotIn("dispatchMouseEvent", reader)
        self.assertNotIn("scrollIntoView", reader)

    def test_pierced_probe_finds_widget_inside_closed_shadow_root(self):
        iframe = {
            "nodeName": "IFRAME",
            "backendNodeId": 42,
            "attributes": [
                "src",
                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/av0/rch/abc",
            ],
        }
        page_stub = _FakeCdpPage(
            document={
                "root": {
                    "nodeName": "HTML",
                    "children": [
                        {
                            "nodeName": "DIV",
                            "shadowRoots": [{"shadowRootType": "closed", "children": [iframe]}],
                        }
                    ],
                }
            },
            box={"model": {"border": [40, 40, 340, 40, 340, 105, 40, 105]}},
        )
        with patch.object(registration_browser, "page", page_stub):
            rect, diagnostics = registration_browser._turnstile_pierced_probe()

        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect["w"], 300.0, places=3)
        self.assertAlmostEqual(rect["h"], 65.0, places=3)
        self.assertTrue(any("pierced iframe" in item for item in diagnostics))

    def test_pierced_probe_ignores_unrelated_and_tiny_iframes(self):
        unrelated = {
            "nodeName": "IFRAME",
            "backendNodeId": 7,
            "attributes": ["src", "https://static.cloudflareinsights.com/beacon.min.js"],
        }
        tiny = {
            "nodeName": "IFRAME",
            "backendNodeId": 8,
            "attributes": ["src", "https://challenges.cloudflare.com/turnstile/v0/api.js"],
        }
        with patch.object(
            registration_browser,
            "page",
            _FakeCdpPage(document={"root": {"nodeName": "HTML", "children": [unrelated]}}, box=None),
        ):
            self.assertEqual(registration_browser._turnstile_pierced_probe(), (None, []))

        with patch.object(
            registration_browser,
            "page",
            _FakeCdpPage(
                document={"root": {"nodeName": "HTML", "children": [tiny]}},
                box={"model": {"border": [10, 10, 20, 10, 20, 20, 10, 20]}},
            ),
        ):
            rect, diagnostics = registration_browser._turnstile_pierced_probe()

        self.assertIsNone(rect)
        self.assertTrue(any("@10x10" in item for item in diagnostics))

    def test_widget_probe_falls_back_to_pierced_dom(self):
        page_stub = _FakeCdpPage(
            document={
                "root": {
                    "nodeName": "HTML",
                    "children": [
                        {
                            "nodeName": "DIV",
                            "shadowRoots": [
                                {
                                    "shadowRootType": "closed",
                                    "children": [
                                        {
                                            "nodeName": "IFRAME",
                                            "backendNodeId": 5,
                                            "attributes": [
                                                "src",
                                                "https://challenges.cloudflare.com/turnstile/v0/x",
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            },
            box={"model": {"border": [40, 40, 340, 40, 340, 105, 40, 105]}},
        )
        with patch.object(registration_browser, "page", page_stub):
            rect, diagnostics = registration_browser._turnstile_widget_probe()

        self.assertIsNotNone(rect)
        self.assertTrue(any("pierced iframe" in item for item in diagnostics))


if __name__ == "__main__":
    unittest.main()
