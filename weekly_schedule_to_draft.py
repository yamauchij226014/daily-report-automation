"""
就労移行支援施設Webサイトの週間カレンダーから「オンライン講座」の内容を抽出し、
Gmailのメールの本文に転記したのち下書きフォルダに保存するスクリプト

仕様:
    - 直近2週間のカレンダー情報を取得する
      (カレンダーは古い週が順次削除されていく仕様のため、Webページの上から2週分を取得すればそれが直近2週間になる)
    - 実行するたびに、件名が「オンライン講座」で始まる既存の下書きメールを全て削除してから、
      新しい「オンライン講座」メールを1件作成する(下書きが溜まらないようにするため)

前提:
    - 週1回程度、手動でこのスクリプトを実行する
    - 実行時にブラウザが開くので、その場で手動でGoogleアカウントにログインする
    - ログイン情報は実行後にPCへ残さない(専用プロファイルを削除する)

事前準備:
    1. pip install selenium beautifulsoup4
    2. Googleアカウントで「アプリパスワード」を発行し、下記 GMAIL_APP_PASSWORD に設定
    3. 下記 GMAIL_ADDRESS にGmailアドレスを設定
"""

import email
import imaplib
import os
import re
import shutil
import tempfile
import time
from datetime import date
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ========== 設定項目 ==========

URL = (
    "https://"
)

# Selenium専用の一時プロファイル(実行後に削除する)
# C:\ 直下などは共用PC(ゲストアカウント等)では書き込み権限がないことが多いため、
# 自分のユーザーフォルダ配下(一時フォルダ)を使う。
PROFILE_DIR = os.path.join(tempfile.gettempdir(), "selenium_chrome_profile")

# 抽出したい行のラベル(この2つの文字列を両方含む1列目のセルを探す)
TARGET_LABEL_KEYWORDS = ["オンライン講座", "10:40"]

# 週間スケジュール下書きの件名プレフィックス(create_daily_report.pyと合わせる)
WEEKLY_SUBJECT_PREFIX = "オンライン講座"

# Gmail下書き作成用の設定(環境変数から読み込む。ローカル実行前に設定する。)
#   例(Windows PowerShell):
#     $env:GMAIL_ADDRESS="xxx@gmail.com"
#     $env:GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

# ========== ブラウザ内で実行するJavaScript ==========

# document及び配下の全Shadow Rootを再帰的に辿り、<table>のouterHTMLを全て返す
DEEP_FIND_TABLES_JS = """
function deepQueryAll(root, selector, results) {
    root.querySelectorAll(selector).forEach(el => results.push(el));
    root.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot) {
            deepQueryAll(el.shadowRoot, selector, results);
        }
    });
    return results;
}
const found = [];
deepQueryAll(document, 'table', found);
return found.map(t => t.outerHTML);
"""


# ========== 日付関連のユーティリティ ==========

