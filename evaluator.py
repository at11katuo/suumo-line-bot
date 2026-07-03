"""
evaluator.py
====================
SUUMO 収集結果（Listing）を評価し、結果を SQLite に保存するパイプライン。

3 つの部品を結合する:
  1. suumo_adapter.suumo_to_candidate()  : Listing → Candidate（変換）
  2. build_curves.get_curve()            : エリアの減価カーブ取得
  3. reinfolib_resale.estimate_resale()  : Candidate + カーブ → 評価結果

【手動実行の使い方】
    # モックモード（APIキー不要・動作確認用）
    USE_MOCK_REINFOLIB=1 python evaluator.py

    # 実カーブ使用（REINFOLIB_API_KEY が必要）
    python evaluator.py

【評価結果の確認】
    # SQLite を開いて最新の評価を見る
    sqlite3 evaluations.db "SELECT listing_name, resale_score, asking_price,
      asking_vs_fair_pct FROM evaluations ORDER BY evaluated_at DESC LIMIT 10;"
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from build_curves import TARGET_AREAS, get_curve
from reinfolib_resale import estimate_resale
from scraper import Listing
from suumo_adapter import suumo_to_candidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 想定保有年数（ヤドカリ戦法の出口計算に使う）
DEFAULT_HOLD_YEARS = 10

# SQLite の保存先（このファイルと同じディレクトリ。.gitignore で追跡対象外）
DB_PATH = Path(__file__).parent / "evaluations.db"

# 価格変動通知のしきい値
# 小さなノイズ（表記揺れ・端数変更）で毎日通知が飛ばないための下限
PRICE_DROP_THRESHOLD = 500_000   # 50万円以上の下落で通知
SCORE_GAIN_THRESHOLD = 10         # 10点以上のスコア改善で通知


# ---------------------------------------------------------------------------
# DB 初期化
# ---------------------------------------------------------------------------

def _init_db(conn: sqlite3.Connection) -> None:
    """
    テーブルとインデックスを作成する（すでに存在する場合はスキップ）。
    アプリ起動時に毎回呼ぶことで、初回のみ自動でスキーマが作られる。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evaluations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_url             TEXT    NOT NULL,      -- 物件の一意キー（SUUMOのURL）
            listing_name            TEXT,                  -- 物件名（デバッグ・通知用）
            city_code               TEXT    NOT NULL,      -- 評価に使ったエリアコード
            evaluated_date          TEXT    NOT NULL,      -- "YYYY-MM-DD"（重複防止・履歴の軸）
            evaluated_at            TEXT    NOT NULL,      -- ISO 8601 フル日時（記録用）
            asking_price            REAL,                  -- 売出価格（円）
            area_sqm                REAL,                  -- 専有面積（㎡）
            building_year           INTEGER,               -- 建築年（西暦）
            walk_minutes            INTEGER,               -- 駅徒歩（分）
            floor_plan              TEXT,                  -- 間取り
            current_fair_unit_price REAL,                  -- 現在の適正㎡単価（円/㎡）
            current_fair_price      REAL,                  -- 現在の適正価格（円）
            asking_vs_fair_pct      REAL,                  -- 売出との乖離率（+割高/-割安）
            future_resale_price     REAL,                  -- N年後の想定売却額（円）
            net_after_tax_and_cost  REAL,                  -- 税・諸費用後の手取り見込み（円）
            resale_score            INTEGER,               -- 売りやすさスコア 0〜100
            notes                   TEXT,                  -- 注意書き（JSON配列）
            hold_years              INTEGER,               -- 想定保有年数
            -- 同日同物件は1行（UPSERT）。別日は新規行として履歴が積まれる。
            UNIQUE(listing_url, evaluated_date)
        )
    """)
    # listing_url でのGROUP BY（値下げ追跡クエリ）を高速化するインデックス
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_listing_url
        ON evaluations(listing_url)
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# 1行保存（UPSERT）
# ---------------------------------------------------------------------------

def _upsert(
    conn: sqlite3.Connection,
    listing: Listing,
    city_code: str,
    candidate,          # reinfolib_resale.Candidate
    estimate,           # reinfolib_resale.ResaleEstimate
    hold_years: int,
    evaluated_date: str,
    evaluated_at: str,
) -> None:
    """
    評価結果を1行 INSERT する。
    同じ (listing_url, evaluated_date) がすでにあれば全カラムを UPDATE する。
    こうすることで「同日の再実行は上書き、翌日は新しい行」になる。
    """
    conn.execute(
        """
        INSERT INTO evaluations (
            listing_url, listing_name, city_code,
            evaluated_date, evaluated_at,
            asking_price, area_sqm, building_year, walk_minutes, floor_plan,
            current_fair_unit_price, current_fair_price, asking_vs_fair_pct,
            future_resale_price, net_after_tax_and_cost,
            resale_score, notes, hold_years
        ) VALUES (
            ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?
        )
        ON CONFLICT(listing_url, evaluated_date)
        DO UPDATE SET
            listing_name            = excluded.listing_name,
            evaluated_at            = excluded.evaluated_at,
            asking_price            = excluded.asking_price,
            area_sqm                = excluded.area_sqm,
            building_year           = excluded.building_year,
            walk_minutes            = excluded.walk_minutes,
            floor_plan              = excluded.floor_plan,
            current_fair_unit_price = excluded.current_fair_unit_price,
            current_fair_price      = excluded.current_fair_price,
            asking_vs_fair_pct      = excluded.asking_vs_fair_pct,
            future_resale_price     = excluded.future_resale_price,
            net_after_tax_and_cost  = excluded.net_after_tax_and_cost,
            resale_score            = excluded.resale_score,
            notes                   = excluded.notes,
            hold_years              = excluded.hold_years
        """,
        (
            listing.url,
            listing.name,
            city_code,
            evaluated_date,
            evaluated_at,
            candidate.asking_price,
            candidate.area_sqm,
            candidate.building_year,
            candidate.walk_minutes,
            candidate.floor_plan,
            estimate.current_fair_unit_price,
            estimate.current_fair_price,
            estimate.asking_vs_fair_pct,
            estimate.future_resale_price,
            estimate.net_after_tax_and_cost,
            estimate.resale_score,
            json.dumps(estimate.notes, ensure_ascii=False),  # list[str] → JSON文字列
            hold_years,
        ),
    )


# ---------------------------------------------------------------------------
# エリアコード → 市区町村名の逆引き（ログ用）
# ---------------------------------------------------------------------------

def _city_name(city_code: str) -> str:
    """TARGET_AREAS からコードに対応する市区町村名を引く。なければコードを返す。"""
    for name, code in TARGET_AREAS.items():
        if code == city_code:
            return name
    return city_code


def resolve_city_code(location: str) -> Optional[str]:
    """
    Listing.location の住所文字列から市区町村コードを引く。
    TARGET_AREAS に登録された市名（"調布市" 等）が含まれていれば対応コードを返す。
    3市外・所在地不明など一致しない場合は None を返す。

    例:
        "東京都調布市曙町" → "13208"
        "東京都府中市中町" → "13206"
        "東京都稲城市矢野口" → "13225"
        "東京都世田谷区砧" → None
        "（所在地不明）"   → None
    """
    for city_name, code in TARGET_AREAS.items():
        if city_name in location:
            return code
    return None


# ---------------------------------------------------------------------------
# パイプライン関数（公開インターフェース）
# ---------------------------------------------------------------------------

def evaluate_and_save(
    listings: list[Listing],
    city_code: str,
    db_path: Optional[Path] = None,
    hold_years: int = DEFAULT_HOLD_YEARS,
    current_year: Optional[int] = None,
    _evaluated_date: Optional[str] = None,
    detail_cache: Optional[dict] = None,
) -> int:
    """
    Listing リストを評価して SQLite に保存する。

    引数:
        listings        : SUUMO収集結果（scrape() や apply_filters() の出力）
        city_code       : 評価に使うエリアの市区町村コード（例: "13208"=調布市）
        db_path         : SQLite ファイルのパス。None なら呼び出し時点の DB_PATH を使う
                          （関数定義時に固定値へ束縛されるのを避けるため、本体側で
                          解決する。テストで evaluator.DB_PATH を monkeypatch した
                          場合にも正しく反映される）
        hold_years      : 想定保有年数（デフォルト: DEFAULT_HOLD_YEARS=10年）
        current_year    : 評価基準年（None のとき今年を使う）
        _evaluated_date : テスト用。"YYYY-MM-DD" を渡すと今日の日付の代わりに使われる
        detail_cache    : {url: {"total_units": int|None, "repair_fund_monthly": float|None}}
                          detail_fetcher.load_detail_cache() の戻り値を渡す。
                          None のとき（または URL が含まれないとき）は中立スコアにフォールバック。

    戻り値:
        保存した件数（スキップされた物件は含まない）

    スキップ条件:
        - suumo_to_candidate() が None を返した物件（価格未定など）
        - エリアの減価カーブが取得できなかった場合（全件スキップ）
    """
    if db_path is None:
        db_path = DB_PATH
    year  = current_year or date.today().year
    today = _evaluated_date or date.today().isoformat()
    now   = datetime.now().isoformat(timespec="seconds")

    # --- エリアの減価カーブ取得 ---
    name  = _city_name(city_code)
    curve = get_curve(city_name=name, city_code=city_code)
    if curve is None:
        logger.warning(
            "evaluate_and_save: %s（%s）のカーブが取得できませんでした。"
            "%d 件をスキップします。",
            name, city_code, len(listings),
        )
        return 0

    # --- DB 接続・初期化 ---
    conn = sqlite3.connect(db_path)
    try:
        _init_db(conn)
        saved = 0

        for listing in listings:
            # Listing → Candidate（詳細キャッシュがあれば total_units / repair_fund_per_sqm も設定）
            # detail_cache に URL がない場合（未取得 or 取得失敗）は detail=None → 中立スコア
            candidate = suumo_to_candidate(
                listing,
                detail=detail_cache.get(listing.url) if detail_cache else None,
            )
            if candidate is None:
                continue  # suumo_to_candidate 側で warning ログ済み

            # 評価実行
            est = estimate_resale(candidate, curve, year, hold_years)

            # SQLite に UPSERT
            _upsert(conn, listing, city_code, candidate, est, hold_years, today, now)
            saved += 1

        conn.commit()
    finally:
        conn.close()

    logger.info(
        "evaluate_and_save 完了: %d/%d 件を保存しました（%s / %s）",
        saved, len(listings), name, today,
    )
    return saved


# ---------------------------------------------------------------------------
# 今日の評価結果をまとめて取得（通知側から呼ぶ）
# ---------------------------------------------------------------------------

def load_evaluations_today(
    urls: list[str],
    db_path: Optional[Path] = None,
    _today: Optional[str] = None,
) -> dict[str, dict]:
    """
    指定された URL リストのうち、今日の評価結果を {url: row_dict} で返す。
    DB 未作成・評価なし・例外のときはすべて空 dict を返す（例外を出さない）。

    引数:
        urls   : 評価結果を引きたい物件URLのリスト
        db_path: SQLite ファイルのパス。None なら呼び出し時点の DB_PATH を使う
        _today : テスト用。"YYYY-MM-DD" を渡すと今日の代わりに使われる
    """
    if db_path is None:
        db_path = DB_PATH
    today = _today or date.today().isoformat()
    if not urls or not db_path.exists():
        return {}

    result: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" * len(urls))
            rows = conn.execute(
                f"SELECT * FROM evaluations "
                f"WHERE evaluated_date = ? AND listing_url IN ({placeholders})",
                [today] + list(urls),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            result[row["listing_url"]] = dict(row)
    except sqlite3.OperationalError:
        # テーブルが未作成（初回実行時など）は空 dict を返す
        pass
    return result


# ---------------------------------------------------------------------------
# 価格変動・スコア改善の検知（2日以上の履歴がある物件のみ対象）
# ---------------------------------------------------------------------------

def detect_changes(
    urls: list[str],
    db_path: Optional[Path] = None,
    _today: Optional[str] = None,
    min_price_drop: int = PRICE_DROP_THRESHOLD,
    min_score_gain: int = SCORE_GAIN_THRESHOLD,
) -> list[dict]:
    """
    指定 URL について今日の評価と直近の別日評価を比較し、
    値下げ or スコア改善が閾値以上だった物件のアラートリストを返す。

    重複抑制の仕組み:
        「直前の別日」との差分を取るため、翌日も同価格なら差分=0 → 通知なし。
        追加のフラグ管理は不要で、evaluate_and_save が毎日 current 全件を
        評価してDB行を積むことで自然に保証される。

    戻り値: list of {
        "url": str,
        "name": str,
        "today": dict,      # 今日の評価行（全カラム）
        "prev":  dict,      # 直近別日の評価行（全カラム）
        "price_drop": int,  # 下落額（正の値 = 価格が下がった）
        "score_gain": int,  # スコア改善（正の値 = スコアが上がった）
    }

    DB がない・前回評価がない・例外が出た場合は空リストを返す（例外を出さない）。

    引数:
        urls          : 比較対象の物件 URL リスト
        db_path       : SQLite ファイルのパス。None なら呼び出し時点の DB_PATH を使う
        _today        : テスト用。"YYYY-MM-DD" を渡すと今日の代わりに使われる
        min_price_drop: 通知する価格下落の下限（円）
        min_score_gain: 通知するスコア改善の下限（点）
    """
    if db_path is None:
        db_path = DB_PATH
    today = _today or date.today().isoformat()
    if not urls or not db_path.exists():
        return []

    alerts: list[dict] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for url in urls:
                # 今日の評価行を取得
                today_row = conn.execute(
                    "SELECT * FROM evaluations "
                    "WHERE listing_url = ? AND evaluated_date = ?",
                    (url, today),
                ).fetchone()
                if today_row is None:
                    continue  # 今日の評価がない物件はスキップ

                # 直近の別日評価（今日より古い最新行）を取得
                prev_row = conn.execute(
                    "SELECT * FROM evaluations "
                    "WHERE listing_url = ? AND evaluated_date < ? "
                    "ORDER BY evaluated_date DESC LIMIT 1",
                    (url, today),
                ).fetchone()
                if prev_row is None:
                    continue  # 前回がない（初回評価）はスキップ

                today_dict = dict(today_row)
                prev_dict  = dict(prev_row)

                t_price = today_dict.get("asking_price")
                p_price = prev_dict.get("asking_price")
                t_score = today_dict.get("resale_score")
                p_score = prev_dict.get("resale_score")

                # 下落額（正 = 価格が下がった）。None のときは 0 扱い
                price_drop = round(p_price - t_price) if (t_price is not None and p_price is not None) else 0
                # スコア改善（正 = スコアが上がった）
                score_gain = (t_score - p_score) if (t_score is not None and p_score is not None) else 0

                # どちらか一方でも閾値を超えたらアラート対象
                if price_drop >= min_price_drop or score_gain >= min_score_gain:
                    alerts.append({
                        "url":        url,
                        "name":       today_dict.get("listing_name", ""),
                        "today":      today_dict,
                        "prev":       prev_dict,
                        "price_drop": price_drop,
                        "score_gain": score_gain,
                    })
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # テーブル未作成（初回実行時）は空リストを返す
        pass

    logger.info(
        "detect_changes: %d 件中 %d 件に変動を検知（%s）",
        len(urls), len(alerts), today,
    )
    return alerts


def get_listing_age_days(
    url: str,
    db_path: Optional[Path] = None,
    _today: Optional[str] = None,
) -> Optional[int]:
    """
    指定 URL が evaluations 履歴に「初めて現れた日」から今日までの経過日数を返す。

    ⚠ 重要な注意（誤解防止）:
        これは SUUMO の実際の掲載日ではなく、このボットが初めてこの物件を
        評価DBに記録した日からの日数（観測開始からの日数）である。
        DB はボット稼働開始以降しか存在しないため、稼働前から出ていた物件は
        実際の掲載期間より短く出る。あくまで「売れ残り」の近似指標として使う。

    戻り値:
        経過日数（0 以上の int）。
        DB がない・該当 URL の履歴がない・日付が壊れている・例外時は None
        （通知側で「行を出さない」判断に使う。ここでは絶対に例外を投げない）。

    引数:
        url    : 対象物件の URL
        db_path: SQLite ファイルのパス。None なら呼び出し時点の DB_PATH を使う
        _today : テスト用。"YYYY-MM-DD" を渡すと today の代わりに使われる
    """
    if db_path is None:
        db_path = DB_PATH
    today_str = _today or date.today().isoformat()
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(db_path)
        try:
            # evaluated_date は "YYYY-MM-DD"。ISO 形式なので文字列 MIN = 最古日。
            row = conn.execute(
                "SELECT MIN(evaluated_date) FROM evaluations WHERE listing_url = ?",
                (url,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # テーブル未作成など。履歴なし扱い。
        return None

    # 該当行なし → MIN は (None,) を返す
    if row is None or row[0] is None:
        return None

    try:
        first_date = date.fromisoformat(row[0])
        today_date = date.fromisoformat(today_str)
    except (ValueError, TypeError):
        return None

    delta = (today_date - first_date).days
    # 念のため負値（未来日付が混入した場合）は 0 に丸める
    return delta if delta >= 0 else 0


# ---------------------------------------------------------------------------
# 指値候補の重複通知抑制（STEP4）
# ---------------------------------------------------------------------------
#
# 「指値候補として通知したことがあるか、そのときの強気度は何だったか」を
# 記録する専用テーブル。evaluations / detail_cache とは独立した追加であり、
# 既存テーブル・既存カラムには一切触れない。
#
# 抑制方針: 同じ強気度（aggressive/standard/mild）のままなら再通知しない。
# 強気度が変わった（例: standard→aggressive に強まった）ときだけ、
# 新しい交渉の目安として再通知する。値下げ検知(detect_changes)と同じ
# 「意味のある変化のときだけ通知する」という設計思想に合わせている。

def _init_sashine_table(conn: sqlite3.Connection) -> None:
    """sashine_notifications テーブルを作る（すでにあれば何もしない）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sashine_notifications (
            listing_url    TEXT PRIMARY KEY,  -- 物件の一意キー（SUUMOのURL）
            aggressiveness TEXT    NOT NULL,  -- 最後に通知したときの強気度
            notified_date  TEXT    NOT NULL   -- 最後に通知した日（"YYYY-MM-DD"）
        )
    """)
    conn.commit()


def get_sashine_notified_aggressiveness(
    url: str,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """
    指定 URL を過去に指値候補として通知したときの強気度を返す。
    一度も通知していない・DB未作成・例外のときは None を返す（例外は出さない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            _init_sashine_table(conn)
            row = conn.execute(
                "SELECT aggressiveness FROM sashine_notifications WHERE listing_url = ?",
                (url,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def mark_sashine_notified(
    url: str,
    aggressiveness: str,
    db_path: Optional[Path] = None,
    _today: Optional[str] = None,
) -> None:
    """
    指値候補として通知したことを記録する（UPSERT）。
    同じ URL に対して二回目以降は aggressiveness・notified_date を上書きする
    （＝常に「最後に通知したときの強気度」を保持する）。
    DB書き込みに失敗しても例外は投げない（通知自体は既に送信済みのため、
    記録の失敗だけで処理全体を止める必要はない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    today = _today or date.today().isoformat()
    try:
        conn = sqlite3.connect(db_path)
        try:
            _init_sashine_table(conn)
            conn.execute(
                """
                INSERT INTO sashine_notifications (listing_url, aggressiveness, notified_date)
                VALUES (?, ?, ?)
                ON CONFLICT(listing_url) DO UPDATE SET
                    aggressiveness = excluded.aggressiveness,
                    notified_date  = excluded.notified_date
                """,
                (url, aggressiveness, today),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("mark_sashine_notified 失敗（通知は既に送信済み）: %s", e)


# ---------------------------------------------------------------------------
# 参考枠の重複通知抑制（既知物件を対象に含めたことに伴う追加）
# ---------------------------------------------------------------------------
#
# 「参考枠として一度でも通知したことがあるか」を記録する専用テーブル。
# sashine_notifications と同じ設計思想だが、参考枠は有望/非有望の二値
# 判定であり強気度のような段階的な指標がないため、抑制方式は
# 「一度通知したら以後ずっと抑制する」というシンプルなものにしている
# （一度非該当になった後に再度該当した場合の再通知は今回のスコープ外）。

def _init_reference_table(conn: sqlite3.Connection) -> None:
    """reference_notifications テーブルを作る（すでにあれば何もしない）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reference_notifications (
            listing_url   TEXT PRIMARY KEY,  -- 物件の一意キー（SUUMOのURL）
            notified_date TEXT NOT NULL      -- 通知した日（"YYYY-MM-DD"）
        )
    """)
    conn.commit()


def is_reference_notified(
    url: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    指定 URL を過去に参考枠として通知したことがあるかを返す。
    DB未作成・例外のときは False を返す（例外は出さない。
    False＝未通知扱いにすることで、記録が読めない場合でも通知自体は
    継続できる安全側の挙動にしている）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            _init_reference_table(conn)
            row = conn.execute(
                "SELECT 1 FROM reference_notifications WHERE listing_url = ?",
                (url,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def mark_reference_notified(
    url: str,
    db_path: Optional[Path] = None,
    _today: Optional[str] = None,
) -> None:
    """
    参考枠として通知したことを記録する（UPSERT）。
    DB書き込みに失敗しても例外は投げない（通知自体は既に送信済みのため、
    記録の失敗だけで処理全体を止める必要はない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    today = _today or date.today().isoformat()
    try:
        conn = sqlite3.connect(db_path)
        try:
            _init_reference_table(conn)
            conn.execute(
                """
                INSERT INTO reference_notifications (listing_url, notified_date)
                VALUES (?, ?)
                ON CONFLICT(listing_url) DO UPDATE SET
                    notified_date = excluded.notified_date
                """,
                (url, today),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("mark_reference_notified 失敗（通知は既に送信済み）: %s", e)


# ---------------------------------------------------------------------------
# エントリポイント（手動実行・動作確認用）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # サンプル物件（動作確認用ダミー）
    sample = Listing(
        name="サンプル物件マンション（テスト用）",
        price="4,200万円",
        location="東京都調布市曙町",
        url="https://suumo.jp/sample/test-99999/",
        station="京王線 調布駅 徒歩6分",
        floor_plan="3LDK",
        area="72.5m²",
        age="2018年3月",
    )

    n = evaluate_and_save([sample], city_code="13208", db_path=DB_PATH)
    print(f"\n保存件数: {n} 件 → {DB_PATH}")
    if n > 0:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT listing_name, resale_score, asking_price, asking_vs_fair_pct, notes "
            "FROM evaluations ORDER BY evaluated_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        print(f"物件名  : {row['listing_name']}")
        print(f"スコア  : {row['resale_score']}/100")
        print(f"売出価格: {row['asking_price']:,.0f} 円")
        print(f"乖離率  : {row['asking_vs_fair_pct']:+.1f}%" if row['asking_vs_fair_pct'] else "乖離率  : （カーブなし）")
        for note in json.loads(row["notes"]):
            print(f"  注意: {note}")
