"""
tests/test_gemini_eval_limit.py
====================
Gemini評価件数の上限・優先評価・data.csv早期保存のテスト。

【背景】
    新着41件が発生した日、Gemini無料枠のレート制限（1日20リクエスト）に
    抵触して429エラーが連鎖し、実行が異常に長時間化して手動キャンセル
    される事故が発生した。キャンセルのタイミングが data.csv 保存より
    前だったため、詳細取得・Gemini評価の成果が失われ、data.csv が
    空のまま再発するリスクを抱えた。

    この事故を受けて:
    1. GEMINI_EVAL_LIMIT_PER_RUN で1回のGemini評価件数に上限を設けた
    2. 上限を超えた分は評価しないが、data.csvには登録する
       （gemini_evaluations未登録＝次回の優先評価対象として自然に記録）
    3. data.csv保存をGemini評価ループより前に移動し、無条件で実行する
       ようにした（Geminiループが中断しても記録が失われないようにする）

全テストは main_env フィクスチャ（既存の test_summary_logs.py 等と
同じ手法）で main() を実際に実行する。実DBファイルには一切触れない。
"""

import csv
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import detail_fetcher
import evaluator
import gemini_cache
import scraper
from build_curves import CurveBundle
from gemini_cache import load_gemini_evaluations
from reinfolib_resale import DepreciationCurve
from scraper import Listing

_FIXED_CURVE = DepreciationCurve(
    median_unit_price={(11, 15): 700_000, (21, 25): 600_000},
    sample_count={(11, 15): 30, (21, 25): 25},
)

_BUILDING_YEAR = date.today().year - 13


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件",
        price="4,800万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/limit-1/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age=f"{_BUILDING_YEAR}年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """scraper.main() を安全に実行するための共通モック環境。"""
    db_path = tmp_path / "evaluations.db"
    monkeypatch.setattr(evaluator, "DB_PATH", db_path)
    monkeypatch.setattr(detail_fetcher, "DB_PATH", db_path)
    monkeypatch.setattr(gemini_cache, "DB_PATH", db_path)
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")

    monkeypatch.setattr(
        evaluator, "get_curve_bundle",
        lambda **kwargs: CurveBundle(city_curve=_FIXED_CURVE, district_curves={}),
    )
    monkeypatch.setattr(build_curves, "get_curve", lambda **kwargs: _FIXED_CURVE)

    monkeypatch.setattr(scraper, "DATA_FILE", str(tmp_path / "data.csv"))
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)

    return db_path


def _read_csv_urls(path: str) -> list[str]:
    with open(path, encoding="utf-8", newline="") as f:
        return [row["url"] for row in csv.DictReader(f)]


class TestGeminiEvalLimitConstant:

    def test_limit_is_eight(self):
        # 1日2回の定期実行 × 8件 = 16件で、Gemini無料枠(1日20件)に
        # 収まるよう設定している
        assert scraper.GEMINI_EVAL_LIMIT_PER_RUN == 8


class TestSaveListingsSurvivesGeminiLoopFailure:
    """
    キャンセル耐性の回帰テスト（最重要）。
    今回の事故の根本原因は「data.csv保存がGeminiループより後にあった」
    ことだった。Geminiループ中に何が起きても、data.csv保存は
    既にそれより前に完了していることを確認する。
    """

    def test_data_csv_saved_even_if_gemini_loop_raises(self, main_env, monkeypatch):
        listing = make_listing(url="https://suumo.jp/test/cancel-repro/")
        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])

        # 本来 evaluate_listing は例外を投げない設計だが、「Geminiループ中に
        # 何か予期しないことが起きた」場合の耐性を確認するため、あえて
        # 例外を発生させる。
        def raising_evaluate_listing(l):
            raise RuntimeError("Geminiループ中に起きた予期しない例外（テスト用）")
        monkeypatch.setattr(scraper, "evaluate_listing", raising_evaluate_listing)

        data_file = Path(scraper.DATA_FILE)
        assert not data_file.exists()

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            with pytest.raises(RuntimeError):
                scraper.main()  # Geminiループで例外→main()自体も中断する

        # main() が例外で中断しても、data.csv は既に保存されている
        assert data_file.exists()
        assert listing.url in _read_csv_urls(str(data_file))


