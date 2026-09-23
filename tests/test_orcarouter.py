"""OrcaRouter provider, the two credential entrances, and terminal 401 recovery.

Offline: no real key, no real network. The PKCE tests run against a local fake
authorization server that speaks the same endpoints as the real one, so the whole
authorize -> callback -> exchange -> persist path is exercised through the adapter the
settings window actually calls — not through hash helpers in isolation.

Nothing here may print a verifier or a key: several assertions check that directly.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import generate          # noqa: E402
import orcarouter        # noqa: E402
import settings_config as config   # noqa: E402
import userconfig        # noqa: E402

FAKE_KEY = "sk-orca-test-only-not-a-real-key"
OTHER_KEY = "sk-orca-test-only-second-key"


class FakeAuthServer:
    """A stand-in for www.orcarouter.ai: /auth, /api/v1/auth/keys, and the wrong path.

    It records what it was sent so the tests can assert the exchange body and that the
    authorize URL never carries the verifier.
    """

    def __init__(self):
        self.requests = []
        self.exchange_status = 200
        self.exchange_body = {"key": FAKE_KEY, "user_id": "4242", "scope": "api"}
        self.record = {}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), None))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                self.send_response(outer.exchange_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(outer.exchange_body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def authorize(self, params):
        """Deliver a callback to the loopback listener, as the consent screen would."""
        import urllib.request
        with urllib.request.urlopen(params, timeout=5) as resp:
            return resp.read().decode()


class Origins(unittest.TestCase):
    def test_default_origins_are_the_two_documented_hosts(self):
        with mock.patch.object(userconfig, "get", return_value=""):
            self.assertEqual(orcarouter.auth_base(), "https://www.orcarouter.ai")
            self.assertEqual(orcarouter.api_base(), "https://api.orcarouter.ai/v1")
            self.assertEqual(orcarouter.authorize_url(orcarouter.auth_base()),
                             "https://www.orcarouter.ai/auth")
            self.assertEqual(orcarouter.exchange_url(orcarouter.auth_base()),
                             "https://www.orcarouter.ai/api/v1/auth/keys")

    def test_auth_and_api_are_never_derived_from_each_other(self):
        # The documented trap: api/v1/auth/keys is a 301. The exchange path is anchored on
        # the auth origin, and the inference base never gains an /auth segment.
        self.assertNotIn("/auth", orcarouter.api_base())
        self.assertNotIn("api.orcarouter.ai", orcarouter.exchange_url(orcarouter.auth_base()))
        self.assertTrue(orcarouter.exchange_url(orcarouter.auth_base())
                        .startswith("https://www.orcarouter.ai/api/v1/"))

    def test_explicit_overrides_win_over_shared_origin(self):
        values = {"ORCAROUTER_AUTH_BASE_URL": "https://auth.example.test",
                  "ORCAROUTER_BASE_URL": "https://api.example.test/v1",
                  "ORCAROUTER_ORIGIN": "https://shared.example.test"}
        with mock.patch.object(userconfig, "get",
                               side_effect=lambda *names: next(
                                   (values[n] for n in names if n in values), "")):
            self.assertEqual(orcarouter.auth_base(), "https://auth.example.test")
            self.assertEqual(orcarouter.api_base(), "https://api.example.test/v1")

    def test_shared_origin_fills_both_when_alone(self):
        values = {"ORCAROUTER_ORIGIN": "https://self.hosted.test"}
        with mock.patch.object(userconfig, "get",
                               side_effect=lambda *names: next(
                                   (values[n] for n in names if n in values), "")):
            self.assertEqual(orcarouter.auth_base(), "https://self.hosted.test")
            self.assertEqual(orcarouter.api_base(), "https://self.hosted.test")

    def test_https_required_except_loopback(self):
        for bad in ("http://api.orcarouter.ai/v1", "http://example.test"):
            with self.assertRaises(ValueError):
                orcarouter.validate_origin(bad, "地址")
        for ok in ("http://127.0.0.1:8123", "http://localhost:8123", "https://any.host"):
            self.assertEqual(orcarouter.validate_origin(ok, "地址"), ok)

    def test_origin_rejects_userinfo_query_and_fragment(self):
        for bad in ("https://user:pw@host.test", "https://host.test/?x=1", "https://host.test/#f"):
            with self.assertRaises(ValueError):
                orcarouter.validate_origin(bad, "地址")


class PkcePrimitives(unittest.TestCase):
    def test_challenge_is_unpadded_base64url_sha256(self):
        import base64
        import hashlib
        verifier = orcarouter.new_verifier()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(orcarouter.challenge_for(verifier), expected)
        self.assertNotIn("=", orcarouter.challenge_for(verifier))

    def test_verifier_and_state_are_fresh_per_attempt(self):
        seen = {orcarouter.new_verifier() for _ in range(50)}
        self.assertEqual(len(seen), 50)
        self.assertEqual(len({orcarouter.new_state() for _ in range(50)}), 50)
        self.assertGreaterEqual(len(orcarouter.new_verifier()), 43)

    def test_state_comparison_is_constant_time_and_exact(self):
        self.assertTrue(orcarouter.constant_time_equal("abc", "abc"))
        self.assertFalse(orcarouter.constant_time_equal("abc", "abd"))
        self.assertFalse(orcarouter.constant_time_equal("abc", ""))

    def test_authorize_url_always_sends_s256_and_never_the_verifier(self):
        verifier = orcarouter.new_verifier()
        url = orcarouter.build_authorize_url("https://www.orcarouter.ai",
                                             "http://127.0.0.1:51733/cb",
                                             orcarouter.challenge_for(verifier), "STATE")
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("scope=api", url)
        self.assertIn("callback_url=http%3A%2F%2F127.0.0.1%3A51733%2Fcb", url)
        self.assertNotIn(verifier, url)
        self.assertNotIn("code_verifier", url)

    def test_oob_callback_is_the_literal_oob(self):
        url = orcarouter.build_authorize_url("https://www.orcarouter.ai", "oob", "C", "S")
        self.assertIn("callback_url=oob", url)


class PkceAdapter(unittest.TestCase):
    """Flow A end to end through the adapter, against a local fake authorization server."""

    def setUp(self):
        self.server = FakeAuthServer()
        self.addCleanup(self.server.stop)

    def _source(self, flow="loopback"):
        return orcarouter.PkceSource(self.server.base, flow=flow, timeout=10)

    def test_flow_a_full_path_authorize_callback_exchange(self):
        source = self._source()
        url = source.start()
        self.assertTrue(url.startswith(self.server.base + "/auth?"))
        self.assertIn("code_challenge_method=S256", url)
        verifier = source.verifier
        self.assertNotIn(verifier, url)

        # The consent screen redirects to the loopback listener the adapter opened.
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        state = params["state"][0]
        callback = params["callback_url"][0]
        self.assertTrue(callback.startswith("http://127.0.0.1:"))
        self.server.authorize(f"{callback}?code=CODE123&state={state}")

        cred = source.finish()
        self.assertEqual(cred.key, FAKE_KEY)
        self.assertEqual(cred.source, "pkce")
        self.assertEqual(cred.account, "4242")
        self.assertEqual(cred.scope, "api")

        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/api/v1/auth/keys")
        self.assertEqual(posts[0][3]["code"], "CODE123")
        self.assertEqual(posts[0][3]["code_verifier"], verifier)
        self.assertEqual(posts[0][3]["code_challenge_method"], "S256")

    def test_flow_a_rejects_state_mismatch_before_using_the_code(self):
        source = self._source()
        url = source.start()
        import urllib.parse
        callback = urllib.parse.parse_qs(
            urllib.parse.urlsplit(url).query)["callback_url"][0]
        self.server.authorize(f"{callback}?code=CODE123&state=not-the-state")
        with self.assertRaises(orcarouter.AuthRejected):
            source.finish()
        self.assertEqual([r for r in self.server.requests if r[0] == "POST"], [])

    def test_denial_ends_the_attempt_without_exchanging(self):
        source = self._source()
        url = source.start()
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.server.authorize(f"{params['callback_url'][0]}"
                              f"?error=access_denied&state={params['state'][0]}")
        with self.assertRaises(orcarouter.AuthRejected) as caught:
            source.finish()
        self.assertIn("拒绝", str(caught.exception))
        self.assertEqual([r for r in self.server.requests if r[0] == "POST"], [])

    def test_flow_b_takes_the_pasted_code(self):
        source = self._source(flow="oob")
        url = source.start()
        self.assertIn("callback_url=oob", url)
        cred = source.finish("PASTED-CODE")
        self.assertEqual(cred.key, FAKE_KEY)
        body = [r for r in self.server.requests if r[0] == "POST"][0][3]
        self.assertEqual(body["code"], "PASTED-CODE")
        self.assertEqual(body["code_challenge_method"], "S256")

    def test_cancel_releases_the_listener_and_finish_raises(self):
        source = self._source()
        source.start()
        listener = source._listener
        source.cancel()
        with self.assertRaises(orcarouter.LoginCancelled):
            source.finish()
        # The port is free again, so a second attempt can bind its own.
        second = self._source()
        second.start()
        self.assertNotEqual(second._listener.port, listener.port)
        second.cancel()

    def test_cancel_during_a_wait_returns_immediately(self):
        source = self._source()
        source.timeout = 30
        source.start()
        result = {}

        def waiting():
            try:
                source.finish()
            except Exception as exc:            # noqa: BLE001 - the test asserts the type
                result["error"] = exc

        worker = threading.Thread(target=waiting, daemon=True)
        worker.start()
        time.sleep(0.2)
        source.cancel()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "cancel must not leave the wait running")
        self.assertIsInstance(result.get("error"), orcarouter.LoginCancelled)

    def test_second_attempt_uses_a_new_verifier(self):
        first = self._source()
        first.start()
        v1 = first.verifier
        first.cancel()
        second = self._source()
        second.start()
        self.assertNotEqual(v1, second.verifier)
        second.cancel()

    def test_timeout_ends_with_a_message_not_a_hang(self):
        source = self._source()
        source.timeout = 0.3
        source.start()
        with self.assertRaises(orcarouter.AuthRejected) as caught:
            source.finish()
        self.assertIn("超时", str(caught.exception))


class ExchangeErrors(unittest.TestCase):
    """Every documented exchange failure is terminal and safe, with no secret leaked."""

    def setUp(self):
        self.server = FakeAuthServer()
        self.addCleanup(self.server.stop)

    def _exchange(self, status, body=None):
        self.server.exchange_status = status
        if body is not None:
            self.server.exchange_body = body
        return orcarouter.exchange_code(self.server.base, "CODE", "VERIFIER-SECRET")

    def test_400_is_a_challenge_method_mismatch(self):
        with self.assertRaises(orcarouter.AuthRejected) as caught:
            self._exchange(400)
        self.assertNotIn("VERIFIER-SECRET", str(caught.exception))

    def test_403_covers_unknown_expired_and_replayed_codes(self):
        for status in (401, 403):
            with self.assertRaises(orcarouter.AuthRejected) as caught:
                self._exchange(status)
            self.assertNotIn("VERIFIER-SECRET", str(caught.exception))

    def test_429_says_the_daily_cap_not_a_generic_error(self):
        with self.assertRaises(orcarouter.AuthRejected) as caught:
            self._exchange(429)
        self.assertIn("10", str(caught.exception))

    def test_network_failure_is_reported_without_the_verifier(self):
        source = orcarouter.PkceSource("http://127.0.0.1:1", timeout=1)
        with self.assertRaises(orcarouter.AuthFailed) as caught:
            orcarouter.exchange_code(source.base, "CODE", "VERIFIER-SECRET", timeout=1)
        self.assertNotIn("VERIFIER-SECRET", str(caught.exception))

    def test_scope_downgrade_is_reported_not_assumed(self):
        with self.assertRaises(orcarouter.AuthRejected) as caught:
            self._exchange(200, {"key": FAKE_KEY, "user_id": "1", "scope": "connector"})
        self.assertIn("connector", str(caught.exception))

    def test_missing_key_in_a_200_is_an_error(self):
        with self.assertRaises(orcarouter.AuthFailed):
            self._exchange(200, {"user_id": "1", "scope": "api"})

    def test_exchange_response_body_is_never_echoed(self):
        self.server.exchange_body = {"key": "sk-orca-LEAKED", "user_id": "1", "scope": "api"}
        cred = self._exchange(200)
        self.assertEqual(cred.key, "sk-orca-LEAKED")     # returned, not printed
        self.assertNotIn("sk-orca-LEAKED", orcarouter.mask(cred.key))


class Masking(unittest.TestCase):
    def test_mask_never_returns_a_usable_key(self):
        masked = orcarouter.mask(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, masked)
        self.assertIn("chars", masked)

    def test_mask_handles_empty_and_short(self):
        self.assertEqual(orcarouter.mask(""), "（未配置）")
        self.assertEqual(orcarouter.mask("short"), "…")

    def test_key_shape_check_is_lightweight_not_a_claim_of_validity(self):
        self.assertTrue(orcarouter.looks_like_key(FAKE_KEY))
        self.assertFalse(orcarouter.looks_like_key("sk-something-else"))
        self.assertFalse(orcarouter.looks_like_key("sk-orca-"))


class CredentialEntrances(unittest.TestCase):
    """Both entrances must produce the same credential result, and downstream must not care."""

    def test_api_key_adapter_produces_the_shared_result(self):
        cred = orcarouter.KeySource(FAKE_KEY, account="7").acquire()
        self.assertTrue(cred.present)
        self.assertEqual(cred.key, FAKE_KEY)
        self.assertEqual(cred.source, "api_key")

    def test_pkce_adapter_produces_the_same_shape(self):
        server = FakeAuthServer()
        self.addCleanup(server.stop)
        cred = orcarouter.exchange_code(server.base, "CODE", "VERIFIER")
        self.assertTrue(cred.present)
        self.assertEqual(cred.source, "pkce")
        # Same type, same fields, same consumer contract.
        self.assertEqual(set(vars(cred)), set(vars(orcarouter.KeySource(FAKE_KEY).acquire())))

    def test_empty_key_adapter_is_absent_not_an_error(self):
        cred = orcarouter.KeySource("").acquire()
        self.assertFalse(cred.present)
        self.assertEqual(cred.source, "none")

    def test_both_entrances_feed_one_generation_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                base, key, model, _src, api = generate.load_credentials()
                self.assertEqual(key, FAKE_KEY)
                self.assertEqual(base, "https://api.orcarouter.ai/v1")
                self.assertEqual(api, "openai")
                # The provider and the transport do not ask where the key came from.
                self.assertEqual(orcarouter.resolve_credential().key, key)

    def test_recorded_account_marks_a_signed_in_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\nORCAROUTER_ACCOUNT_ID=99\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                cred = orcarouter.resolve_credential()
                self.assertEqual(cred.source, "pkce")
                self.assertEqual(cred.account, "99")
                # Same downstream result as the pasted-key entrance.
                self.assertEqual(cred.key, FAKE_KEY)


class ProviderRegistration(unittest.TestCase):
    def test_orcarouter_is_a_first_class_named_provider(self):
        self.assertIn("ORCAROUTER", config.PREFIXES)
        self.assertEqual(config.DEFAULTS["ORCAROUTER"][0], "https://api.orcarouter.ai/v1")
        self.assertIn("ORCAROUTER_ACCOUNT_ID", config.EXTRA_KEYS)
        # The label the user sees in the provider list is a named provider, not a
        # "custom base URL" field.
        self.assertEqual(orcarouter.PREFIX, "ORCAROUTER")

    def test_settings_writer_accepts_the_account_label_and_rejects_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("# keep\n")
            out = config.write_settings(path, path.read_text(),
                                        {"ORCAROUTER_API_KEY": FAKE_KEY,
                                         "ORCAROUTER_ACCOUNT_ID": "5"})
            self.assertIn("ORCAROUTER_API_KEY", out)
            self.assertIn("ORCAROUTER_ACCOUNT_ID", out)
            self.assertIn("# keep", out)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                config.write_settings(path, out, {"SOMETHING_ELSE": "x"})

    def test_generation_precedence_leaves_openai_and_anthropic_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"OPENAI_API_KEY=sk-openai-x\nOPENAI_MODEL=m\n"
                            f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                base, key, _model, _src, api = generate.load_credentials()
                self.assertEqual(key, "sk-openai-x")
                self.assertEqual(api, "openai")


class Reauth(unittest.TestCase):
    """A revoked durable key is terminal, generation-safe, and never faked into a refresh."""

    def setUp(self):
        orcarouter.clear_reauth()
        self.addCleanup(orcarouter.clear_reauth)

    def test_401_marks_only_the_rejected_account_generation(self):
        old = orcarouter.Credential(key=FAKE_KEY, source="pkce", account="1", generation=1)
        new = orcarouter.Credential(key=OTHER_KEY, source="pkce", account="1", generation=2)
        self.assertTrue(orcarouter.mark_needs_reauth(old, "revoked"))
        self.assertTrue(orcarouter.needs_reauth(old))
        # A late failure from the old generation must not touch the new credential.
        self.assertFalse(orcarouter.mark_needs_reauth(old, "late"))
        self.assertFalse(orcarouter.needs_reauth(new))
        self.assertFalse(orcarouter.needs_reauth(
            orcarouter.Credential(key=FAKE_KEY, source="api_key", account="other",
                                  generation=1)))

    def test_generator_attributes_401_to_the_credential_that_sent_it(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\nORCAROUTER_ACCOUNT_ID=77\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                gen = generate.Generator()
                gen._last_key = FAKE_KEY
                gen.note_http_error(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
                cred = orcarouter.resolve_credential()
                self.assertTrue(orcarouter.needs_reauth(cred))
                self.assertIn("401", orcarouter.reauth_reason(cred))

    def test_a_401_for_a_different_key_does_not_mark_this_one(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\nORCAROUTER_ACCOUNT_ID=77\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                gen = generate.Generator()
                gen._last_key = OTHER_KEY          # a key that is no longer stored
                gen.note_http_error(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
                self.assertFalse(orcarouter.needs_reauth(orcarouter.resolve_credential()))

    def test_non_401_errors_do_not_mark_reauth(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                gen = generate.Generator()
                gen._last_key = FAKE_KEY
                for status in (429, 500, 503):
                    gen.note_http_error(
                        urllib.error.HTTPError("u", status, "x", {}, None))
                self.assertFalse(orcarouter.needs_reauth(orcarouter.resolve_credential()))

    def test_successful_login_clears_the_flag(self):
        cred = orcarouter.Credential(key=FAKE_KEY, source="pkce", account="1", generation=1)
        orcarouter.mark_needs_reauth(cred, "revoked")
        orcarouter.clear_reauth()
        self.assertFalse(orcarouter.needs_reauth(cred))

    def test_no_refresh_grant_exists_anywhere(self):
        # OrcaRouter issues a durable key, not an access/refresh pair. There is no refresh
        # endpoint in this module, and no code that could call one.
        text = (Path(orcarouter.__file__).read_text()
                + Path(generate.__file__).read_text())
        self.assertNotIn("refresh_token", text)
        self.assertNotIn("grant_type=refresh_token", text)
        self.assertNotIn("/oauth/token", text)

    def test_a_revoked_key_is_not_deleted_before_a_replacement(self):
        # Marking needs-reauth must leave the stored secret in place: deleting it would turn
        # a transient failure into irreversible loss.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                cred = orcarouter.resolve_credential()
                orcarouter.mark_needs_reauth(cred, "revoked")
                self.assertEqual(userconfig.get("ORCAROUTER_API_KEY"), FAKE_KEY)
                self.assertIn(FAKE_KEY, path.read_text())


class NoSecretLeaks(unittest.TestCase):
    """The verifier and the key must not reach a URL, a log line, or an error string."""

    def test_verifier_is_absent_from_every_url_the_adapter_builds(self):
        server = FakeAuthServer()
        self.addCleanup(server.stop)
        source = orcarouter.PkceSource(server.base)
        url = source.start()
        # The challenge and the opaque state ride the URL; the verifier never does.
        self.assertNotIn(source.verifier, url)
        self.assertIn(source._state, url)
        self.assertIn(orcarouter.challenge_for(source.verifier), url)
        source.cancel()

    def test_exchange_errors_never_carry_the_verifier_or_the_key(self):
        server = FakeAuthServer()
        self.addCleanup(server.stop)
        for status in (400, 401, 403, 429, 500):
            server.exchange_status = status
            try:
                orcarouter.exchange_code(server.base, "CODE", "VERIFIER-SECRET")
            except orcarouter.OrcaRouterError as exc:
                self.assertNotIn("VERIFIER-SECRET", str(exc))
                self.assertNotIn(FAKE_KEY, str(exc))

    def test_status_line_reports_the_key_masked_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict("os.environ", {}, clear=True):
                userconfig.load()
                status = generate.credential_status()
                self.assertNotIn(FAKE_KEY, status)
                self.assertIn("OrcaRouter", status)
                self.assertIn("入口", status)


if __name__ == "__main__":
    unittest.main()
