"""
tests/test_district_curve.py
====================
地区単位の減価カーブ（フォールバック付き）のテスト。

【背景】
    同じ物件が異なる市の平均と比較されると評価が大きく変わる問題が
    実際に起きた（調布市カーブで-15.3%、稲城市カーブで+40.0%という
    真逆の乖離率が同一物件で発生）。この粗さを解消するため、地区単位
    （市区町村＋地区名）でカーブを作り、サンプル数が閾値
    （DISTRICT_MIN_SAMPLES=8件）未満の場合は市区町村単位のカーブに
    フォールバックする機能を追加した。

    実データ確認（check_district_sample_distribution.py）により、
    稲城市は調布市・府中市より地区あたりのサンプル数が少なく、
    地区単位カーブのカバレッジが低いことが分かっている。これは
    データの実態であり、無理に閾値を下げて精度の低いカーブを使うより
    安全という判断のもと許容する。

対象:
    - reinfolib_resale.select_curve（地区優先・フォールバック判定）
    - suumo_adapter._extract_district（住所文字列からの地区名抽出）
    - build_curves._build_district_curves（地区ごとのカーブ生成）
    - build_curves.get_curve_bundle（市単位＋地区単位のキャッシュ込み取得）
    - evaluator.evaluate_and_save の curve_source 記録・ログ出力

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー・実DBファイル不要。
"""

import json
import logging
from datetime import date
from pathlib import Path

import pytest

import build_curves
import evaluator
from build_curves import CurveBundle, _build_district_curves, get_curve, get_curve_bundle
from evaluator import evaluate_and_save
from reinfolib_resale import DepreciationCurve, Trade, select_curve
from scraper import Listing
from suumo_adapter import _extract_district

# ---------------------------------------------------------------------------
# 共通フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件マンション",
        price="4,200万円",
        location="東京都府中市紅葉丘２",
        url="https://suumo.jp/test/district-1/",
        station="西武多摩川線 多磨駅 徒歩4分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


def _make_trade(district: str, age: int, unit_price: float = 500_000) -> Trade:
    return Trade(
        price=unit_price * 70,
        area_sqm=70,
        unit_price=unit_price,
        building_year=2020 - age,
        trade_year=2020,
        age_at_trade=age,
        floor_plan="3LDK",
        municipality="テスト市",
        district=district,
    )


# ---------------------------------------------------------------------------
# 1. select_curve: 地区優先・フォールバック判定
# ---------------------------------------------------------------------------

class TestSelectCurve:

    CITY_CURVE = DepreciationCurve(
        median_unit_price={(11, 15): 500_000, (16, 20): 480_000},
        sample_count={(11, 15): 50, (16, 20): 45},
    )

    def test_uses_district_curve_when_bucket_has_enough_samples(self):
        # 地区カーブに現在の築年数バケットが実在する → 地区カーブを使う
        district_curves = {
            "紅葉丘": DepreciationCurve(
                median_unit_price={(11, 15): 600_000},
                sample_count={(11, 15): 19},
            ),
        }
        curve, source = select_curve(
            "紅葉丘", self.CITY_CURVE, district_curves, current_age=13, city_name="府中市",
        )
        assert curve.median_unit_price[(11, 15)] == 600_000
        assert source == "district:紅葉丘(n=19)"

    def test_falls_back_to_city_when_district_bucket_missing(self):
        # 紅葉丘カーブはあるが、現在の築年数バケット(11-15年)がサンプル
        # 不足で含まれていない（build_depreciation_curveの時点で除外済み）
        district_curves = {
            "紅葉丘": DepreciationCurve(
                median_unit_price={(21, 25): 600_000},  # 11-15年は含まれない
                sample_count={(21, 25): 10},
            ),
        }
        curve, source = select_curve(
            "紅葉丘", self.CITY_CURVE, district_curves, current_age=13, city_name="府中市",
        )
        assert curve is self.CITY_CURVE
        assert source == "city:府中市"

    def test_falls_back_to_city_when_district_name_unknown(self):
        # district_curves に存在しない地区名 → 市単位にフォールバック
        district_curves = {
            "紅葉丘": DepreciationCurve(
                median_unit_price={(11, 15): 600_000}, sample_count={(11, 15): 19},
            ),
        }
        curve, source = select_curve(
            "存在しない地区", self.CITY_CURVE, district_curves, current_age=13, city_name="府中市",
        )
        assert curve is self.CITY_CURVE
        assert source == "city:府中市"

    def test_falls_back_to_city_when_district_is_none(self):
        # 地区名が特定できない(None)物件 → 例外を投げず市単位にフォールバック
        district_curves = {
            "紅葉丘": DepreciationCurve(
                median_unit_price={(11, 15): 600_000}, sample_count={(11, 15): 19},
            ),
        }
        curve, source = select_curve(
            None, self.CITY_CURVE, district_curves, current_age=13, city_name="府中市",
        )
        assert curve is self.CITY_CURVE
        assert source == "city:府中市"

    def test_falls_back_to_city_when_no_district_curves_at_all(self):
        # 地区単位カーブが1つも生成されなかった市 → 常に市単位
        curve, source = select_curve(
            "紅葉丘", self.CITY_CURVE, {}, current_age=13, city_name="府中市",
        )
        assert curve is self.CITY_CURVE
        assert source == "city:府中市"

    def test_no_exception_for_any_input_combination(self):
        # 地区名がNone/空文字/未知のいずれでも例外を投げない
        for district in [None, "", "存在しない地区名"]:
            curve, source = select_curve(
                district, self.CITY_CURVE, {}, current_age=999, city_name="テスト市",
            )
            assert curve is self.CITY_CURVE
            assert source == "city:テスト市"


