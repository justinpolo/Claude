import httpx
import pytest

from sniper.models import Watch
from sniper.sources import BlockedError, DepopSource, SourceError, VintedSource


def W(**kw):
    return Watch.from_dict({"id": "w", "query": "nike dunk", **kw})


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


HOME_HTML = '<html><script>window.__x={"CSRF_TOKEN":"abc123-csrf-token"}</script></html>'
HOME_COOKIES = [("set-cookie", "access_token_web=tok123; Path=/; Domain=.vinted.co.uk"),
                ("set-cookie", "anon_id=anon-42; Path=/; Domain=.vinted.co.uk")]


async def test_vinted_modern_endpoint_parse_and_session(fixture_json):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/":
            assert req.url.host == "www.vinted.co.uk"
            return httpx.Response(200, text=HOME_HTML, headers=HOME_COOKIES)
        assert (req.url.host, req.url.path) == ("api.vinted.co.uk", "/svc-catalogue/items")
        assert "access_token_web=tok123" in req.headers["cookie"]
        assert req.headers["x-anon-id"] == "anon-42" and req.headers["x-csrf-token"] == "abc123-csrf-token"
        assert req.headers["origin"] == "https://www.vinted.co.uk" and req.headers["platform"] == "web"
        return httpx.Response(200, json=fixture_json("vinted_catalog.json"))

    src = VintedSource({"domain": "www.vinted.co.uk"}, client=client_for(handler))
    items = await src.search(W(max_price=50, min_condition="good"))
    assert len(items) == 4  # bogus item skipped, not crashed
    a = items[0]
    assert (a.id, a.price, a.total_price, a.currency) == ("5123456789", 45.0, 48.95, "GBP")
    assert a.condition == "very_good" and a.size == "UK 9" and a.brand == "Nike"
    assert a.image_url.endswith("full/1.jpeg") and a.created_ts and a.seller == "sneakerhead22"
    # legacy flat price + relative URL
    assert items[1].price == 18.0 and items[1].url == "https://www.vinted.co.uk/items/5123456700-nike-dunk-low-kids"
    assert items[2].condition == "new_with_tags" and items[2].image_url == ""
    q = calls[1].url.params
    assert q["order"] == "newest_first" and q["price_to"] == "50"
    assert q["attribute_ids[status]"] == "1,2,3,6"
    await src.search(W())
    assert [c.url.path for c in calls].count("/") == 1   # session cached


async def test_vinted_falls_back_to_legacy_endpoint(fixture_json):
    paths = []

    def handler(req):
        paths.append((req.url.host, req.url.path))
        if req.url.path == "/":
            return httpx.Response(200, text="home", headers=HOME_COOKIES)
        if req.url.path == "/svc-catalogue/items":
            return httpx.Response(404, json={})
        if req.url.params.get("search_text") == "nike dunk" and len(paths) <= 3:
            assert req.url.params.get_list("status_ids[]") == ["2"]
        return httpx.Response(200, json=fixture_json("vinted_catalog.json"))

    src = VintedSource({}, client=client_for(handler))
    assert len(await src.search(W(conditions=["very_good"]))) == 4
    await src.search(W())
    assert paths[-1] == ("www.vinted.co.uk", "/api/v2/catalog/items")
    assert paths.count(("api.vinted.co.uk", "/svc-catalogue/items")) == 1  # remembered


async def test_vinted_401_refreshes_session():
    state = {"api": 0, "home": 0}

    def handler(req):
        if req.url.path == "/":
            state["home"] += 1
            return httpx.Response(200, text="ok")
        state["api"] += 1
        if state["api"] == 1:
            return httpx.Response(401, json={"code": 100})
        return httpx.Response(200, json={"items": []})

    src = VintedSource({}, client=client_for(handler))
    assert await src.search(W()) == []
    assert state == {"api": 2, "home": 2}


@pytest.mark.parametrize("resp", [
    httpx.Response(403, text="Forbidden"),
    httpx.Response(429, text="slow down", headers={"retry-after": "120"}),
    httpx.Response(200, text="<!doctype html><title>Just a moment</title>",
                   headers={"content-type": "text/html"}),
])
async def test_vinted_blocked(resp):
    def handler(req):
        return httpx.Response(200, text="home") if req.url.path == "/" else resp
    src = VintedSource({"api": "modern"}, client=client_for(handler))
    with pytest.raises(BlockedError) as ei:
        await src.search(W())
    if resp.status_code == 429:
        assert ei.value.retry_after == 120


async def test_vinted_blocked_on_both_endpoints_raises_blocked():
    def handler(req):
        return httpx.Response(200, text="home") if req.url.path == "/" else httpx.Response(403)
    with pytest.raises(BlockedError):
        await VintedSource({}, client=client_for(handler)).search(W())


async def test_vinted_extra_params_both_styles():
    seen = {}

    def handler(req):
        if req.url.path == "/":
            return httpx.Response(200)
        seen[req.url.path] = {k: req.url.params.get_list(k) for k in req.url.params.keys()}
        return httpx.Response(200, json={"items": []})

    w = W(vinted={"catalog_ids": [1231, 79], "brand_ids": [53]}, min_condition="new")
    await VintedSource({"api": "modern"}, client=client_for(handler)).search(w)
    await VintedSource({"api": "legacy"}, client=client_for(handler)).search(w)
    m, l = seen["/svc-catalogue/items"], seen["/api/v2/catalog/items"]
    assert m["attribute_ids[catalog]"] == ["1231,79"] and m["attribute_ids[brand]"] == ["53"]
    assert m["attribute_ids[status]"] == ["1,6"]
    assert l["catalog_ids[]"] == ["1231", "79"] and l["brand_ids[]"] == ["53"]


