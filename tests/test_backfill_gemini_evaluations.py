"""
tests/test_backfill_gemini_evaluations.py
====================
backfill_gemini_evaluations.py の単体テスト。

既知物件のうち gemini_evaluations に未保存のものへ、一度だけ
Gemini評価を実行して保存する「バックフィル」機能を検証する。

全テストは一時DB・一時data.csv・モックした evaluate_listing を使う。
実DB・実Gemini APIには一切触れない。
"""

import csv
from pathlib import Path

import pytest

import backfill_gemini_evaluations as backfill
import gemini_cache
from gemini_cache import load_gemini_evaluations, save_gemini_evaluation
from scraper import Listing


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件",
        price="4,800万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/test/backfill-1/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age="2013年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> Path:
    """
    一時DBのパスを返す。あわせて gemini_cache.DB_PATH もこの一時パスに
    差し替える（autouse相当）。main() は db_path 引数を省略して
    find_backfill_targets/run_backfill を呼ぶため、これを差し替えないと
    テストが実DBに書き込んでしまう（過去に一度この漏れで事故を
    起こしているため、db_path フィクスチャ自体に組み込んで防止する）。
    """
    path = tmp_path / "test_evaluations.db"
    monkeypatch.setattr(gemini_cache, "DB_PATH", path)
    return path


@pytest.fixture
def data_csv(tmp_path) -> Path:
    return tmp_path / "test_data.csv"


