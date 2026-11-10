"""Apply explicit settings through Radiopaedia Connect's admin API."""

import argparse
import http.client
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SECRET_MASK = "********"
LOCAL_FIELDS = {
    "storageScpAeTitle", "maxConcurrentDownloads", "radiopaediaClientId",
    "radiopaediaClientSecret", "smtpHost", "smtpPort", "smtpUsername",
    "smtpPassword", "smtpFromAddress", "notificationRecipients",
}
FILE_FIELDS = {
    "radiopaediaClientIdFile": "radiopaediaClientId",
    "radiopaediaClientSecretFile": "radiopaediaClientSecret",
    "smtpPasswordFile": "smtpPassword",
}
NODE_FIELDS = {"name", "aeTitle", "host", "port", "callingAe", "sortOrder"}


class ConfigurationError(Exception):
    pass


class ApiError(ConfigurationError):
    def __init__(self, method, route, status):
        self.status = status
        super().__init__(f"{method} {route} failed (HTTP {status}); response body withheld.")


class ConnectionError(ConfigurationError):
    pass


def read_secret(path):
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError("Cannot read a configured secret file.") from None
    if not value or "\n" in value or "\r" in value or not value.strip():
        raise ConfigurationError("Secret files must contain one non-empty line.")
    if value == SECRET_MASK:
        raise ConfigurationError("A secret cannot be the API's masking placeholder.")
    return value


def require_integer(value, minimum, maximum, field):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{field} must be an integer from {minimum} to {maximum}.")


def require_ae(value, field):
    if (not isinstance(value, str) or not value.strip() or len(value) > 16
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) > 126 or char == "\\" for char in value)):
        raise ConfigurationError(f"{field} must be a non-empty ASCII AE title, at most 16 characters, without backslashes or surrounding whitespace.")


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError("Cannot read configuration: expected a UTF-8 JSON file.") from None
    if not isinstance(config, dict) or set(config) - {"localSettings", "orthancNode"}:
        raise ConfigurationError("Configuration accepts only localSettings and orthancNode.")
    local = config.get("localSettings", {})
    if not isinstance(local, dict) or set(local) - LOCAL_FIELDS - FILE_FIELDS.keys():
        raise ConfigurationError("localSettings contains unsupported fields.")
    local = dict(local)
    for source, target in FILE_FIELDS.items():
        if source in local:
            if target in local:
                raise ConfigurationError(f"Supply either {source} or {target}, not both.")
            secret_path = local.pop(source)
            if not isinstance(secret_path, str) or not secret_path:
                raise ConfigurationError(f"{source} must be a file path.")
            resolved = Path(secret_path)
            if not resolved.is_absolute():
                resolved = Path(path).parent / resolved
            local[target] = read_secret(resolved)
    for field, value in local.items():
        if field == "maxConcurrentDownloads":
            require_integer(value, 1, 2147483647, field)
        elif field == "smtpPort":
            if value is not None:
                require_integer(value, 1, 65535, field)
        elif field == "storageScpAeTitle":
            require_ae(value, field)
        elif not isinstance(value, str):
            raise ConfigurationError(f"{field} must be a string; use an empty string to clear it.")

    node = config.get("orthancNode")
    if "orthancNode" in config:
        if not isinstance(node, dict) or set(node) - NODE_FIELDS:
            raise ConfigurationError("orthancNode contains unsupported fields.")
        if not {"name", "aeTitle", "host", "port", "callingAe"} <= node.keys():
            raise ConfigurationError("orthancNode requires name, aeTitle, host, port and callingAe.")
        for field in ("name", "host"):
            if not isinstance(node[field], str) or not node[field].strip():
                raise ConfigurationError(f"orthancNode.{field} must be a non-empty string.")
        for field in ("aeTitle", "callingAe"):
            require_ae(node[field], f"orthancNode.{field}")
        require_integer(node["port"], 1, 65535, "orthancNode.port")
        if "sortOrder" in node:
            require_integer(node["sortOrder"], 0, 2147483647, "orthancNode.sortOrder")
    return local, node


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConfigurationError("API redirects are refused to avoid forwarding credentials.")


class Api:
    def __init__(self, base_url, timeout=15):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ConfigurationError("Base URL must be an HTTP(S) origin without credentials.")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("Use HTTPS for non-loopback API connections.")
        self.base_url = base_url.rstrip("/") + "/api/settings"
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, method, route, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + route, data=data, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ApiError(method, route, status) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ConnectionError(f"{method} {route}: API connection failed.") from None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConfigurationError(f"{method} {route}: expected a JSON response.") from None


