"""
tests/test_price_change_dedup.py
====================
価格変動通知（値下げ・スコア改善）の重複抑制のテスト。

【背景】
    物件 nc_20697502 の値下げ（5280万→5180万）が、07-07朝の実行と
    夜の実行で全く同じ内容で2回通知される事故が実際に発生した。

    detect_changes は「今日の評価」と「直近の別日の評価」を比較する
    設計のため、1日2回の定期実行の両方で全く同じ差分を検知してしまう
    （detect_changes 自体はこの機能追加で一切変更していない）。

    この事故を受け、「同じ変化（URL＋旧価格→新価格＋旧スコア→新スコア
    の組）は一度通知したら再通知しない」仕組みを追加した。

    ⚠ 重要な設計判断: 参考枠・指値候補の通知済み記録は「候補を検知した
    時点」でマーキングするが、価格変動通知は意図的にこれと異なり
    「LINE送信が成功した後」にのみマーキングする。値下げ情報は
    「今しか使えない情報」であり、送信失敗時に「通知済みだが実際は
    届いていない」事態を避けるため。

対象:
    - evaluator.is_price_change_notified / mark_price_change_notified
    - scraper._filter_unnotified_price_changes
    - scraper.notify_line_price_drops（送信成功後マーキング・失敗時非マーキング）

全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー・実DBファイル不要。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import build_curves
import evaluator
import scraper
from evaluator import (
    detect_changes,
    evaluate_and_save,
    is_price_change_notified,
    mark_price_change_notified,
)
from scraper import (
    Listing,
    _filter_unnotified_price_changes,
    notify_line_price_drops,
)

CHOFU_CODE = "13208"
BASE_URL   = "https://suumo.jp/test/dedup-99999/"


def make_listing(**overrides) -> Listing:
    defaults = dict(
        name="テスト物件マンション101",
        price="4,200万円",
        location="東京都調布市曙町",
        url=BASE_URL,
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


@pytest.fixture
def line_env(monkeypatch):
    monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
    monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")


def _collect_sent_texts(mock_post) -> list[str]:
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


def _make_alert(prev_price=45_000_000.0, today_price=42_000_000.0,
                 prev_score=72, today_score=72, url=BASE_URL) -> dict:
    return {
        "url":        url,
        "name":       "テスト物件マンション101",
        "price_drop": round(prev_price - today_price),
        "score_gain": today_score - prev_score,
        "today": {"asking_price": today_price, "resale_score": today_score, "evaluated_date": "2026-07-07"},
        "prev":  {"asking_price": prev_price,  "resale_score": prev_score,  "evaluated_date": "2026-07-06"},
    }


# ---------------------------------------------------------------------------
# 1. is_price_change_notified / mark_price_change_notified の基本動作
# ---------------------------------------------------------------------------

class TestPriceChangeNotifiedFlags:

    def test_not_notified_before_marking(self, db_path):
        assert is_price_change_notified(
            BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path,
        ) is False

    def test_notified_after_marking(self, db_path):
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        assert is_price_change_notified(
            BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path,
        ) is True

    def test_different_today_price_is_new_change(self, db_path):
        # 45,000,000→42,000,000 を通知済みにした後、さらに 42,000,000→41,000,000
        # に値下げされた場合は「新しい変化」として扱われる
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        assert is_price_change_notified(
            BASE_URL, 42_000_000.0, 41_000_000.0, 72, 72, db_path=db_path,
        ) is False

    def test_different_prev_price_is_new_change(self, db_path):
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        assert is_price_change_notified(
            BASE_URL, 44_000_000.0, 42_000_000.0, 72, 72, db_path=db_path,
        ) is False

    def test_different_today_score_is_new_change(self, db_path):
        # スコア改善についても同じ仕組みで重複抑制される
        mark_price_change_notified(BASE_URL, 42_000_000.0, 42_000_000.0, 65, 76, db_path=db_path)
        assert is_price_change_notified(
            BASE_URL, 42_000_000.0, 42_000_000.0, 65, 80, db_path=db_path,
        ) is False

    def test_different_url_tracked_independently(self, db_path):
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        assert is_price_change_notified(
            "https://suumo.jp/test/other/", 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path,
        ) is False

    def test_nonexistent_db_returns_false(self, tmp_path):
        assert is_price_change_notified(
            BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=tmp_path / "no.db",
        ) is False

    def test_mark_does_not_raise_on_repeat(self, db_path):
        # 同じ変化を2回マーキングしても例外を投げない（UPSERT）
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        mark_price_change_notified(BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path)
        assert is_price_change_notified(
            BASE_URL, 45_000_000.0, 42_000_000.0, 72, 72, db_path=db_path,
        ) is True


# ---------------------------------------------------------------------------
# 2. _filter_unnotified_price_changes のテスト
# ---------------------------------------------------------------------------

class TestFilterUnnotifiedPriceChanges:

    def test_unnotified_alert_passes_through(self, db_path):
        alerts = [_make_alert()]
        result = _filter_unnotified_price_changes(alerts, db_path=db_path)
        assert result == alerts

    def test_notified_alert_is_excluded(self, db_path):
        alert = _make_alert()
        mark_price_change_notified(
            alert["url"], alert["prev"]["asking_price"], alert["today"]["asking_price"],
            alert["prev"]["resale_score"], alert["today"]["resale_score"], db_path=db_path,
        )
        result = _filter_unnotified_price_changes([alert], db_path=db_path)
        assert result == []

    def test_mixed_notified_and_unnotified(self, db_path):
        notified_alert = _make_alert(url="https://suumo.jp/test/notified/")
        new_alert = _make_alert(url="https://suumo.jp/test/new/")
        mark_price_change_notified(
            notified_alert["url"],
            notified_alert["prev"]["asking_price"], notified_alert["today"]["asking_price"],
            notified_alert["prev"]["resale_score"], notified_alert["today"]["resale_score"],
            db_path=db_path,
        )
        result = _filter_unnotified_price_changes([notified_alert, new_alert], db_path=db_path)
        assert result == [new_alert]

    def test_empty_list_returns_empty(self, db_path):
        assert _filter_unnotified_price_changes([], db_path=db_path) == []


# ---------------------------------------------------------------------------
# 3. 同日2回実行の再現テスト（★最重要・事故の再現）
# ---------------------------------------------------------------------------

class TestSameDayTwiceReproduction:
    """
    07-07朝・夜の2回実行で同じ値下げが2回通知された事故を再現し、
    修正後は2回目が抑制されることを確認する。
    """

    def test_same_change_notified_only_once_across_two_runs(self, db_path, line_env):
        # 前日(07-06)の評価 + 当日(07-07)の評価（朝の実行を模す）
        evaluate_and_save(
            [make_listing(price="5,280万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-06",
        )
        evaluate_and_save(
            [make_listing(price="5,180万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-07",
        )

        # --- 朝の実行 ---
        morning_alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-07-07")
        morning_alerts = _filter_unnotified_price_changes(morning_alerts, db_path=db_path)
        assert len(morning_alerts) == 1, "朝の実行では値下げが検知されるはず"

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops(morning_alerts, db_path=db_path)
        assert mock_post.call_count == 1, "朝の実行ではLINE送信が1回行われるはず"

        # --- 夜の実行（同日、evaluate_and_save が再度07-07の行をUPSERT）---
        evaluate_and_save(
            [make_listing(price="5,180万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-07",
        )
        evening_alerts = detect_changes([BASE_URL], db_path=db_path, _today="2026-07-07")
        assert len(evening_alerts) == 1, "detect_changes自体は無変更なので同じ差分を検知する"
        evening_alerts = _filter_unnotified_price_changes(evening_alerts, db_path=db_path)
        assert evening_alerts == [], "夜の実行では既に通知済みのため抑制されるはず"

        with patch("scraper.requests.post") as mock_post2:
            mock_post2.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops(evening_alerts, db_path=db_path)
        mock_post2.assert_not_called()

    def test_further_price_drop_is_notified_as_new_change(self, db_path, line_env):
        # 07-06: 5280万 → 07-07: 5180万（1回目の値下げ）→ 07-08: 5080万（2回目の値下げ）
        evaluate_and_save(
            [make_listing(price="5,280万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-06",
        )
        evaluate_and_save(
            [make_listing(price="5,180万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-07",
        )
        alerts_07 = _filter_unnotified_price_changes(
            detect_changes([BASE_URL], db_path=db_path, _today="2026-07-07"), db_path=db_path,
        )
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops(alerts_07, db_path=db_path)

        evaluate_and_save(
            [make_listing(price="5,080万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-08",
        )
        alerts_08 = _filter_unnotified_price_changes(
            detect_changes([BASE_URL], db_path=db_path, _today="2026-07-08"), db_path=db_path,
        )
        assert len(alerts_08) == 1, "さらに値下げされたら新しい変化として検知・通知されるはず"

    def test_further_score_gain_is_notified_as_new_change(self, db_path):
        # スコア改善についても同様に、新たな改善は新しい変化として通知される
        mark_price_change_notified(BASE_URL, 42_000_000.0, 42_000_000.0, 65, 76, db_path=db_path)
        alert_more_gain = _make_alert(
            prev_price=42_000_000.0, today_price=42_000_000.0, prev_score=65, today_score=85,
        )
        result = _filter_unnotified_price_changes([alert_more_gain], db_path=db_path)
        assert result == [alert_more_gain]


# ---------------------------------------------------------------------------
# 4. LINE送信失敗時は非マーキング（★今回の設計変更の核心）
# ---------------------------------------------------------------------------

class TestFailedSendDoesNotMark:
    """
    LINE送信が失敗した場合、mark_price_change_notified が呼ばれず、
    次回の実行で同じ変化が再度通知対象になることを確認する。
    """

    def test_non_200_response_does_not_mark(self, db_path, line_env):
        alert = _make_alert()
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
            notify_line_price_drops([alert], db_path=db_path)

        assert is_price_change_notified(
            alert["url"], alert["prev"]["asking_price"], alert["today"]["asking_price"],
            alert["prev"]["resale_score"], alert["today"]["resale_score"], db_path=db_path,
        ) is False, "送信失敗時はマーキングされてはいけない"

    def test_failed_send_alert_still_returned_by_filter_next_time(self, db_path, line_env):
        # 1回目: 送信失敗 → マーキングされない
        alert = _make_alert()
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
            notify_line_price_drops([alert], db_path=db_path)

        # 2回目: 同じ変化がフィルタを通しても除外されず、再送対象のまま
        result = _filter_unnotified_price_changes([alert], db_path=db_path)
        assert result == [alert], "送信失敗時は次回また通知対象に残るはず"

    def test_retry_after_failure_succeeds_and_marks(self, db_path, line_env):
        # 1回目: 送信失敗
        alert = _make_alert()
        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
            notify_line_price_drops([alert], db_path=db_path)

        # 2回目（再試行）: 送信成功 → 今度はマーキングされる
        with patch("scraper.requests.post") as mock_post2:
            mock_post2.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops([alert], db_path=db_path)

        assert is_price_change_notified(
            alert["url"], alert["prev"]["asking_price"], alert["today"]["asking_price"],
            alert["prev"]["resale_score"], alert["today"]["resale_score"], db_path=db_path,
        ) is True

    def test_request_exception_does_not_mark(self, db_path, line_env):
        # ネットワークエラー等で例外が飛んだ場合もマーキングされない
        import requests
        alert = _make_alert()
        with patch("scraper.requests.post", side_effect=requests.ConnectionError("network down")):
            with pytest.raises(requests.ConnectionError):
                notify_line_price_drops([alert], db_path=db_path)

        assert is_price_change_notified(
            alert["url"], alert["prev"]["asking_price"], alert["today"]["asking_price"],
            alert["prev"]["resale_score"], alert["today"]["resale_score"], db_path=db_path,
        ) is False

    def test_partial_batch_failure_marks_only_successful_batch(self, db_path, line_env):
        # _MAX_MESSAGES_PER_CALL=5 のため、ヘッダー1件+アラート5件で最初の
        # バッチが埋まる。6件のアラートを渡すと2バッチに分かれる。
        # 1バッチ目は成功、2バッチ目は失敗、という状況を再現する。
        alerts = [
            _make_alert(url=f"https://suumo.jp/test/batch-{i}/", today_price=42_000_000.0 - i)
            for i in range(6)
        ]
        responses = [
            MagicMock(status_code=200, text="OK"),             # 1バッチ目（ヘッダー+4件）
            MagicMock(status_code=500, text="error"),           # 2バッチ目（残り2件）
        ]
        with patch("scraper.requests.post", side_effect=responses):
            notify_line_price_drops(alerts, db_path=db_path)

        marked = [
            is_price_change_notified(
                a["url"], a["prev"]["asking_price"], a["today"]["asking_price"],
                a["prev"]["resale_score"], a["today"]["resale_score"], db_path=db_path,
            )
            for a in alerts
        ]
        # scraper._MAX_MESSAGES_PER_CALL = 5 なので、1バッチ目は
        # ヘッダー1件+アラート4件（message_texts[0:5]）が成功、
        # 残り2件（message_texts[5:7]）は2バッチ目で失敗する。
        assert marked == [True, True, True, True, False, False]


# ---------------------------------------------------------------------------
# 5. detect_changes 自体は無変更であることの確認（非回帰）
# ---------------------------------------------------------------------------

class TestDetectChangesUnchanged:
    """
    detect_changes は「値下げ検知ロジック自体は変えない」という制約により
    無変更のはず。重複抑制フィルタをかける前の生の検知結果は、
    今回の変更前と同じ挙動（同日なら何度呼んでも同じ結果）になることを確認する。
    """

    def test_detect_changes_still_returns_same_alert_when_called_twice(self, db_path):
        evaluate_and_save(
            [make_listing(price="5,280万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-06",
        )
        evaluate_and_save(
            [make_listing(price="5,180万円")], CHOFU_CODE,
            db_path=db_path, _evaluated_date="2026-07-07",
        )
        # フィルタをかけない生の detect_changes は、何度呼んでも同じ結果
        # （＝重複抑制は detect_changes の責務ではなく、呼び出し側の追加の層）
        first  = detect_changes([BASE_URL], db_path=db_path, _today="2026-07-07")
        second = detect_changes([BASE_URL], db_path=db_path, _today="2026-07-07")
        assert len(first) == 1
        assert len(second) == 1
        assert first[0]["price_drop"] == second[0]["price_drop"]
