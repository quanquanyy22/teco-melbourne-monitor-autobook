#!/usr/bin/env python3
"""Regression tests built from the form schema saved from the live YCBM page.

These tests deliberately reproduce the three-select structure that exposed the
2026-08-03 bug: Q11 and Q14 are bot questions while Q12 is an ordinary 是/否
question in the same form. They validate DOM logic only; they do not claim to
exercise a real open slot or a real server submission.
"""

import inspect
import time
import unittest
from unittest import mock

from playwright.sync_api import sync_playwright

import teco_autobook as autobook


TEST_ANSWERS = {
    "FNAME": "測試姓名",
    "LNAME": "TEST PERSON",
    "EMAIL": "test@example.invalid",
    "Q3": "0400000000",
    "Q10": "TEST-VISA-001",
    "Q9": "14/11/2026",
    "Q12": "否",
}


LIVE_SCHEMA_FORM = """<!doctype html><meta charset="utf-8">
<div data-testid="formContent"><form id="booking-form">
  <p>申請前須知：請先確認資料。此表單包含為了證明您不是機器人的問題。</p>

  <div data-testid="FNAME_group">
  <label for="fname">申請人護照中文姓名 (繁體中文) (Required)</label>
  <input id="fname" data-testid="FNAME" value="測試姓名">
  </div>

  <div data-testid="LNAME_group">
  <label for="lname">申請人護照英文全名 (護照英文姓名) (Required)</label>
  <input id="lname" data-testid="LNAME" value="TEST PERSON">
  </div>

  <label for="q11">為了證明您不是機器人，請回答：115 減 58 等於多少？ (Required)</label>
  <select id="q11"><option>Please choose</option><option>52</option><option>57</option><option>58</option></select>

  <div data-testid="EMAIL_group">
  <label for="email">有效 Email (Required)</label>
  <input id="email" data-testid="EMAIL" type="email" value="test@example.invalid">
  </div>

  <div data-testid="Q3_group">
  <label for="phone">澳洲手機號 Phone number (Required)</label>
  <input id="phone" data-testid="Q3" type="tel" value="0400000000">
  </div>

  <div data-testid="Q10_group">
  <label for="visa">澳洲簽證號碼 Visa Grant No. (Required)</label>
  <input id="visa" data-testid="Q10" value="TEST-VISA-001">
  </div>

  <label for="q14">為了證明您不是機器人，請回答：哪一個是澳洲城市? (Required)</label>
  <select id="q14"><option>Please choose</option><option>里斯本</option><option>達爾文</option><option>波士頓</option></select>

  <div data-testid="Q9">
  <label for="date">預計入台旅遊日期 (Required)</label>
  <input id="date" value="14/11/2026">
  </div>

  <label for="q12">是否有同行親屬申請人員? (僅限: 父母、子女、婚姻關係配偶並有關係證明) (Required)</label>
  <select id="q12"><option>Please choose</option><option>是</option><option selected>否</option></select>

  <label for="q8">聲明 (Required)</label>
  <input id="q8" type="checkbox" checked>

  <button type="submit" data-testid="confirm_button">Confirm Booking</button>
</form></div>
"""


class LiveSchemaDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(LIVE_SCHEMA_FORM)
        self.log_patch = mock.patch.object(autobook, "log")
        self.log_mock = self.log_patch.start()
        self.answers_patch = mock.patch.dict(autobook.ANSWERS, TEST_ANSWERS, clear=True)
        self.answers_patch.start()

    def tearDown(self):
        self.answers_patch.stop()
        self.log_patch.stop()
        self.page.close()

    def test_bot_discovery_answers_only_q11_and_q14(self):
        results = autobook.answer_bot_questions(self.page)

        self.assertEqual(2, len(results))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual("57", self.page.locator("#q11").input_value())
        self.assertEqual("達爾文", self.page.locator("#q14").input_value())
        # This is the exact regression: Q12 must not be treated as arithmetic.
        self.assertEqual("否", self.page.locator("#q12").input_value())

    def test_api_slot_maps_to_current_live_bundle_testids(self):
        day, slot = autobook.slot_dom_targets({"startsAt": "0"})

        self.assertEqual("1970-01-01", day)
        self.assertEqual("1970-01-01T00:00:00.000Z", slot)
        self.assertIsNone(autobook.LATEST_ACCEPTABLE_APPOINTMENT_DATE)
        self.assertTrue(autobook.slot_is_eligible_for_live_submit({"startsAt": "0"}))
        self.assertTrue(autobook.slot_is_eligible_for_live_submit(
            {"startsAt": "2026-10-19T00:00:00.000Z"}
        ))

    def test_fast_poll_staggers_near_term_without_missing_plus_60(self):
        fast_time = __import__("datetime").datetime(
            2026, 8, 7, 8, 50, tzinfo=autobook.MELBOURNE_TZ)

        cycles = [
            autobook.polling_profile(fast_time, poll_count=i)[0]
            for i in range(8)
        ]

        self.assertEqual([0, 60], cycles[0])
        self.assertEqual([0, 60], cycles[4])
        self.assertTrue(all(60 in anchors for anchors in cycles))
        self.assertTrue(all(anchors == [60] for i, anchors in enumerate(cycles) if i % 4))
        self.assertEqual(0.5, autobook.polling_profile(fast_time, 1)[1])

    def test_exact_detected_slot_is_clicked_by_live_bundle_testid(self):
        self.page.set_content(
            '<button aria-label="Booking duration: 20 minutes">20 minutes</button>'
            '<button class="avl_dayButton" data-testid="day_1970-01-01">1</button>'
            '<div id="times"></div>'
        )
        self.page.evaluate(
            r"""() => {
                window.clicked = null;
                document.querySelector('[data-testid="day_1970-01-01"]').onclick = () => {
                    document.querySelector('#times').innerHTML =
                      '<button data-testid="slot_1970-01-01T00:00:00.000Z">10:00 AM</button>' +
                      '<button data-testid="slot_1970-01-01T01:00:00.000Z">11:00 AM</button>';
                    for (const button of document.querySelectorAll('#times button')) {
                        button.onclick = () => { window.clicked = button.dataset.testid; };
                    }
                };
            }"""
        )

        ok = autobook.select_slot_in_ui(
            self.page, 0, slot={"startsAt": "0"}
        )

        self.assertTrue(ok)
        self.assertEqual(
            "slot_1970-01-01T00:00:00.000Z",
            self.page.evaluate("window.clicked"),
        )

    def test_batch_text_fill_uses_live_question_testids(self):
        self.page.evaluate(
            """() => {
                for (const id of ['fname', 'lname', 'email', 'phone', 'visa', 'date']) {
                    document.getElementById(id).value = '';
                }
            }"""
        )

        status = autobook.fill_required_text_fields_batch(self.page)

        self.assertTrue(all(status.values()), status)
        self.assertEqual(TEST_ANSWERS["EMAIL"], self.page.locator("#email").input_value())
        self.assertEqual(TEST_ANSWERS["Q10"], self.page.locator("#visa").input_value())

    def test_batch_phone_accepts_site_display_formatting(self):
        self.page.evaluate(
            r"""() => {
                const phone = document.querySelector('[data-testid="Q3"]');
                const date = document.querySelector('[data-testid="Q9"] input');
                phone.value = '';
                phone.addEventListener('input', () => {
                    const digits = phone.value.replace(/\D/g, '');
                    phone.value = digits.replace(/(\d{4})(\d{3})(\d{3})/, '$1 $2 $3');
                });
                date.addEventListener('input', () => {
                    const match = date.value.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
                    if (match) date.value = `${match[3]}-${match[2]}-${match[1]}`;
                });
            }"""
        )

        status = autobook.fill_required_text_fields_batch(self.page)

        self.assertTrue(status["Q3"], status)
        self.assertEqual("0400 000 000", self.page.locator("#phone").input_value())
        self.assertTrue(status["Q9"], status)
        self.assertEqual("2026-11-14", self.page.locator("#date").input_value())

    def test_direct_intent_patch_uses_live_bundle_contract(self):
        self.page.evaluate(
            """() => {
                window.uiStore = {currentIntentId: 'itt_abc123'};
                window.patchCapture = null;
                window.fetch = async (url, options) => {
                    window.patchCapture = {url, options};
                    return {ok: true, status: 200, text: async () => '{}'};
                };
            }"""
        )

        accepted, intent_id, reason = autobook.patch_intent_slot_selection(
            self.page, {"startsAt": "0"}
        )

        captured = self.page.evaluate("window.patchCapture")
        self.assertTrue(accepted)
        self.assertEqual("itt_abc123", intent_id)
        self.assertIsNone(reason)
        self.assertEqual(
            "https://api.youcanbook.me/v1/intents/itt_abc123/selections",
            captured["url"],
        )
        self.assertEqual("PATCH", captured["options"]["method"])
        self.assertEqual({"startsAt": 0}, __import__("json").loads(captured["options"]["body"]))

    def test_fast_selection_prefers_direct_route_without_ui_click(self):
        with (
            mock.patch.object(
                autobook, "patch_intent_slot_selection",
                return_value=(True, "itt_abc123", None),
            ) as patch_mock,
            mock.patch.object(
                autobook, "navigate_to_intent_form", return_value=True,
            ) as route_mock,
            mock.patch.object(autobook, "select_slot_in_ui") as ui_mock,
        ):
            ok = autobook.select_detected_slot_fast(
                self.page, 60, {"startsAt": "0"}, attempt_started=123.0)

        self.assertTrue(ok)
        patch_mock.assert_called_once()
        route_mock.assert_called_once_with(
            self.page, "itt_abc123", attempt_started=123.0)
        ui_mock.assert_not_called()

    def test_fast_selection_falls_back_to_exact_ui(self):
        with (
            mock.patch.object(
                autobook, "patch_intent_slot_selection",
                return_value=(False, "itt_abc123", "HTTP 409"),
            ),
            mock.patch.object(autobook, "navigate_to_intent_form") as route_mock,
            mock.patch.object(
                autobook, "select_slot_in_ui", return_value=True,
            ) as ui_mock,
        ):
            ok = autobook.select_detected_slot_fast(
                self.page, 60, {"startsAt": "0"}, attempt_started=123.0)

        self.assertTrue(ok)
        route_mock.assert_not_called()
        ui_mock.assert_called_once_with(
            self.page, 60, slot={"startsAt": "0"}, attempt_started=123.0)

    def test_batch_fills_q9_when_live_date_control_mounts_late(self):
        self.page.locator('[data-testid="Q9"]').evaluate(
            """root => {
                root.querySelector('input').remove();
                setTimeout(() => {
                    root.insertAdjacentHTML('beforeend', '<input id="late-date">');
                }, 75);
            }"""
        )

        status = autobook.fill_required_text_fields_batch(self.page)

        self.assertTrue(status["Q9"], status)
        self.assertEqual(TEST_ANSWERS["Q9"], self.page.locator("#late-date").input_value())

    def test_waits_for_saved_loading_form_state_to_finish(self):
        self.page.set_content(
            '<div data-testid="loadingForm">Loading...</div>'
            '<textarea id="g-recaptcha-response-1" name="g-recaptcha-response" hidden></textarea>'
            '<div id="mount"></div>'
        )
        self.page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('[data-testid="loadingForm"]').remove();
                document.querySelector('#mount').innerHTML =
                    '<div data-testid="formContent"><input><input><input><select>' +
                    '<option>ready</option></select><input type="checkbox"></div>';
            }, 100)"""
        )
        with mock.patch.object(autobook, "dump_form_html"):
            self.assertTrue(autobook.wait_for_booking_form(self.page, timeout_ms=1_000))

    def test_bot_discovery_fails_closed_unless_exactly_two(self):
        self.page.locator("label[for=q14]").evaluate("el => el.remove()")
        results = autobook.answer_bot_questions(self.page)

        self.assertEqual(1, len(results))
        self.assertFalse(results[0]["ok"])
        self.assertIn("expected exactly 2", results[0]["question"])

    def test_pre_submit_audit_passes_all_ten_live_schema_fields(self):
        bot_results = autobook.answer_bot_questions(self.page)
        passed, status = autobook.pre_submit_audit(self.page, bot_results)

        self.assertTrue(passed)
        self.assertEqual(
            {"FNAME", "LNAME", "EMAIL", "Q3", "Q10", "Q9", "Q12", "Q8", "Q11", "Q14"},
            {key for key, ok in status.items() if ok},
        )

    def test_pre_submit_audit_fails_closed_on_wrong_or_missing_values(self):
        bot_results = autobook.answer_bot_questions(self.page)
        self.page.locator("#email").fill("")
        self.page.locator("#visa").fill("WRONG")
        self.page.locator("#q8").uncheck()

        passed, status = autobook.pre_submit_audit(self.page, bot_results)

        self.assertFalse(passed)
        self.assertFalse(status["EMAIL"])
        self.assertFalse(status["Q10"])
        self.assertFalse(status["Q8"])

    def test_full_local_schema_flow_fills_audits_and_stops_before_submit(self):
        self.page.evaluate(
            """() => {
                for (const id of ['fname', 'lname', 'email', 'phone', 'visa', 'date']) {
                    document.getElementById(id).value = '';
                }
                for (const id of ['q11', 'q14', 'q12']) document.getElementById(id).selectedIndex = 0;
                document.getElementById('q8').checked = false;
                window.finalSubmitClicked = false;
                document.getElementById('booking-form').addEventListener('submit', event => {
                    event.preventDefault();
                    window.finalSubmitClicked = true;
                });
            }"""
        )

        with (
            mock.patch.object(autobook, "dump_form_html"),
            mock.patch.object(autobook, "save_confirmation_screenshot"),
            mock.patch.object(autobook, "ntfy"),
        ):
            started = time.perf_counter()
            outcome = autobook.fill_form_and_submit(self.page)
            elapsed_ms = (time.perf_counter() - started) * 1000

        self.assertEqual(
            "dry_run_ready", outcome,
            msg="\n".join(str(call.args[0]) for call in self.log_mock.call_args_list if call.args),
        )
        self.assertFalse(self.page.evaluate("window.finalSubmitClicked"))
        print(f"LOCAL_FULL_FILL_AUDIT_MS={elapsed_ms:.1f}")
        self.assertLess(elapsed_ms, 1_200, "local fixture fill+audit path regressed")
        passed, _ = autobook.pre_submit_audit(
            self.page,
            [
                {"question": "115 減 58", "answer": "57", "ok": True},
                {"question": "哪一個是澳洲城市", "answer": "達爾文", "ok": True},
            ],
        )
        self.assertTrue(passed)

    def test_production_flow_has_no_positional_force_fill(self):
        self.assertFalse(hasattr(autobook, "force_fill_by_position"))
        self.assertNotIn("force_fill_by_position", inspect.getsource(autobook.fill_form_and_submit))
        self.assertTrue(autobook.DRY_RUN_BEFORE_SUBMIT)

    def test_single_autofill_switch_controls_live_submit(self):
        with mock.patch.object(autobook, "AUTOFILL_ENABLED", False):
            self.assertFalse(autobook.live_submit_armed())
        with mock.patch.object(autobook, "AUTOFILL_ENABLED", True):
            self.assertTrue(autobook.live_submit_armed())

    def test_saved_site_confirmation_text_is_recognised(self):
        self.page.set_content(
            "<main><h1>Booking confirmed</h1>"
            "<p>The booking has been confirmed, you will get the meeting details in an email soon.</p></main>"
        )
        self.assertTrue(autobook.check_page_for_success(self.page))

    def test_saved_chinese_confirmation_text_is_recognised(self):
        self.page.set_content(
            "<main><h1>駐墨爾本辦事處 - 入台旅遊申請預約</h1>"
            "<p>預約確認程序已變更如下</p>"
            "<p>預約確認電郵必須打印並於面交時提交</p></main>"
        )
        self.assertTrue(autobook.check_page_for_success(self.page))

    def test_post_submit_wait_catches_delayed_confirmation(self):
        self.page.set_content("<main>Confirming booking...</main>")
        self.page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('main').innerHTML =
                    '<h1>Booking confirmed</h1><p>The booking has been confirmed.</p>';
            }, 120)"""
        )

        outcome, detail = autobook.wait_for_post_submit_outcome(
            self.page, timeout_ms=1_000, poll_interval_ms=25)

        self.assertEqual("submitted", outcome)
        self.assertIsNone(detail)

    def test_post_submit_wait_distinguishes_positive_rejection(self):
        self.page.set_content("<main>One of your answers seems to be invalid</main>")

        outcome, detail = autobook.wait_for_post_submit_outcome(
            self.page, timeout_ms=500, poll_interval_ms=25)

        self.assertEqual("rejected", outcome)
        self.assertEqual("One of your answers seems to be invalid", detail)

    def test_post_submit_wait_fails_closed_when_status_stays_unclear(self):
        self.page.set_content("<main>Confirming booking...</main>")

        outcome, detail = autobook.wait_for_post_submit_outcome(
            self.page, timeout_ms=100, poll_interval_ms=20)

        self.assertEqual("unclear_after_submit", outcome)
        self.assertIsNone(detail)

    def test_generic_thank_you_is_not_booking_confirmation(self):
        self.page.set_content("<main>Thank you for reading the instructions.</main>")
        self.assertFalse(autobook.check_page_for_success(self.page))

    def test_parallel_race_grants_exactly_one_submit_claim(self):
        event = __import__("threading").Event()
        claims = []
        claims_lock = __import__("threading").Lock()

        def attempt_claim():
            granted = autobook.claim_single_race_submit(event)
            with claims_lock:
                claims.append(granted)

        threads = [__import__("threading").Thread(target=attempt_claim) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, sum(claims), claims)

    def test_armed_full_flow_clicks_once_and_waits_for_confirmation(self):
        self.page.evaluate(
            """() => {
                for (const id of ['q11', 'q14', 'q12']) {
                    document.getElementById(id).selectedIndex = 0;
                }
                document.getElementById('q8').checked = false;
                window.finalSubmitClicks = 0;
                document.getElementById('booking-form').addEventListener('submit', event => {
                    event.preventDefault();
                    window.finalSubmitClicks += 1;
                    const status = document.createElement('main');
                    status.textContent = 'Confirming booking...';
                    document.body.appendChild(status);
                    setTimeout(() => {
                        status.innerHTML = '<h1>Booking confirmed</h1>' +
                            '<p>The booking has been confirmed.</p>';
                    }, 120);
                });
            }"""
        )

        with (
            mock.patch.object(autobook, "live_submit_armed", return_value=True),
            mock.patch.object(autobook, "dump_form_html"),
            mock.patch.object(autobook, "save_confirmation_screenshot"),
            mock.patch.object(autobook, "ntfy"),
            mock.patch.object(autobook, "POST_SUBMIT_OBSERVATION_TIMEOUT_MS", 1_000),
        ):
            outcome = autobook.fill_form_and_submit(self.page)

        self.assertEqual("submitted", outcome)
        self.assertEqual(1, self.page.evaluate("window.finalSubmitClicks"))


if __name__ == "__main__":
    unittest.main()
