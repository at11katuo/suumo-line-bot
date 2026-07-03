"""
detail_fetcher.py
====================
SUUMO 詳細ページから「総戸数」「修繕積立金」を取得し、
evaluations.db の detail_cache テーブルにキャッシュするモジュール。

【責務】
  - 詳細ページへの HTTP アクセス（新着・未登録物件のみ）
  - 総戸数・修繕積立金のパース
  - detail_cache テーブルへの保存・読み込み

【やらないこと】
  - LINE通知・評価（evaluate_and_save の責務）
  - parse_listings への混入（一覧取得と詳細取得は分離）

【補強事項（設計時に確認済み）】
  - fetch_detail は timeout=DETAIL_TIMEOUT を設定。無限待ちによる Actions 固まり防止。
  - scraper.main() から「detail_cache 未登録の新着のみ」を渡してもらう。
    get_uncached_urls() でフィルタ後に fetch するのが正しい使い方。
  - 取得失敗時も NULL レコードを保存して「試み済み」フラグとする（次回重複 fetch 防止）。
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# HEADERS は既存 scraper.py と同じ User-Agent を使う（偽装・小細工なし）
# 循環インポートを避けるため、lazy import ではなくここで直接インポートする。
# scraper.py は detail_fetcher を main() 内でのみインポートするため循環なし。
from scraper import HEADERS

# ---- 設定 ----

# evaluations.db と同じファイルに detail_cache テーブルを置く（管理1ファイルで完結）
DB_PATH = Path(__file__).parent / "evaluations.db"

# 詳細ページ1件ごとのアクセス間隔（秒）。礼儀正しいアクセス。
DETAIL_SLEEP_SEC: float = 4.0

# タイムアウト（秒）。GitHub Actions が無限待ちになるのを防ぐ。
DETAIL_TIMEOUT: int = 15

# SUUMO 詳細ページのラベルキーワード。
# 実際には "総戸数ヒント" のようなサフィックス付きのラベルになるが、
# _find_value() の部分一致でヒットするため直書きでよい（probe_detail.py で確認済み）。
_LABEL_TOTAL_UNITS = "総戸数"
_LABEL_REPAIR_FUND = "修繕積立金"


# ---------------------------------------------------------------------------
# HTML 解析ヘルパー（probe_detail.py の build_label_map / find_value と同ロジック）
# ---------------------------------------------------------------------------

def _build_label_map(soup: BeautifulSoup) -> dict[str, str]:
    """
    ページ内の dt/dd ペアと table(th/td) ペアから「ラベル→値」辞書を作る。
    SUUMO は両形式を混在させているため両方を収集する。
    """
    label_map: dict[str, str] = {}

    # dt/dd 形式: <dt>総戸数ヒント</dt><dd>32戸</dd>
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True)
        dd = dt.find_next_sibling("dd")
        if dd and label:
            label_map[label] = dd.get_text(strip=True)

    # table の th/td 形式: <th>修繕積立金ヒント</th><td>2万4080円／月</td>
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            ths = tr.find_all("th")
            tds = tr.find_all("td")
            if ths and tds:
                for th, td in zip(ths, tds):
                    label = th.get_text(strip=True)
                    if label:
                        label_map[label] = td.get_text(strip=True)

    return label_map


def _find_value(label_map: dict[str, str], keyword: str) -> Optional[str]:
    """
    keyword に完全一致するラベルを優先し、なければ keyword を含む部分一致を試みる。
    例: "総戸数" で "総戸数ヒント" にもマッチする。
    """
    if keyword in label_map:
        return label_map[keyword]
    for key, val in label_map.items():
        if keyword in key:
            return val
    return None


# ---------------------------------------------------------------------------
# パース関数
# ---------------------------------------------------------------------------

def _parse_total_units(s: str) -> Optional[int]:
    """
    "32戸" → 32。パース失敗は None。

    対応パターン: "32戸" "100戸"
    非対応（None を返す）: "-" "" "32" など「戸」のない文字列
    """
    if not s:
        return None
    m = re.search(r'(\d+)戸', s)
    return int(m.group(1)) if m else None


def _parse_repair_fund_monthly(s: str) -> Optional[float]:
    """
    修繕積立金の月額を円(float)に変換する。

    対応パターン:
      "2万4080円／月"          → 24080.0   （万 + 端数）
      "1万7230円／月（委託…）" → 17230.0   （万 + 端数 + 括弧内説明）
      "12,300円／月"           → 12300.0   （コンマ区切り）
      "5000円"                 → 5000.0    （単純な円表記）
    非対応（None を返す）: "-" "－" "−" "" None
    """
    if not s or s.strip() in ("-", "－", "−", ""):
        return None

    # 万 + 端数: "2万4080円"
    m = re.search(r'(\d+)万(\d+)円', s)
    if m:
        return float(int(m.group(1)) * 10_000 + int(m.group(2)))

    # 万のみ（小数あり可）: "2.5万円"
    m = re.search(r'([\d.]+)万円', s)
    if m:
        return float(m.group(1)) * 10_000

    # 通常の円: "12,300円" "5000円"
    m = re.search(r'([\d,]+)円', s)
    if m:
        return float(m.group(1).replace(",", ""))

    return None


# ---------------------------------------------------------------------------
# DB 操作
# ---------------------------------------------------------------------------

def _init_detail_table(conn: sqlite3.Connection) -> None:
    """
    detail_cache テーブルを作成する（すでに存在する場合はスキップ）。
    evaluations.db に同居させるが、evaluations テーブルとは独立して管理する。
    総戸数・修繕積立金は物件固有の不変データのため、日次履歴は不要（1行 = 1物件）。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS detail_cache (
            listing_url         TEXT PRIMARY KEY,  -- SUUMO 物件 URL（一意キー）
            total_units         INTEGER,           -- 総戸数（戸）。取得不可なら NULL
            repair_fund_monthly REAL,              -- 修繕積立金月額（円）。取得不可なら NULL
            fetched_at          TEXT               -- 取得日時（ISO 8601）
        )
    """)
    conn.commit()


def save_detail_cache(url: str, data: dict, db_path: Optional[Path] = None) -> None:
    """
    取得した詳細データを detail_cache テーブルに保存する（INSERT OR REPLACE）。

    取得失敗時（data = {"total_units": None, "repair_fund_monthly": None}）も
    保存することで「試み済み」のフラグとなり、次回実行での重複 fetch を防ぐ。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        _init_detail_table(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO detail_cache
                (listing_url, total_units, repair_fund_monthly, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (url, data.get("total_units"), data.get("repair_fund_monthly"), now),
        )
        conn.commit()
    finally:
        conn.close()


def load_detail_cache(urls: list[str], db_path: Optional[Path] = None) -> dict[str, dict]:
    """
    detail_cache テーブルから指定 URL 群のキャッシュを一括取得する。

    戻り値:
        {url: {"total_units": int|None, "repair_fund_monthly": float|None}}
        DB が存在しない・テーブル未作成・例外時は空 dict（例外を出さない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    if not urls or not db_path.exists():
        return {}

    result: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" * len(urls))
            rows = conn.execute(
                f"SELECT listing_url, total_units, repair_fund_monthly "
                f"FROM detail_cache WHERE listing_url IN ({placeholders})",
                list(urls),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            result[row["listing_url"]] = {
                "total_units":         row["total_units"],
                "repair_fund_monthly": row["repair_fund_monthly"],
            }
    except sqlite3.OperationalError:
        # テーブル未作成（初回実行など）は空 dict を返す
        pass

    return result


def get_uncached_urls(urls: list[str], db_path: Optional[Path] = None) -> list[str]:
    """
    detail_cache テーブルに「一度も登録されていない」URL のみを返す。
    登録済み（取得失敗で NULL の場合も含む）はスキップ対象。

    これにより「新着物件のうち未取得の物件だけ」fetch できる。
    取得失敗（NULL）の物件は再 fetch しない（transient エラーのリトライは行わない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    if not urls:
        return []
    if not db_path.exists():
        return list(urls)

    try:
        conn = sqlite3.connect(db_path)
        try:
            placeholders = ",".join("?" * len(urls))
            cached_rows = conn.execute(
                f"SELECT listing_url FROM detail_cache WHERE listing_url IN ({placeholders})",
                list(urls),
            ).fetchall()
        finally:
            conn.close()
        cached_set = {row[0] for row in cached_rows}
        return [u for u in urls if u not in cached_set]
    except sqlite3.OperationalError:
        # detail_cache テーブル未作成のときは全件を未登録とみなす
        return list(urls)


# ---------------------------------------------------------------------------
# 詳細ページ取得（メイン関数）
# ---------------------------------------------------------------------------

def fetch_detail(url: str, sleep_sec: float = DETAIL_SLEEP_SEC) -> Optional[dict]:
    """
    SUUMO 詳細ページ 1件から総戸数・修繕積立金を取得して返す。

    引数:
        url      : SUUMO 詳細ページの URL
        sleep_sec: リクエスト前の待機秒数（礼儀正しいアクセス。テスト時は 0 を渡す）

    戻り値:
        {"total_units": int|None, "repair_fund_monthly": float|None}
        HTTP エラー / タイムアウト / パース例外のいずれでも None を返す。
        None の場合、呼び出し側は中立スコア（total_units=None / repair_fund_per_sqm=None）
        のままフォールバックする（物件はスキップしない）。

    注意:
        - timeout=DETAIL_TIMEOUT を設定済み。タイムアウトしても None を返す。
          GitHub Actions が無限待ちになることはない。
        - sleep は「リクエスト前」に入れる。最後の物件の後は追加待機なし（自然に終了）。
    """
    time.sleep(sleep_sec)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(
            f"  [詳細取得] 失敗 → 中立スコアにフォールバック: {e.__class__.__name__}",
            flush=True,
        )
        return None

    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    label_map = _build_label_map(soup)

    raw_units  = _find_value(label_map, _LABEL_TOTAL_UNITS)
    raw_repair = _find_value(label_map, _LABEL_REPAIR_FUND)

    total_units         = _parse_total_units(raw_units)           if raw_units  else None
    repair_fund_monthly = _parse_repair_fund_monthly(raw_repair)  if raw_repair else None

    print(
        f"  [詳細取得] 総戸数={total_units}戸 修繕積立金={repair_fund_monthly}円/月"
        f"  ({url[:55]})",
        flush=True,
    )
    return {"total_units": total_units, "repair_fund_monthly": repair_fund_monthly}