class TestGeminiEvalLimitEnforced:

    def test_excess_new_listings_partially_evaluated(self, main_env, monkeypatch):
        # 新着が上限を超える場合、上限件数だけがGemini評価され、
        # 残りは gemini_evaluations に登録されない（＝次回優先評価対象）
        n = scraper.GEMINI_EVAL_LIMIT_PER_RUN + 3  # 上限より3件多い
        listings = [
            make_listing(url=f"https://suumo.jp/test/excess-{i}/", name=f"物件{i}")
            for i in range(n)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: listings)

        call_count = {"n": 0}
        def counting_evaluate(l):
            call_count["n"] += 1
            return (0, "")
        monkeypatch.setattr(scraper, "evaluate_listing", counting_evaluate)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        assert call_count["n"] == scraper.GEMINI_EVAL_LIMIT_PER_RUN

        db_path = main_env
        all_urls = [l.url for l in listings]
        saved = load_gemini_evaluations(all_urls, db_path=db_path)
        assert len(saved) == scraper.GEMINI_EVAL_LIMIT_PER_RUN

    def test_data_csv_registers_all_listings_regardless_of_eval_limit(self, main_env, monkeypatch):
        # 上限を超えて評価されなかった物件も、data.csvには全件登録される
        # （「宙ぶらりん」を防ぐという今回の要求の核心）
        n = scraper.GEMINI_EVAL_LIMIT_PER_RUN + 3
        listings = [
            make_listing(url=f"https://suumo.jp/test/registerall-{i}/", name=f"物件{i}")
            for i in range(n)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: listings)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        saved_urls = _read_csv_urls(scraper.DATA_FILE)
        for listing in listings:
            assert listing.url in saved_urls

    def test_warning_logged_when_limit_exceeded(self, main_env, monkeypatch, capsys):
        n = scraper.GEMINI_EVAL_LIMIT_PER_RUN + 3
        listings = [
            make_listing(url=f"https://suumo.jp/test/warn-{i}/", name=f"物件{i}")
            for i in range(n)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: listings)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert "[警告]" in out
        assert "上限" in out
        assert "3件は次回の実行に持ち越します" in out

    def test_no_warning_when_within_limit(self, main_env, monkeypatch, capsys):
        # 上限以内なら警告ログは出ない
        listings = [
            make_listing(url=f"https://suumo.jp/test/nowarn-{i}/", name=f"物件{i}")
            for i in range(3)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: listings)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert "次回の実行に持ち越します" not in out


class TestUnevaluatedListingsPrioritizedNextRun:

    def test_leftover_listings_evaluated_before_new_ones_next_run(self, main_env, monkeypatch):
        # 1回目: 上限より2件多い新着 → 2件が未評価のまま残る
        n = scraper.GEMINI_EVAL_LIMIT_PER_RUN + 2
        first_batch = [
            make_listing(url=f"https://suumo.jp/test/prio-{i}/", name=f"物件{i}")
            for i in range(n)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: first_batch)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 1回目

        db_path = main_env
        all_urls = [l.url for l in first_batch]
        saved_after_first = load_gemini_evaluations(all_urls, db_path=db_path)
        unevaluated_urls = {u for u in all_urls if u not in saved_after_first}
        assert len(unevaluated_urls) == 2  # 2件が未評価のまま残っている

        # 2回目: 新着1件を追加。「未評価2件 + 新着1件」が対象になり、
        # 上限(8件)に十分収まるので全部評価されるはず。
        new_listing = make_listing(url="https://suumo.jp/test/prio-new/", name="新着物件")
        second_batch = first_batch + [new_listing]
        monkeypatch.setattr(scraper, "scrape", lambda url: second_batch)

        call_order: list[str] = []
        def order_tracking_evaluate(l):
            call_order.append(l.url)
            return (0, "")
        monkeypatch.setattr(scraper, "evaluate_listing", order_tracking_evaluate)

        with patch("scraper.requests.post") as mock_post2:
            mock_post2.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 2回目

        # 前回未評価だった2件が、新着1件より先に評価されている（優先度順）
        assert len(call_order) == 3
        assert call_order[0] in unevaluated_urls
        assert call_order[1] in unevaluated_urls
        assert call_order[2] == new_listing.url

    def test_all_leftover_eventually_evaluated_without_duplicate_calls(self, main_env, monkeypatch):
        # 前回評価済みの物件が、2回目の実行で再度Gemini APIを呼ばれないこと
        # （冪等性。find_backfill_targets が保存済みURLを正しく除外するため）
        n = scraper.GEMINI_EVAL_LIMIT_PER_RUN + 2
        listings = [
            make_listing(url=f"https://suumo.jp/test/idempotent-{i}/", name=f"物件{i}")
            for i in range(n)
        ]
        monkeypatch.setattr(scraper, "scrape", lambda url: listings)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 1回目: 8件評価、2件は次回に持ち越し

        call_count = {"n": 0}
        def counting_evaluate(l):
            call_count["n"] += 1
            return (0, "")
        monkeypatch.setattr(scraper, "evaluate_listing", counting_evaluate)

        with patch("scraper.requests.post") as mock_post2:
            mock_post2.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 2回目: 持ち越し分の2件だけ評価されるはず

        assert call_count["n"] == 2  # 既に評価済みの8件は再度呼ばれない