def _write_csv(path: Path, listings: list[Listing]) -> None:
    from dataclasses import fields
    fieldnames = [f.name for f in fields(Listing)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(l.to_dict() for l in listings)


# ---------------------------------------------------------------------------
# 1. _load_all_known_listings: data.csv からの復元
# ---------------------------------------------------------------------------

class TestLoadAllKnownListings:

    def test_loads_listings_from_csv(self, data_csv):
        listings = [make_listing(url="https://suumo.jp/test/a/"),
                    make_listing(url="https://suumo.jp/test/b/")]
        _write_csv(data_csv, listings)

        result = backfill._load_all_known_listings(str(data_csv))
        assert len(result) == 2
        assert {l.url for l in result} == {"https://suumo.jp/test/a/", "https://suumo.jp/test/b/"}

    def test_missing_csv_returns_empty_list(self, tmp_path):
        result = backfill._load_all_known_listings(str(tmp_path / "no_such_file.csv"))
        assert result == []


# ---------------------------------------------------------------------------
# 2. find_backfill_targets: 未保存物件のみを対象にする（冪等性の核心）
# ---------------------------------------------------------------------------

class TestFindBackfillTargets:

    def test_all_unsaved_are_targets(self, db_path):
        listings = [make_listing(url="https://suumo.jp/test/a/"),
                    make_listing(url="https://suumo.jp/test/b/")]
        targets = backfill.find_backfill_targets(listings, db_path=db_path)
        assert len(targets) == 2

    def test_already_saved_is_excluded(self, db_path):
        listing_a = make_listing(url="https://suumo.jp/test/a/")
        listing_b = make_listing(url="https://suumo.jp/test/b/")
        save_gemini_evaluation(listing_a.url, 1, "懸念点：バス便", db_path=db_path)

        targets = backfill.find_backfill_targets([listing_a, listing_b], db_path=db_path)

        assert len(targets) == 1
        assert targets[0].url == listing_b.url

    def test_all_saved_returns_empty(self, db_path):
        listing = make_listing(url="https://suumo.jp/test/a/")
        save_gemini_evaluation(listing.url, 2, "text", db_path=db_path)

        targets = backfill.find_backfill_targets([listing], db_path=db_path)
        assert targets == []

    def test_empty_listings_returns_empty(self, db_path):
        assert backfill.find_backfill_targets([], db_path=db_path) == []


# ---------------------------------------------------------------------------
# 3. run_backfill: dry_run の安全性 と 本実行の動作
# ---------------------------------------------------------------------------

class TestRunBackfill:

    def test_dry_run_does_not_call_evaluate_listing(self, db_path, monkeypatch):
        call_count = {"n": 0}
        def fake_evaluate(listing):
            call_count["n"] += 1
            return (3, "text")
        monkeypatch.setattr(backfill, "evaluate_listing", fake_evaluate)

        listing = make_listing()
        saved = backfill.run_backfill([listing], dry_run=True, db_path=db_path, sleep_sec=0)

        assert saved == 0
        assert call_count["n"] == 0  # Gemini APIは一切呼ばれない

    def test_dry_run_does_not_write_to_db(self, db_path, monkeypatch):
        monkeypatch.setattr(backfill, "evaluate_listing", lambda l: (3, "text"))
        listing = make_listing()

        backfill.run_backfill([listing], dry_run=True, db_path=db_path, sleep_sec=0)

        result = load_gemini_evaluations([listing.url], db_path=db_path)
        assert listing.url not in result  # DBには何も保存されていない

    def test_real_run_calls_evaluate_listing_once_per_target(self, db_path, monkeypatch):
        call_count = {"n": 0}
        def fake_evaluate(listing):
            call_count["n"] += 1
            return (4, "★★★★☆ (4/5)")
        monkeypatch.setattr(backfill, "evaluate_listing", fake_evaluate)

        listings = [make_listing(url="https://suumo.jp/test/a/"),
                    make_listing(url="https://suumo.jp/test/b/")]
        saved = backfill.run_backfill(listings, dry_run=False, db_path=db_path, sleep_sec=0)

        assert saved == 2
        assert call_count["n"] == 2

    def test_real_run_saves_correct_score_and_text(self, db_path, monkeypatch):
        monkeypatch.setattr(backfill, "evaluate_listing",
                            lambda l: (1, "★☆☆☆☆ (1/5)\n懸念点：バス便"))
        listing = make_listing(url="https://suumo.jp/test/chofu-like/")

        backfill.run_backfill([listing], dry_run=False, db_path=db_path, sleep_sec=0)

        result = load_gemini_evaluations([listing.url], db_path=db_path)
        assert result[listing.url] == (1, "★☆☆☆☆ (1/5)\n懸念点：バス便")

    def test_empty_targets_real_run_calls_nothing(self, db_path, monkeypatch):
        call_count = {"n": 0}
        monkeypatch.setattr(backfill, "evaluate_listing",
                            lambda l: call_count.__setitem__("n", call_count["n"] + 1) or (0, ""))

        saved = backfill.run_backfill([], dry_run=False, db_path=db_path, sleep_sec=0)

        assert saved == 0
        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# 4. 冪等性: 2回実行してもAPI呼び出しが重複しない
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_second_run_has_zero_targets_and_zero_api_calls(self, db_path, monkeypatch):
        call_count = {"n": 0}
        def fake_evaluate(listing):
            call_count["n"] += 1
            return (2, "text")
        monkeypatch.setattr(backfill, "evaluate_listing", fake_evaluate)

        listings = [make_listing(url="https://suumo.jp/test/a/"),
                    make_listing(url="https://suumo.jp/test/b/")]

        # 1回目: 2件とも未保存 → 2件評価
        targets_1 = backfill.find_backfill_targets(listings, db_path=db_path)
        backfill.run_backfill(targets_1, dry_run=False, db_path=db_path, sleep_sec=0)
        assert call_count["n"] == 2

        # 2回目: 同じ2件を再度対象にしようとしても、既に保存済みなので対象は0件
        targets_2 = backfill.find_backfill_targets(listings, db_path=db_path)
        backfill.run_backfill(targets_2, dry_run=False, db_path=db_path, sleep_sec=0)

        assert targets_2 == []
        assert call_count["n"] == 2  # 2回目でAPI呼び出しは増えていない

    def test_partial_backfill_then_rerun_only_evaluates_remaining(self, db_path, monkeypatch):
        # 1件は事前に保存済み、もう1件は未保存 → 未保存の1件だけが対象になる
        listing_a = make_listing(url="https://suumo.jp/test/a/")
        listing_b = make_listing(url="https://suumo.jp/test/b/")
        save_gemini_evaluation(listing_a.url, 5, "既存の評価", db_path=db_path)

        call_count = {"n": 0}
        def fake_evaluate(listing):
            call_count["n"] += 1
            return (3, "新規評価")
        monkeypatch.setattr(backfill, "evaluate_listing", fake_evaluate)

        targets = backfill.find_backfill_targets([listing_a, listing_b], db_path=db_path)
        assert len(targets) == 1
        assert targets[0].url == listing_b.url

        backfill.run_backfill(targets, dry_run=False, db_path=db_path, sleep_sec=0)
        assert call_count["n"] == 1  # 既存の1件は呼ばれない

        # 両方とも最終的に保存されていること（片方は元々、片方は今回）
        result = load_gemini_evaluations([listing_a.url, listing_b.url], db_path=db_path)
        assert result[listing_a.url] == (5, "既存の評価")   # 上書きされていない
        assert result[listing_b.url] == (3, "新規評価")


# ---------------------------------------------------------------------------
# 5. main(): DRY_RUN 環境変数のデフォルト安全性
# ---------------------------------------------------------------------------

class TestMainDryRunDefault:

    def test_dry_run_defaults_to_true_when_env_unset(self, monkeypatch, data_csv, db_path):
        monkeypatch.delenv("DRY_RUN", raising=False)
        monkeypatch.setattr(backfill, "DATA_FILE", str(data_csv))

        listing = make_listing()
        _write_csv(data_csv, [listing])

        call_count = {"n": 0}
        monkeypatch.setattr(backfill, "evaluate_listing",
                            lambda l: call_count.__setitem__("n", call_count["n"] + 1) or (0, ""))

        backfill.main()

        assert call_count["n"] == 0  # DRY_RUN未設定はデフォルトでドライラン扱い

    def test_dry_run_true_string_does_not_call_api(self, monkeypatch, data_csv, db_path):
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setattr(backfill, "DATA_FILE", str(data_csv))
        listing = make_listing()
        _write_csv(data_csv, [listing])

        call_count = {"n": 0}
        monkeypatch.setattr(backfill, "evaluate_listing",
                            lambda l: call_count.__setitem__("n", call_count["n"] + 1) or (0, ""))

        backfill.main()
        assert call_count["n"] == 0

    def test_dry_run_false_string_calls_api(self, monkeypatch, data_csv, db_path):
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setattr(backfill, "DATA_FILE", str(data_csv))
        listing = make_listing()
        _write_csv(data_csv, [listing])

        call_count = {"n": 0}
        def fake_evaluate(l):
            call_count["n"] += 1
            return (3, "text")
        monkeypatch.setattr(backfill, "evaluate_listing", fake_evaluate)
        monkeypatch.setattr(backfill, "GEMINI_SLEEP_SEC", 0)

        backfill.main()
        assert call_count["n"] == 1
