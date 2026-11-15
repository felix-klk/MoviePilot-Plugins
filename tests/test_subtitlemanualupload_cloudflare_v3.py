import importlib.util
import importlib
import io
import json
from email.message import Message
from pathlib import Path
import sys
import types
import urllib.error
import urllib.response

import pytest


@pytest.fixture
def clients(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "plugins.v3/subtitlemanualupload/online_subtitles"
    package = types.ModuleType("cf_test")
    package.__path__ = [str(root.parent)]
    monkeypatch.setitem(sys.modules, "cf_test", package)
    online = types.ModuleType("cf_test.online_subtitles")
    online.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, "cf_test.online_subtitles", online)
    config = types.ModuleType("app.sdk.config")
    config.settings = types.SimpleNamespace(PROXY=None, PROXY_SERVER=None)
    log = types.ModuleType("app.sdk.logging")
    log.logger = types.SimpleNamespace(info=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "app.sdk.config", config)
    monkeypatch.setitem(sys.modules, "app.sdk.logging", log)
    spec = importlib.util.spec_from_file_location("cf_test.online_subtitles.clients", root / "clients.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(body=b"ok", status=200, challenge=False, url="https://subhd.me/api/sub/down"):
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    if challenge:
        headers["cf-mitigated"] = "challenge"
    if status >= 400:
        return urllib.error.HTTPError(url, status, "Forbidden", headers, io.BytesIO(body))
    return urllib.response.addinfourl(io.BytesIO(body), headers, url, status)


class Opener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.data, dict(request.header_items()), request.full_url))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.parametrize("method", ["get_text", "post_json", "get_bytes"])
@pytest.mark.parametrize("status", [200, 403, 503])
def test_challenge_retries_original_request(clients, monkeypatch, method, status):
    client = clients.OnlinePageClient()
    body = b'{"success": true}' if method == "post_json" else b"PK\x03\x04subtitle"
    client.opener = Opener(response(b"challenge", status, True), response(body))
    recovered = []
    def recover(url, jar, **kwargs):
        recovered.append(url)
        return [dict(name="cf_clearance", value="clear", domain="subhd.me", path="/", secure=True)], "Browser UA"
    monkeypatch.setattr(clients, "browser_session", recover)
    args = ("https://subhd.me/api/sub/down", {"sid": "abc", "cap": "xyz"}) if method == "post_json" else ("https://subhd.me/down/abc",)
    result = getattr(client, method)(*args, referer="https://subhd.me/down/abc")
    assert recovered == ["https://subhd.me/down/abc"]
    assert len(client.opener.calls) == 2
    assert client.opener.calls[0][:2] == client.opener.calls[1][:2]
    assert client.opener.calls[1][2]["User-agent"] == "Browser UA"
    assert [(c.name, c.value, c.domain) for c in client.cookie_jar] == [("cf_clearance", "clear", "subhd.me")]
    if method == "post_json":
        assert result == {"success": True}


def test_ordinary_403_does_not_launch_browser(clients, monkeypatch):
    client = clients.OnlinePageClient()
    client.opener = Opener(response(b"Forbidden", 403))
    monkeypatch.setattr(clients, "browser_session", lambda *a, **k: pytest.fail("unexpected browser"))
    assert client.get_text("https://subhd.me/a/abc")[:2] == (403, "Forbidden")


def test_repeated_challenge_is_bounded(clients, monkeypatch):
    client = clients.OnlinePageClient()
    client.opener = Opener(response(challenge=True), response(challenge=True))
    monkeypatch.setattr(clients, "browser_session", lambda *a, **k: ([], "UA"))
    with pytest.raises(clients.CaptchaRequiredError, match="仍被拦截"):
        client.post_json("https://subhd.me/api/sub/down", {"sid": "abc"})
    assert len(client.opener.calls) == 2


def test_unavailable_browser_has_actionable_error(clients, monkeypatch):
    client = clients.OnlinePageClient()
    client.opener = Opener(response(challenge=True, url="https://cdn.example/sub.zip"))
    def unavailable(*a, **k):
        raise ImportError("no SDK")
    monkeypatch.setattr(clients, "browser_session", unavailable)
    with pytest.raises(clients.CaptchaRequiredError, match="浏览器组件") as error:
        client.get_bytes("https://cdn.example/sub.zip", referer="https://subhd.me/down/abc")
    assert error.value.verify_url == "https://cdn.example/sub.zip"


def test_challenge_detection_avoids_regular_cf_scripts(clients):
    assert clients.is_challenge({}, b"<html><script>window._cf_chl_opt={}</script>")
    assert not clients.is_challenge({}, b'<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>subtitle</html>')


def test_mirror_preserves_post_and_scopes_cookies(clients, monkeypatch):
    cf = importlib.import_module("cf_test.online_subtitles.cloudflare")
    first = response(b"page")
    first.headers["Set-Cookie"] = "session=abc; Path=/; Secure"
    opener = Opener(first, response(b'{"success":true}'), response(b"PKzip"))
    monkeypatch.setattr(cf.urllib.request, "build_opener", lambda *a: opener)
    client = clients.OnlinePageClient(bypass_url="http://nas:18089")
    assert client.get_text("https://subhd.me/a/abc")[2] == "https://subhd.me/a/abc"
    assert client.post_json("https://subhd.me/api/sub/down?test=1", {"sid": "abc"}, referer="https://subhd.me/down/abc")["success"]
    client.get_bytes("https://cdn.example/sub.zip")
    post = opener.calls[1]
    assert post[0] == "POST"
    assert json.loads(post[1]) == {"sid": "abc"}
    assert post[2]["X-hostname"] == "subhd.me"
    assert post[2]["Cookie"] == "session=abc"
    assert post[2]["Origin"] == "https://subhd.me"
    assert post[2]["Sec-fetch-site"] == "same-origin"
    assert post[3] == "http://nas:18089/api/sub/down?test=1"
    assert "Cookie" not in opener.calls[2][2]


def test_mirror_redirect_updates_origin(clients, monkeypatch):
    cf = importlib.import_module("cf_test.online_subtitles.cloudflare")
    redirect = response(status=302)
    redirect.headers["Location"] = "https://subhd.me/a/abc"
    opener = Opener(redirect, response(b"page"))
    monkeypatch.setattr(cf.urllib.request, "build_opener", lambda *a: opener)
    client = clients.OnlinePageClient(bypass_url="http://nas:18089")
    assert client.get_text("https://subhd.tv/a/abc")[2] == "https://subhd.me/a/abc"
    assert opener.calls[1][2]["X-hostname"] == "subhd.me"


def test_subhd_download_uses_redirected_domain(clients):
    provider_module = importlib.import_module("cf_test.online_subtitles.providers.subhd")
    class Fetcher:
        def get_text(self, url, **kwargs):
            if "/a/" in url:
                return 200, '<button data-sid="abc">Download</button>', "https://subhd.me/a/abc"
            assert url == "https://subhd.me/down/xyz"
            return 200, "download page", url
        def post_json(self, url, payload, **kwargs):
            assert url.startswith("https://subhd.me/api/")
            if "prepare-download" in url:
                return {"success": True, "url": "/down/xyz"}
            return {"success": True, "url": "/file.zip"}
        def get_bytes(self, url, **kwargs):
            assert url == "https://subhd.me/file.zip"
            return "file.zip", b"PKzip", url
    provider = provider_module.SubHDProvider(Fetcher())
    assert provider.download({"page_url": "https://subhd.tv/a/abc"}) == ("file.zip", b"PKzip")
