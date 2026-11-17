from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from promise_monitor.api.client import (
    ApiError,
    ApiUnavailableError,
    PromiseTrackerClient,
    RateLimitedError,
    SchemaError,
)
from promise_monitor.config import ApiConfig, load_app_config
from tests.conftest import REPO_ROOT
from tests.fakes import fixture_handler, load_fixture


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def api_cfg() -> ApiConfig:
    return load_app_config(REPO_ROOT / "config").tunables.api


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


Make = Callable[[Callable[[httpx.Request], httpx.Response]], PromiseTrackerClient]


@pytest.fixture
def make(api_cfg: ApiConfig, fake_time: FakeTime) -> Iterator[Make]:
    clients: list[PromiseTrackerClient] = []

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> PromiseTrackerClient:
        c = PromiseTrackerClient(
            api_cfg,
            contact="admin@example.org",
            transport=httpx.MockTransport(handler),
            sleep=fake_time.sleep,
            clock=fake_time.clock,
            jitter=lambda: 0.0,
        )
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


def _seq(*responses: httpx.Response | Exception) -> Callable[[httpx.Request], httpx.Response]:
    it = iter(responses)

    def handler(_req: httpx.Request) -> httpx.Response:
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return handler


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, "message": "ok"})


def test_typed_endpoint_and_user_agent(make: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return fixture_handler()(req)

    page = make(handler).presidential()
    assert page.current_meter is not None
    ua = seen[0].headers["user-agent"]
    assert ua.startswith("AdvoKC-PromiseMonitor/1.0")
    assert "admin@example.org" in ua


def test_state_name_with_space_is_encoded(make: Make) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return _ok({"promises": [], "all_meters": []})

    make(handler).state("Akwa Ibom")
    assert "state/Akwa%20Ibom" in seen[0]


def test_throttle_spaces_requests(make: Make, fake_time: FakeTime, api_cfg: ApiConfig) -> None:
    c = make(lambda _r: _ok([]))
    c.get_data("a")
    c.get_data("b")
    c.get_data("c")
    assert fake_time.sleeps == [api_cfg.min_interval_seconds] * 2


def test_retries_5xx_then_succeeds(make: Make, fake_time: FakeTime) -> None:
    c = make(_seq(httpx.Response(502), httpx.Response(503), _ok([1])))
    assert c.get_data("x") == [1]
    assert c.request_count == 3
    backoffs = [s for s in fake_time.sleeps if s != 1.0]
    assert backoffs == [2.0, 4.0]  # base * 2**attempt, jitter 0


def test_timeouts_exhaust_retries(make: Make, api_cfg: ApiConfig) -> None:
    errors = [httpx.ConnectTimeout("t")] * (api_cfg.max_retries + 1)
    c = make(_seq(*errors))
    with pytest.raises(ApiUnavailableError):
        c.get_data("x")
    assert c.request_count == api_cfg.max_retries + 1


def test_429_short_retry_after_is_honoured(make: Make, fake_time: FakeTime) -> None:
    c = make(_seq(httpx.Response(429, headers={"Retry-After": "5"}), _ok("fine")))
    assert c.get_data("x") == "fine"
    assert 5.0 in fake_time.sleeps


def test_429_long_retry_after_stops(make: Make) -> None:
    c = make(_seq(httpx.Response(429, headers={"Retry-After": "3600"})))
    with pytest.raises(RateLimitedError):
        c.get_data("x")
    assert c.request_count == 1


def test_404_not_retried(make: Make) -> None:
    c = make(_seq(httpx.Response(404, json={"success": False})))
    with pytest.raises(ApiError) as ei:
        c.get_data("x")
    assert ei.value.status == 404
    assert not isinstance(ei.value, ApiUnavailableError)
    assert c.request_count == 1


def test_html_instead_of_json_is_schema_error(make: Make) -> None:
    html = httpx.Response(200, text="<html>admin</html>", headers={"content-type": "text/html"})
    with pytest.raises(SchemaError):
        make(_seq(html)).get_data("x")


def test_success_false_raises(make: Make) -> None:
    resp = httpx.Response(200, json={"success": False, "data": None, "message": "nope"})
    with pytest.raises(ApiError, match="nope"):
        make(_seq(resp)).get_data("x")


def test_shape_change_is_schema_error(make: Make) -> None:
    data = load_fixture("api_presidential.json")["data"]
    data["current_meter"]["meter_type"] = "something_new"
    with pytest.raises(SchemaError):
        make(_seq(_ok(data))).presidential()
