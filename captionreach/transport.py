import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LIMIT = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _ensure_local(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('默认实验请求只允许本机 HTTP')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL 不允许凭据或片段')
    return urllib.parse.urlunsplit(parsed)


def _read(response, limit):
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('单次响应超过接收限制')
    return response.status, response.headers.get_content_type(), body


def fetch(url, limit=DEFAULT_LIMIT):
    url = _ensure_local(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(url, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return _read(response, limit)


def fetch_range(url, start, end, limit=DEFAULT_LIMIT):
    """单个显式 HTTP Range 请求；206/200 都如实返回，由调用方核对区间。"""
    url = _ensure_local(url)
    request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return _read(response, limit)
