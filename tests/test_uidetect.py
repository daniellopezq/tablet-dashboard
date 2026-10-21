"""Tests for uidetect.py (classic vs modern front end). Run: python3 -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uidetect  # noqa: E402
from uidetect import CLASSIC, MODERN, choose_ui, parse_ua, ui_for_user_agent  # noqa: E402

TABLET = ("Mozilla/5.0 (Linux; Android 4.4.2; TAB-10 Build/KOT49H) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/50.0.2661.89 Safari/537.36")
UAS = {
    CLASSIC: [
        TABLET,
        "Mozilla/5.0 (Linux; Android 4.4.2; X Build/KOT49H) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Version/4.0 Chrome/30.0.0.0 Mobile Safari/537.36",                                   # 4.4 WebView
        "Mozilla/5.0 (Linux; U; Android 4.1.2; en-us; X Build/JZO54K) AppleWebKit/534.30 "
        "(KHTML, like Gecko) Version/4.0 Mobile Safari/534.30",                               # stock browser
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/79.0.3945.130 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:60.0) Gecko/20100101 Firefox/60.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/12.1.2 Safari/605.1.15",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 12_5_7 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/12.1.2 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko",               # IE11
    ],
    MODERN: [
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) CriOS/130.0.6723.90 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Safari/605.1.15",
        "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36",
        "",
        "curl/8.5.0",
    ],
}


class UserAgentTests(unittest.TestCase):
    def test_classification(self):
        for expected, uas in UAS.items():
            for ua in uas:
                with self.subTest(ua=ua):
                    self.assertEqual(ui_for_user_agent(ua), expected)

    def test_parse(self):
        self.assertEqual(parse_ua(TABLET), ("chrome", 50))
        self.assertEqual(parse_ua(UAS[MODERN][4]), ("ios", 17))
        self.assertEqual(parse_ua(None), ("unknown", None))

    def test_thresholds(self):
        self.assertEqual(ui_for_user_agent("Mozilla/5.0 Chrome/80.0.0.0 Safari/537.36"), MODERN)
        self.assertEqual(ui_for_user_agent("Mozilla/5.0 Gecko/20100101 Firefox/78.0"), MODERN)
        self.assertEqual(ui_for_user_agent("Mozilla/5.0 Gecko/20100101 Firefox/77.0"), CLASSIC)


class ChooseTests(unittest.TestCase):
    def test_user_agent_only(self):
        self.assertEqual(choose_ui(TABLET, "", ""), (CLASSIC, None))
        self.assertEqual(choose_ui(UAS[MODERN][0], "", None), (MODERN, None))

    def test_query_override_sets_cookie(self):
        ui, cookie = choose_ui(UAS[MODERN][0], "ui=classic", "")
        self.assertEqual(ui, CLASSIC)
        self.assertEqual(cookie, "ui=classic; Max-Age=%d; Path=/; SameSite=Lax; HttpOnly" % uidetect.COOKIE_MAX_AGE)
        ui, cookie = choose_ui(TABLET, "x=1&ui=MODERN", "")
        self.assertEqual(ui, MODERN)
        self.assertTrue(cookie.startswith("ui=modern;"))

    def test_once_does_not_remember(self):
        self.assertEqual(choose_ui(UAS[MODERN][0], "ui=classic&once=1", ""), (CLASSIC, None))
        self.assertEqual(choose_ui(UAS[MODERN][0], "ui=classic&once=1", "ui=modern"), (CLASSIC, None))

    def test_cookie_beats_user_agent(self):
        self.assertEqual(choose_ui(UAS[MODERN][0], "", "a=1; ui=classic"), (CLASSIC, None))
        self.assertEqual(choose_ui(TABLET, "", "ui=modern"), (MODERN, None))

    def test_query_beats_cookie(self):
        self.assertEqual(choose_ui(TABLET, "ui=classic", "ui=modern")[0], CLASSIC)

    def test_auto_clears_cookie(self):
        ui, cookie = choose_ui(TABLET, "ui=auto", "ui=modern")
        self.assertEqual(ui, CLASSIC)
        self.assertIn("Max-Age=0", cookie)

    def test_bad_values_ignored(self):
        self.assertEqual(choose_ui(TABLET, "ui=fancy", "ui=fancy"), (CLASSIC, None))
        self.assertEqual(choose_ui(TABLET, "", 'ui="><script>'), (CLASSIC, None))
        self.assertEqual(choose_ui(UAS[MODERN][0], "ui=%00", "garbage;;=="), (MODERN, None))

    def test_kill_switch(self):
        self.assertEqual(choose_ui(UAS[MODERN][0], "", "ui=modern", modern_enabled=False), (CLASSIC, None))
        self.assertEqual(choose_ui(UAS[MODERN][0], "ui=modern", "", modern_enabled=False)[0], CLASSIC)
        self.assertEqual(choose_ui(UAS[MODERN][0], "ui=auto", "", modern_enabled=False)[0], CLASSIC)


if __name__ == "__main__":
    unittest.main()
