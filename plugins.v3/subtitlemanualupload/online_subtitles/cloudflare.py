"""Lazy MoviePilot V3 browser recovery for HTTP challenge responses."""
from concurrent.futures import ThreadPoolExecutor
import time
import io
import urllib.request
import urllib.response
import urllib.error
from urllib.parse import urlparse, urljoin, quote


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mirror_request(service_url, request, cookie_jar, *, proxy, timeout):
    """Keep target cookies scoped to target domains, never to the NAS host."""
    endpoint = urlparse(service_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.query or endpoint.fragment:
        raise ValueError("CF 服务地址必须是有效的 HTTP/HTTPS 基础地址")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for _ in range(6):
        target = urlparse(request.full_url)
        if target.scheme != "https":
            raise ValueError("CF 转发仅支持 HTTPS 字幕地址")
        cookie_jar.add_cookie_header(request)
        headers = {k: v for k, v in request.header_items() if k.lower() not in {"host", "content-length"}}
        headers["x-hostname"] = target.netloc
        headers["Accept-Encoding"] = "identity"
        if proxy:
            server = urlparse(proxy["server"])
            auth = ""
            if proxy.get("username"):
                auth = quote(proxy["username"], safe="") + ":" + quote(proxy.get("password", ""), safe="") + "@"
            headers["x-proxy"] = server._replace(netloc=auth + server.netloc).geturl()
        url = service_url + (target.path or "/") + ("?" + target.query if target.query else "")
        forwarded = urllib.request.Request(url, data=request.data, headers=headers, method=request.get_method())
        try:
            response = opener.open(forwarded, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body, status, response_headers = response.read(), response.code, response.headers
        result = urllib.response.addinfourl(io.BytesIO(body), response_headers, request.full_url, status)
        cookie_jar.extract_cookies(result, request)
        if status in {301, 302, 303, 307, 308} and response_headers.get("Location"):
            redirect = urljoin(request.full_url, response_headers["Location"])
            method, data = request.get_method(), request.data
            if status == 303 or (status in {301, 302} and method == "POST"):
                method, data = "GET", None
            next_headers = {k: v for k, v in request.header_items() if k.lower() not in {"cookie", "host", "content-length"}}
            if urlparse(redirect).netloc != target.netloc:
                next_headers = {k: v for k, v in next_headers.items() if k.lower() != "authorization"}
            request = urllib.request.Request(redirect, data=data, headers=next_headers, method=method)
            result.close()
            continue
        if status >= 400 and not is_challenge(response_headers, body):
            result.close()
            raise urllib.error.HTTPError(request.full_url, status, "CF 服务或目标站请求失败", response_headers, io.BytesIO(b"CF service/upstream request failed"))
        result.peek_body = body
        return result
    raise ValueError("CF 转发重定向次数过多")


def is_challenge(headers, body: bytes) -> bool:
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    text = body[:65536].decode("utf-8", errors="ignore").lower()
    return "<" in text and (
        "window._cf_chl_opt" in text or "cf-chl-widget" in text
        or ("/cdn-cgi/challenge-platform/" in text and "<title>just a moment" in text)
    )


def browser_session(url, cookie_jar, *, proxy, timeout):
    # The synchronous browser API must be created/used/closed on one worker,
    # including when the caller is running inside an asyncio request handler.
    cookies = []
    for cookie in cookie_jar:
        if cookie.is_expired():
            continue
        item = dict(name=cookie.name, value=cookie.value, domain=cookie.domain,
                    path=cookie.path or "/", secure=cookie.secure)
        if cookie.expires:
            item["expires"] = cookie.expires
        cookies.append(item)

    def recover():
        from app.sdk.browser import launch_browser_context

        options = {"headless": True}
        if proxy:
            options["proxy"] = proxy
        context = launch_browser_context(**options)
        try:
            context.set_default_timeout(max(1, timeout) * 1000)
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            deadline = time.monotonic() + max(1, timeout)
            response = page.goto(url, wait_until="domcontentloaded", timeout=max(1, timeout) * 1000)
            while is_challenge({}, page.content().encode("utf-8")):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Cloudflare challenge did not complete")
                page.wait_for_timeout(500)
            if response and response.status >= 400:
                # A successful challenge navigation may replace the initial 403.
                if not any(item["name"] == "cf_clearance" for item in context.cookies()):
                    raise ValueError("Browser verification page unavailable")
            return context.cookies(), page.evaluate("navigator.userAgent")
        finally:
            context.close()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="subtitle-cf") as worker:
        return worker.submit(recover).result()
