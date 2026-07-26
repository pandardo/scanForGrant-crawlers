"""Stage A's condensed page view (§6.4 rung 3).

The regression that matters: a page whose chrome (header, cookie banner, filter
sidebar) is bigger than the truncation window. If condensation is not aggressive
enough, the grant list falls off the end and Stage A truthfully reports "no list
on this page" — the UK Find-a-grant case, where a sidebar of ~60 checkboxes
pushed `ul.grants_list` past 18k chars and the source could never be scanned.
"""

from crawler.infer import _condense_html


def _page_with_heavy_chrome() -> str:
    checkboxes = "".join(
        f'<div class="govuk-checkboxes__item">'
        f'<input class="govuk-checkboxes__input" id="filter-{i}" name="filters.option.en-US" '
        f'type="checkbox" data-cy="cyFilterCheckbox{i}" value="{i}"/>'
        f'<label class="govuk-label govuk-checkboxes__label" for="filter-{i}" '
        f'data-cy="cyFilterLabel{i}">Filter option number {i} with a long name</label></div>'
        for i in range(300)
    )
    grants = "".join(
        f'<li id="Grant {i}"><h2 class="heading"><a class="link" href="/grants/grant-{i}">'
        f"Grant number {i}</a></h2><p>A description of grant {i}.</p></li>"
        for i in range(10)
    )
    return (
        "<html><head><title>Find a grant</title>"
        '<link rel="stylesheet" href="/style.css"/><script src="/app.js"></script></head>'
        f'<body><header class="site-header">chrome</header><form>{checkboxes}</form>'
        f'<ul class="grants_list">{grants}</ul></body></html>'
    )


class TestCondenseHtml:
    def test_list_survives_heavy_chrome_before_it(self):
        condensed = _condense_html(_page_with_heavy_chrome())
        assert "grants_list" in condensed
        assert 'href="/grants/grant-0"' in condensed

    def test_selector_usable_attributes_are_kept(self):
        condensed = _condense_html(_page_with_heavy_chrome())
        assert 'class="grants_list"' in condensed
        assert 'href="/grants/grant-3"' in condensed

    def test_selector_useless_attributes_are_dropped(self):
        condensed = _condense_html(_page_with_heavy_chrome())
        assert "data-cy" not in condensed
        assert "stylesheet" not in condensed

    def test_long_prose_is_stubbed(self):
        html = "<div><p>" + "x" * 500 + "</p></div>"
        condensed = _condense_html(html)
        assert "x" * 41 not in condensed

    def test_respects_max_chars(self):
        assert len(_condense_html(_page_with_heavy_chrome(), max_chars=1000)) <= 1000
