import importlib.util
import importlib
import io
import json
import gzip
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
    log.logger.warning = lambda *a, **k: None
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
        if isinstance(request, str):
            request = urllib.request.Request(request)
        self.calls.append((request.get_method(), request.data, dict(request.header_items()), request.full_url))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def test_subhd_download_counts_use_search_response_and_keep_zero(clients):
    module = importlib.import_module("cf_test.online_subtitles.providers.subhd")
    first = '''
        <div class="clearfix"><a href="/a/abc">Tulsa King S04E05</a>
        <svg class="bi bi-file-earmark-fill"></svg><span>112</span>
        <svg class="bi bi-download"></svg><span>1,411</span></div>
        <div class="clearfix"><a href="/a/zero">Tulsa King S04E05</a>
        <svg class="bi bi-download"></svg><span>0</span></div>
        <div class="clearfix"><a href="/a/absent">Tulsa King S04E05</a>
        <svg class="bi bi-chat"></svg><span>99</span></div>
    '''
    pages = iter([first, first.replace("1,411", "1,427")])
    calls = []
    def get_text(url, **kwargs):
        calls.append(url)
        return 200, next(pages), url
    provider = module.SubHDProvider(types.SimpleNamespace(get_text=get_text), root_url="https://subhd.tv")
    for expected in (1411, 1427):
        results = {item.result_id: item.to_dict() for item in provider.search("Tulsa King S04E05", [], "episode")}
        assert results["abc"]["download_count"] == expected
        assert results["zero"]["download_count"] == 0
        assert "download_count" not in results["absent"]
        assert results["abc"]["page_url"] == "https://subhd.tv/a/abc"
        assert "match_detail" in results["abc"]
    assert len(calls) == 2 and all("/search/" in url for url in calls)


def test_subhd_download_count_column_uses_the_header_and_subtitle_id(clients):
    module = importlib.import_module("cf_test.online_subtitles.providers.subhd")
    html = '''
        <div class="rounded-top bg-purple"><div class="row">
          <div>字幕信息</div><div>上传人</div><div>下载次数</div>
        </div></div>
        <div class="row"><div><a href="/a/one">字幕一</a></div><div>99</div><div>1，629</div></div>
        <div class="row"><div><a href="/a/two">字幕二</a></div><div>100</div><div>—</div></div>
        <div class="row"><div><a href="/a/three">字幕三</a></div><div>101</div><div>0</div></div>
    '''
    assert module._subtitle_download_counts(html) == {"one": 1629, "three": 0}


@pytest.mark.parametrize("direct_body", [b"PK\x03\x04subtitle", b"<html>unavailable</html>", None])
def test_cdn_download_tries_direct_before_mirror(clients, monkeypatch, direct_body):
    client = clients.OnlinePageClient(use_proxy=False, bypass_url="http://nas:18089")
    client.user_agent = "Session UA"
    calls = []
    def fake_open(self, request, *, referer=""):
        calls.append((self.bypass_url, self.user_agent, referer, len(list(self.cookie_jar))))
        if not self.bypass_url:
            if direct_body is None:
                raise ValueError("direct unavailable")
            return response(direct_body, url=request.full_url)
        return response(b"PK\x03\x04fallback", url=request.full_url)
    monkeypatch.setattr(clients.OnlinePageClient, "_open", fake_open)
    result = client.get_bytes("https://dlus.subhd.me/sub.zip", referer="https://subhd.tv/down/abc")
    assert calls[0] == ("", "Session UA", "https://subhd.tv/down/abc", 0)
    assert len(calls) == (1 if direct_body and direct_body.startswith(b"PK") else 2)
    assert result[1].startswith(b"PK")


def test_same_host_download_keeps_mirror(clients, monkeypatch):
    client = clients.OnlinePageClient(bypass_url="http://nas:18089")
    def fake_open(self, request, *, referer=""):
        assert self is client
        return response(b"subtitle", url=request.full_url)
    monkeypatch.setattr(clients.OnlinePageClient, "_open", fake_open)
    assert client.get_bytes("https://subhd.tv/file", referer="https://subhd.tv/down/abc")[1] == b"subtitle"


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


