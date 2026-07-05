"""
tests/test_scraper_config.py
====================
scraper.py のモジュールレベル設定値（検索URL等）のテスト。
"""

from scraper import DEFAULT_URL


class TestDefaultUrlSortOrder:

    def test_contains_new_listing_sort_params(self):
        # 新着・更新順(po=1&pj=2)を固定していること。
        # デフォルトの並び順（おすすめ順等）だと、総件数が多い日に
        # MAX_PAGES=3(90件)から新着物件が漏れる事例が実際に確認された
        # （稲城市の物件が170番目で圏外になったケース）ための対策。
        assert "po=1" in DEFAULT_URL
        assert "pj=2" in DEFAULT_URL
