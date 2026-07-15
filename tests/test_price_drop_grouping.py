"""
tests/test_price_drop_grouping.py
aggregate_alerts_by_group（横断重複グループ単位の値下げ通知集約）の単体テスト。

test_price_drop.py の _insert_row と同じ手法でDB行を直接操作する。
全テストは USE_MOCK_REINFOLIB=1 で動作し、APIキー不要。
"""

from pathlib import Path

import pytest

import build_curves
import evaluator
from evaluator import detect_changes, is_price_change_notified
from scraper import Listing, aggregate_alerts_by_group, notify_line_price_drops

MOMIJI_URL_A = "https://suumo.jp/test/momiji-a/"
MOMIJI_URL_B = "https://suumo.jp/test/momiji-b/"


def _momiji_listing(url: str, price: str = "5290万円") -> Listing:
    """紅葉丘2グループ相当（同一部屋・別URL）の Listing を作る。"""
    return Listing(
        name=f"物件 {url}",
        price=price,
        location="東京都府中市紅葉丘２",
        url=url,
        floor_plan="3LDK",
        area="90.02m2（壁芯）",
        age="2003年7月",
    )


def _insert_row(db_path: Path, url: str, name: str, date_str: str, asking_price: float, resale_score: int = 72) -> None:
    import sqlite3
    conn = sqlite3.connect(db_path)
    evaluator._init_db(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO evaluations
            (listing_url, listing_name, city_code,
             evaluated_date, evaluated_at,
             asking_price, resale_score, notes, hold_years)
        VALUES (?, ?, '13206', ?, datetime('now'), ?, ?, '[]', 10)
        """,
        (url, name, date_str, asking_price, resale_score),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build_curves, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def enable_mock_mode(monkeypatch):
    monkeypatch.setenv("USE_MOCK_REINFOLIB", "1")


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "test_evaluations.db"


class TestAggregateAlertsByGroup:

    def test_one_member_drops_group_alert_fires(self, db_path):
        # A: 5290万→5190万（100万円値下げ、閾値超）。Bは価格変化なし。
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-13", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-13", 52_900_000)

        alerts = detect_changes([MOMIJI_URL_A, MOMIJI_URL_B], db_path=db_path, _today="2026-07-13")
        assert len(alerts) == 1  # Aだけが閾値超で検知される

        listings = [_momiji_listing(MOMIJI_URL_A, "5190万円"), _momiji_listing(MOMIJI_URL_B, "5290万円")]
        grouped = aggregate_alerts_by_group(alerts, listings, db_path=db_path)

        assert len(grouped) == 1
        assert grouped[0]["price_drop"] == 1_000_000
        assert "2業者" in grouped[0]["dual_note"]
        assert set(grouped[0]["group_urls"]) == {MOMIJI_URL_A, MOMIJI_URL_B}

    def test_catch_up_drop_does_not_refire_when_group_min_unchanged(self, db_path):
        """実例再現: 片方だけ値下げ→通知1件。その後もう片方が追従値下げしても
        グループ最安値（=A）は動いていないので、Bの追従値下げでは再通知しない。"""
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-13", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-13", 52_900_000)
        # Day3: Aは変化なし（51,900,000のまま）。Bが追従値下げ（52,900,000→51,900,000）。
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-14", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-14", 51_900_000)

        alerts_day3 = detect_changes([MOMIJI_URL_A, MOMIJI_URL_B], db_path=db_path, _today="2026-07-14")
        assert len(alerts_day3) == 1  # Bの100万円値下げ自体は個別に検知される
        assert alerts_day3[0]["url"] == MOMIJI_URL_B

        listings = [_momiji_listing(MOMIJI_URL_A, "5190万円"), _momiji_listing(MOMIJI_URL_B, "5190万円")]
        grouped = aggregate_alerts_by_group(alerts_day3, listings, db_path=db_path)

        # グループ最安値は Day2→Day3 で 51,900,000 のまま変わっていないため通知なし
        assert grouped == []

    def test_group_min_shifts_to_cheaper_member_fires(self, db_path):
        # A(前回最安51,900,000)は変化なし。Bが51,900,000→50,000,000へさらに値下げ
        # →グループ最安値自体が動くので通知する。
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-13", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-13", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-14", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-14", 50_000_000)

        alerts = detect_changes([MOMIJI_URL_A, MOMIJI_URL_B], db_path=db_path, _today="2026-07-14")
        listings = [_momiji_listing(MOMIJI_URL_A, "5190万円"), _momiji_listing(MOMIJI_URL_B, "5000万円")]
        grouped = aggregate_alerts_by_group(alerts, listings, db_path=db_path)

        assert len(grouped) == 1
        assert grouped[0]["price_drop"] == 1_900_000
        assert grouped[0]["url"] == MOMIJI_URL_B

    def test_single_member_group_passthrough_unaffected(self, db_path):
        # 重複のない単独物件は通常通りそのままアラートが通る
        url = "https://suumo.jp/test/single/"
        _insert_row(db_path, url, "単独物件", "2026-07-12", 45_000_000)
        _insert_row(db_path, url, "単独物件", "2026-07-13", 44_000_000)
        alerts = detect_changes([url], db_path=db_path, _today="2026-07-13")

        listing = Listing(
            name="単独物件", price="4400万円", location="東京都調布市曙町",
            url=url, floor_plan="3LDK", area="65m2", age="2010年1月",
        )
        grouped = aggregate_alerts_by_group(alerts, [listing], db_path=db_path)

        assert len(grouped) == 1
        assert grouped[0]["url"] == url
        assert grouped[0]["dual_note"] == ""

    def test_marking_after_notify_covers_all_group_urls(self, db_path, monkeypatch):
        """通知成功後、グループ内の全URLがそれぞれ自分自身の値でマーキングされること。"""
        import scraper
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "test_token")
        monkeypatch.setattr(scraper, "LINE_USER_ID", "test_user_id")

        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-12", 52_900_000)
        _insert_row(db_path, MOMIJI_URL_A, "紅葉丘2 A", "2026-07-13", 51_900_000)
        _insert_row(db_path, MOMIJI_URL_B, "紅葉丘2 B", "2026-07-13", 52_900_000)

        alerts = detect_changes([MOMIJI_URL_A, MOMIJI_URL_B], db_path=db_path, _today="2026-07-13")
        listings = [_momiji_listing(MOMIJI_URL_A, "5190万円"), _momiji_listing(MOMIJI_URL_B, "5290万円")]
        grouped = aggregate_alerts_by_group(alerts, listings, db_path=db_path)

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            notify_line_price_drops(grouped, db_path=db_path)

        # Bは価格自体は変化していないが、グループ通知が出た以上、Bの
        # (前回=今回=52,900,000, スコア72→72)の組み合わせでも「通知済み」として記録される
        # （DB(SQLite REAL)から読んだ値はfloatなので、_price_change_keyの文字列化と
        #   一致させるためここもfloatで比較する）
        assert is_price_change_notified(
            MOMIJI_URL_A, 52_900_000.0, 51_900_000.0, 72, 72, db_path=db_path,
        )
        assert is_price_change_notified(
            MOMIJI_URL_B, 52_900_000.0, 52_900_000.0, 72, 72, db_path=db_path,
        )

    def test_delisted_cheapest_members_do_not_leak_into_remaining_member(self, db_path):
        """
        エッジケース: グループ最安値メンバー（5,190万×4件）が掲載終了して消え、
        5,290万の1件だけが current に残るケース。グループ最安値は結果的に
        上がるので値下げ通知は出ないのが正しい挙動。

        aggregate_alerts_by_group は current_listings から都度グループを
        組み直すため、消えたURL（A/B/C/D）は member_urls に含まれ得ず、
        detect_changes の再呼び出しにも混ざらない構造になっている。
        E（残存1件）がスコア改善で個別にアラート対象になった場合でも、
        消えた側の安い価格が誤って「グループ最安値」として計算に紛れ込まない
        （= E単独のグループとして扱われ、E自身の値がそのまま通る）ことを固定する。
        """
        cheap_urls = [
            "https://suumo.jp/test/momiji-cheap-a/",
            "https://suumo.jp/test/momiji-cheap-b/",
            "https://suumo.jp/test/momiji-cheap-c/",
            "https://suumo.jp/test/momiji-cheap-d/",
        ]
        expensive_url = "https://suumo.jp/test/momiji-expensive-e/"

        # Day1: 安い4件・高い1件とも履歴あり（安い4件はこの後 current から消える想定）
        for u in cheap_urls:
            _insert_row(db_path, u, "紅葉丘2 安い側", "2026-07-13", 51_900_000, resale_score=60)
        _insert_row(db_path, expensive_url, "紅葉丘2 E", "2026-07-13", 52_900_000, resale_score=60)

        # Day2: 安い4件は current に存在しない（削除・掲載終了）。
        # Eだけが評価される。価格は不変だが、スコアが閾値超で改善（無関係な要因）。
        for u in cheap_urls:
            _insert_row(db_path, u, "紅葉丘2 安い側", "2026-07-14", 51_900_000, resale_score=60)
        _insert_row(db_path, expensive_url, "紅葉丘2 E", "2026-07-14", 52_900_000, resale_score=75)

        # detect_changes は current に存在するURL（Eのみ）にしか呼ばれない
        alerts = detect_changes([expensive_url], db_path=db_path, _today="2026-07-14")
        assert len(alerts) == 1
        assert alerts[0]["url"] == expensive_url
        assert alerts[0]["price_drop"] == 0
        assert alerts[0]["score_gain"] == 15

        # current_listings にも安い4件は含まれない（削除された前提を再現）
        current_listings = [_momiji_listing(expensive_url, "5290万円")]
        grouped = aggregate_alerts_by_group(alerts, current_listings, db_path=db_path)

        assert len(grouped) == 1
        assert grouped[0]["url"] == expensive_url
        # 消えた安い側(51,900,000)が誤って prev/today に紛れ込んでいないこと
        assert grouped[0]["price_drop"] == 0
        assert grouped[0]["today"]["asking_price"] == 52_900_000.0
        assert grouped[0]["prev"]["asking_price"] == 52_900_000.0