def wait_for_status(api, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            status = api.request("GET", "/status", timeout=min(api.timeout, max(0.1, deadline - time.monotonic())))
            if (not isinstance(status, dict) or type(status.get("isPasswordSet")) is not bool
                    or type(status.get("isConfigured")) is not bool
                    or not isinstance(status.get("issues"), list)):
                raise ConfigurationError("Unsupported setup status response.")
            return status
        except (ConnectionError, ApiError) as error:
            if isinstance(error, ApiError) and error.status not in {502, 503, 504}:
                raise
            if time.monotonic() >= deadline:
                raise ConfigurationError("Timed out waiting for the settings API.") from None
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def get_local(api):
    local = api.request("GET", "/local")
    if not isinstance(local, dict) or not LOCAL_FIELDS <= local.keys():
        raise ConfigurationError("Unsupported local settings response; refusing a replacement write.")
    return {field: local[field] for field in LOCAL_FIELDS}


def get_nodes(api):
    nodes = api.request("GET", "/nodes")
    if (not isinstance(nodes, list)
            or any(not isinstance(node, dict) or not NODE_FIELDS | {"id"} <= node.keys()
                   or type(node["id"]) is not int or node["id"] <= 0
                   or not isinstance(node["name"], str) for node in nodes)):
        raise ConfigurationError("Unsupported remote nodes response.")
    return nodes


def matching_node(nodes, desired):
    matches = [node for node in nodes if node["name"].casefold() == desired["name"].casefold()]
    if len(matches) > 1:
        raise ConfigurationError("Multiple nodes match the managed name; resolve duplicates before applying.")
    return matches[0] if matches else None


def check_echo(api, node, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    body = {key: node[key] for key in ("host", "port", "aeTitle", "callingAe")}
    while True:
        result = api.request("POST", "/nodes/echo", body)
        if not isinstance(result, dict) or type(result.get("success")) is not bool:
            raise ConfigurationError("Unsupported DICOM C-ECHO response.")
        if result["success"]:
            return
        if time.monotonic() >= deadline:
            raise ConfigurationError("Orthanc DICOM C-ECHO failed; settings were saved. Check host, port, AE titles and Orthanc logs.")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def configure(api, local_changes, desired_node, password, wait_seconds=60):
    status = wait_for_status(api, wait_seconds)
    route = "/password/verify" if status["isPasswordSet"] else "/password/setup"
    api.request("POST", route, {"password": password})
    print("Admin session established.")
    try:
        current = get_local(api)
        existing = matching_node(get_nodes(api), desired_node) if desired_node else None
        expected = dict(current)
        expected.update(local_changes)
        if expected != current:
            api.request("PUT", "/local", expected)
            print("Local settings saved.")
        else:
            print("Local settings already match.")

        node_expected = None
        if desired_node:
            node_expected = {field: existing[field] for field in NODE_FIELDS} if existing else {"sortOrder": 0}
            node_expected.update(desired_node)
            if existing is None:
                api.request("POST", "/nodes", node_expected)
                print("Orthanc node created.")
            elif any(existing[field] != value for field, value in node_expected.items()):
                api.request("PUT", f"/nodes/{existing['id']}", node_expected)
                print("Orthanc node updated.")
            else:
                print("Orthanc node already matches.")

        # Also repairs a stale in-memory AE allowlist after an interrupted prior run.
        api.request("POST", "/scp/restart")
        print("DICOM listener restarted.")

        actual = get_local(api)
        for field, value in expected.items():
            wanted = SECRET_MASK if field in {"radiopaediaClientSecret", "smtpPassword"} and value else value
            if actual[field] != wanted:
                raise ConfigurationError(f"Read-back verification failed for {field}.")
        if desired_node:
            actual_node = matching_node(get_nodes(api), desired_node)
            if actual_node is None or any(actual_node[field] != value for field, value in node_expected.items()):
                raise ConfigurationError("Orthanc node read-back verification failed.")
            check_echo(api, node_expected, wait_seconds)
            print("Orthanc DICOM C-ECHO succeeded.")

        final_status = wait_for_status(api, wait_seconds)
        if not final_status["isConfigured"]:
            print("Requested settings verified; Connect setup remains incomplete. Check /api/settings/status for missing settings.")
        else:
            print("Requested settings verified; Connect reports setup complete.")
        print("Secret values are masked by the API; read-back confirms presence, not their contents.")
    finally:
        already_failed = sys.exc_info()[0] is not None
        try:
            api.request("POST", "/logout")
        except ConfigurationError:
            if not already_failed:
                raise ConfigurationError("Settings applied, but the temporary admin session could not be invalidated.") from None
            print("Warning: could not invalidate the temporary admin session.", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--admin-password-file", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--wait-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        if args.wait_seconds <= 0:
            raise ConfigurationError("--wait-seconds must be positive.")
        local, node = load_config(args.config)
        password = read_secret(args.admin_password_file)
        if len(password) < 6:
            raise ConfigurationError("The admin password must contain at least 6 characters.")
        configure(Api(args.base_url), local, node, password, args.wait_seconds)
        return 0
    except ConfigurationError as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        print("Earlier writes may have persisted. Correct the problem and rerun; no rollback is attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
