"""
tests/test_main_integration.py
====================
scraper.main() を実際に実行して、指値候補（STEP4）の処理が既存の
4種類の通知（強調版・控えめ版・参考枠・値下げ）を壊さないことを
「ソースコードの見た目」ではなく「実行結果」で確認する。

【背景】
    以前は evaluator.py / detail_fetcher.py の db_path 引数が
    「関数定義時」に実際のプロジェクト直下の evaluations.db（本番相当
    ファイル）へ束縛されており、main() を安全に実行する手段がなかった
    （モックだけでは本番DBへの書き込みを防げなかった）。
    このため一度は「main() のソース構造検証」で代替したが、それは
    「try/exceptがある」という位置関係の確認に過ぎず、実際にコードが
    正しく動く証明にはならないという指摘を受けた。

    根本原因（db_path のデフォルト値が定義時に評価される設計）を
    evaluator.py / detail_fetcher.py 側で修正した（db_path の型を
    Optional[Path] = None にし、関数本体の先頭で
    `if db_path is None: db_path = DB_PATH` と解決するよう変更）。
    これにより evaluator.DB_PATH / detail_fetcher.DB_PATH を
    monkeypatch すれば、db_path 引数を省略している既存の呼び出し
    （main() 自身の呼び出しを含む）も安全に一時DBへ向けられるように
    なったため、本ファイルでは main() を実際に実行して確認する。

    ※ get_curve は evaluator.py 内で `from build_curves import get_curve`
      によりモジュールレベルで束縛されているため、evaluate_and_save
      内部の呼び出しをテスト用カーブに差し替えるには
      `evaluator.get_curve` を直接 monkeypatch する必要がある
      （build_curves.get_curve を差し替えるだけでは効かないことを
      実験で確認済み）。scraper._find_sashine_candidates は関数内で
      毎回 `from build_curves import get_curve` を実行する（遅延
      インポート）ため、こちらは build_curves.get_curve の差し替えが
      正しく反映される。テスト対象によって差し替え先が異なる点に注意。
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
from reinfolib_resale import DepreciationCurve
from scraper import Listing


# ---------------------------------------------------------------------------
# 固定カーブ（fair_price_now = 700,000円/㎡ × 72㎡ = 50,400,000円）
# ---------------------------------------------------------------------------
#
# 築13年（バケット11-15）→ 保有10年後は築23年（バケット21-25）。
# future_age=23 は「>25」にも「<=20」にも該当しないため resale_score への
# 加減点なし（+0）になるよう選んでいる。
# apply_filters のデフォルト条件（築10〜25年）を満たす必要があるため、
# 築8年・築18年ではなく築13年を採用している（前回の _find_sashine_candidates
# 単体テストとは異なる築年数だが、fair_price_now は同じ700,000円/㎡×72㎡で揃え、
# 検証済みの乖離率の数値をそのまま再利用できるようにしている）。

_BUILDING_YEAR = date.today().year - 13

_FIXED_CURVE = DepreciationCurve(
    median_unit_price={(11, 15): 700_000, (21, 25): 600_000},
    sample_count={(11, 15): 30, (21, 25): 25},
)


def make_listing(name: str, price: str, url: str) -> Listing:
    return Listing(
        name=name,
        price=price,
        location="東京都調布市曙町",
        url=url,
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72m²",
        age=f"{_BUILDING_YEAR}年3月",
    )


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """
    scraper.main() を安全に実行するための共通モック環境。

    - DB・カーブキャッシュ・data.csv を tmp_path に隔離
      （db_path のデフォルト値遅延解決により、main() 自身が db_path を
      省略して呼ぶ箇所も含めて正しく一時DBへ向く）
    - get_curve を固定カーブに差し替え（evaluator側・build_curves側の
      両方。理由はファイル冒頭のdocstring参照）
    - LINE認証情報を注入
    - scrape/詳細取得/Gemini/sleep を外部通信ゼロになるようモック

    ⚠ gemini_cache.DB_PATH も必ず差し替えること。main() の Gemini評価
      ループが save_gemini_evaluation を呼ぶようになったため、これを
      忘れると本番の evaluations.db に書き込んでしまう
      （実際に一度この漏れで本番DBに test/main-* という偽URLが
      書き込まれる事故が起きたため、二度と忘れないようここに明記する）。
    """
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

    monkeypatch.setattr(detail_fetcher, "fetch_detail", lambda url, **kw: None)
    monkeypatch.setattr(scraper.time, "sleep", lambda *a, **k: None)

    return db_path


def _collect_sent_texts(mock_post) -> list[str]:
    texts = []
    for call in mock_post.call_args_list:
        payload = call.kwargs.get("json", {})
        for msg in payload.get("messages", []):
            texts.append(msg.get("text", ""))
    return texts


# ---------------------------------------------------------------------------
# 1. 最重要: 指値候補処理が例外を起こしても既存の4種類の通知が実際に動く
# ---------------------------------------------------------------------------

class TestSashineFailureDoesNotBlockExistingNotifications:

    def test_all_four_existing_notifications_fire_when_sashine_raises(
        self, main_env, monkeypatch,
    ):
        db_path = main_env

        # --- 4種類の通知をそれぞれ発火させる物件を用意 ---
        # 強調版: Gemini★5 かつ reinfolib有望(乖離率-4.76%, score75)
        promising = make_listing("強調版物件", "4,800万円", "https://suumo.jp/test/main-promising/")
        # 控えめ版: Gemini★4 だが reinfolib非有望(乖離率+9.13%)
        normal = make_listing("控えめ版物件", "5,500万円", "https://suumo.jp/test/main-normal/")
        # 参考枠: Gemini★2(4未満) だが reinfolib有望(乖離率-3.77%)
        rejected_promising = make_listing("参考枠物件", "4,850万円", "https://suumo.jp/test/main-rejected/")
        # 値下げ: 前日4,500万円→本日4,000万円（500万円の下落、閾値50万円を超過）
        price_drop = make_listing("値下げ物件", "4,000万円", "https://suumo.jp/test/main-pricedrop/")

        all_listings = [promising, normal, rejected_promising, price_drop]

        # 値下げ検知のため、前日の評価履歴を先に保存しておく
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        evaluator.evaluate_and_save(
            [make_listing("値下げ物件", "4,500万円", price_drop.url)],
            city_code="13208", db_path=db_path, _evaluated_date=yesterday,
        )

        # --- 外部通信のモック ---
        monkeypatch.setattr(scraper, "scrape", lambda url: all_listings)

        def fake_evaluate_listing(listing):
            scores = {
                promising.url:          (5, "★★★★★ (5/5)\n懸念点：特になし"),
                normal.url:              (4, "★★★★☆ (4/5)\n懸念点：特になし"),
                rejected_promising.url:  (2, "★★☆☆☆ (2/5)\n懸念点：駅からやや遠い"),
                price_drop.url:          (5, "★★★★★ (5/5)\n懸念点：特になし"),
            }
            return scores.get(listing.url, (0, ""))

        monkeypatch.setattr(scraper, "evaluate_listing", fake_evaluate_listing)

        # --- 指値候補処理をわざと失敗させる（今回の核心） ---
        monkeypatch.setattr(
            scraper, "_find_sashine_candidates",
            MagicMock(side_effect=RuntimeError("わざと起こした例外（テスト用）")),
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # ← 実際に実行する（例外が外に漏れないことも同時に確認）

        sent = _collect_sent_texts(mock_post)
        blob = "\n".join(sent)

        # 1) 値下げ通知が実際に送られたこと
        assert "値下げ物件" in blob and ("値下がり" in blob or "下落" in blob or "→" in blob), \
            "値下げ通知が送信されていない"

        # 2) 強調版（2段階通知のうち有望）が実際に送られたこと
        assert "★★ 有望物件 ★★" in blob
        assert "強調版物件" in blob

        # 3) 控えめ版（2段階通知のうち非有望）が実際に送られたこと
        assert "控えめ版物件" in blob

        # 4) 参考枠が実際に送られたこと
        assert "📋 参考" in blob
        assert "参考枠物件" in blob

        # 5) 指値候補通知は送られていないこと（失敗したので当然）
        assert "💰 指値候補" not in blob

    def test_main_does_not_raise_when_sashine_fails(self, main_env, monkeypatch):
        # main() 自体が例外を外へ漏らさないこと（呼び出し元のGitHub Actions
        # ジョブを失敗させない、という最低限の生存保証）
        listing = make_listing("物件A", "4,800万円", "https://suumo.jp/test/main-simple/")
        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))
        monkeypatch.setattr(
            scraper, "_find_sashine_candidates",
            MagicMock(side_effect=RuntimeError("わざと起こした例外（テスト用）")),
        )

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()  # 例外を送出したら test は自動的に FAIL する


# ---------------------------------------------------------------------------
# 2. 新着0件でも指値候補通知は実際に実行される
# ---------------------------------------------------------------------------

class TestSashineRunsEvenWithoutNewListings:

    def test_sashine_notification_fires_with_zero_new_listings(self, main_env, monkeypatch):
        db_path = main_env

        # 5,500万円、乖離率+9.13%で現在は非有望・割高（標準指値なら有望化する
        # 検証済みのシナリオ）。この物件を「既知（新着ではない）」にする。
        listing = make_listing("指値候補物件", "5,500万円", "https://suumo.jp/test/main-sashine-only/")

        # data.csv に事前登録 → new_listings が空になる
        scraper.save_listings(scraper.DATA_FILE, [listing])

        # 40日前に初めて評価された履歴を作る → 強気度 standard
        past_date = (date.today() - timedelta(days=40)).isoformat()
        evaluator.evaluate_and_save(
            [listing], city_code="13208", db_path=db_path, _evaluated_date=past_date,
        )

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        # Gemini は new_listings が空なら呼ばれないはずだが、念のため設置
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        sent = _collect_sent_texts(mock_post)
        blob = "\n".join(sent)

        # 指値候補通知が実際に送信されていること（新着0件でも動く証明）
        assert "💰 指値候補" in blob
        assert "指値候補物件" in blob
        assert "5115万円" in blob  # 落としどころ(標準)の金額が含まれること

        # 新着0件なので、2段階通知・参考枠の見出しは出ていないこと
        # （既存仕様どおり。sashine追加がこの既存挙動を変えていないことの確認）
        assert "★★ 有望物件 ★★" not in blob
        assert "📋 参考" not in blob

    def test_no_sashine_notification_when_no_qualifying_listing(self, main_env, monkeypatch):
        # 新着0件・かつ指値候補にも該当しない物件のみ → 何も送信されない
        db_path = main_env
        listing = make_listing("普通の既知物件", "4,800万円", "https://suumo.jp/test/main-none/")
        # 乖離率-4.76%で既に有望 → 指値候補の条件1(現在非有望)を満たさない

        scraper.save_listings(scraper.DATA_FILE, [listing])
        past_date = (date.today() - timedelta(days=40)).isoformat()
        evaluator.evaluate_and_save(
            [listing], city_code="13208", db_path=db_path, _evaluated_date=past_date,
        )
        # Gemini評価件数上限の対応（優先評価）により、gemini_evaluations
        # 未登録の既知物件は自動的に評価対象になる。このテストは「指値候補・
        # 参考枠いずれにも該当しない」ことを見たいので、Gemini評価済み
        # （4★以上）として事前登録し、優先評価の対象から外す
        # （参考枠は4★未満のみを対象にするため、評価済み4★以上なら
        # 対象外のまま維持される）。
        from gemini_cache import save_gemini_evaluation
        save_gemini_evaluation(listing.url, 5, "評価済み", db_path=db_path)

        monkeypatch.setattr(scraper, "scrape", lambda url: [listing])
        monkeypatch.setattr(scraper, "evaluate_listing", lambda l: (0, ""))

        with patch("scraper.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, text="OK")
            scraper.main()

        mock_post.assert_not_called()
