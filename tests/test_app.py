"""Offline tests: a fake Mist API stands in for requests, so nothing here ever
reaches Mist. Run with:  python3 -m unittest discover -s tests"""

import re
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as webapp  # noqa: E402

BASE = "https://api.example.test/api/v1"
SRC, DST1, DST2, HIDDEN = "src-org", "dst-1", "dst-2", "hidden-org"
NOW = time.time()
DAY = 86400


def licenses_payload():
    return {
        "licenses": [
            {"subscription_id": "SUB-ACTIVE", "type": "SUB-MAN", "quantity": 100, "remaining_quantity": 90,
             "start_time": NOW - DAY, "end_time": NOW + 300 * DAY},
            {"subscription_id": "SUB-FUTURE", "type": "SUB-WAN", "quantity": 10, "remaining_quantity": 10,
             "start_time": NOW + 10 * DAY, "end_time": NOW + 400 * DAY},
            {"subscription_id": "SUB-EXPIRED", "type": "SUB-MAN", "quantity": 50, "remaining_quantity": 50,
             "start_time": NOW - 400 * DAY, "end_time": NOW - DAY},
            {"subscription_id": "SUB-EMPTY", "type": "SUB-MAN", "quantity": 5, "remaining_quantity": 0,
             "start_time": NOW - DAY, "end_time": NOW + 300 * DAY},
        ],
        "amendments": [
            {"id": "AM-KNOWN", "subscription_id": "SUB-ACTIVE", "type": "SUB-MAN", "quantity": -6,
             "dst_org_id": DST1, "start_time": NOW - DAY, "end_time": NOW + 300 * DAY},
            {"id": "AM-HIDDEN", "subscription_id": "SUB-ACTIVE", "type": "SUB-MAN", "quantity": -4,
             "dst_org_id": HIDDEN, "start_time": NOW - DAY, "end_time": NOW + 300 * DAY},
        ],
    }