def test_vinted_hosts_for_other_countries():
    s = VintedSource({"domain": "vinted.fr"})
    assert (s.base, s.api_base, s.locale) == ("https://www.vinted.fr", "https://api.vinted.fr", "fr-FR")


async def test_depop_v3_falls_back_to_v2(fixture_json):
    hits = []

    def handler(req):
        hits.append(req.url.path)
        if "/v3/" in req.url.path:
            return httpx.Response(404, json={"error": "nope"})
        q = req.url.params
        assert q["sort"] == "newlyListed" and q["what"] == "carhartt detroit"
        assert q["priceMax"] == "100" and q["conditions"] == "brand_new,used_excellent,used_like_new"
        return httpx.Response(200, json=fixture_json("depop_v2.json"))

    src = DepopSource({}, client=client_for(handler))
    w = Watch.from_dict({"id": "c", "query": "carhartt detroit", "max_price": 100,
                         "min_condition": "very_good"})
    items = await src.search(w)
    assert len(items) == 3
    a, b, c = items
    assert a.url == "https://www.depop.com/products/vintagefinds-carhartt-detroit-jacket-brown-1f3a/"
    assert (a.price, a.size, a.brand, a.condition, a.seller) == (85.0, "L", "Carhartt", "very_good", "vintagefinds")
    assert a.title == "Carhartt detroit jacket brown" and a.image_url.endswith("640.jpg")
    assert a.created_ts and b.price == 110.0 and b.condition == "like_new"
    assert c.size == "XL" and c.image_url == "https://m/3/p0.jpg" and "replica" in c.description
    # sticks to the working endpoint afterwards
    await src.search(w)
    assert hits[-1].startswith("/api/v2/") and hits.count("/api/v3/search/products/") == 1


async def test_depop_v3_shape(fixture_json):
    src = DepopSource({}, client=client_for(lambda r: httpx.Response(200, json=fixture_json("depop_v3.json"))))
    [p] = await src.search(W())
    assert (p.price, p.brand, p.condition, p.seller, p.image_url) == (
        180.0, "Arc'teryx", "new", "seller1", "https://m/a/p1.jpg")
    assert p.created_ts and p.created_ts > 1e9


async def test_depop_403_is_blocked():
    src = DepopSource({}, client=client_for(lambda r: httpx.Response(403, text="<html>Forbidden")))
    with pytest.raises(BlockedError):
        await src.search(W())


async def test_network_error_is_source_error():
    def boom(req):
        raise httpx.ConnectError("dns fail")
    src = DepopSource({}, client=client_for(boom))
    with pytest.raises(SourceError):
        await src.search(W())


async def test_depop_html_page_fallback(fixture_json):
    import json as _j
    page = ('<html><head></head><body><script id="__NEXT_DATA__" type="application/json">'
            + _j.dumps({"props": {"pageProps": {"dehydratedState": {"queries": [
                {"state": {"data": {"pages": [fixture_json("depop_v2.json")]}}}]}}}})
            + '</script></body></html>')
    hits = []

    def handler(req):
        hits.append(req.url.host + req.url.path)
        if req.url.host == "webapi.depop.com":
            return httpx.Response(410, json={"error": "gone"})
        assert req.url.params["q"] == "carhartt" and req.url.params["sort"] == "newlyListed"
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    src = DepopSource({}, client=client_for(handler))
    items = await src.search(Watch.from_dict({"id": "c", "query": "carhartt"}))
    assert [i.price for i in items] == [85.0, 110.0, 25.0]
    assert items[0].url.startswith("https://www.depop.com/products/vintagefinds-")
    await src.search(Watch.from_dict({"id": "c", "query": "carhartt"}))
    assert hits[-1] == "www.depop.com/search/" and hits.count("webapi.depop.com/api/v3/search/products/") == 1


async def test_depop_all_endpoints_dead():
    def handler(req):
        if req.url.host == "webapi.depop.com":
            return httpx.Response(404)
        return httpx.Response(200, text="<html><script>nothing</script></html>")
    with pytest.raises(SourceError) as ei:
        await DepopSource({}, client=client_for(handler)).search(W())
    assert "no product data" in str(ei.value)


async def test_depop_api_403_tries_page_then_reports_blocked(fixture_json):
    def handler(req):
        return httpx.Response(403, text="<html>Forbidden</html>", headers={"content-type": "text/html"})
    with pytest.raises(BlockedError):
        await DepopSource({}, client=client_for(handler)).search(W())

    import json as _j
    page = '<script type="application/json">' + _j.dumps(fixture_json("depop_v2.json")) + "</script>"

    def handler2(req):
        if req.url.host == "webapi.depop.com":
            return httpx.Response(403, text="<html>Forbidden</html>")
        return httpx.Response(200, text=page)
    assert len(await DepopSource({}, client=client_for(handler2)).search(W())) == 3