@pytest.mark.parametrize("http_error", [False, True])
@pytest.mark.parametrize("reject_token", [False, True])
def test_turnstile_submits_fresh_token_once(clients, http_error, reject_token):
    provider_module = importlib.import_module("cf_test.online_subtitles.providers.subhd")
    class Fetcher:
        calls = []
        solves = []
        def get_text(self, url, **kwargs):
            if "/a/" in url:
                return 200, '<a href="/down/abc">Download</a>', url
            return 200, 'const turnstileConfig = {"siteKey":"public-key","required":false,"words":{}};', url
        def post_json(self, url, payload, **kwargs):
            self.calls.append(dict(payload))
            if len(self.calls) == 1 or reject_token:
                data = {"success": False, "pass": False, "turnstileRequired": True, "msg": "请先完成人机验证"}
                if http_error:
                    raise provider_module.OnlineJSONError(403, data)
                return data
            return {"success": True, "url": "/file.ass"}
        def solve_turnstile(self, url, **kwargs):
            self.solves.append((url, kwargs))
            return "single-use-token"
        def get_bytes(self, url, **kwargs):
            return "file.ass", b"[Script Info]\n", url
    fetcher = Fetcher()
    provider = provider_module.SubHDProvider(fetcher)
    if reject_token:
        with pytest.raises(provider_module.CaptchaRequiredError, match="未接受"):
            provider.download({"page_url": "https://subhd.me/a/abc"})
    else:
        assert provider.download({"page_url": "https://subhd.me/a/abc"})[0] == "file.ass"
    assert len(fetcher.calls) == 2
    assert len(fetcher.solves) == 1
    assert fetcher.solves[0] == ("https://subhd.me/down/abc", {"sitekey": "public-key", "action": "download"})
    assert "turnstileToken" not in fetcher.calls[0]
    assert fetcher.calls[1]["turnstileToken"] == "single-use-token"
    assert fetcher.calls[1]["cap"] == ""


def test_http_403_retains_structured_turnstile_error(clients):
    client = clients.OnlinePageClient()
    client.opener = Opener(response(b'{"turnstileRequired":true,"msg":"verify"}',403))
    with pytest.raises(clients.OnlineJSONError) as error:
        client.post_json("https://subhd.me/api/sub/down", {"sid":"abc"})
    assert error.value.status == 403
    assert error.value.payload["turnstileRequired"] is True


def test_old_mirror_cannot_claim_turnstile_support(clients, monkeypatch):
    cf = importlib.import_module("cf_test.online_subtitles.cloudflare")
    opener = Opener(response(b'{"info":{"version":"2.0.0"},"paths":{"/{path}":{"post":{}}}}'))
    monkeypatch.setattr(cf.urllib.request,"build_opener",lambda *a:opener)
    with pytest.raises(ValueError, match="2.0.0 不支持 /turnstile"):
        cf.request_turnstile("http://nas:18089","https://subhd.me/down/abc",sitekey="key",action="download",proxy=None,timeout=120)
    assert len(opener.calls) == 1


def test_turnstile_service_receives_page_action_and_proxy(clients, monkeypatch):
    cf = importlib.import_module("cf_test.online_subtitles.cloudflare")
    opener = Opener(response(b'{"paths":{"/turnstile":{"post":{}}}}'), response(b'{"token":"fresh","user_agent":"Browser UA"}'))
    monkeypatch.setattr(cf.urllib.request,"build_opener",lambda *a:opener)
    result=cf.request_turnstile("http://nas:18089","https://subhd.me/down/abc",sitekey="key",action="download",proxy={"server":"http://proxy:7890"},timeout=120)
    assert result["token"] == "fresh"
    assert json.loads(opener.calls[1][1]) == {"url":"https://subhd.me/down/abc","sitekey":"key","action":"download","proxy":"http://proxy:7890"}


def test_mirror_decompresses_html_and_keeps_application_error(clients,monkeypatch):
    cf = importlib.import_module("cf_test.online_subtitles.cloudflare")
    compressed=response(gzip.compress(b'<html>search results</html>'))
    compressed.headers['Content-Encoding']='gzip'
    opener=Opener(compressed,response(b'{"turnstileRequired":true}',403))
    monkeypatch.setattr(cf.urllib.request,"build_opener",lambda *a:opener)
    client=clients.OnlinePageClient(bypass_url="http://nas:18089")
    assert client.get_text("https://subhd.me/search/test")[1] == '<html>search results</html>'
    with pytest.raises(clients.OnlineJSONError) as error:
        client.post_json("https://subhd.me/api/sub/down",{})
    assert error.value.payload['turnstileRequired'] is True