# ---------------------------------------------------------------------------
# 2. _extract_district: 住所文字列からの地区名抽出
# ---------------------------------------------------------------------------

class TestExtractDistrict:

    def test_extracts_district_with_trailing_zenkaku_chome_number(self):
        assert _extract_district("東京都府中市紅葉丘２", "府中市") == "紅葉丘"

    def test_extracts_district_without_trailing_number(self):
        assert _extract_district("東京都稲城市矢野口", "稲城市") == "矢野口"

    def test_extracts_district_with_hyphenated_chome(self):
        assert _extract_district("東京都調布市多摩川１－２", "調布市") == "多摩川"

    def test_extracts_district_with_halfwidth_number(self):
        assert _extract_district("東京都府中市白糸台3", "府中市") == "白糸台"

    def test_returns_none_when_city_name_not_in_location(self):
        assert _extract_district("東京都世田谷区砧", "府中市") is None

    def test_returns_none_when_nothing_remains_after_city_name(self):
        assert _extract_district("東京都府中市", "府中市") is None

    def test_returns_none_for_empty_location(self):
        assert _extract_district("", "府中市") is None

    def test_returns_none_for_empty_city_name(self):
        assert _extract_district("東京都府中市紅葉丘２", "") is None


# ---------------------------------------------------------------------------
# 3. _build_district_curves: 地区ごとのカーブ生成・閾値判定
# ---------------------------------------------------------------------------

class TestBuildDistrictCurves:

    def test_district_with_enough_samples_gets_curve(self):
        trades = [_make_trade("多い地区", age=12) for _ in range(10)]
        result = _build_district_curves(trades, min_samples=8)
        assert "多い地区" in result
        assert result["多い地区"].sample_count[(11, 15)] == 10

    def test_district_with_insufficient_samples_excluded(self):
        trades = [_make_trade("少ない地区", age=12) for _ in range(5)]
        result = _build_district_curves(trades, min_samples=8)
        assert "少ない地区" not in result

    def test_trades_without_district_are_excluded(self):
        trades = [_make_trade("", age=12) for _ in range(10)]
        result = _build_district_curves(trades, min_samples=8)
        assert result == {}

    def test_multiple_districts_independently_evaluated(self):
        trades = (
            [_make_trade("十分な地区", age=12) for _ in range(10)]
            + [_make_trade("不十分な地区", age=12) for _ in range(3)]
        )
        result = _build_district_curves(trades, min_samples=8)
        assert "十分な地区" in result
        assert "不十分な地区" not in result


class TestSparseDataFallbackBehavior:
    """
    稲城市のように地区あたりのサンプル数が少ない都市では、多くの物件が
    市単位カーブにフォールバックすることを確認する回帰テスト。

    実データ確認（check_district_sample_distribution.py、2026-07-07実行）:
    稲城市は9地区中、閾値8件以上を満たす(地区,築年数バケット)の組み合わせが
    23個のみ（調布66・府中78に対して少ない）。この性質が select_curve の
    フォールバック判定を通じて正しく反映されることを確認する。
    """

    def test_small_sample_city_mostly_falls_back_to_city_curve(self):
        # 稲城市を模した「地区ごとのサンプル数が少ない」データ:
        # 9地区、いずれも閾値8件未満
        trades = []
        for i, n in enumerate([5, 3, 4, 2, 1, 3, 2, 4, 1]):
            trades += [_make_trade(f"地区{i}", age=12) for _ in range(n)]

        district_curves = _build_district_curves(trades, min_samples=8)
        # 全地区が閾値未満 → district_curvesは空 → 全物件が市単位フォールバック
        assert district_curves == {}

    def test_large_sample_city_mostly_uses_district_curve(self):
        # 調布市・府中市のような「大きな地区がある」データとの対比
        trades = [_make_trade("大きい地区", age=12) for _ in range(20)]
        district_curves = _build_district_curves(trades, min_samples=8)
        assert "大きい地区" in district_curves


# ---------------------------------------------------------------------------
# 4. get_curve_bundle: 市単位＋地区単位のキャッシュ込み取得
# ---------------------------------------------------------------------------

