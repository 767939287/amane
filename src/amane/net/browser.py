"""浏览器后端: 协议、按来源会话复用与失败映射.

``HttpClient.get_rendered`` 经 ``BrowserClient`` 协议调用; 本地引擎与外部 solver 服务可互换.
后端不感知爬虫层: 结果以 ``BrowserPageResult`` 返回, 失败语义由 ``RequestFailure`` 表达, 由
``HttpClient`` 统一分类. 协议中的 ``timeout`` 与 ``BrowserPool`` 的超时参数均为毫秒 (与 Playwright 一致).
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict
from urllib.parse import unquote, urlsplit

import structlog

from ..enums import BrowserBackendName, SolverProtocol
from .bsk import BskBackend
from .errors import FailureKind, FailureReason, RequestError, RequestFailure, classify_block
from .recording import skip_body_recording

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .http import WebClient

logger = structlog.get_logger()

BrowserPageResult = tuple[str | None, RequestFailure | None]
"""(html, failure): 成功时 failure 为 None, 失败时 html 为 None."""

# 挑战页轮询间隔与浏览器空闲关闭时限 (秒).
_CHALLENGE_POLL_S = 1.0
_IDLE_TIMEOUT_S = 600.0
# 同一后端的并发页数上限. 限速由 RateLimiters 承担, 这里只约束浏览器资源.
_MAX_CONCURRENCY = 2
# solver 的 HTTP 调用在服务端等待挑战之外留出的余量 (秒), 以及会话建立 / 销毁的调用时限.
_SOLVER_HTTP_SLACK_S = 10.0
_SOLVER_SESSION_TIMEOUT_S = 15.0
# 连续服务级失败 (连接失败 / 429 / 5xx) 达到次数后进入冷却, 期内直接拒绝, 不再发起请求.
_SOLVER_COOLDOWN_FAILURES = 3
_SOLVER_COOLDOWN_S = 300.0


class BrowserBackend(Protocol):
    """单个浏览器引擎."""

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> BrowserPageResult: ...

    async def close(self) -> None: ...


class BrowserClient(Protocol):
    """``HttpClient`` 依赖的浏览器通道: 按后端名解析实际引擎, 调用方不感知实现."""

    def resolve(self, override: BrowserBackendName | None) -> BrowserBackendName | None: ...

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        backend: BrowserBackendName | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> BrowserPageResult: ...


class BrowserPool:
    """按配置解析并缓存后端实例; 引擎惰性启动, ``off`` 不产生任何实例.

    来源覆盖优先于全局: ``SiteConfig.browser_backend`` 为 ``off`` 表示该来源显式禁用浏览器.
    solver 经注入的 HTTP 通道出站, 因此复用本实例的调用方在热重建后须调用 ``rebind_web_client``.
    """

    def __init__(
        self,
        *,
        default_backend: BrowserBackendName,
        solver_url: str,
        proxy: str | None,
        web_client: WebClient,
        timeout_ms: float,
        solver_protocol: SolverProtocol = SolverProtocol.FLARESOLVERR,
        bypass_proxy: str | None = None,
        trusted_hosts: Sequence[str] = (),
        bsk_path: str | None = None,
    ) -> None:
        self._default = default_backend
        self._solver_url = solver_url
        self._solver_protocol = solver_protocol
        self._bypass_proxy = bypass_proxy
        self._trusted_hosts = tuple(trusted_hosts)
        self._proxy = proxy
        self._web = web_client
        self._timeout_ms = timeout_ms
        self._bsk_path = bsk_path
        self._instances: dict[BrowserBackendName, BrowserBackend] = {}
        self._closed = False

    def resolve(self, override: BrowserBackendName | None) -> BrowserBackendName | None:
        """来源覆盖优先; 覆盖为 ``off`` 表示显式禁用, 结果为 None."""
        if override is not None:
            return None if override is BrowserBackendName.OFF else override
        return None if self._default is BrowserBackendName.OFF else self._default

    def rebind_web_client(self, web_client: WebClient) -> None:
        """热重建复用本实例时同步新的 HTTP 通道 (solver 经它出站)."""
        self._web = web_client

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        backend: BrowserBackendName | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> BrowserPageResult:
        name = self.resolve(backend)
        if name is None:
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser backend disabled")
        if self._closed:
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser pool closed")
        instance = self._instances.get(name)
        if instance is None:
            instance = self._create(name)
            self._instances[name] = instance
        return await instance.get_page(
            url,
            scope=scope,
            cookies=cookies,
            headers=headers,
            wait_for=wait_for,
            timeout=timeout if timeout is not None else self._timeout_ms,
        )

    def _create(self, name: BrowserBackendName) -> BrowserBackend:
        match name:
            case BrowserBackendName.PATCHRIGHT:
                return PatchrightBackend(proxy=self._proxy, default_timeout=self._timeout_ms)
            case BrowserBackendName.CAMOUFOX:
                return CamoufoxBackend(proxy=self._proxy, default_timeout=self._timeout_ms)
            case BrowserBackendName.BSK:
                if self._proxy:
                    logger.warning("bsk backend ignores global proxy", proxy=self._proxy)
                return BskBackend(bsk_path=self._bsk_path, default_timeout=self._timeout_ms)
            case BrowserBackendName.SOLVER:
                return SolverBackend(
                    web_provider=lambda: self._web,
                    url=self._solver_url,
                    default_timeout=self._timeout_ms,
                    protocol=self._solver_protocol,
                    bypass_proxy=self._bypass_proxy,
                    trusted_hosts=self._trusted_hosts,
                )
            case _:
                raise ValueError(f"unsupported browser backend: {name}")

    async def close(self) -> None:
        self._closed = True
        instances = list(self._instances.values())
        self._instances.clear()
        for instance in instances:
            await instance.close()


class _ChallengeUnresolved(Exception):
    """导航结束仍在挑战页: 由 ``_LocalBackend`` 转成带原因的失败."""


class _BackendClosed(Exception):
    """后端已关闭: 拒绝重新启动引擎."""


class _PageUnreadable(Exception):
    """等待期内未读到任何正文: 页面崩溃或已关闭, 与挑战未解决区分."""

    def __init__(self, error: BaseException | None) -> None:
        super().__init__(str(error) if error is not None else "page content unreadable")
        self.error = error


_WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class _Cookie(TypedDict, total=False):
    """playwright 系 ``SetCookieParam`` 的结构镜像.

    引擎是可选依赖, 协议不能直接引用其类型; 字段须与引擎定义保持一致才能结构匹配.
    """

    name: str
    value: str
    url: str | None
    domain: str | None
    path: str | None
    expires: float | None
    httpOnly: bool | None
    secure: bool | None
    sameSite: Literal["Lax", "None", "Strict"] | None
    partitionKey: str | None


class _PageLike(Protocol):
    """``_LocalBackend`` 使用的页面能力: 导航, 读取正文与等待选择器."""

    async def goto(self, url: str, *, timeout: float, wait_until: _WaitUntil) -> object: ...

    async def content(self) -> str: ...

    async def wait_for_selector(self, selector: str, *, timeout: float) -> object: ...

    async def close(self) -> None: ...


class _ContextLike(Protocol):
    """``_LocalBackend`` 使用的 context 能力: 注入 cookie / 请求头, 创建页面."""

    async def add_cookies(self, cookies: Sequence[_Cookie]) -> None: ...

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None: ...

    async def new_page(self) -> _PageLike: ...


class _BrowserLike(Protocol):
    """``_LocalBackend`` 使用的浏览器能力: 按来源创建独立 context."""

    async def new_context(self) -> _ContextLike: ...


class _LocalBackend(ABC):
    """playwright 系本地引擎共用: 会话复用, 挑战等待, 并发与空闲释放.

    每个来源一个 context: 同一来源的连续请求复用 clearance; 不同来源互不污染 cookie.
    返回给调用方的正文仍由 ``HttpClient`` 二次分类, 这里只等待挑战消解.
    """

    def __init__(self, *, proxy: str | None, default_timeout: float, idle_timeout: float = _IDLE_TIMEOUT_S) -> None:
        self._proxy = proxy
        self._default_timeout = default_timeout
        self._idle_timeout = idle_timeout
        self._stack: AsyncExitStack | None = None
        self._browser: _BrowserLike | None = None
        self._contexts: dict[str, _ContextLike] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._launch_lock = asyncio.Lock()
        self._active = 0
        self._idle_gen = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._closed = False

    @abstractmethod
    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        """启动浏览器, 并把退出清理压入 ``stack``."""

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> BrowserPageResult:
        effective = timeout if timeout is not None else self._default_timeout
        self._active += 1
        self._schedule_idle()
        try:
            async with self._semaphore:
                return await self._fetch(
                    url,
                    scope=scope,
                    cookies=cookies,
                    headers=headers,
                    wait_for=wait_for,
                    timeout=effective,
                )
        finally:
            self._active -= 1

    async def _fetch(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        wait_for: str | None,
        timeout: float,
    ) -> BrowserPageResult:
        page: _PageLike | None = None
        try:
            context = await self._context(scope)
            # 引擎启动 (含 camoufox 首次下载) 不计入单次渲染时限.
            deadline = time.monotonic() + timeout / 1000
            if cookies:
                context_cookies: list[_Cookie] = [
                    {"name": name, "value": value, "url": url} for name, value in cookies.items()
                ]
                await context.add_cookies(context_cookies)
            if headers:
                await context.set_extra_http_headers(dict(headers))
            page = await context.new_page()
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            content = await self._settle_challenge(page, deadline)
            if wait_for is not None:
                remaining = max(1.0, (deadline - time.monotonic()) * 1000)
                await page.wait_for_selector(wait_for, timeout=remaining)
                content = await page.content()
            return content, None
        except _BackendClosed:
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message="browser backend closed")
        except _PageUnreadable as exc:
            if exc.error is not None and _is_timeout(exc.error):
                return None, RequestFailure(kind=FailureKind.TIMEOUT, message=f"browser page unreadable: {url}")
            detail = "unknown" if exc.error is None else f"{type(exc.error).__name__}: {exc.error}"
            logger.error("browser page unreadable", url=url, error=detail)
            return None, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"browser page unreadable: {detail}")
        except _ChallengeUnresolved:
            return None, RequestFailure(
                kind=FailureKind.UNEXPECTED,
                message=f"cloudflare challenge not resolved: {url}",
                reason=FailureReason.CLOUDFLARE_CHALLENGE,
            )
        except Exception as exc:
            if _is_timeout(exc):
                return None, RequestFailure(kind=FailureKind.TIMEOUT, message=f"browser timeout: {url}")
            logger.error("browser page fetch failed", url=url, error=str(exc))
            return None, RequestFailure(
                kind=FailureKind.UNEXPECTED,
                message=f"browser error: {type(exc).__name__}: {exc}",
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.debug("browser page close failed (ignored)", url=url, error=str(exc))

    async def _settle_challenge(self, page: _PageLike, deadline: float) -> str:
        """轮询正文直到挑战消解; 超时抛 ``_ChallengeUnresolved`` 或 ``_PageUnreadable``.

        导航瞬间读正文可能撞上框架重建; 读失败不视为空页, 计入等待直到超时.
        整个等待期一次都没读到正文时抛 ``_PageUnreadable``, 页面崩溃不误报为挑战未解决.
        """
        read_ok = False
        last_error: BaseException | None = None
        while True:
            try:
                content: str | None = await page.content()
            except Exception as exc:
                content = None
                last_error = exc
            else:
                read_ok = True
            if content is not None and classify_block(content) is not FailureReason.CLOUDFLARE_CHALLENGE:
                return content
            if time.monotonic() >= deadline:
                if not read_ok:
                    raise _PageUnreadable(last_error)
                raise _ChallengeUnresolved
            await asyncio.sleep(_CHALLENGE_POLL_S)

    async def _ensure_browser(self) -> _BrowserLike:
        browser = self._browser
        if browser is not None:
            return browser
        async with self._launch_lock:
            if self._closed:
                raise _BackendClosed
            browser = self._browser
            if browser is None:
                stack = AsyncExitStack()
                try:
                    browser = await self._launch(stack)
                except BaseException:
                    await stack.aclose()
                    raise
                self._browser = browser
                self._stack = stack
        return browser

    async def _context(self, scope: str) -> _ContextLike:
        context = self._contexts.get(scope)
        if context is None:
            browser = await self._ensure_browser()
            context = await browser.new_context()
            self._contexts[scope] = context
        return context

    def _schedule_idle(self) -> None:
        if self._idle_timeout <= 0:
            return
        self._idle_gen += 1
        self._idle_task = asyncio.create_task(self._idle_close(self._idle_gen))

    async def _idle_close(self, gen: int) -> None:
        """代际替代取消: 已有请求时重新计时, 关闭途中被越过不会中断退出清理."""
        await asyncio.sleep(self._idle_timeout)
        if gen != self._idle_gen:
            return
        if self._active:
            self._schedule_idle()
            return
        await self._close_browser()

    async def _close_browser(self, *, force: bool = False) -> None:
        async with self._launch_lock:
            if self._active and not force:
                return
            self._contexts.clear()
            stack, self._stack = self._stack, None
            self._browser = None
            if stack is not None:
                await stack.aclose()

    async def close(self) -> None:
        self._closed = True
        self._idle_gen += 1
        task, self._idle_task = self._idle_task, None
        await self._close_browser(force=True)
        # 释放完成后取消残留的休眠任务; cleanup 只在锁内进行, 此刻不可能被打断.
        if task is not None and not task.done():
            task.cancel()


class PatchrightBackend(_LocalBackend):
    """patchright 驱动的系统 Chrome; ``AMANE_SHOW_BROWSER`` 置位时强制有头."""

    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        from patchright.async_api import async_playwright

        playwright = await stack.enter_async_context(async_playwright())
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=os.getenv("AMANE_SHOW_BROWSER") is None,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": self._proxy} if self._proxy else None,
        )
        stack.push_async_callback(browser.close)
        return browser


class _CamoufoxOptions(TypedDict):
    """``AsyncCamoufox`` 的启动参数."""

    headless: bool
    humanize: bool
    locale: str
    proxy: NotRequired[dict[str, str]]


class CamoufoxBackend(_LocalBackend):
    """stealth Firefox; 浏览器二进制在首次启动时下载."""

    async def _launch(self, stack: AsyncExitStack) -> _BrowserLike:
        from camoufox.async_api import AsyncCamoufox
        from playwright.async_api import Browser

        options: _CamoufoxOptions = {
            "headless": os.getenv("AMANE_SHOW_BROWSER") is None,
            "humanize": True,
            "locale": "ja-JP",
        }
        if self._proxy:
            options["proxy"] = {"server": self._proxy}
        browser = await stack.enter_async_context(AsyncCamoufox(**options))
        # 未启用 persistent_context, 返回值必为 Browser.
        if not isinstance(browser, Browser):
            raise RuntimeError("camoufox persistent context is not supported")
        return browser


class _SolverPage(TypedDict):
    """solver 归一化后的页面: 正文与最终落地地址 (白名单校验用)."""

    html: str
    final_url: str


class SolverBackend:
    """外部 CF 绕过服务: FlareSolverr 取 ``solution.response``, TRAWL 取 ``html``.

    FlareSolverr 按来源复用会话且同会话串行; TRAWL 无会话, cookies 经 ``Cookie`` 头传递.
    请求头由本后端转发, ``wait_for`` 不参与转发 (等待策略由 solver 决定).
    连续服务级失败达到阈值后进入冷却, 冷却期内直接拒绝, 不再发起请求.
    """

    def __init__(
        self,
        *,
        web_provider: Callable[[], WebClient],
        url: str,
        default_timeout: float,
        protocol: SolverProtocol = SolverProtocol.FLARESOLVERR,
        bypass_proxy: str | None = None,
        trusted_hosts: Sequence[str] = (),
    ) -> None:
        self._web_provider = web_provider
        self._url = url.rstrip("/")
        self._default_timeout = default_timeout
        self._protocol = protocol
        self._bypass_proxy = (bypass_proxy or "").strip() or None
        self._trusted_hosts = tuple(host for host in (raw.strip().lower() for raw in trusted_hosts) if host)
        self._sessions: set[str] = set()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    async def get_page(
        self,
        url: str,
        *,
        scope: str,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> BrowserPageResult:
        effective = timeout if timeout is not None else self._default_timeout
        async with self._semaphore, self._lock_for(scope):
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                return None, _solver_cooldown_failure(remaining)
            try:
                page = await self._fetch(url, scope=scope, timeout_ms=effective, cookies=cookies, headers=headers)
            except RequestError as exc:
                failure = exc.failure or RequestFailure(kind=FailureKind.UNEXPECTED, message=exc.message)
                self._note_failure(failure)
                return None, failure
            if not self._landing_allowed(page["final_url"]):
                failure = RequestFailure(
                    kind=FailureKind.UNEXPECTED,
                    message=f"bypass landed on untrusted host: {page['final_url']}",
                )
                self._note_service_failure()
                return None, failure
            self._note_success()
            return page["html"], None

    async def _fetch(
        self,
        url: str,
        *,
        scope: str,
        timeout_ms: float,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> _SolverPage:
        if self._protocol is SolverProtocol.TRAWL:
            return await self._fetch_trawl(url, timeout_ms=timeout_ms, cookies=cookies, headers=headers)
        return await self._fetch_flaresolverr(url, scope=scope, timeout_ms=timeout_ms, cookies=cookies, headers=headers)

    async def _fetch_flaresolverr(
        self,
        url: str,
        *,
        scope: str,
        timeout_ms: float,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> _SolverPage:
        await self._ensure_session(scope)
        data = await self._request_get(url, scope, timeout_ms, cookies, headers)
        if not _solver_ok(data):
            message = _solver_message(data)
            if _is_missing_session(message):
                self._sessions.discard(scope)
                await self._ensure_session(scope)
                data = await self._request_get(url, scope, timeout_ms, cookies, headers)
            if not _solver_ok(data):
                raise RequestError(self._url, _solver_failure(_solver_message(data)))
        solution = data.get("solution")
        if not isinstance(solution, dict):
            raise RequestError(
                self._url,
                RequestFailure(kind=FailureKind.UNEXPECTED, message="solver response has no solution"),
            )
        html = solution.get("response")
        if not isinstance(html, str):
            raise RequestError(
                self._url,
                RequestFailure(kind=FailureKind.UNEXPECTED, message="solver solution has no response"),
            )
        final_url = solution.get("url")
        return {"html": html, "final_url": final_url if isinstance(final_url, str) and final_url else url}

    async def _fetch_trawl(
        self,
        url: str,
        *,
        timeout_ms: float,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> _SolverPage:
        payload: dict[str, Any] = {"url": url, "maxTimeout": int(timeout_ms)}
        merged = dict(headers or {})
        if cookies:
            merged["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        if merged:
            payload["headers"] = merged
        if self._bypass_proxy:
            payload["proxy"] = self._bypass_proxy
        data = await self._post("/scrape", payload, timeout_s=timeout_ms / 1000 + _SOLVER_HTTP_SLACK_S)
        error = data.get("error")
        if isinstance(error, str) and error:
            raise RequestError(self._url, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"trawl error: {error}"))
        html = data.get("html")
        if not isinstance(html, str):
            raise RequestError(
                self._url,
                RequestFailure(kind=FailureKind.UNEXPECTED, message="trawl response has no html"),
            )
        final_url = data.get("url")
        return {"html": html, "final_url": final_url if isinstance(final_url, str) and final_url else url}

    def _landing_allowed(self, final_url: str) -> bool:
        if not self._trusted_hosts:
            return True
        try:
            host = (urlsplit(final_url).hostname or "").lower()
        except ValueError:
            return False
        return any(fnmatchcase(host, pattern) for pattern in self._trusted_hosts)

    def _note_failure(self, failure: RequestFailure) -> None:
        if _counts_toward_cooldown(failure):
            self._note_service_failure()

    def _note_service_failure(self) -> None:
        """服务自身不可用 (连接失败 / 429 / 5xx / 落地域名不符) 才计入冷却."""
        self._consecutive_failures += 1
        if self._consecutive_failures < _SOLVER_COOLDOWN_FAILURES:
            return
        self._cooldown_until = time.monotonic() + _SOLVER_COOLDOWN_S
        logger.warning(
            "cf bypass service cooling down",
            url=self._url,
            failures=self._consecutive_failures,
            cooldown_s=_SOLVER_COOLDOWN_S,
        )

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    def _lock_for(self, scope: str) -> asyncio.Lock:
        """会话级互斥: 一个会话只有一个浏览器标签, 并发请求会互相读到对方导航的正文."""
        lock = self._scope_locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[scope] = lock
        return lock

    async def _request_get(
        self,
        url: str,
        scope: str,
        timeout_ms: float,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "session": scope,
            "maxTimeout": int(timeout_ms),
        }
        if headers:
            payload["headers"] = dict(headers)
        if cookies:
            payload["cookies"] = [{"name": name, "value": value} for name, value in cookies.items()]
        if self._bypass_proxy:
            payload["proxy"] = _flaresolverr_proxy(self._bypass_proxy)
        return await self._post("/v1", payload, timeout_s=timeout_ms / 1000 + _SOLVER_HTTP_SLACK_S)

    async def _ensure_session(self, scope: str) -> None:
        if scope in self._sessions:
            return
        async with self._lock:
            if self._closed:
                raise self._closed_error()
            if scope in self._sessions:
                return
            data = await self._post(
                "/v1",
                {"cmd": "sessions.create", "session": scope},
                timeout_s=_SOLVER_SESSION_TIMEOUT_S,
            )
            message = _solver_message(data)
            # 同名会话已存在同样可用, 不视为失败.
            if not _solver_ok(data) and not _is_existing_session(message):
                raise RequestError(self._url, _solver_failure(message))
            if self._closed:
                # close 的销毁快照可能错过本次创建; 立即回收, 不写回 _sessions.
                await self._destroy_session(scope)
                raise self._closed_error()
            self._sessions.add(scope)

    def _closed_error(self) -> RequestError:
        return RequestError(self._url, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver backend closed"))

    async def _post(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        web = self._web_provider()
        # solver 响应内嵌目标页 HTML, 只记 meta 避免任务记录膨胀.
        with skip_body_recording():
            try:
                resp = await web.request(
                    "POST", f"{self._url}{path}", json=payload, use_proxy=False, timeout=timeout_s, max_attempts=1
                )
            except RequestError as exc:
                failure = exc.failure
                if failure is None or failure.kind is not FailureKind.HTTP_STATUS:
                    raise
                raise RequestError(
                    self._url,
                    RequestFailure(
                        kind=failure.kind,
                        message=_solver_http_message(failure.status),
                        status=failure.status,
                        reason=failure.reason,
                    ),
                ) from exc
        try:
            data = resp.json()
        except Exception as exc:
            raise RequestError(
                self._url,
                RequestFailure(kind=FailureKind.UNEXPECTED, message=f"solver response is not JSON: {exc}"),
            ) from exc
        if not isinstance(data, dict):
            raise RequestError(
                self._url, RequestFailure(kind=FailureKind.UNEXPECTED, message="solver response is not an object")
            )
        return data

    async def close(self) -> None:
        self._closed = True
        sessions = list(self._sessions)
        self._sessions.clear()
        await asyncio.gather(*(self._destroy_session(scope) for scope in sessions))

    async def _destroy_session(self, scope: str) -> None:
        try:
            await self._post("/v1", {"cmd": "sessions.destroy", "session": scope}, timeout_s=_SOLVER_SESSION_TIMEOUT_S)
        except Exception as exc:
            logger.debug("solver session destroy failed (ignored)", session=scope, error=str(exc))


def _solver_http_message(status: int | None) -> str:
    if status == 503:
        return "CF bypass service browser pool is starting, retry later"
    if status == 429:
        return "CF bypass service browser pool is saturated, retry later"
    return f"CF bypass service returned HTTP {status}"


def _solver_cooldown_failure(remaining: float) -> RequestFailure:
    return RequestFailure(
        kind=FailureKind.UNEXPECTED,
        message=f"CF bypass service cooling down, retry in {remaining:.0f}s",
    )


def _counts_toward_cooldown(failure: RequestFailure) -> bool:
    """只有服务自身不可用才计入冷却: 连接失败与 429/5xx; 挑战未解 / 302 等不在此列."""
    if failure.kind is FailureKind.CURL:
        return True
    if failure.kind is FailureKind.HTTP_STATUS and failure.status is not None:
        return failure.status == 429 or failure.status >= 500
    return False


def _flaresolverr_proxy(proxy: str) -> dict[str, str]:
    """FlareSolverr 只接受 ``{"url", "username"?, "password"?}`` 对象, 直接传字符串会被忽略."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    host = parts.hostname or ""
    if not host:
        return {"url": proxy}
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    obj = {"url": f"{parts.scheme or 'http'}://{netloc}"}
    if parts.username:
        obj["username"] = unquote(parts.username)
    if parts.password:
        obj["password"] = unquote(parts.password)
    return obj


def _solver_ok(data: dict[str, Any]) -> bool:
    return data.get("status") == "ok"


def _solver_message(data: dict[str, Any]) -> str:
    message = data.get("message")
    return message if isinstance(message, str) and message else "solver error"


def _solver_failure(message: str) -> RequestFailure:
    lower = message.lower()
    if "timeout" in lower:
        return RequestFailure(kind=FailureKind.TIMEOUT, message=message)
    if "challenge" in lower:
        return RequestFailure(kind=FailureKind.UNEXPECTED, message=message, reason=FailureReason.CLOUDFLARE_CHALLENGE)
    return RequestFailure(kind=FailureKind.UNEXPECTED, message=message)


def _is_missing_session(message: str) -> bool:
    lower = message.lower()
    return "session" in lower and ("not found" in lower or "does not exist" in lower or "unknown" in lower)


def _is_existing_session(message: str) -> bool:
    lower = message.lower()
    return "session" in lower and ("already" in lower or "exist" in lower)


def _is_timeout(exc: BaseException) -> bool:
    """playwright 的 TimeoutError 不继承内建异常, 且可选依赖不能顶层导入, 按类名识别."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError"