def test_search_500_is_not_a_zero_results_success(clients):
    provider_module=importlib.import_module("cf_test.online_subtitles.providers.subhd")
    fetcher=types.SimpleNamespace(get_text=lambda *a,**k:(500,'service unavailable',a[0]))
    provider=provider_module.SubHDProvider(fetcher)
    with pytest.raises(ValueError,match='HTTP 500'):
        provider._search_keyword('Tulsa King S04E05',[])


@pytest.mark.parametrize("status", [200, 404, 403, 429, 500, "network", "challenge"])
def test_search_direct_first_with_bounded_mirror_fallback(clients, monkeypatch, status):
    client = clients.OnlinePageClient(bypass_url="http://nas:18089", use_proxy=True)
    calls = []
    def mirror(*a, **k):
        calls.append("mirror")
        return 200, "mirror results", a[0]
    client.get_text = mirror
    class Direct:
        def __init__(self, **kwargs):
            assert kwargs == {"use_proxy": False, "timeout": 15, "browser_recovery": False}
        def get_text(self, url, **kwargs):
            calls.append("direct")
            if status == "network":
                raise ValueError("unavailable")
            if status == "challenge":
                raise clients.CaptchaRequiredError("challenge", verify_url=url)
            return status, "direct results", url
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(clients, "OnlinePageClient", Direct)
    result = client.get_search_text("https://subhd.me/search/test")
    assert calls == (["direct", "closed"] if status in (200, 404) else ["direct", "closed", "mirror"])
    assert result[0] == (404 if status == 404 else 200)


def test_search_probe_does_not_launch_local_browser(clients, monkeypatch):
    client = clients.OnlinePageClient(browser_recovery=False)
    client.opener = Opener(response(b"challenge", challenge=True))
    monkeypatch.setattr(clients, "browser_session", lambda *a, **k: pytest.fail("unexpected local browser"))
    with pytest.raises(clients.CaptchaRequiredError):
        client.get_text("https://subhd.me/search/test")



def test_remote_subhd_does_not_inherit_mp_proxy(clients):
    service_module=importlib.import_module("cf_test.online_subtitles.service")
    service=service_module.OnlineSubtitleSearchService(use_proxy=True,subhd_bypass_url="http://nas:18089")
    assert service.fetcher.use_proxy is True
    assert service.providers["subhd"].fetcher.use_proxy is False
    assert service.providers["subhd"].fetcher.bypass_url == "http://nas:18089"
    assert service.providers["subtitlecat"].fetcher.use_proxy is True
    direct=service_module.OnlineSubtitleSearchService(use_proxy=True)
    assert direct.providers["subhd"].fetcher is direct.fetcher
    assert direct.providers["subhd"].fetcher.use_proxy is True


@pytest.mark.parametrize("outcome", ["success", "challenge", "http_error"])
def test_download_session_switch_isolated_and_bounded(clients, monkeypatch, outcome):
    mod = importlib.import_module("cf_test.online_subtitles.providers.subhd")
    shared = types.SimpleNamespace(bypass_url="http://nas:18089", timeout=20)
    made, calls = [], []
    class Client:
        def __init__(self, **kwargs):
            self.bypass_url = kwargs.get("bypass_url", "")
            self.closed = False
            self.cookies = []
            assert kwargs["use_proxy"] is False
            made.append(self)
        def close(self):
            self.closed = True
    def session(self, result, code=""):
        calls.append(self.fetcher.bypass_url)
        assert self.fetcher.cookies == []
        self.fetcher.cookies.append("session")
        if not self.fetcher.bypass_url:
            if outcome == "challenge":
                raise mod.CaptchaRequiredError("verification", verify_url=result["page_url"])
            if outcome == "http_error":
                raise ValueError("HTTP 500")
        return "test.ass", b"subtitle"
    monkeypatch.setattr(mod, "OnlinePageClient", Client)
    monkeypatch.setattr(mod.SubHDProvider, "_download_session", session)
    provider = mod.SubHDProvider(shared)
    if outcome == "http_error":
        with pytest.raises(ValueError, match="HTTP 500"):
            provider.download({"page_url": "https://subhd.me/a/test"})
    else:
        assert provider.download({"page_url": "https://subhd.me/a/test"}) == ("test.ass", b"subtitle")
    assert calls == (["", "http://nas:18089"] if outcome == "challenge" else [""])
    assert all(c.closed for c in made)
    assert provider.fetcher is shared
