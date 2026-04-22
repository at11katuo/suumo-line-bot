"""
不動産新着物件スクレイパー (SUUMO 中古マンション)
- 新着物件を取得し data.csv と差分比較
- 新着があれば LINE Notify で通知
"""

import csv
import os
import sys
import time
from dataclasses import dataclass, fields
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ #
# 設定
# ------------------------------------------------------------------ #
LINE_TOKEN: Optional[str] = os.environ.get("LINE_TOKEN")
DATA_FILE = "data.csv"

# 検索URLは環境変数で上書き可能
# デフォルト: 首都圏・中古マンション・5000万以下
# SUUMO の検索条件を変えたい場合は TARGET_URL を書き換える
DEFAULT_URL = (
    "https://suumo.jp/jj/bukken/ichiran/JJ010FJ001/"
    "?ar=030&bs=011&ta=13&cb=0.0&ct=5000.0"
    "&md=&et=&mb=0&mt=9999999&shkr1=03&cnb=0&cn=9999999&srch_navi=1"
)
TARGET_URL: str = os.environ.get("TARGET_URL", DEFAULT_URL)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}

MAX_PAGES = 3          # 取得する最大ページ数（増やすと通知件数が増える）
REQUEST_INTERVAL = 2   # ページ間のウェイト（秒）


# ------------------------------------------------------------------ #
# データモデル
# ------------------------------------------------------------------ #
@dataclass
class Listing:
    name: str
    price: str
    location: str
    url: str

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ------------------------------------------------------------------ #
# スクレイピング
# ------------------------------------------------------------------ #
def fetch_page(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "html.parser")


def parse_listings(soup: BeautifulSoup) -> list[Listing]:
    """
    SUUMO の物件一覧ページをパースする。

    ※ SUUMO はサイトリニューアルでセレクタが変わることがあります。
      実際の HTML 構造を確認して、下記セレクタを調整してください。
    """
    results: list[Listing] = []

    # 各物件カセット
    for card in soup.select("div.cassette"):
        # 物件名
        name_el = card.select_one("h2.property_unit-title, .cassette_top-title")
        name = name_el.get_text(strip=True) if name_el else "（名称不明）"

        # 価格（最初の価格要素を取得）
        price_el = card.select_one(
            "span.dottable-value, .cassette_price-price, .price"
        )
        price = price_el.get_text(strip=True) if price_el else "（価格不明）"

        # 所在地
        location_el = card.select_one(
            ".cassette_detail-col1 li:first-child, "
            ".dottable-valueRow .dottable-value"
        )
        location = location_el.get_text(strip=True) if location_el else "（所在地不明）"

        # URL
        link_el = card.select_one("a[href*='/ms/bukken/']")
        if link_el is None:
            link_el = card.select_one("a[href]")
        url = ""
        if link_el:
            href = link_el.get("href", "")
            url = href if href.startswith("http") else f"https://suumo.jp{href}"

        if url:  # URL が取れた物件だけ追加
            results.append(Listing(name=name, price=price, location=location, url=url))

    return results


def get_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """「次へ」リンクの URL を返す。なければ None。"""
    next_el = soup.select_one("p.pagination-pager_next a, a[class*='pagination'][rel='next']")
    if not next_el:
        return None
    href = next_el.get("href", "")
    return href if href.startswith("http") else f"https://suumo.jp{href}"


def scrape(start_url: str) -> list[Listing]:
    all_listings: list[Listing] = []
    url: Optional[str] = start_url

    for page in range(1, MAX_PAGES + 1):
        print(f"  ページ {page} 取得中: {url}", flush=True)
        try:
            soup = fetch_page(url)
        except requests.RequestException as e:
            print(f"  [警告] ページ取得失敗: {e}", flush=True)
            break

        listings = parse_listings(soup)
        print(f"  → {len(listings)} 件パース", flush=True)
        all_listings.extend(listings)

        next_url = get_next_page_url(soup, url)
        if not next_url:
            break
        url = next_url
        time.sleep(REQUEST_INTERVAL)

    # URL 重複除去（URLを一意キーとする）
    seen: set[str] = set()
    unique: list[Listing] = []
    for l in all_listings:
        if l.url not in seen:
            seen.add(l.url)
            unique.append(l)

    return unique


# ------------------------------------------------------------------ #
# CSV 操作
# ------------------------------------------------------------------ #
CSV_FIELDNAMES = [f.name for f in fields(Listing)]


def load_known_urls(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {row["url"] for row in reader if row.get("url")}


def save_listings(path: str, listings: list[Listing]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(l.to_dict() for l in listings)


# ------------------------------------------------------------------ #
# LINE Notify
# ------------------------------------------------------------------ #
def notify_line(new_listings: list[Listing]) -> None:
    if not LINE_TOKEN:
        print("[警告] LINE_TOKEN が未設定のため通知をスキップします。", flush=True)
        return

    # 通知を 10 件ずつ分割（1メッセージが長すぎるのを防ぐ）
    chunk_size = 10
    for i in range(0, len(new_listings), chunk_size):
        chunk = new_listings[i : i + chunk_size]

        lines = [f"\n🏠 新着物件 {i + 1}〜{i + len(chunk)} 件"]
        for idx, l in enumerate(chunk, start=i + 1):
            lines.append(
                f"\n【{idx}】{l.name}\n"
                f"  価格 : {l.price}\n"
                f"  所在地: {l.location}\n"
                f"  URL  : {l.url}"
            )
        message = "\n".join(lines)

        resp = requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            data={"message": message},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[警告] LINE 通知失敗: {resp.status_code} {resp.text}", flush=True)
        else:
            print(f"  LINE 通知送信: {len(chunk)} 件", flush=True)

        time.sleep(1)


# ------------------------------------------------------------------ #
# エントリポイント
# ------------------------------------------------------------------ #
def main() -> None:
    print(f"=== 不動産スクレイパー 開始 ===", flush=True)
    print(f"対象URL: {TARGET_URL}", flush=True)

    # スクレイピング
    current = scrape(TARGET_URL)
    if not current:
        print("物件が取得できませんでした。セレクタを確認してください。", flush=True)
        sys.exit(1)
    print(f"合計取得: {len(current)} 件", flush=True)

    # 差分比較
    known_urls = load_known_urls(DATA_FILE)
    new_listings = [l for l in current if l.url not in known_urls]
    print(f"新着: {len(new_listings)} 件 (既知: {len(known_urls)} 件)", flush=True)

    # 通知 & 保存
    if new_listings:
        notify_line(new_listings)
        save_listings(DATA_FILE, current)
        print("data.csv を更新しました。", flush=True)
    else:
        print("新着物件はありませんでした。", flush=True)

    print("=== 完了 ===", flush=True)


if __name__ == "__main__":
    main()
