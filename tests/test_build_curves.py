"""
tests/test_build_curves.py
build_curves.py の単体テスト。

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー不要。
pytest を suumo-line-bot/ ディレクトリで実行する前提:
    cd suumo-line-bot
    pytest tests/
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import build_curves
from build_curves import (
    CACHE_TTL_DAYS,
    _cache_path,
    _curve_to_dict,
    _dict_to_curve,
    _load_cache,
    _make_mock_trades,
    _save_cache,
    get_curve,
)
from reinfolib_resale import DepreciationCurve, build_depreciation_curve, normalize


# ---------------------------------------------------------------------------
# 共通フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    """
    全テストでキャッシュディレクトリを一時ディレクトリに差し替える。
    実際の cache/ ディレクトリを汚さず、テスト間の干渉もなくなる。
    """
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    """
    全テストで USE_MOCK_REINFOLIB=1 を設定する。
    これにより APIキー不要で get_curve が動作する。
    """
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

def _build_test_curve() -> DepreciationCurve:
    """テスト用の減価カーブを生成する共通処理。"""
    rows = _make_mock_trades("13208", 2022, 2025)
    trades = normalize(rows)
    return build_depreciation_curve(trades)


# ---------------------------------------------------------------------------
# 1. モックデータ生成のテスト
# ---------------------------------------------------------------------------

class TestMockData:
    """_make_mock_trades のテスト。"""

    def test_generates_non_empty_rows(self):
        # モックデータが1件以上生成されること
        rows = _make_mock_trades("13208", 2022, 2025)
        assert len(rows) > 0

    def test_all_rows_are_mansion_type(self):
        # normalize() が通るよう、全件「中古マンション等」であること
        rows = _make_mock_trades("13208", 2022, 2025)
        assert all(r["Type"] == "中古マンション等" for r in rows)

    def test_normalize_produces_trades(self):
        # normalize() を通して Trade オブジェクトが得られること
        rows = _make_mock_trades("13208", 2022, 2025)
        trades = normalize(rows)
        assert len(trades) > 0

    def test_curve_has_at_least_one_bucket(self):
        # カーブのバケットが1つ以上埋まること
        rows = _make_mock_trades("13208", 2022, 2025)
        trades = normalize(rows)
        curve = build_depreciation_curve(trades)
        assert len(curve.median_unit_price) > 0

    def test_reproducible_with_fixed_seed(self):
        # seed が固定されているので何度呼んでも同じデータが出ること
        rows1 = _make_mock_trades("13208", 2022, 2025)
        rows2 = _make_mock_trades("13208", 2022, 2025)
        assert rows1 == rows2


# ---------------------------------------------------------------------------
# 2. JSON ラウンドトリップのテスト（保存→読み戻しで完全一致すること）
# ---------------------------------------------------------------------------

class TestJsonRoundTrip:
    """
    tuple キーを文字列に変換して JSON 保存し、読み戻す処理のテスト。
    ここがずれると「カーブが空になっても気づかない」バグになるため念入りに検証する。
    """

    def test_median_unit_price_survives_roundtrip(self):
        # 保存→読み戻し後に median_unit_price が完全に一致すること
        curve = _build_test_curve()
        restored = _dict_to_curve(_curve_to_dict(curve))
        assert restored.median_unit_price == curve.median_unit_price

    def test_sample_count_survives_roundtrip(self):
        # 保存→読み戻し後に sample_count が完全に一致すること
        curve = _build_test_curve()
        restored = _dict_to_curve(_curve_to_dict(curve))
        assert restored.sample_count == curve.sample_count

    def test_tuple_keys_are_restored(self):
        # dict のキーが tuple (int, int) として復元されること
        curve = _build_test_curve()
        restored = _dict_to_curve(_curve_to_dict(curve))
        for k in restored.median_unit_price:
            assert isinstance(k, tuple)
            assert len(k) == 2
            assert isinstance(k[0], int)
            assert isinstance(k[1], int)

    def test_dict_keys_are_strings(self):
        # JSON に渡す dict のキーが文字列形式 "lo-hi" になっていること
        curve = _build_test_curve()
        d = _curve_to_dict(curve)
        for k in d["median_unit_price"]:
            assert isinstance(k, str)
            lo, hi = k.split("-")
            assert lo.isdigit() and hi.isdigit()

    def test_roundtrip_through_json_serialization(self, tmp_path):
        # json.dumps → json.loads の往復でも一致すること（実際の保存経路と同じ）
        curve = _build_test_curve()
        serialized = json.dumps(_curve_to_dict(curve), ensure_ascii=False)
        d = json.loads(serialized)
        restored = _dict_to_curve(d)
        assert restored.median_unit_price == curve.median_unit_price


# ---------------------------------------------------------------------------
# 3. キャッシュ保存・読み込みのテスト
# ---------------------------------------------------------------------------

class TestCache:
    """_save_cache / _load_cache のテスト。"""

    def test_save_creates_file(self, tmp_path):
        # _save_cache を呼んだ後にファイルが存在すること
        curve = _build_test_curve()
        path = tmp_path / "test_cache.json"
        _save_cache(path, "13208", "調布市", 2022, 2025, 100, curve)
        assert path.exists()

    def test_load_after_save_returns_curve(self, tmp_path):
        # 保存直後に読み込むと DepreciationCurve が返ること
        curve = _build_test_curve()
        path = tmp_path / "test_cache.json"
        _save_cache(path, "13208", "調布市", 2022, 2025, 100, curve)
        loaded = _load_cache(path)
        assert loaded is not None

    def test_load_returns_none_if_file_not_exists(self, tmp_path):
        # ファイルが存在しない場合は None を返すこと
        path = tmp_path / "nonexistent.json"
        assert _load_cache(path) is None

    def test_expired_cache_returns_none(self, tmp_path):
        # fetched_at が CACHE_TTL_DAYS + 1 日前のキャッシュは None を返すこと
        curve = _build_test_curve()
        path = tmp_path / "old_cache.json"
        _save_cache(path, "13208", "調布市", 2022, 2025, 100, curve)

        # fetched_at を期限切れの日付に書き換える
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        expired_date = (
            datetime.now() - timedelta(days=CACHE_TTL_DAYS + 1)
        ).isoformat(timespec="seconds")
        data["fetched_at"] = expired_date
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        assert _load_cache(path) is None

    def test_fresh_cache_within_ttl_is_loaded(self, tmp_path):
        # fetched_at が1日前のキャッシュは有効期限内なので返ること
        curve = _build_test_curve()
        path = tmp_path / "fresh_cache.json"
        _save_cache(path, "13208", "調布市", 2022, 2025, 100, curve)

        # fetched_at を1日前に書き換える（それでも TTL=90日以内）
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        one_day_ago = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        data["fetched_at"] = one_day_ago
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        assert _load_cache(path) is not None

    def test_saved_json_contains_metadata(self, tmp_path):
        # 保存した JSON に必要なメタデータが含まれていること
        curve = _build_test_curve()
        path = tmp_path / "meta_check.json"
        _save_cache(path, "13208", "調布市", 2022, 2025, 123, curve)
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        assert data["city_code"]   == "13208"
        assert data["city_name"]   == "調布市"
        assert data["start_year"]  == 2022
        assert data["end_year"]    == 2025
        assert data["trade_count"] == 123
        assert "fetched_at" in data
        assert "curve" in data


# ---------------------------------------------------------------------------
# 4. get_curve のテスト（モックモードで通しテスト）
# ---------------------------------------------------------------------------

class TestGetCurve:
    """get_curve の統合テスト（USE_MOCK_REINFOLIB=1 で実行）。"""

    def test_returns_curve_not_none_in_mock_mode(self):
        # モックモードで曲線が返ること
        curve = get_curve("調布市", "13208")
        assert curve is not None

    def test_returns_depreciation_curve_instance(self):
        # 戻り値が DepreciationCurve であること
        curve = get_curve("調布市", "13208")
        assert isinstance(curve, DepreciationCurve)

    def test_curve_has_buckets_in_mock_mode(self):
        # バケットが1つ以上埋まっていること
        curve = get_curve("調布市", "13208")
        assert len(curve.median_unit_price) > 0

    def test_cache_file_created_after_first_call(self, tmp_path):
        # get_curve を呼んだ後にキャッシュファイルが作られること
        get_curve("調布市", "13208")
        cache_files = list(tmp_path.glob("*.json"))
        assert len(cache_files) == 1

    def test_second_call_reads_from_cache(self, tmp_path):
        """
        2回目の get_curve はキャッシュから読むこと。
        キャッシュを改ざんして、改ざん後の値が返ることで確認する。

        ※ get_curve は内部で get_curve_bundle に委譲するようになったため、
        キャッシュファイルの構造は data["bundle"]["city_curve"] になった
        （地区単位カーブ機能追加に伴う変更）。
        """
        # 1回目: モックデータでカーブ生成 → キャッシュ保存
        get_curve("調布市", "13208")
        cache_file = next(tmp_path.glob("*.json"))

        # キャッシュの median_unit_price を全バケット 99999.0 に書き換える
        with cache_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for k in data["bundle"]["city_curve"]["median_unit_price"]:
            data["bundle"]["city_curve"]["median_unit_price"][k] = 99999.0
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

        # 2回目: キャッシュから読むはずなので改ざん値が返る
        curve2 = get_curve("調布市", "13208")
        assert all(v == 99999.0 for v in curve2.median_unit_price.values())

    def test_force_refresh_ignores_cache(self, tmp_path):
        """
        force_refresh=True のとき、有効なキャッシュがあっても無視して再生成すること。
        """
        # 1回目でキャッシュ作成 → 全バケットを 99999.0 に改ざん
        get_curve("調布市", "13208")
        cache_file = next(tmp_path.glob("*.json"))
        with cache_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for k in data["bundle"]["city_curve"]["median_unit_price"]:
            data["bundle"]["city_curve"]["median_unit_price"][k] = 99999.0
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

        # force_refresh=True → 改ざんキャッシュを無視して再生成する
        curve = get_curve("調布市", "13208", force_refresh=True)
        # モックで再生成した値は 99999.0 ではないはず
        assert not all(v == 99999.0 for v in curve.median_unit_price.values())

    def test_no_api_key_without_mock_returns_none(self, monkeypatch):
        """
        モックモードをオフにして REINFOLIB_API_KEY も未設定なら None を返すこと。
        """
        monkeypatch.delenv("USE_MOCK_REINFOLIB", raising=False)
        monkeypatch.delenv("REINFOLIB_API_KEY", raising=False)
        curve = get_curve("調布市", "13208")
        assert curve is None
