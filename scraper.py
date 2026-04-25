"""
不動産新着物件スクレイパー (SUUMO 中古マンション)
- 新着物件を取得し data.csv と差分比較
- 新着があれば LINE Messaging API (Push) で通知
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
LINE_CHANNEL_ACCESS_TOKEN: Optional[str] = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID: Optional[str] = os.environ.get("LINE_USER_ID")
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
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
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
    print(f"  [HTTP] status={resp.status_code} url={url}", flush=True)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "（タイトルなし）"
    print(f"  [PAGE] title=「{title}」", flush=True)
    return soup


def _extract_dt_dd(card: BeautifulSoup) -> dict[str, str]:
    """dl.cassetteItem_data 内の dt/dd ペアを辞書で返す。"""
    result: dict[str, str] = {}
    dl = card.select_one("dl.cassetteItem_data")
    if not dl:
        return result
    for dt in dl.select("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            result[dt.get_text(strip=True)] = dd.get_text(strip=True)
    return result


def parse_listings(soup: BeautifulSoup) -> list[Listing]:
    results: list[Listing] = []

    for card in soup.select("div.cassetteItem"):
        # 物件名 + URL（h3.cassetteItem_title 内の a タグ）
        title_el = card.select_one("h3.cassetteItem_title a")
        if not title_el:
            continue
        name = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = href if href.startswith("http") else f"https://suumo.jp{href}"

        # dt/dd ペアから価格・所在地を取得
        data = _extract_dt_dd(card)
        price    = data.get("販売価格", "（価格不明）")
        location = data.get("所在地",   "（所在地不明）")

        results.append(Listing(name=name, price=price, location=location, url=url))

    return results


def get_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """テキストが「次へ」の a タグを探して URL を返す。なければ None。"""
    for a in soup.select("a"):
        if a.get_text(strip=True) == "次へ":
            href = a.get("href", "")
            return href if href.startswith("http") else f"https://suumo.jp{href}"
    return None


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
# LINE Messaging API (Push)
# ------------------------------------------------------------------ #
LINE_API_URL = "https://api.line.me/v2/bot/message/push"
# 1回のAPIコールで送れるメッセージ数の上限（LINE仕様）
_MAX_MESSAGES_PER_CALL = 5
# 1メッセージに含める物件数
_LISTINGS_PER_MESSAGE = 5


def _build_text(listings: list[Listing], offset: int) -> str:
    lines = []
    for idx, l in enumerate(listings, start=offset + 1):
        lines.append(
            f"【{idx}】{l.name}\n"
            f"  価格 : {l.price}\n"
            f"  所在地: {l.location}\n"
            f"  URL  : {l.url}"
        )
    return "\n\n".join(lines)


def notify_line(new_listings: list[Listing]) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[警告] LINE_CHANNEL_ACCESS_TOKEN が未設定のため通知をスキップします。", flush=True)
        return
    if not LINE_USER_ID:
        print("[警告] LINE_USER_ID が未設定のため通知をスキップします。", flush=True)
        return

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # 物件を _LISTINGS_PER_MESSAGE 件ずつのテキストメッセージに変換
    message_texts: list[str] = []

    # 先頭に件数サマリを追加
    message_texts.append(f"🏠 SUUMO 新着物件 {len(new_listings)} 件が見つかりました！")

    for i in range(0, len(new_listings), _LISTINGS_PER_MESSAGE):
        chunk = new_listings[i : i + _LISTINGS_PER_MESSAGE]
        message_texts.append(_build_text(chunk, i))

    # _MAX_MESSAGES_PER_CALL 件ずつ API を呼ぶ
    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch = message_texts[batch_start : batch_start + _MAX_MESSAGES_PER_CALL]
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[警告] LINE 通知失敗: {resp.status_code} {resp.text}", flush=True)
        else:
            print(f"  LINE Push 送信: {len(batch)} メッセージ", flush=True)
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
