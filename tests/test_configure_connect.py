import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_connect", ROOT / "scripts" / "configure_connect.py")
connect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect)

PASSWORD = "fixture-admin-password"
CLIENT_SECRET = "fixture-client-secret"
SMTP_PASSWORD = "fixture-smtp-password"
NODE = {
    "name": "orthanc", "aeTitle": "ORTHANC", "host": "127.0.0.1",
    "port": 4242, "callingAe": "RCONNECT_SCP", "sortOrder": 0,
}
LOCAL = {
    "storageScpAeTitle": "OLD_SCP", "maxConcurrentDownloads": 3,
    "radiopaediaClientId": "existing-client", "radiopaediaClientSecret": CLIENT_SECRET,
    "smtpHost": "mail.example.test", "smtpPort": 587, "smtpUsername": "existing-user",
    "smtpPassword": SMTP_PASSWORD, "smtpFromAddress": "connect@example.test",
    "notificationRecipients": "admin@example.test",
}


class Fixture:
    def __init__(self):
        self.password = PASSWORD
        self.local = copy.deepcopy(LOCAL)
        self.nodes = []
        self.calls = []
        self.restarts = 0
        self.allowed_aes = []
        self.session = False
        self.echo_success = True
        self.status_failures = 0
        self.fail_route = None
        self.redirect_route = None
        self.omit_local_field = None
        self.ignore_local_write = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.handle_request("GET")

            def do_POST(self):
                self.handle_request("POST")

            def do_PUT(self):
                self.handle_request("PUT")

            def respond(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def handle_request(self, method):
                route = self.path.removeprefix("/api/settings")
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else None
                fixture.calls.append((method, route, body))
                if route == fixture.redirect_route:
                    return self.respond(307, {}, {"Location": "http://127.0.0.1:1/leak"})
                if route == fixture.fail_route:
                    return self.respond(500, {"message": CLIENT_SECRET + PASSWORD})
                if route == "/status":
                    if fixture.status_failures:
                        fixture.status_failures -= 1
                        return self.respond(503, {})
                    configured = bool(fixture.local["radiopaediaClientId"] and
                                      fixture.local["radiopaediaClientSecret"] and fixture.nodes)
                    return self.respond(200, {
                        "isPasswordSet": fixture.password is not None,
                        "isConfigured": configured,
                        "issues": [] if configured else ["Missing settings"],
                    })
                if route in {"/password/setup", "/password/verify"}:
                    if route.endswith("setup"):
                        if fixture.password is not None:
                            return self.respond(409, {})
                        fixture.password = body["password"]
                    if body["password"] != fixture.password:
                        return self.respond(401, {"message": PASSWORD})
                    fixture.session = True
                    return self.respond(200, {}, {"Set-Cookie": "rconnect_admin_session=fixture; Path=/; HttpOnly"})
                if not fixture.session or "rconnect_admin_session=fixture" not in self.headers.get("Cookie", ""):
                    return self.respond(401, {})
                if route == "/local" and method == "GET":
                    local = copy.deepcopy(fixture.local)
                    for field in ("radiopaediaClientSecret", "smtpPassword"):
                        local[field] = connect.SECRET_MASK if local[field] else ""
                    if fixture.omit_local_field:
                        local.pop(fixture.omit_local_field)
                    return self.respond(200, local)
                if route == "/local" and method == "PUT":
                    if not fixture.ignore_local_write:
                        for field, value in body.items():
                            if field in {"radiopaediaClientSecret", "smtpPassword"} and value == connect.SECRET_MASK:
                                continue
                            fixture.local[field] = value
                    return self.respond(200, {"scpRestartRequired": True})
                if route == "/nodes" and method == "GET":
                    return self.respond(200, fixture.nodes)
                if route == "/nodes" and method == "POST":
                    node_id = max((node["id"] for node in fixture.nodes), default=0) + 1
                    fixture.nodes.append(dict(body, id=node_id))
                    return self.respond(200, {"id": node_id})
                if route.startswith("/nodes/") and method == "PUT":
                    node_id = int(route.rsplit("/", 1)[1])
                    for index, node in enumerate(fixture.nodes):
                        if node["id"] == node_id:
                            fixture.nodes[index] = dict(body, id=node_id)
                            return self.respond(200, {})
                    return self.respond(404, {})
                if route == "/scp/restart":
                    fixture.restarts += 1
                    fixture.allowed_aes = [node["aeTitle"] for node in fixture.nodes]
                    return self.respond(200, {})
                if route == "/nodes/echo":
                    return self.respond(200, {"success": fixture.echo_success, "message": CLIENT_SECRET})
                if route == "/logout":
                    fixture.session = False
                    return self.respond(200, {})
                self.respond(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.api = connect.Api(f"http://127.0.0.1:{self.server.server_port}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def apply(self, local=None, node=NODE, password=PASSWORD):
        connect.configure(self.fixture.api, local or {}, node, password, wait_seconds=0)

    def test_existing_install_preserves_unmanaged_fields_and_secrets(self):
        self.apply({"storageScpAeTitle": "RCONNECT_SCP", "maxConcurrentDownloads": 5})
        self.assertEqual(self.fixture.local, dict(LOCAL, storageScpAeTitle="RCONNECT_SCP", maxConcurrentDownloads=5))
        self.assertEqual(self.fixture.nodes, [dict(NODE, id=1)])
        self.assertEqual(self.fixture.allowed_aes, ["ORTHANC"])
        self.assertEqual(self.fixture.restarts, 1)
        self.assertFalse(self.fixture.session)
        self.assertNotIn(CLIENT_SECRET, self.output.getvalue())

    def test_fresh_setup_and_repeat_do_not_duplicate_or_rewrite(self):
        self.fixture.password = None
        self.fixture.local.update(radiopaediaClientId="", radiopaediaClientSecret="")
        changes = {"radiopaediaClientId": "new-client", "radiopaediaClientSecret": "new-secret"}
        self.apply(changes)
        self.assertEqual(self.fixture.password, PASSWORD)
        self.assertEqual(self.fixture.local["radiopaediaClientSecret"], "new-secret")
        self.assertIn("presence, not their contents", self.output.getvalue())
        self.fixture.calls.clear()
        self.apply()
        self.assertEqual(len(self.fixture.nodes), 1)
        self.assertFalse(any(method == "PUT" or route == "/password/setup"
                             or (method == "POST" and route == "/nodes")
                             for method, route, _ in self.fixture.calls))
        self.assertEqual(self.fixture.restarts, 2)

    def test_update_by_name_preserves_id_sort_order_and_unrelated_node(self):
        old = dict(NODE, id=12, name="Orthanc", host="orthanc", sortOrder=7)
        other = dict(NODE, id=20, name="other", aeTitle="OTHER")
        self.fixture.nodes = [old, copy.deepcopy(other)]
        desired = {key: value for key, value in NODE.items() if key != "sortOrder"}
        self.apply(node=desired)
        self.assertEqual(self.fixture.nodes, [dict(NODE, id=12, sortOrder=7), other])
        self.assertEqual(self.fixture.allowed_aes, ["ORTHANC", "OTHER"])

    def test_duplicate_names_fail_before_settings_writes(self):
        self.fixture.nodes = [dict(NODE, id=1), dict(NODE, id=2, name="ORTHANC")]
        with self.assertRaisesRegex(connect.ConfigurationError, "Multiple nodes"):
            self.apply({"maxConcurrentDownloads": 9})
        self.assertEqual(self.fixture.local, LOCAL)
        self.assertEqual(self.fixture.restarts, 0)
        self.assertFalse(self.fixture.session)

    def test_wrong_password_does_not_change_settings(self):
        with self.assertRaises(connect.ApiError) as error:
            self.apply(password="incorrect")
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(self.fixture.local, LOCAL)
        self.assertNotIn(PASSWORD, str(error.exception))

    def test_unknown_api_shape_refuses_replacement(self):
        self.fixture.omit_local_field = "smtpPassword"
        with self.assertRaisesRegex(connect.ConfigurationError, "Unsupported local"):
            self.apply({"maxConcurrentDownloads": 9})
        self.assertFalse(any(method == "PUT" for method, _, _ in self.fixture.calls))

    def test_failed_echo_is_not_success_even_with_http_200(self):
        self.fixture.echo_success = False
        with self.assertRaisesRegex(connect.ConfigurationError, "C-ECHO failed"):
            self.apply()
        self.assertFalse(self.fixture.session)

    def test_readback_detects_unsaved_settings(self):
        self.fixture.ignore_local_write = True
        with self.assertRaisesRegex(connect.ConfigurationError, "Read-back verification failed"):
            self.apply({"maxConcurrentDownloads": 9})

    def test_api_error_does_not_include_sensitive_response(self):
        self.fixture.fail_route = "/local"
        with self.assertRaises(connect.ApiError) as error:
            self.apply()
        self.assertNotIn(CLIENT_SECRET, str(error.exception))
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertFalse(self.fixture.session)

    def test_redirect_is_refused(self):
        self.fixture.redirect_route = "/password/verify"
        with self.assertRaisesRegex(connect.ConfigurationError, "redirects are refused"):
            self.apply()

    def test_logout_failure_returns_error_after_successful_apply(self):
        self.fixture.fail_route = "/logout"
        with self.assertRaisesRegex(connect.ConfigurationError, "session could not be invalidated"):
            self.apply()

    def test_logout_failure_does_not_hide_primary_error(self):
        self.fixture.fail_route = "/logout"
        self.fixture.echo_success = False
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(connect.ConfigurationError, "C-ECHO failed"):
                self.apply()
        self.assertIn("could not invalidate", errors.getvalue())

    def test_readiness_retries_transient_status(self):
        self.fixture.status_failures = 1
        with patch.object(connect.time, "sleep"):
            status = connect.wait_for_status(self.fixture.api, 5)
        self.assertTrue(status["isPasswordSet"])

    def test_incomplete_oauth_is_reported_without_claiming_full_setup(self):
        self.fixture.local["radiopaediaClientId"] = ""
        self.apply()
        self.assertIn("setup remains incomplete", self.output.getvalue())
        self.assertNotIn("setup complete.", self.output.getvalue())

    def test_unmanaged_nodes_are_not_required(self):
        self.apply(node=None)
        self.assertFalse(any(route.startswith("/nodes") for _, route, _ in self.fixture.calls))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "connect.json"

    def load(self, config):
        self.path.write_text(json.dumps(config), encoding="utf-8")
        return connect.load_config(self.path)

    def test_shipped_configuration(self):
        local, node = connect.load_config(ROOT / "connect-config.json")
        self.assertEqual(local, {"storageScpAeTitle": "RCONNECT_SCP", "maxConcurrentDownloads": 5})
        self.assertEqual(node, NODE)

    def test_secret_file_resolution_and_newline_handling(self):
        (self.path.parent / "secret").write_bytes(b"  preserve spaces  \r\n")
        local, node = self.load({"localSettings": {"radiopaediaClientSecretFile": "secret"}})
        self.assertEqual(local["radiopaediaClientSecret"], "  preserve spaces  ")
        self.assertIsNone(node)

    def test_invalid_inputs(self):
        invalid = [
            {"localSetting": {}},
            {"localSettings": {"maxConcurrentDownloads": True}},
            {"localSettings": {"maxConcurrentDownloads": 0}},
            {"localSettings": {"smtpPort": 65536}},
            {"localSettings": {"storageScpAeTitle": "title-too-long-for-dicom"}},
            {"localSettings": {"storageScpAeTitle": "BAD\\AE"}},
            {"localSettings": {"smtpHost": None}},
            {"localSettings": {"smtpPassword": "value", "smtpPasswordFile": "secret"}},
            {"localSettings": {"smtpPasswordFile": "missing"}},
            {"orthancNode": None},
            {"orthancNode": {"name": "orthanc"}},
            {"orthancNode": dict(NODE, port=0)},
            {"orthancNode": dict(NODE, sortOrder=-1)},
            {"orthancNode": dict(NODE, aeTitle=" ORTHANC")},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(connect.ConfigurationError):
                self.load(config)

    def test_invalid_secret_files(self):
        path = self.path.parent / "secret"
        for value in ("", " \n", "********", "line1\nline2"):
            path.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(connect.ConfigurationError):
                connect.read_secret(path)

    def test_remote_plaintext_and_credential_urls_are_refused(self):
        for url in ("http://example.test", "https://user:password@example.test", "http://localhost/path"):
            with self.subTest(url=url), self.assertRaises(connect.ConfigurationError):
                connect.Api(url)

    def test_cli_failure_has_nonzero_exit_without_secret_output(self):
        self.path.write_text("invalid json", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = connect.main(["--config", str(self.path), "--admin-password-file", "missing"])
        self.assertEqual(result, 1)
        self.assertIn("Configuration failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
