"""
tests/test_evaluator.py
evaluator.py の単体テスト。

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー・実DBファイル不要。
pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/

テストの構成:
    1. 正常系  — 評価結果が正しく1行保存される
    2. スキップ — Candidate が None / カーブなしの場合は保存されない
    3. 履歴    — 同日UPSERT・別日追加の挙動
"""

import json
import sqlite3
from pathlib import Path

import pytest

import build_curves
import evaluator
from evaluator import (
    DEFAULT_HOLD_YEARS,
    evaluate_and_save,
    get_listing_age_days,
    get_sashine_notified_aggressiveness,
    is_reference_notified,
    mark_reference_notified,
    mark_sashine_notified,
)
from scraper import Listing

# ---------------------------------------------------------------------------
# 共通フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    """
    全テストでキャッシュディレクトリを一時ディレクトリに差し替える。
    evaluate_and_save → get_curve → CACHE_DIR の経路で使われる。
    """
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    """全テストで USE_MOCK_REINFOLIB=1 を設定し、APIキー不要にする。"""
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture
def db_path(tmp_path) -> Path:
    """テストごとに独立した一時 SQLite ファイルを返す。"""
    return tmp_path / "test_evaluations.db"


# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

CHOFU_CODE = "13208"  # 調布市コード（TARGET_AREAS に登録済み）


def make_listing(**overrides) -> Listing:
    """標準的な物件データを持つ Listing を返す。上書きしたいフィールドだけ渡す。"""
    defaults = dict(
        name="テスト物件マンション101",
        price="4,200万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/99999/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


def _count_rows(db_path: Path) -> int:
    """evaluations テーブルの行数を返す。"""
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]


def _fetch_one(db_path: Path) -> sqlite3.Row:
    """evaluations テーブルの最初の1行を返す（sqlite3.Row 形式）。"""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM evaluations").fetchone()


# ---------------------------------------------------------------------------
# 1. 正常系: 評価結果が正しく保存されること
# ---------------------------------------------------------------------------

class TestNormal:
    """正常系テスト。"""

    def test_saves_one_row(self, db_path):
        # 1件の有効な物件を評価すると1行保存されること
        n = evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        assert n == 1
        assert _count_rows(db_path) == 1

    def test_returns_saved_count(self, db_path):
        # 戻り値が保存件数と一致すること
        listings = [
            make_listing(url="https://suumo.jp/test/1/"),
            make_listing(url="https://suumo.jp/test/2/"),
        ]
        n = evaluate_and_save(listings, CHOFU_CODE, db_path=db_path)
        assert n == 2
        assert _count_rows(db_path) == 2

    def test_listing_url_is_saved(self, db_path):
        # listing_url が正しく保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        row = _fetch_one(db_path)
        assert row["listing_url"] == "https://suumo.jp/test/99999/"

    def test_city_code_is_saved(self, db_path):
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        assert _fetch_one(db_path)["city_code"] == CHOFU_CODE

    def test_candidate_fields_are_saved(self, db_path):
        # suumo_adapter で変換したフィールドが正しく保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        row = _fetch_one(db_path)
        assert row["asking_price"]  == 42_000_000
        assert row["area_sqm"]      == 72.5
        assert row["building_year"] == 2018
        assert row["walk_minutes"]  == 6
        assert row["floor_plan"]    == "3LDK"

    def test_resale_score_is_saved(self, db_path):
        # estimate_resale の resale_score が保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        row = _fetch_one(db_path)
        assert row["resale_score"] is not None
        assert 0 <= row["resale_score"] <= 100

    def test_hold_years_is_default(self, db_path):
        # hold_years が DEFAULT_HOLD_YEARS で保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        assert _fetch_one(db_path)["hold_years"] == DEFAULT_HOLD_YEARS

    def test_notes_is_valid_json_array(self, db_path):
        # notes が JSON 配列文字列として保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        notes_raw = _fetch_one(db_path)["notes"]
        notes = json.loads(notes_raw)
        assert isinstance(notes, list)

    def test_current_fair_unit_price_is_saved(self, db_path):
        # current_fair_unit_price（適正㎡単価）が保存されること
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        val = _fetch_one(db_path)["current_fair_unit_price"]
        # モックカーブでカーブが生成されていれば None にはならない
        assert val is not None

    def test_evaluated_date_is_today_format(self, db_path):
        # evaluated_date が "YYYY-MM-DD" 形式で保存されること
        import re
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        date_str = _fetch_one(db_path)["evaluated_date"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)


# ---------------------------------------------------------------------------
# 2. スキップ系: 無効な物件・カーブ未取得は保存されないこと
# ---------------------------------------------------------------------------

class TestSkip:
    """スキップ系テスト。"""

    def test_invalid_price_is_skipped(self, db_path):
        # 価格未定の物件は Candidate=None → 保存されない
        n = evaluate_and_save([make_listing(price="価格未定")], CHOFU_CODE, db_path=db_path)
        assert n == 0
        assert _count_rows(db_path) == 0

    def test_empty_area_is_skipped(self, db_path):
        # 面積が空の物件はスキップ
        n = evaluate_and_save([make_listing(area="")], CHOFU_CODE, db_path=db_path)
        assert n == 0

    def test_empty_age_is_skipped(self, db_path):
        # 築年月が空の物件はスキップ
        n = evaluate_and_save([make_listing(age="")], CHOFU_CODE, db_path=db_path)
        assert n == 0

    def test_mixed_valid_invalid_saves_only_valid(self, db_path):
        # 有効・無効が混在するとき、有効な物件だけ保存されること
        listings = [
            make_listing(url="https://suumo.jp/test/valid/",   price="4,200万円"),
            make_listing(url="https://suumo.jp/test/invalid/", price="価格未定"),
        ]
        n = evaluate_and_save(listings, CHOFU_CODE, db_path=db_path)
        assert n == 1
        assert _count_rows(db_path) == 1

    def test_no_curve_skips_all(self, db_path, monkeypatch):
        # get_curve_bundle が None を返すとき → 全件スキップ、DB は空のまま
        monkeypatch.setattr(evaluator, "get_curve_bundle", lambda *a, **kw: None)
        n = evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path)
        assert n == 0
        # DBファイルが作られていないか、テーブルが空であること
        try:
            assert _count_rows(db_path) == 0
        except Exception:
            pass  # テーブル未作成の場合も OK

    def test_empty_listings_returns_zero(self, db_path):
        # 物件リストが空のときは 0 を返す
        n = evaluate_and_save([], CHOFU_CODE, db_path=db_path)
        assert n == 0


# ---------------------------------------------------------------------------
# 3. 履歴系: UPSERT と別日追加の挙動
# ---------------------------------------------------------------------------

class TestHistory:
    """履歴系テスト。同日UPSERT・別日追加の仕様を検証する。"""

    def test_same_day_same_listing_does_not_add_row(self, db_path):
        # 同日に同じ物件を2回評価しても1行のまま（UPSERT で上書き）
        evaluate_and_save(
            [make_listing()], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        evaluate_and_save(
            [make_listing()], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        assert _count_rows(db_path) == 1

    def test_different_day_adds_new_row(self, db_path):
        # 別の日に評価すると新しい行が追加される（履歴が積まれる）
        evaluate_and_save(
            [make_listing()], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        evaluate_and_save(
            [make_listing()], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-20",
        )
        assert _count_rows(db_path) == 2

    def test_same_day_upsert_updates_asking_price(self, db_path):
        # 同日の再評価で asking_price が最新値（値下げ後）に更新されること
        evaluate_and_save(
            [make_listing(price="4,200万円")], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        evaluate_and_save(
            [make_listing(price="4,000万円")], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        price = _fetch_one(db_path)["asking_price"]
        assert price == 40_000_000  # 最新の値（値下げ後）に更新されていること

    def test_different_listings_on_same_day(self, db_path):
        # 同日でも URL が異なれば別の行として保存される
        evaluate_and_save(
            [
                make_listing(url="https://suumo.jp/test/A/"),
                make_listing(url="https://suumo.jp/test/B/"),
            ],
            CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        assert _count_rows(db_path) == 2

    def test_history_tracks_price_drop(self, db_path):
        """
        値下げ追跡クエリの動作確認。
        1月10日: 4,200万 → 1月20日: 4,000万 の履歴から値下がりを検知できること。
        """
        evaluate_and_save(
            [make_listing(price="4,200万円")], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-10",
        )
        evaluate_and_save(
            [make_listing(price="4,000万円")], CHOFU_CODE, db_path=db_path,
            _evaluated_date="2026-01-20",
        )
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT asking_price FROM evaluations "
                "WHERE listing_url = ? ORDER BY evaluated_date ASC",
                ("https://suumo.jp/test/99999/",),
            ).fetchall()

        prices = [r[0] for r in rows]
        assert len(prices) == 2
        assert prices[0] == 42_000_000  # 1月10日
        assert prices[1] == 40_000_000  # 1月20日
        assert prices[0] > prices[1]    # 値下がりを確認


# ---------------------------------------------------------------------------
# 4. get_listing_age_days（観測開始からの経過日数）
# ---------------------------------------------------------------------------

class TestListingAgeDays:
    """
    get_listing_age_days の仕様検証。
    最古の evaluated_date から today までの日数を返す。
    履歴なし・DBなしは None（通知側で「行を出さない」判断に使う）。
    """

    URL = "https://suumo.jp/test/99999/"

    def test_multiple_history_returns_days_from_oldest(self, db_path):
        # 1/10・1/20 に評価履歴 → today=1/25 なら最古(1/10)からの15日を返す
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path,
                          _evaluated_date="2026-01-10")
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path,
                          _evaluated_date="2026-01-20")
        age = get_listing_age_days(self.URL, db_path=db_path, _today="2026-01-25")
        assert age == 15  # 1/10 → 1/25

    def test_first_seen_today_returns_zero(self, db_path):
        # 本日初めて評価された物件 → 0（呼び出し側で「本日はじめて確認」表示）
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path,
                          _evaluated_date="2026-01-10")
        age = get_listing_age_days(self.URL, db_path=db_path, _today="2026-01-10")
        assert age == 0

    def test_no_history_for_url_returns_none(self, db_path):
        # DB はあるが対象 URL の履歴がない → None
        evaluate_and_save([make_listing(url="https://suumo.jp/test/OTHER/")],
                          CHOFU_CODE, db_path=db_path, _evaluated_date="2026-01-10")
        age = get_listing_age_days(self.URL, db_path=db_path, _today="2026-01-25")
        assert age is None

    def test_missing_db_returns_none(self, tmp_path):
        # DB ファイル自体が存在しない → None（落ちない）
        age = get_listing_age_days(self.URL, db_path=tmp_path / "no.db",
                                   _today="2026-01-25")
        assert age is None


# ---------------------------------------------------------------------------
# 5. 指値候補の重複通知抑制（sashine_notifications テーブル。STEP4）
# ---------------------------------------------------------------------------

class TestSashineNotifications:
    """
    get_sashine_notified_aggressiveness / mark_sashine_notified の仕様検証。
    「同じ強気度なら再通知しない、強気度が変わったら再通知する」という
    抑制ロジックの土台となるテーブルなので、ここを厳密に固定する。
    """

    URL = "https://suumo.jp/test/sashine-1/"

    def test_never_notified_returns_none(self, db_path):
        # 一度も通知していない URL は None（初回として扱われる）
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) is None

    def test_missing_db_returns_none(self, tmp_path):
        # DB ファイル自体が存在しない場合も None（落ちない）
        result = get_sashine_notified_aggressiveness(self.URL, db_path=tmp_path / "no.db")
        assert result is None

    def test_mark_then_get_returns_same_aggressiveness(self, db_path):
        # 通知を記録した直後は、その強気度がそのまま返ること
        mark_sashine_notified(self.URL, "standard", db_path=db_path, _today="2026-01-10")
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) == "standard"

    def test_mark_twice_same_aggressiveness_overwrites_date_not_value(self, db_path):
        # 同じ強気度で2回記録しても、値は同じまま（UPSERTで上書き）
        mark_sashine_notified(self.URL, "mild", db_path=db_path, _today="2026-01-10")
        mark_sashine_notified(self.URL, "mild", db_path=db_path, _today="2026-01-20")
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) == "mild"

    def test_mark_escalation_updates_to_new_aggressiveness(self, db_path):
        # 強気度が上がったとき（mild→standard→aggressive）、記録が更新されること
        mark_sashine_notified(self.URL, "mild", db_path=db_path, _today="2026-01-10")
        mark_sashine_notified(self.URL, "standard", db_path=db_path, _today="2026-02-10")
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) == "standard"

        mark_sashine_notified(self.URL, "aggressive", db_path=db_path, _today="2026-03-10")
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) == "aggressive"

    def test_different_urls_tracked_independently(self, db_path):
        # URL ごとに別々に記録されること（他物件の記録に影響しない）
        url_a = "https://suumo.jp/test/sashine-a/"
        url_b = "https://suumo.jp/test/sashine-b/"
        mark_sashine_notified(url_a, "aggressive", db_path=db_path, _today="2026-01-10")
        mark_sashine_notified(url_b, "mild", db_path=db_path, _today="2026-01-10")
        assert get_sashine_notified_aggressiveness(url_a, db_path=db_path) == "aggressive"
        assert get_sashine_notified_aggressiveness(url_b, db_path=db_path) == "mild"

    def test_does_not_touch_evaluations_table(self, db_path):
        # sashine_notifications は evaluations テーブルとは独立した追加
        # であることの確認（既存データ・既存件数に影響しない）
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path,
                          _evaluated_date="2026-01-10")
        assert _count_rows(db_path) == 1

        mark_sashine_notified(self.URL, "standard", db_path=db_path, _today="2026-01-10")

        # evaluations テーブルの件数は変わらない
        assert _count_rows(db_path) == 1
        # sashine_notifications 側は正しく記録されている
        assert get_sashine_notified_aggressiveness(self.URL, db_path=db_path) == "standard"


# ---------------------------------------------------------------------------
# 6. 参考枠の重複通知抑制（reference_notifications テーブル）
# ---------------------------------------------------------------------------

class TestReferenceNotifications:
    """
    is_reference_notified / mark_reference_notified の仕様検証。
    参考枠は有望/非有望の二値判定のため、sashine の強気度のような段階が
    なく「一度通知したら以後ずっと抑制する」という単純な方式である。
    """

    URL = "https://suumo.jp/test/reference-1/"

    def test_never_notified_returns_false(self, db_path):
        assert is_reference_notified(self.URL, db_path=db_path) is False

    def test_missing_db_returns_false(self, tmp_path):
        result = is_reference_notified(self.URL, db_path=tmp_path / "no.db")
        assert result is False

    def test_mark_then_check_returns_true(self, db_path):
        mark_reference_notified(self.URL, db_path=db_path, _today="2026-01-10")
        assert is_reference_notified(self.URL, db_path=db_path) is True

    def test_mark_twice_still_notified(self, db_path):
        # 二回記録しても（同じURLの再通知が発生しても）通知済み状態は変わらない
        mark_reference_notified(self.URL, db_path=db_path, _today="2026-01-10")
        mark_reference_notified(self.URL, db_path=db_path, _today="2026-02-10")
        assert is_reference_notified(self.URL, db_path=db_path) is True

    def test_different_urls_tracked_independently(self, db_path):
        url_a = "https://suumo.jp/test/reference-a/"
        url_b = "https://suumo.jp/test/reference-b/"
        mark_reference_notified(url_a, db_path=db_path, _today="2026-01-10")
        assert is_reference_notified(url_a, db_path=db_path) is True
        assert is_reference_notified(url_b, db_path=db_path) is False

    def test_does_not_touch_evaluations_or_sashine_tables(self, db_path):
        # reference_notifications は evaluations・sashine_notifications とは
        # 独立した追加であることの確認
        evaluate_and_save([make_listing()], CHOFU_CODE, db_path=db_path,
                          _evaluated_date="2026-01-10")
        mark_sashine_notified("https://suumo.jp/test/other/", "standard",
                              db_path=db_path, _today="2026-01-10")

        mark_reference_notified(self.URL, db_path=db_path, _today="2026-01-10")

        assert _count_rows(db_path) == 1  # evaluations は変わらない
        assert get_sashine_notified_aggressiveness(
            "https://suumo.jp/test/other/", db_path=db_path
        ) == "standard"  # sashine_notifications も変わらない
        assert is_reference_notified(self.URL, db_path=db_path) is True
