"""
tests/test_summary_logs.py
====================
詳細取得サマリ・参考枠サマリのログ出力テスト。

【背景】
    本番実行で「詳細取得: 11件中0件成功」という趣旨の報告を受けたが、
    調査の結果コード上にそのような文言は存在せず、実際のActionsログにも
    詳細取得の失敗は記録されていなかった。母数（新着件数）と内訳
    （キャッシュ既存で対象外／新規fetch成功・失敗）が1行で分からない
    ログだったため、誤解が生じたと考えられる。

    この反省を踏まえ、scraper.main() に追加した集計ログ
    （[詳細取得サマリ] [参考枠サマリ]）が、母数と内訳を明示した形式で
    正しく出力されることを確認する。

全テストは main_env フィクスチャ（既存の test_main_integration.py /
test_reference_known_listings.py と同じ手法）で main() を実際に実行し、
capsys で標準出力を検証する。実DBファイルには一切触れない。
"""

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import detail_fetcher
import evaluator
import gemini_cache
import scraper
from build_curves import CurveBundle
from gemini_cache import save_gemini_evaluation
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
        url="https://suumo.jp/test/summary-1/",
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


class TestDetailFetchSummaryLog:

    def test_shows_denominator_and_breakdown_when_all_new_succeed(
        self, main_env, monkeypatch, capsys,
    ):
        # 新着2件、両方とも新規fetchで成功 → 母数と内訳が正しく出る
        # （横断重複グルーピングで誤って1件に集約されないよう、location を分ける）
        listing_a = make_listing(url="https://suumo.jp/test/summary-a/")
        listing_b = make_listing(url="https://suumo.jp/test/summary-b/", location="東京都府中市本町１")
        monkeypatch.setattr(scraper, "scrape", lambda url: [listing_a, listing_b])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))
        monkeypatch.setattr(
            detail_fetcher, "fetch_detail",
            lambda url, **kw: {"total_units": 50, "repair_fund_monthly": 10000.0},
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert "[詳細取得サマリ] 新着2件" in out
        assert "キャッシュ既存(対象外)0件" in out
        assert "新規fetch対象2件" in out
        assert "成功2件・失敗0件" in out

    def test_shows_fetch_failure_distinctly_from_success(
        self, main_env, monkeypatch,
    ):
        # 新着2件のうち1件は取得失敗(None) → 成功/失敗が分けて出る
        # （横断重複グルーピングで誤って1件に集約されないよう、location を分ける）
        listing_a = make_listing(url="https://suumo.jp/test/summary-c/")
        listing_b = make_listing(url="https://suumo.jp/test/summary-d/", location="東京都府中市本町１")
        monkeypatch.setattr(scraper, "scrape", lambda url: [listing_a, listing_b])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        call_urls = []
        def fake_fetch(url, **kw):
            call_urls.append(url)
            if url.endswith("summary-c/"):
                return {"total_units": 50, "repair_fund_monthly": 10000.0}
            return None  # 失敗
        monkeypatch.setattr(detail_fetcher, "fetch_detail", fake_fetch)

        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with patch("scraper.requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=200, text="OK")
                scraper.main()

        out = buf.getvalue()
        assert "[詳細取得サマリ] 新着2件" in out
        assert "新規fetch対象2件" in out
        assert "成功1件・失敗1件" in out

    def test_shows_cached_count_when_listing_already_registered(
        self, main_env, monkeypatch, capsys,
    ):
        # 新着1件だが detail_cache に既に登録済み → fetchせずキャッシュ既存扱い
        listing = make_listing(url="https://suumo.jp/test/summary-e/")
        db_path = main_env
        detail_fetcher.save_detail_cache(
            listing.url, {"total_units": 40, "repair_fund_monthly": 8000.0}, db_path=db_path,
        )

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        fetch_called = {"n": 0}
        def counting_fetch(url, **kw):
            fetch_called["n"] += 1
            return {"total_units": 999, "repair_fund_monthly": 999.0}
        monkeypatch.setattr(detail_fetcher, "fetch_detail", counting_fetch)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert "[詳細取得サマリ] 新着1件" in out
        assert "キャッシュ既存(対象外)1件" in out
        assert "新規fetch対象0件" in out
        assert fetch_called["n"] == 0  # 既存キャッシュがあるので再fetchされない

    def test_zero_new_listings_shows_zero_denominator_explicitly(
        self, main_env, monkeypatch, capsys,
    ):
        # 新着0件（全て既知）の日は、母数が「0件」であることが明示される
        # （かつて「11件中0件成功」と誤読された状況の再現・確認）
        listing = make_listing(url="https://suumo.jp/test/summary-f/")
        scraper.save_listings(scraper.DATA_FILE, [listing])  # 既知として登録

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        # 母数が明示されているため「新着0件」であることが一目で分かる
        assert "[詳細取得サマリ] 新着0件" in out


class TestReferenceSummaryLog:

    def test_shows_known_count_and_gemini_saved_breakdown(
        self, main_env, monkeypatch, capsys,
    ):
        db_path = main_env
        listing = make_listing(url="https://suumo.jp/test/summary-g/")
        scraper.save_listings(scraper.DATA_FILE, [listing])  # 既知物件として登録
        save_gemini_evaluation(listing.url, 1, "懸念点：バス便", db_path=db_path)

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert "[参考枠サマリ] 既知1件" in out
        assert "Gemini評価保存済み1件" in out
        assert "未保存0件" in out
        assert "うち4★未満1件" in out

    def test_shows_missing_gemini_evaluation_count_distinctly(
        self, main_env, monkeypatch, capsys,
    ):
        # gemini_evaluations が未登録の既知物件（機能デプロイ前からの既知物件を再現）
        # → 「未保存」件数として明示され、「対象外」という理由も分かる。
        #
        # ※ Gemini評価件数上限対応（優先評価）により、未登録の既知物件は
        #   GEMINI_EVAL_LIMIT_PER_RUN件までは自動的に評価されるようになった。
        #   「未保存」が残るケースを再現するため、上限を超える件数
        #   （9件、上限8件）を用意し、1件だけが未評価のまま残ることを確認する。
        listings = [
            make_listing(url=f"https://suumo.jp/test/summary-h{i}/", name=f"物件{i}")
            for i in range(scraper.GEMINI_EVAL_LIMIT_PER_RUN + 1)  # 9件
        ]
        scraper.save_listings(scraper.DATA_FILE, listings)  # Gemini評価は一度も保存されない

        monkeypatch.setattr(scraper, "scrape", lambda url: listings)
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        out = capsys.readouterr().out
        assert f"[参考枠サマリ] 既知{len(listings)}件" in out
        assert f"Gemini評価保存済み{scraper.GEMINI_EVAL_LIMIT_PER_RUN}件" in out
        assert "未保存1件" in out
        assert "gemini_evaluations未登録のため対象外" in out
        # 評価された8件は evaluate_listing のモックが score=0 を返すため
        # 全て4★未満（うち4★未満8件）になる
        assert f"うち4★未満{scraper.GEMINI_EVAL_LIMIT_PER_RUN}件" in out