class FakeMist:
    """Records every call; GET /orgs/{id} and /licenses answer from fixtures."""

    def __init__(self, source_status=200):
        self.calls = []
        self.source_status = source_status

    def __call__(self, session, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        resp = requests.Response()
        resp.url = url
        names = {SRC: "Source Org", DST1: "Dest One", DST2: "Dest Two"}
        if method == "GET" and url == f"{BASE}/orgs/{SRC}" and self.source_status != 200:
            resp.status_code, resp._content = self.source_status, b'{"detail":"nope"}'
        elif method == "GET" and url.endswith("/licenses"):
            resp.status_code, resp._content = 200, requests.compat.json.dumps(licenses_payload()).encode()
        elif method == "GET" and url.split("/orgs/")[-1] in names:
            resp.status_code = 200
            resp._content = requests.compat.json.dumps({"name": names[url.split("/orgs/")[-1]]}).encode()
        elif method == "GET":
            resp.status_code, resp._content = 403, b'{"detail":"forbidden"}'
        elif method == "PUT":
            resp.status_code, resp._content = 200, b"{}"
        else:
            resp.status_code, resp._content = 400, b""
        return resp

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


INI = textwrap.dedent(f"""\
    [mist]
    base_url = {BASE}
    api_token = test-token
    {{extra}}
    [source]
    org_id = {SRC}
    [destinations]
    org_ids = {DST1}, {DST2}
    """)


class AppTestCase(unittest.TestCase):
    extra_ini = ""
    dry_run_only = False
    force_dry_run = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        (folder / "A.ini").write_text(INI.format(extra=self.extra_ini))
        (folder / "B.ini").write_text(INI.format(extra=""))
        (folder / "web.conf").write_text(textwrap.dedent(f"""\
            [web]
            port = 5030
            [user alice]
            password_hash = {generate_password_hash('correct horse')}
            configs = A.ini
            dry_run_only = {str(self.dry_run_only).lower()}
            [user bob]
            password_hash = {generate_password_hash('battery staple')}
            configs = *
            """))
        self.web_conf = webapp.load_web_conf(folder / "web.conf", config_folder=folder)
        self.change_log = folder / "changes.log"
        patcher = mock.patch.object(webapp, "CHANGE_LOG", self.change_log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeMist()
        patcher = mock.patch.object(requests.Session, "request", autospec=True, side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.activity_log = folder / "activity.log"
        self.app = webapp.create_app(self.web_conf, force_dry_run=self.force_dry_run, config_folder=folder,
                                     activity_log=self.activity_log)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        webapp.setup_activity_log(Path(self.tmp.name) / "closed.log")  # release the file
        self.tmp.cleanup()

    def activity_lines(self):
        return self.activity_log.read_text().splitlines() if self.activity_log.exists() else []

    def events(self):
        return [dict(f.split('=', 1) for f in l.split('\t')[1:]) for l in self.activity_lines()]

    def login(self, user="alice", password="correct horse"):
        return self.client.post("/login", data={"username": user, "password": password})

    def csrf(self):
        with self.client.session_transaction() as s:
            return s["csrf"]

    def confirm_token(self, html):
        return re.search(r'name="token" value="([^"]+)"', html).group(1)


class TestLoginAndConfigs(AppTestCase):
    def test_redirects_to_login(self):
        self.assertIn("/login", self.client.get("/").headers["Location"])

    def test_wrong_password_and_lockout(self):
        for _ in range(webapp.MAX_FAILED_LOGINS):
            self.assertEqual(self.login(password="wrong").status_code, 401)
        self.assertEqual(self.login().status_code, 429)  # locked even with the right password

    def test_single_config_user_goes_straight_to_table(self):
        self.login()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Source Org", html)
        self.assertIn("A.ini", html)

    def test_multi_config_user_picks_and_cannot_pick_others(self):
        self.login("bob", "battery staple")
        self.assertIn("/configs", self.client.get("/").headers["Location"])
        self.client.post("/configs", data={"csrf": self.csrf(), "config": "B.ini"})
        self.assertIn("Source Org", self.client.get("/").get_data(as_text=True))
        self.client.post("/logout", data={"csrf": self.csrf()})
        self.login()
        r = self.client.post("/configs", data={"csrf": self.csrf(), "config": "B.ini"})
        self.assertEqual(r.status_code, 403)

    def test_post_without_csrf_rejected(self):
        self.login()
        r = self.client.post("/move/confirm", data={"subscription_id": "SUB-ACTIVE", "quantity": "1"})
        self.assertEqual(r.status_code, 400)

    def test_bad_web_conf_fails_fast(self):
        folder = Path(self.tmp.name)
        (folder / "bad.conf").write_text("[user carol]\npassword_hash = x:y\nconfigs = Missing.ini\n")
        with self.assertRaises(webapp.WebConfError):
            webapp.load_web_conf(folder / "bad.conf", config_folder=folder)


class TestListing(AppTestCase):
    def test_table_contents(self):
        self.login()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("SUB-ACTIVE", html)
        self.assertIn("SUB-FUTURE", html)
        self.assertIn("Not Started", html)
        self.assertNotIn("SUB-EXPIRED", html)
        self.assertNotIn("SUB-EMPTY", html)
        self.assertIn('max="90"', html)
        self.assertIn('value="AM-KNOWN"', html)
        self.assertIn(HIDDEN, html)                       # listed for information
        self.assertNotIn('value="AM-HIDDEN"', html)       # but no Return button
        self.assertEqual(self.fake.puts(), [])

    def test_destination_switch(self):
        self.login()
        self.client.post("/destination", data={"csrf": self.csrf(), "dest": DST2})
        self.assertIn("move to Dest Two", self.client.get("/").get_data(as_text=True))
        self.client.post("/destination", data={"csrf": self.csrf(), "dest": "not-configured"})
        self.assertIn("move to Dest One", self.client.get("/").get_data(as_text=True))


class TestSourceAccessErrors(AppTestCase):
    def check(self, status, text):
        self.fake.source_status = status
        self.login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 502)
        self.assertIn(text, r.get_data(as_text=True))

    def test_401(self):
        self.check(401, "rejected as invalid")

    def test_403(self):
        self.check(403, "can&#39;t access the source org")

    def test_404(self):
        self.check(404, "can&#39;t access the source org")


class TestMoveAndReturn(AppTestCase):
    def test_move_needs_confirmation_then_puts_once(self):
        self.login()
        html = self.client.post("/move/confirm", data={
            "csrf": self.csrf(), "subscription_id": "SUB-ACTIVE", "quantity": "5"}).get_data(as_text=True)
        self.assertIn("Confirm move", html)
        self.assertEqual(self.fake.puts(), [])
        token = self.confirm_token(html)
        r = self.client.post("/move", data={"csrf": self.csrf(), "token": token}, follow_redirects=True)
        self.assertIn("Moved 5 units of SUB-ACTIVE", r.get_data(as_text=True))
        self.assertEqual(self.fake.puts(), [("PUT", f"{BASE}/orgs/{SRC}/licenses",
                         {"op": "amend", "subscription_id": "SUB-ACTIVE", "dst_org_id": DST1, "quantity": 5})])
        # replaying the same confirmation does nothing
        r = self.client.post("/move", data={"csrf": self.csrf(), "token": token}, follow_redirects=True)
        self.assertIn("already used", r.get_data(as_text=True))
        self.assertEqual(len(self.fake.puts()), 1)
        self.assertIn("result=OK", self.change_log.read_text())

    def test_quantity_over_avail_rejected(self):
        self.login()
        r = self.client.post("/move/confirm", data={
            "csrf": self.csrf(), "subscription_id": "SUB-ACTIVE", "quantity": "91"}, follow_redirects=True)
        self.assertIn("between 1 and 90", r.get_data(as_text=True))

    def test_return_known(self):
        self.login()
        html = self.client.post("/return/confirm", data={
            "csrf": self.csrf(), "amendment_id": "AM-KNOWN"}).get_data(as_text=True)
        self.client.post("/return", data={"csrf": self.csrf(), "token": self.confirm_token(html)})
        self.assertEqual(self.fake.puts(), [("PUT", f"{BASE}/orgs/{SRC}/licenses",
                                             {"op": "unamend", "amendment_id": "AM-KNOWN"})])

    def test_return_unknown_org_blocked_by_default(self):
        self.login()
        r = self.client.post("/return/confirm", data={"csrf": self.csrf(), "amendment_id": "AM-HIDDEN"},
                             follow_redirects=True)
        self.assertIn("can&#39;t be returned here", r.get_data(as_text=True))
        self.assertEqual(self.fake.puts(), [])

    def test_session_dry_run_toggle_skips_put(self):
        self.login()
        self.client.post("/dry-run", data={"csrf": self.csrf(), "on": "1"})
        html = self.client.post("/move/confirm", data={
            "csrf": self.csrf(), "subscription_id": "SUB-ACTIVE", "quantity": "5"}).get_data(as_text=True)
        r = self.client.post("/move", data={"csrf": self.csrf(), "token": self.confirm_token(html)},
                             follow_redirects=True)
        self.assertIn("[dry-run] Would have moved 5", r.get_data(as_text=True))
        self.assertEqual(self.fake.puts(), [])
        self.assertIn("result=DRY-RUN", self.change_log.read_text())


class TestUnknownOrgAllowed(AppTestCase):
    extra_ini = "allow_unknown_org_returns = true"

    def test_hidden_flag_enables_return(self):
        self.login()
        self.assertIn('value="AM-HIDDEN"', self.client.get("/").get_data(as_text=True))
        html = self.client.post("/return/confirm", data={
            "csrf": self.csrf(), "amendment_id": "AM-HIDDEN"}).get_data(as_text=True)
        self.assertIn("can&#39;t access the destination org", html)
        self.client.post("/return", data={"csrf": self.csrf(), "token": self.confirm_token(html)})
        self.assertEqual(self.fake.puts()[0][2], {"op": "unamend", "amendment_id": "AM-HIDDEN"})


class TestForcedDryRun(AppTestCase):
    force_dry_run = True

    def test_cannot_switch_to_live(self):
        self.login()
        self.client.post("/dry-run", data={"csrf": self.csrf(), "on": "0"})
        html = self.client.post("/return/confirm", data={
            "csrf": self.csrf(), "amendment_id": "AM-KNOWN"}).get_data(as_text=True)
        self.client.post("/return", data={"csrf": self.csrf(), "token": self.confirm_token(html)})
        self.assertEqual(self.fake.puts(), [])


class TestDryRunOnlyUser(TestForcedDryRun):
    force_dry_run = False
    dry_run_only = True


class TestActivityLog(AppTestCase):
    def test_sign_in_failure_and_lockout_logged_with_ip(self):
        self.login(password="wrong")
        self.login(user="mallory", password="x")
        ev = self.events()
        self.assertEqual(ev[0]["event"], "sign-in failed")
        self.assertEqual(ev[0]["user"], "alice")
        self.assertEqual(ev[0]["reason"], "wrong password")
        self.assertEqual(ev[0]["ip"], "127.0.0.1")
        self.assertEqual(ev[1]["reason"], "unknown user name")
        for _ in range(webapp.MAX_FAILED_LOGINS):
            self.login(password="wrong")
        self.assertEqual(self.events()[-1]["event"], "sign-in blocked")
        self.assertNotIn("correct horse", self.activity_log.read_text())

    def test_session_actions_logged(self):
        self.login()
        self.client.get("/")
        self.client.post("/destination", data={"csrf": self.csrf(), "dest": DST2})
        self.client.post("/dry-run", data={"csrf": self.csrf(), "on": "1"})
        html = self.client.post("/move/confirm", data={
            "csrf": self.csrf(), "subscription_id": "SUB-ACTIVE", "quantity": "5"}).get_data(as_text=True)
        self.client.get("/")   # walked away from the confirmation page
        html = self.client.post("/move/confirm", data={
            "csrf": self.csrf(), "subscription_id": "SUB-ACTIVE", "quantity": "5"}).get_data(as_text=True)
        self.client.post("/move", data={"csrf": self.csrf(), "token": self.confirm_token(html)})
        self.client.post("/move/confirm", data={"csrf": self.csrf(), "subscription_id": "SUB-ACTIVE",
                                                "quantity": "999"})
        self.client.post("/logout", data={"csrf": self.csrf()})
        names = [e["event"] for e in self.events()]
        for expected in ("signed in", "viewed licenses", "chose destination", "dry-run on",
                         "move confirmation opened", "move cancelled", "move", "move refused", "signed out"):
            self.assertIn(expected, names)
        move = next(e for e in self.events() if e["event"] == "move")
        self.assertEqual((move["result"], move["quantity"], move["dst_org_id"], move["user"]),
                         ("DRY-RUN", "5", DST2, "alice"))
        self.assertTrue(all(e["ip"] == "127.0.0.1" for e in self.events()))

    def test_real_return_and_refused_config_logged(self):
        self.login()
        html = self.client.post("/return/confirm", data={
            "csrf": self.csrf(), "amendment_id": "AM-KNOWN"}).get_data(as_text=True)
        self.client.post("/return", data={"csrf": self.csrf(), "token": self.confirm_token(html)})
        self.client.post("/configs", data={"csrf": self.csrf(), "config": "B.ini"})
        ret = next(e for e in self.events() if e["event"] == "return")
        self.assertEqual((ret["result"], ret["amendment_id"]), ("OK", "AM-KNOWN"))
        self.assertEqual(self.events()[-1]["event"], "config refused")

    def test_user_input_cannot_break_log_lines(self):
        self.client.post("/login", data={"username": "evil\tname\nevent=signed in", "password": "x"})
        self.assertEqual(len(self.activity_lines()), 1)


if __name__ == "__main__":
    unittest.main()
