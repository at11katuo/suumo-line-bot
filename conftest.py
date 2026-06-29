import sys
import os

# suumo-line-bot/ をモジュール検索パスに追加する。
# pytest をどのディレクトリから実行しても "from scraper import Listing" などが通るようにする。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