class TestGetCurveBundle:

    def test_returns_curve_bundle_instance(self):
        bundle = get_curve_bundle("調布市", "13208")
        assert isinstance(bundle, CurveBundle)

    def test_city_curve_matches_get_curve(self):
        bundle = get_curve_bundle("調布市", "13208")
        curve = get_curve("調布市", "13208")
        assert bundle.city_curve.median_unit_price == curve.median_unit_price

    def test_mock_data_single_district_produces_district_curve(self):
        # _make_mock_trades は全レコードが同一地区名("テスト町")のため、
        # サンプル数が十分なら district_curves に含まれるはず
        bundle = get_curve_bundle("調布市", "13208")
        assert "テスト町" in bundle.district_curves

    def test_cache_file_created_after_first_call(self, tmp_path):
        get_curve_bundle("調布市", "13208")
        cache_files = list(tmp_path.glob("*.json"))
        assert len(cache_files) == 1

    def test_second_call_reads_district_curves_from_cache(self, tmp_path):
        get_curve_bundle("調布市", "13208")
        cache_file = next(tmp_path.glob("*.json"))
        with cache_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for k in data["bundle"]["district_curves"]["テスト町"]["median_unit_price"]:
            data["bundle"]["district_curves"]["テスト町"]["median_unit_price"][k] = 88888.0
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

        bundle2 = get_curve_bundle("調布市", "13208")
        assert all(
            v == 88888.0 for v in bundle2.district_curves["テスト町"].median_unit_price.values()
        )

    def test_force_refresh_ignores_district_cache_too(self, tmp_path):
        get_curve_bundle("調布市", "13208")
        cache_file = next(tmp_path.glob("*.json"))
        with cache_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for k in data["bundle"]["district_curves"]["テスト町"]["median_unit_price"]:
            data["bundle"]["district_curves"]["テスト町"]["median_unit_price"][k] = 88888.0
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

        bundle2 = get_curve_bundle("調布市", "13208", force_refresh=True)
        assert not all(
            v == 88888.0 for v in bundle2.district_curves["テスト町"].median_unit_price.values()
        )


# ---------------------------------------------------------------------------
# 5. evaluate_and_save: curve_source の記録・ログ出力
# ---------------------------------------------------------------------------

CHOFU_CODE = "13208"


class TestCurveSourceRecording:

    def test_curve_source_recorded_when_district_curve_used(self, tmp_path, monkeypatch):
        district_curve = DepreciationCurve(
            median_unit_price={(6, 10): 800_000}, sample_count={(6, 10): 15},
        )
        city_curve = DepreciationCurve(
            median_unit_price={(6, 10): 500_000}, sample_count={(6, 10): 100},
        )
        bundle = CurveBundle(city_curve=city_curve, district_curves={"曙町": district_curve})
        monkeypatch.setattr(evaluator, "get_curve_bundle", lambda **kw: bundle)

        db_path = tmp_path / "eval.db"
        listing = make_listing(
            location="東京都調布市曙町",
            age=f"{date.today().year - 8}年3月",
        )
        evaluate_and_save([listing], CHOFU_CODE, db_path=db_path)

        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM evaluations").fetchone()
        assert row["curve_source"].startswith("district:曙町")

    def test_curve_source_recorded_when_falls_back_to_city(self, tmp_path, monkeypatch):
        city_curve = DepreciationCurve(
            median_unit_price={(6, 10): 500_000}, sample_count={(6, 10): 100},
        )
        bundle = CurveBundle(city_curve=city_curve, district_curves={})  # 地区カーブなし
        monkeypatch.setattr(evaluator, "get_curve_bundle", lambda **kw: bundle)

        db_path = tmp_path / "eval.db"
        listing = make_listing(
            location="東京都調布市曙町",
            age=f"{date.today().year - 8}年3月",
        )
        evaluate_and_save([listing], CHOFU_CODE, db_path=db_path)

        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM evaluations").fetchone()
        assert row["curve_source"] == "city:調布市"

    def test_curve_selection_logged(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db_path = tmp_path / "eval.db"
        listing = make_listing()
        evaluate_and_save([listing], CHOFU_CODE, db_path=db_path)
        assert "[カーブ選択]" in caplog.text

    def test_existing_db_without_curve_source_column_is_migrated(self, tmp_path):
        # curve_source カラムがない旧スキーマのDBに対しても例外なく動作すること
        import sqlite3
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_url TEXT NOT NULL,
                    listing_name TEXT,
                    city_code TEXT NOT NULL,
                    evaluated_date TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    asking_price REAL,
                    area_sqm REAL,
                    building_year INTEGER,
                    walk_minutes INTEGER,
                    floor_plan TEXT,
                    current_fair_unit_price REAL,
                    current_fair_price REAL,
                    asking_vs_fair_pct REAL,
                    future_resale_price REAL,
                    net_after_tax_and_cost REAL,
                    resale_score INTEGER,
                    notes TEXT,
                    hold_years INTEGER,
                    UNIQUE(listing_url, evaluated_date)
                )
            """)
            conn.commit()

        listing = make_listing()
        n = evaluate_and_save([listing], CHOFU_CODE, db_path=db_path)
        assert n == 1

        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM evaluations").fetchone()
        assert row["curve_source"]  # マイグレーション後、正常に値が入っている
