"""
tests/test_scraper_config.py
====================
scraper.py のモジュールレベル設定値（検索URL等）のテスト。
"""

from unittest.mock import patch

from bs4 import BeautifulSoup

import scraper
from scraper import (
    DEFAULT_URL,
    FALLBACK_PAGES,
    ITEMS_PER_PAGE,
    MAX_PAGES,
    _calculate_pages_needed,
    _extract_total_count,
    scrape,
)


class TestDefaultUrlSortOrder:

    def test_contains_new_listing_sort_params(self):
        # 新着・更新順(po=1&pj=2)を固定していること。
        # デフォルトの並び順（おすすめ順等）だと、総件数が多い日に
        # 固定ページ数のウィンドウから新着物件が漏れる事例が実際に
        # 確認された（稲城市の物件が170番目で圏外になったケース）
        # ための対策。
        assert "po=1" in DEFAULT_URL
        assert "pj=2" in DEFAULT_URL


class TestMaxPages:

    def test_max_pages_is_safety_cap_of_fifteen(self):
        # MAX_PAGESは「固定取得ページ数」ではなく「安全上限」に役割が
        # 変わっている（450件）。総件数から計算したページ数がこれを
        # 超えたら打ち切る。
        assert MAX_PAGES == 15

    def test_fallback_pages_is_ten(self):
        # 総件数が読み取れない場合のデフォルト（旧固定ページ数の実績値）
        assert FALLBACK_PAGES == 10

    def test_items_per_page_is_thirty(self):
        assert ITEMS_PER_PAGE == 30


# ---------------------------------------------------------------------------
# 総件数の自動検出・必要ページ数の計算（根本解決の核心）
# ---------------------------------------------------------------------------

def _make_soup_with_total_count(text: str) -> BeautifulSoup:
    """div.pagination_set-hit に指定テキストを入れたHTML片を作る。"""
    html = f'<div class="pagination_set-hit">{text}</div>'
    return BeautifulSoup(html, "html.parser")


class TestExtractTotalCount:

    def test_extracts_number_from_pagination_set_hit(self):
        soup = _make_soup_with_total_count("226 件")
        assert _extract_total_count(soup) == 226

    def test_extracts_number_with_comma(self):
        soup = _make_soup_with_total_count("1,234 件")
        assert _extract_total_count(soup) == 1234

    def test_missing_element_returns_none(self):
        soup = BeautifulSoup("<div>total件数なし</div>", "html.parser")
        assert _extract_total_count(soup) is None

    def test_element_without_number_returns_none(self):
        soup = _make_soup_with_total_count("件数不明")
        assert _extract_total_count(soup) is None


class TestCalculatePagesNeeded:

    def test_current_actual_total_count_226_needs_8_pages(self):
        # 実測値（226件）での計算結果を固定する回帰テスト。
        # 226 / 30 = 7.53... → 切り上げて8ページ
        assert _calculate_pages_needed(226) == 8

    def test_exact_multiple_of_items_per_page(self):
        # 300件 = 30件×10ページちょうど
        assert _calculate_pages_needed(300) == 10

    def test_one_over_multiple_rounds_up(self):
        # 301件 → 11ページに切り上げ
        assert _calculate_pages_needed(301) == 11

    def test_over_safety_cap_is_truncated_to_max_pages(self):
        # 1000件 → 34ページ相当だが、安全上限15ページで打ち切り
        assert _calculate_pages_needed(1000) == MAX_PAGES

    def test_exactly_at_safety_cap_boundary(self):
        # 450件 = 30件×15ページちょうど（上限と一致。打ち切りではない）
        assert _calculate_pages_needed(MAX_PAGES * ITEMS_PER_PAGE) == MAX_PAGES

    def test_none_falls_back_to_fallback_pages(self):
        # 総件数が読み取れない場合はFALLBACK_PAGES（例外を投げない）
        assert _calculate_pages_needed(None) == FALLBACK_PAGES

    def test_small_count_needs_only_one_page(self):
        assert _calculate_pages_needed(10) == 1


# ---------------------------------------------------------------------------
# scrape() 統合テスト: 総件数読み取りの成否に関わらず1ページ目は必ず保持される
# ---------------------------------------------------------------------------
#
# fetch_page / get_next_page_url / time.sleep をモックし、実際のSUUMO
# アクセスなしで scrape() の統合的なふるまいを検証する。

def _make_listing_card(name: str, url: str) -> str:
    """parse_listings が認識できる最小限の物件カードHTMLを作る。"""
    return f"""
    <div class="property_unit">
      <h2 class="property_unit-title"><a href="{url}">{name}</a></h2>
      <span class="dottable-value">4,500万円</span>
    </div>
    """


def _make_search_page_soup(names_urls: list[tuple[str, str]], total_count_text: str | None) -> BeautifulSoup:
    """検索結果ページ相当のHTMLを組み立てる。total_count_textがNoneなら総件数要素を含めない。"""
    cards = "".join(_make_listing_card(name, url) for name, url in names_urls)
    hit_html = f'<div class="pagination_set-hit">{total_count_text}</div>' if total_count_text else ""
    return BeautifulSoup(f"<html><body>{hit_html}{cards}</body></html>", "html.parser")


class TestScrapeKeepsFirstPageRegardlessOfTotalCountReadResult:

    def test_first_page_listings_kept_when_total_count_extraction_fails(self):
        """
        総件数の読み取りに失敗しても、1ページ目でパースした物件は
        必ず結果に含まれる（フォールバックの二重防御）。
        さらに、失敗時はFALLBACK_PAGES扱いになるため、2ページ目への
        「次へ」リンクがあれば追従されることも確認する。
        """
        page1_soup = _make_search_page_soup(
            [("物件A", "https://suumo.jp/test/a/")], total_count_text=None,  # 総件数要素なし＝読み取り失敗
        )
        page2_soup = _make_search_page_soup(
            [("物件B", "https://suumo.jp/test/b/")], total_count_text=None,
        )

        with patch("scraper.fetch_page", side_effect=[page1_soup, page2_soup]), \
             patch("scraper.get_next_page_url", side_effect=["https://suumo.jp/test/?page=2", None]), \
             patch("scraper.time.sleep"):
            results = scrape("https://suumo.jp/test/")

        urls = {l.url for l in results}
        # 1ページ目（総件数読み取り失敗の当該ページ）の物件が必ず含まれる
        assert "https://suumo.jp/test/a/" in urls
        # 読み取り失敗時はFALLBACK_PAGES(10)扱いのため、2ページ目も取得される
        assert "https://suumo.jp/test/b/" in urls

    def test_stops_early_when_total_count_indicates_fewer_pages(self):
        """
        総件数が読み取れて必要ページ数が少ないと分かれば、「次へ」リンクが
        あっても早期に打ち切る（無駄なアクセスをしない）。
        """
        # 1件しかないので必要ページ数は1（_calculate_pages_needed(1) == 1）
        page1_soup = _make_search_page_soup(
            [("物件A", "https://suumo.jp/test/a/")], total_count_text="1 件",
        )

        with patch("scraper.fetch_page", return_value=page1_soup) as mock_fetch, \
             patch("scraper.get_next_page_url", return_value="https://suumo.jp/test/?page=2") as mock_next, \
             patch("scraper.time.sleep"):
            results = scrape("https://suumo.jp/test/")

        assert len(results) == 1
        assert mock_fetch.call_count == 1  # 2ページ目は取得されない
        mock_next.assert_not_called()      # 「次へ」リンクの確認すら行われない
