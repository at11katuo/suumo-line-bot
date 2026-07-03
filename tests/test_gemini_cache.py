"""
tests/test_gemini_cache.py
====================
gemini_cache.py の単体テスト（Gemini評価結果の永続化）。

全テストは tmp_path の一時DBを使い、実DBファイル（evaluations.db）には
一切触れない（db_path を明示的に渡すため）。
"""

from pathlib import Path

import pytest

from gemini_cache import load_gemini_evaluations, save_gemini_evaluation


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


class TestSaveAndLoadGeminiEvaluation:

    def test_save_then_load_returns_same_score_and_text(self, db_path):
        url = "https://suumo.jp/test/gemini-1/"
        save_gemini_evaluation(url, 1, "★☆☆☆☆ (1/5)\n懸念点：バス便", db_path=db_path)

        result = load_gemini_evaluations([url], db_path=db_path)
        assert result[url] == (1, "★☆☆☆☆ (1/5)\n懸念点：バス便")

    def test_load_missing_url_not_in_result(self, db_path):
        save_gemini_evaluation("https://suumo.jp/test/known/", 2, "text", db_path=db_path)
        result = load_gemini_evaluations(["https://suumo.jp/test/unknown/"], db_path=db_path)
        assert "https://suumo.jp/test/unknown/" not in result

    def test_load_empty_urls_returns_empty_dict(self, db_path):
        assert load_gemini_evaluations([], db_path=db_path) == {}

    def test_load_missing_db_returns_empty_dict(self, tmp_path):
        result = load_gemini_evaluations(["https://x/1/"], db_path=tmp_path / "no.db")
        assert result == {}

    def test_save_overwrites_on_same_url(self, db_path):
        # 同じURLに2回保存すると、最新の内容で上書きされる（UPSERT）
        url = "https://suumo.jp/test/gemini-2/"
        save_gemini_evaluation(url, 1, "古い評価", db_path=db_path)
        save_gemini_evaluation(url, 4, "新しい評価", db_path=db_path)

        result = load_gemini_evaluations([url], db_path=db_path)
        assert result[url] == (4, "新しい評価")

    def test_batch_load_multiple_urls(self, db_path):
        url_a = "https://suumo.jp/test/gemini-a/"
        url_b = "https://suumo.jp/test/gemini-b/"
        save_gemini_evaluation(url_a, 1, "評価A", db_path=db_path)
        save_gemini_evaluation(url_b, 5, "評価B", db_path=db_path)

        result = load_gemini_evaluations([url_a, url_b], db_path=db_path)
        assert result[url_a] == (1, "評価A")
        assert result[url_b] == (5, "評価B")

    def test_score_zero_api_failure_case_is_saved(self, db_path):
        # Gemini API失敗時の score=0, eval_text="" も正しく保存・取得できる
        url = "https://suumo.jp/test/gemini-fail/"
        save_gemini_evaluation(url, 0, "", db_path=db_path)
        result = load_gemini_evaluations([url], db_path=db_path)
        assert result[url] == (0, "")

    def test_save_does_not_raise_on_invalid_db_path(self, tmp_path):
        # 書き込めないパス（存在しないディレクトリ配下）を渡しても例外を出さない
        bad_path = tmp_path / "nonexistent_dir" / "test.db"
        save_gemini_evaluation("https://x/1/", 1, "text", db_path=bad_path)  # 例外が出なければOK

    def test_default_db_path_omitted_does_not_raise(self, monkeypatch, tmp_path):
        # db_path 省略時は呼び出し時点の gemini_cache.DB_PATH を使う
        # （関数定義時に固定値へ束縛されないことの確認。他モジュールと同じ設計）
        import gemini_cache
        tmp_db = tmp_path / "evaluations.db"
        monkeypatch.setattr(gemini_cache, "DB_PATH", tmp_db)

        save_gemini_evaluation("https://x/omitted/", 3, "text")  # db_path省略
        assert tmp_db.exists()
        result = load_gemini_evaluations(["https://x/omitted/"])  # db_path省略
        assert result["https://x/omitted/"] == (3, "text")
