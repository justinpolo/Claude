import argparse

import httpx

from sniper import cli
from sniper.config import DEFAULTS, load_config
from sniper.sources import DepopSource, VintedSource


def mock_sources(fixture_json, depop_status=200):
    def h(req):
        if req.url.path == "/":
            return httpx.Response(200, text="home")
        if req.url.host == "api.vinted.co.uk":
            return httpx.Response(200, json=fixture_json("vinted_catalog.json"))
        if depop_status != 200:
            return httpx.Response(depop_status, text="<html>no</html>")
        return httpx.Response(200, json=fixture_json("depop_v2.json"))
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    return lambda cfg: {"vinted": VintedSource({}, client=c), "depop": DepopSource({}, client=c)}


async def test_doctor_ok_and_blocked(monkeypatch, fixture_json, tmp_path, capsys):
    args = argparse.Namespace(watches=str(tmp_path / "none.yaml"))
    monkeypatch.setattr(cli, "build_sources", mock_sources(fixture_json))
    assert await cli.cmd_doctor(args, load_config(tmp_path / "x.yaml")) == 0
    out = capsys.readouterr().out
    assert "✅ vinted: 4 listings" in out and "✅ depop: 3 listings" in out
    monkeypatch.setattr(cli, "build_sources", mock_sources(fixture_json, depop_status=403))
    assert await cli.cmd_doctor(args, load_config(tmp_path / "x.yaml")) == 1
    assert "depop: BLOCKED" in capsys.readouterr().out


async def test_search_shows_verdicts(monkeypatch, fixture_json, tmp_path, capsys):
    monkeypatch.setattr(cli, "build_sources", mock_sources(fixture_json))
    args = argparse.Namespace(query="nike dunk", platform=["vinted"], max=50.0, min=None,
                              cond="good", size=["UK 9"], limit=10)
    await cli.cmd_search(args, load_config(tmp_path / "x.yaml"))
    out = capsys.readouterr().out
    assert "✅ [vinted] Nike Dunk Low Panda" in out
    assert "✗ price 120.0 > max 50.0" in out and "✗ condition fair" in out and "✗ size 'UK 3'" in out


async def test_test_notify_console(tmp_path, capsys):
    rc = await cli.cmd_test_notify(argparse.Namespace(), load_config(tmp_path / "x.yaml"))
    assert rc == 0 and "TEST ALERT" in capsys.readouterr().out


async def test_doctor_lists_every_depop_attempt(monkeypatch, fixture_json, tmp_path, capsys):
    def h(req):
        if req.url.host == "webapi.depop.com" and "/v3/" in req.url.path:
            return httpx.Response(404, json={"message": "not found"})
        if req.url.host == "webapi.depop.com":
            return httpx.Response(403, text="<html><title>Forbidden</title></html>",
                                  headers={"server": "awselb/2.0", "content-type": "text/html"})
        if req.url.host == "www.depop.com":
            return httpx.Response(403, text="<html><title>Just a moment...</title>challenge-platform</html>",
                                  headers={"server": "cloudflare", "cf-ray": "1", "cf-mitigated": "challenge",
                                           "content-type": "text/html"})
        return httpx.Response(200, text="home") if req.url.path == "/" else httpx.Response(200, json={"items": []})
    c = httpx.AsyncClient(transport=httpx.MockTransport(h))
    monkeypatch.setattr(cli, "build_sources", lambda cfg: {"depop": DepopSource({}, client=c)})
    rc = await cli.cmd_doctor(argparse.Namespace(watches=str(tmp_path / "n.yaml")), load_config(tmp_path / "x"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "v3/search/products/ -> HTTP 404" in out
    assert "v2/search/products/ -> HTTP 403 · text/html · server=awselb/2.0 · title='Forbidden'" in out
    assert "www.depop.com/search/ -> HTTP 403" in out and "=> Cloudflare bot challenge" in out
