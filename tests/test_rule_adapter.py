"""The rule interpreter (§6.4 Track 2): a rule DOES something, without being code."""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from crawler.adapters.rule import RuleAdapter
from crawler.fetch import Fetcher
from tests.test_extract import make_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def make_fetcher(pages: dict[str, object]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        body = pages.get(url)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, dict):  # JSON API response
            return httpx.Response(200, json=body)
        return httpx.Response(200, text=body)

    config = make_config()
    object.__setattr__(config, "request_delay_seconds", 0.0)
    return Fetcher(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


LIST_URL = "https://www.regione-esempio.it/bandi"


def test_html_rule_extracts_candidates():
    html = (FIXTURES / "regione_list.html").read_text()
    source = {
        "url": LIST_URL,
        "type": "rule",
        "extraction_rules": {"item_selector": "li.bando-item"},
    }
    result = RuleAdapter(make_fetcher({LIST_URL: html})).discover(source)
    assert len(result.candidates) == 3
    assert result.diagnostics["adapter"] == "rule"


def test_api_rule_extracts_from_json():
    api_url = "https://api.example.it/search"
    payload = {
        "results": [
            {"url": "https://api.example.it/g/1", "title": "Bando Uno"},
            {"url": "https://api.example.it/g/2", "title": "Bando Due"},
        ]
    }
    source = {
        "url": api_url,
        "type": "rule",
        "extraction_rules": {
            "results_path": "results",
            "item_url_path": "url",
            "item_title_path": "title",
            "request": {"method": "POST", "json_body": {"q": "*"}},
        },
    }
    result = RuleAdapter(make_fetcher({api_url: payload})).discover(source)
    assert len(result.candidates) == 2
    assert result.candidates[0].payload is not None  # carried for extraction


def test_invalid_rule_is_rejected_not_run():
    source = {"url": LIST_URL, "type": "rule", "extraction_rules": {"item_selector": "<script>x</script>"}}
    result = RuleAdapter(make_fetcher({LIST_URL: "<html></html>"})).discover(source)
    assert result.candidates == []
    assert "invalid extraction rule" in (result.error or "")


def test_missing_rule_errors_cleanly():
    source = {"url": LIST_URL, "type": "rule"}
    result = RuleAdapter(make_fetcher({})).discover(source)
    assert "no extraction_rules" in (result.error or "")


def test_rule_json_string_is_parsed():
    """extraction_rules arrives from the DB as a JSON string sometimes."""
    html = (FIXTURES / "regione_list.html").read_text()
    source = {
        "url": LIST_URL,
        "type": "rule",
        "extraction_rules": json.dumps({"item_selector": "li.bando-item"}),
    }
    result = RuleAdapter(make_fetcher({LIST_URL: html})).discover(source)
    assert len(result.candidates) == 3


def test_gate_still_applies_to_rule_output():
    """A rule that matches the nav is rejected by the gate, same as any selector."""
    html = (FIXTURES / "regione_list.html").read_text()
    source = {"url": LIST_URL, "type": "rule", "extraction_rules": {"item_selector": "nav.main-nav"}}
    result = RuleAdapter(make_fetcher({LIST_URL: html})).discover(source)
    assert result.candidates == []  # nav links gated out
