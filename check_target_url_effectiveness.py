"""
check_target_url_effectiveness.py
====================
【診断専用スクリプト】

本番の GitHub Secrets TARGET_URL が実際にどう機能しているかを、
値そのものを見ずに確認する。

【背景】
    ローカル環境では po=1&pj=2 込みの URL でグローブスクエア
    （nc_21178478）が27番目に来ることを確認済みだが、本番Actions実行
    では MAX_PAGES 件まで見ても一度も出てこない。物件自体はまだ
    掲載中で動きもないため、Secrets TARGET_URL が本当に意図通り
    設定されているかを、効果を通じて確認する必要がある。

【やること】
    1. TARGET_URL に "po=1"・"pj=2" の両方が含まれるかを真偽値のみ
       出力する（値そのものは一切出力しない）
    2. TARGET_URL が scraper.DEFAULT_URL と一致するか（＝Secretsが
       未設定でデフォルトにフォールバックしているだけか）を真偽値のみ
       出力する
    3. TARGET_URL で実際に検索結果を取得し（本番と同じ MAX_PAGES・
       REQUEST_INTERVAL）、グローブスクエア（nc_21178478）が
       何番目に位置するか、または見つからないかを出力する

【安全設計（重要）】
    - TARGET_URL の値そのものは一切ログに出さない。
      scraper.fetch_page は url=... をログ出力するためここでは使わず、
      URL を出力しない専用の取得関数を使う。
    - 例外メッセージも str(e) は使わず e.__class__.__name__ のみ出力する
      （requests例外のメッセージにURLが含まれることがあるため）。
    - LINE通知・DB書き込み・data.csvコミット・キャッシュ書き込みは
      一切しない（SUUMOへの読み取りアクセスのみ）。
"""

import time

import requests
from bs4 import BeautifulSoup

from scraper import (
    DEFAULT_URL,
    HEADERS,
    MAX_PAGES,
    REQUEST_INTERVAL,
    TARGET_URL,
    get_next_page_url,
    parse_listings,
)

# 確認対象物件（グローブスクエア）
TARGET_LISTING_URL_FRAGMENT = "nc_21178478"


def _fetch_page_without_logging_url(url: str) -> BeautifulSoup:
    """
    scraper.fetch_page と同じ取得処理だが、URLそのものをログに出さない版。
    TARGET_URL は Secrets 由来のため、値をログに残さないための配慮。
    """
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"  [HTTP] status={resp.status_code}", flush=True)  # URLは出さない
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "html.parser")


def main() -> None:
    print("=== TARGET_URL 実効性確認（診断専用・読み取り専用） ===\n")

    # ---- 確認1: po=1&pj=2 が含まれるか（真偽値のみ）----
    has_po1 = "po=1" in TARGET_URL
    has_pj2 = "pj=2" in TARGET_URL
    print(f"po=1 を含む: {has_po1}")
    print(f"pj=2 を含む: {has_pj2}")
    print(f"新着・更新順パラメータ(po=1&pj=2)を含む: {has_po1 and has_pj2}")

    # ---- 確認2: Secretsが未設定でデフォルトにフォールバックしていないか ----
    is_default = TARGET_URL == DEFAULT_URL
    print(f"TARGET_URL が scraper.DEFAULT_URL と完全一致: {is_default}")
    print()

    # ---- 確認3: 実際に検索してグローブスクエアの位置を確認 ----
    print(f"MAX_PAGES={MAX_PAGES} 件まで検索します...")
    url = TARGET_URL
    found_position = None
    total_parsed = 0

    for page in range(1, MAX_PAGES + 1):
        print(f"  ページ{page}取得中...")
        try:
            soup = _fetch_page_without_logging_url(url)
        except requests.RequestException as e:
            # str(e) は使わない（URLが含まれる可能性があるため）
            print(f"  [警告] ページ取得失敗: {e.__class__.__name__}")
            break

        listings = parse_listings(soup)
        print(f"  → {len(listings)}件パース")

        for listing in listings:
            total_parsed += 1
            if TARGET_LISTING_URL_FRAGMENT in listing.url:
                found_position = total_parsed
                print(f"  ★発見！ 全体で{found_position}番目")
                print(f"    物件名: {listing.name}")
                print(f"    価格: {listing.price}")
                print(f"    所在地: {listing.location}")

        if found_position is not None:
            break

        next_url = get_next_page_url(soup, url)
        if not next_url:
            print("  次ページなし（終了）")
            break
        url = next_url  # 中身はログに出さない
        time.sleep(REQUEST_INTERVAL)

    print()
    print("=== 結果 ===")
    print(f"パース総件数: {total_parsed}")
    if found_position is not None:
        print(f"グローブスクエア(nc_21178478): {found_position}番目に発見")
    else:
        print(
            f"グローブスクエア(nc_21178478): "
            f"MAX_PAGES={MAX_PAGES}件（{total_parsed}件）の範囲内に見つかりませんでした"
        )


if __name__ == "__main__":
    main()
