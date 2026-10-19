"""HTTPX sessions with shared proxy, cookie and timeout policy.

The small session boundary also adapts steampy's cookie API. All network I/O
uses HTTPX; no requests session or curl subprocess is used here.
"""
from collections import OrderedDict
from copy import copy
from http.cookiejar import Cookie
import os
import threading
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx


class Cookies(httpx.Cookies):
    """HTTPX cookie store with the extra methods required by steampy."""

    def set(self, name, value, domain="", path="/", *, secure=False, expires=None, **kwargs):
        self.jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=bool(domain), domain_initial_dot=domain.startswith('.'),
            path=path, path_specified=True, secure=secure, expires=expires,
            discard=expires is None, comment=None, comment_url=None,
            rest=kwargs.get('rest', {'HttpOnly': None}), rfc2109=False,
        ))

    def get_dict(self, domain=None, path=None):
        return {c.name: c.value for c in self.jar
                if (domain is None or c.domain == domain) and (path is None or c.path == path)}

    def set_cookie(self, cookie):
        self.jar.set_cookie(cookie)

    def clear_expired_cookies(self):
        self.jar.clear_expired_cookies()

    def clear(self, domain=None, path=None, name=None):
        self.jar.clear(domain, path, name)


def _timeout(value):
    if isinstance(value, tuple):
        connect, read = value
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    return value


def _scoped_cookies(cookies, url):
    """Bind imported dictionary cookies to their first destination.

    A cookie with an empty domain otherwise travels to every host and cannot
    be replaced/deleted by a server's host-scoped Set-Cookie response.
    Explicit cookie domains, paths and expiry remain authoritative.
    """
    target = httpx.URL(url)
    result = Cookies()
    for original in Cookies(cookies).jar:
        cookie = copy(original)
        if not cookie.domain:
            cookie.domain = target.host if '.' in target.host else target.host + '.local'
            cookie.domain_specified = False
            cookie.secure = target.scheme == 'https'
        result.set_cookie(cookie)
    return result


def _encode_body(kwargs, session_headers):
    """Preserve form bytes (notably steampy's base64 RSA password)."""
    data = kwargs.get('data')
    if isinstance(data, (str, bytes, bytearray)):
        kwargs['content'] = kwargs.pop('data')
    elif isinstance(data, Mapping) and data and not kwargs.get('files'):
        # HTTPX stringifies bytes as b'...' and booleans in JSON style. These
        # endpoints expect the previous URL-encoded form representation.
        pairs = [(key, item) for key, value in data.items()
                 for item in (value if isinstance(value, (list, tuple)) else [value])
                 if item is not None]
        kwargs.pop('data')
        kwargs['content'] = urlencode(pairs).encode('ascii')
        headers = httpx.Headers(kwargs.get('headers') or {})
        if 'Content-Type' not in headers and 'Content-Type' not in session_headers:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        kwargs['headers'] = headers


class Client:
    """Account-local HTTPX connection pools, isolated by proxy/TLS settings.

    Some public helpers select a proxy or verification setting per call.
    HTTPX binds these to a transport, so cache bounded, explicitly closed
    transports instead of rebuilding a client for every authenticated request.
    """

    def __init__(self, *, headers=None, cookies=None, verify=True, trust_env=True,
                 proxies=None, timeout=15, follow_redirects=True, max_redirects=20):
        self.headers = httpx.Headers({
            'User-Agent': f'python-httpx/{httpx.__version__}',
            'Accept': '*/*', 'Accept-Encoding': 'gzip, deflate', 'Connection': 'keep-alive',
        })
        self.headers.update(headers or {})
        self.cookies = Cookies(cookies)
        self.verify, self.trust_env = verify, trust_env
        self.proxies = dict(proxies or {})
        self.timeout, self.follow_redirects = timeout, follow_redirects
        self.max_redirects = max_redirects
        self._clients = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False

    def _client(self, verify, proxies):
        effective = {**self.proxies, **(proxies or {})}
        env = tuple((k, v) for k, v in sorted(os.environ.items())
                    if k.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'ssl_cert_file', 'ssl_cert_dir')) if self.trust_env else ()
        key = (verify, tuple(sorted(effective.items())), self.trust_env, self.max_redirects, env)
        if key in self._clients:
            self._clients.move_to_end(key)
            return self._clients[key]
        mounts = {}
        for scheme, proxy in effective.items():
            if scheme in ('no_proxy', 'no'):
                continue
            pattern = scheme if '://' in scheme else scheme + '://'
            mounts[pattern] = (httpx.HTTPTransport(proxy=proxy, verify=verify,
                               trust_env=self.trust_env, retries=0, http2=False) if proxy else None)
        client = httpx.Client(verify=verify, trust_env=self.trust_env, mounts=mounts,
                             http2=False, follow_redirects=self.follow_redirects,
                             max_redirects=self.max_redirects, timeout=_timeout(self.timeout))
        self._clients[key] = client
        if len(self._clients) > 4:
            _, old = self._clients.popitem(last=False)
            old.close()
        return client

    def request(self, method, url, *, verify=None, proxies=None, timeout=None,
                allow_redirects=None, follow_redirects=None, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError('HTTP client is closed')
            client = self._client(self.verify if verify is None else verify, proxies)
            client.headers = self.headers
            self.cookies = _scoped_cookies(self.cookies, url)
            client.cookies = self.cookies
            # HTTPX uses its client jar for redirects, not request cookies.
            # Keep overrides there for this call only; persist server updates
            # (including deletion) separately, even if a later redirect fails.
            client.cookies.update(_scoped_cookies(kwargs.pop('cookies', None), url))
            client.event_hooks['response'] = [self.cookies.extract_cookies]
            if follow_redirects is None:
                follow_redirects = self.follow_redirects if allow_redirects is None else allow_redirects
            _encode_body(kwargs, self.headers)
            try:
                return client.request(method, url, timeout=_timeout(self.timeout if timeout is None else timeout),
                                      follow_redirects=follow_redirects, **kwargs)
            finally:
                client.event_hooks['response'] = []
                client.cookies = self.cookies

    def build_request(self, method, url, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError('HTTP client is closed')
            client = self._client(self.verify, None)
            self.cookies = _scoped_cookies(self.cookies, url)
            client.headers, client.cookies = self.headers, self.cookies
            kwargs['cookies'] = _scoped_cookies(kwargs.get('cookies'), url)
            _encode_body(kwargs, self.headers)
            return client.build_request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, data=None, **kwargs):
        return self.request('POST', url, data=data, **kwargs)

    def close(self):
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def request(method, url, **kwargs):
    # HTTPX rebuilds redirect cookies from the client jar. Request-level
    # cookies alone disappear on the next hop (e.g. Steam market eligibility).
    # This helper owns an isolated session, so seed that jar before sending.
    cookies = kwargs.pop('cookies', None)
    with Client(cookies=cookies) as client:
        return client.request(method, url, **kwargs)


def get(url, **kwargs):
    return request('GET', url, **kwargs)


def post(url, **kwargs):
    return request('POST', url, **kwargs)