def parse_header_date(cell_text, reference_year):
    """
    "9月7日月" のような文字列から date オブジェクトを作る。
    年をまたぐカレンダー(例: 12月末〜1月頭)は今回は考慮していない。
    """
    m = re.search(r"(\d+)月(\d+)日", cell_text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        return date(reference_year, month, day)
    except ValueError:
        return None


# ========== 表の探索・抽出 ==========

def find_tables_in_current_frame(driver):
    """現在switch_to済みのフレーム内から、Shadow DOMも含めて<table>を探し、
    BeautifulSoupなしでも扱えるよう、outerHTMLの文字列リストとして返す"""
    return driver.execute_script(DEEP_FIND_TABLES_JS)


def extract_row_texts_from_table_html(table_outer_html):
    """
    1つのtableのouterHTML文字列から、
    - ヘッダー行の日付リスト(None含む)
    - "オンライン講座"行の各列テキスト(リンクの場合は文字だけ)
    を取り出す。BeautifulSoupを使って解析する。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(table_outer_html, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        return None, None

    # ヘッダー行: th要素(または1行目)から日付を取り出す
    header_cells = rows[0].find_all(["th", "td"])
    header_dates = [parse_header_date(c.get_text(strip=True), date.today().year) for c in header_cells]

    # "オンライン講座"かつ"10:40"を含む行を探す
    for row in rows[1:]:
        cells = row.find_all("td")
        if not cells:
            continue
        label_text = cells[0].get_text(strip=True)
        if all(keyword in label_text for keyword in TARGET_LABEL_KEYWORDS):
            content_texts = [c.get_text(strip=True) for c in cells[1:]]
            return header_dates, content_texts

    return header_dates, None


def recurse_frames_and_collect(driver, result, state, depth=0):
    """
    フレームを再帰的に辿りながら、"オンライン講座"行のテキストを
    result(dict: date -> text) に集める。

    このページは、古い週のカレンダーが順次削除されて上に詰められる仕様のため、
    ページ上部から出てくる順(= 見つかった順)に2つの週テーブルを収集すれば、
    それが常に「直近2週間」に相当する。
    state['weeks_found'] で収集済みの週数を数え、2に達したら探索を打ち切る。
    """
    if depth > 5 or state["weeks_found"] >= 2:
        return

    table_htmls = find_tables_in_current_frame(driver)
    for table_html in table_htmls:
        if state["weeks_found"] >= 2:
            break

        header_dates, content_texts = extract_row_texts_from_table_html(table_html)
        if header_dates is None or not any(d is not None for d in header_dates):
            continue  # 日付ヘッダーを持たない表(カレンダー以外)は対象外

        state["weeks_found"] += 1  # 1つの週テーブルとしてカウント

        if content_texts is not None:
            # ヘッダーの1列目は空(ラベル列)なので、header_dates[1:]とcontent_textsが対応する
            for header_date, content_text in zip(header_dates[1:], content_texts):
                if header_date is not None:
                    result[header_date] = content_text

    if state["weeks_found"] >= 2:
        return

    # 子フレームを再帰的に探索
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for iframe in iframes:
        if state["weeks_found"] >= 2:
            break
        try:
            driver.switch_to.frame(iframe)
        except Exception:
            continue
        recurse_frames_and_collect(driver, result, state, depth + 1)
        driver.switch_to.parent_frame()


# ========== Gmail下書き作成(IMAP + アプリパスワード) ==========

def find_drafts_folder_name(imap):
    """
    Gmailの「下書き」フォルダは表示言語によって
    '[Gmail]/Drafts' だったり '[Gmail]/下書き' だったりするため、
    IMAPのLIST応答からGmailが付与する \\Drafts フラグを持つフォルダを
    自動で探す。
    """
    status, folders = imap.list()
    if status != "OK":
        raise RuntimeError("IMAPフォルダ一覧の取得に失敗しました")

    for entry in folders:
        entry_str = entry.decode("utf-8", errors="ignore")
        if "\\Drafts" in entry_str:
            # 例: '(\\HasNoChildren \\Drafts) "/" "[Gmail]/下書き"'
            m = re.search(r'"([^"]+)"$', entry_str)
            if m:
                return m.group(1)
    raise RuntimeError("下書きフォルダが見つかりませんでした")


def decode_mime_words(raw_subject):
    """MIMEエンコードされた件名をデコードして通常の文字列にする"""
    parts = decode_header(raw_subject or "")
    result = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            result += text.decode(charset or "utf-8", errors="replace")
        else:
            result += text
    return result


def delete_old_weekly_drafts(imap, drafts_folder):
    """件名が WEEKLY_SUBJECT_PREFIX で始まる既存の下書きを全て削除する
    (毎回実行するたびに古い週間スケジュールの下書きが溜まっていくのを防ぐため)"""
    imap.select(drafts_folder)
    status, nums = imap.search(None, "ALL")
    if status != "OK":
        return

    deleted_count = 0
    for num in nums[0].split():
        status, data = imap.fetch(num, "(RFC822.HEADER)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        subject = decode_mime_words(msg.get("Subject", ""))
        if subject.startswith(WEEKLY_SUBJECT_PREFIX):
            imap.store(num, "+FLAGS", "\\Deleted")
            deleted_count += 1

    if deleted_count:
        imap.expunge()
    print(f"古い「{WEEKLY_SUBJECT_PREFIX}」下書きを{deleted_count}件削除しました。")


def create_gmail_draft(gmail_address, app_password, subject, body):
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    msg["Date"] = formatdate(localtime=True)

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(gmail_address, app_password)
    try:
        drafts_folder = find_drafts_folder_name(imap)

        # 新しい下書きを作る前に、古い週間スケジュールの下書きを削除する
        delete_old_weekly_drafts(imap, drafts_folder)

        imap.append(
            drafts_folder,
            "",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
    finally:
        imap.logout()


# ========== メイン処理 ==========

def main():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("環境変数 GMAIL_ADDRESS / GMAIL_APP_PASSWORD が設定されていません。")

    options = Options()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    driver = webdriver.Chrome(options=options)

    try:
        driver.get(URL)
        time.sleep(5)

        input(
            "\nブラウザでGoogleアカウントにログインし、"
            "カレンダー表が表示されるページまで進めてください。\n"
            "準備ができたら Enter キーを押してください..."
        )

        result = {}
        state = {"weeks_found": 0}

        driver.switch_to.default_content()
        recurse_frames_and_collect(driver, result, state)

        if not result:
            raise RuntimeError("カレンダーからデータを1件も抽出できませんでした。")

        sorted_dates = sorted(result.keys())

        # 結果を日付順に整形
        lines = []
        weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
        for d in sorted_dates:
            lines.append(f"{d.strftime('%m/%d')}({weekday_names[d.weekday()]}): {result[d]}")

        body = "\n".join(lines)
        print("\n===== 抽出結果 =====")
        print(body)
        print("=====================\n")

        subject = f"オンライン講座 {sorted_dates[0].strftime('%m/%d')}〜{sorted_dates[-1].strftime('%m/%d')}"
        create_gmail_draft(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, subject, body)
        print("Gmail下書きを作成しました。")

    finally:
        driver.quit()
        # ログイン情報を共用PCに残さないため、専用プロファイルを削除する
        # shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        # print("一時プロファイルを削除しました。")


if __name__ == "__main__":
    main()
