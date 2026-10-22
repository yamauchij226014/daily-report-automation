"""
【共通モジュール】
create_daily_report.py と create_work_plan.py の両方で使用される、
Gmail(IMAP)操作・週間スケジュール解析・下書きコピー作成などの共通処理をまとめたもの。
"""

import email
import html as html_lib
import imaplib
import re
import time
from datetime import date
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate, parsedate_to_datetime

from bs4 import BeautifulSoup

# weekly_schedule_to_draft.py が作成する下書きの件名プレフィックス
WEEKLY_SUBJECT_PREFIX = "オンライン講座"

# カレンダー表で「休み」を表す文字列
HOLIDAY_TEXT = "（休み）"


# ========== 件名デコード ==========

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


# ========== フォルダ探索 ==========

def find_folder_by_flag(imap, flag):
    """
    Gmailの特殊フォルダ(下書き/送信済みなど)は表示言語によって名前が変わるため、
    IMAPのLIST応答からGmailが付与する特殊フラグ(\\Drafts, \\Sentなど)を
    持つフォルダを自動で探す。
    """
    status, folders = imap.list()
    if status != "OK":
        raise RuntimeError("IMAPフォルダ一覧の取得に失敗しました")
    for entry in folders:
        entry_str = entry.decode("utf-8", errors="ignore")
        if flag in entry_str:
            m = re.search(r'"([^"]+)"$', entry_str)
            if m:
                return m.group(1)
    raise RuntimeError(f"{flag} フォルダが見つかりませんでした")


def find_drafts_folder_name(imap):
    return find_folder_by_flag(imap, "\\Drafts")


def find_sent_folder_name(imap):
    return find_folder_by_flag(imap, "\\Sent")


# ========== メッセージ取得 ==========

def html_to_text_with_links(html_content):
    """
    HTMLをテキストに変換する。その際、<a>タグのリンクは
    "表示文字 (URL)" という形式に変換することで、URL情報を保持する
    (プレーンテキスト化するとリンクのURLは通常失われるため)。

    改行は、元のHTMLで実際に改行を意味する箇所
    (<br>タグ、および<div>/<p>のようなブロック要素の区切り)にのみ挿入する。
    (単純に get_text(separator="\\n") を使うと、本来同じ行にあるべき
     文字列の間にも改行が入ってしまうため、この方式にしている)
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # <br> を改行に変換しておく
    for br in soup.find_all("br"):
        br.replace_with("\n")

    # <a href="...">表示文字</a> を "表示文字 (URL)" に変換
    for a in soup.find_all("a"):
        href = a.get("href", "").strip()
        text = a.get_text().strip()
        if href and href != text:
            a.replace_with(f"{text} ({href})")
        else:
            a.replace_with(text)

    # ブロック要素(div, p)の末尾に改行を追加する。
    # ただし、以下の場合は追加しない(改行が二重にならないようにするため):
    #   - 既に内部の<br>変換によって末尾が改行で終わっている場合
    #     (例: <div><br></div> という「空行だけの行」を表すdivは、
    #      <br>の変換で既に"\n"になっているので、ここで重ねて追加すると
    #      1つの空行が2行分になってしまう)
    #   - div/pが入れ子になっている「ラッパー」の場合
    #     (中の子要素側で既に改行が処理されるため)
    for block in soup.find_all(["div", "p"]):
        if block.find(["div", "p"]) is not None:
            continue  # 入れ子(ラッパー)なのでスキップ
        if block.get_text().endswith("\n"):
            continue  # 既に改行で終わっているのでスキップ
        block.append("\n")

    # separatorを指定しない(=空文字)ことで、上記で明示的に挿入した
    # 改行だけがそのまま反映されるようにする
    return soup.get_text().strip()


def get_body_text(msg):
    """
    emailメッセージ本体を文字列として取得する。
    HTML形式のパートがあれば、リンクを "表示文字 (URL)" の形で保持しつつ
    テキスト化する(通常のplain textパートだとリンクのURLが失われるため)。
    HTMLパートが無ければ、plain textパートをそのまま使う。
    """
    html_payload = None
    plain_payload = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/html", "text/plain"):
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/html" and html_payload is None:
                html_payload = decoded
            elif content_type == "text/plain" and plain_payload is None:
                plain_payload = decoded
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True).decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            html_payload = payload
        else:
            plain_payload = payload

    if html_payload is not None:
        return html_to_text_with_links(html_payload)
    return plain_payload or ""


def fetch_all_messages(imap):
    """現在selectされているフォルダの全メッセージを (件名, 日付, emailメッセージ) のリストで返す"""
    status, nums = imap.search(None, "ALL")
    if status != "OK":
        raise RuntimeError("メッセージ一覧の取得に失敗しました")

    messages = []
    for num in nums[0].split():
        status, data = imap.fetch(num, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        subject = decode_mime_words(msg.get("Subject", ""))
        try:
            msg_date = parsedate_to_datetime(msg.get("Date"))
        except Exception:
            msg_date = None
        messages.append((subject, msg_date, msg))
    return messages


def get_draft_by_subject(imap, subject_exact):
    """
    (Draftsフォルダがselect済みである前提で)
    件名が subject_exact と完全一致する下書きを取得する。
    テンプレートメールの取得などに使う。
    """
    drafts = fetch_all_messages(imap)
    for subject, msg_date, msg in drafts:
        if subject.strip() == subject_exact:
            return msg
    raise RuntimeError(f"件名が「{subject_exact}」の下書きが見つかりません。")


# ========== 週間スケジュールの解析 ==========

def parse_weekly_schedule_body(body_text):
    """"MM/DD(曜): 内容" 形式の行を解析し、{date: 内容} の辞書を返す"""
    result = {}
    pattern = re.compile(r"(\d{2})/(\d{2})\([月火水木金]\):\s*(.+)")
    for line in body_text.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        month, day, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        try:
            d = date(date.today().year, month, day)
        except ValueError:
            continue
        result[d] = content
    return result


def get_latest_weekly_schedule(imap):
    """
    (Draftsフォルダがselect済みである前提で)
    件名が WEEKLY_SUBJECT_PREFIX で始まる下書きのうち最新のものを取得して解析する。
    """
    drafts = fetch_all_messages(imap)
    candidates = [
        (subject, msg_date, msg)
        for subject, msg_date, msg in drafts
        if subject.startswith(WEEKLY_SUBJECT_PREFIX)
    ]
    if not candidates:
        raise RuntimeError(
            f"件名が「{WEEKLY_SUBJECT_PREFIX}」で始まる下書きが見つかりません。"
            "先に weekly_schedule_to_draft.py を実行してください。"
        )

    candidates.sort(key=lambda x: x[1] or date.min, reverse=True)
    _, _, latest_msg = candidates[0]
    body_text = get_body_text(latest_msg)
    return parse_weekly_schedule_body(body_text)


def get_next_content_skipping_holidays(schedule, today):
    """
    schedule(dict: date -> 内容)を日付順にたどり、
    todayより後の日付のうち、内容が HOLIDAY_TEXT ではない最初のものを
    (日付, 内容) のタプルで返す。見つからなければ None を返す。
    (日報の「次回の予定」と、翌日分の作業計画の両方で使う)
    """
    for d in sorted(schedule.keys()):
        if d <= today:
            continue
        if schedule[d] == HOLIDAY_TEXT:
            continue
        return d, schedule[d]
    return None


# ========== 本文の挿入・抽出 ==========

def insert_block_after_heading(body_text, heading, block_text):
    """
    本文の中から heading と完全一致する行を探し、
    その次の行として block_text(複数行可)を挿入した本文を返す。
    """
    lines = body_text.splitlines()
    new_lines = []
    inserted = False
    for line in lines:
        new_lines.append(line)
        if line.strip() == heading:
            new_lines.extend(block_text.splitlines())
            inserted = True

    if not inserted:
        raise RuntimeError(f"本文に「{heading}」という行が見つかりませんでした。")

    return "\n".join(new_lines)


def extract_section_block(body_text, heading):
    """
    body_textの中から heading と完全一致する行を探し、
    その次の行から、次の「■」で始まる行、または「以上です」を含む行の
    直前までをブロックとして抜き出して返す(見出し行自体は含まない)。

    途中に現れる空行は区切りとして扱わず、単に読み飛ばす
    (HTMLをテキスト化する際に、意図しない空行が紛れ込むことがあるため)。
    """
    STOP_MARKERS = ("■", "以上です")

    lines = body_text.splitlines()
    capturing = False
    block_lines = []
    for line in lines:
        stripped = line.strip()
        if capturing:
            if any(stripped.startswith(m) for m in STOP_MARKERS):
                break
            if stripped == "":
                continue  # 空行は区切りとして扱わず読み飛ばす
            block_lines.append(line)
            continue
        if stripped == heading:
            capturing = True

    if not capturing:
        raise RuntimeError(f"本文に「{heading}」という行が見つかりませんでした。")

    return "\n".join(block_lines)


# ========== 下書きの新規作成 ==========

URL_PATTERN = re.compile(r'https?://[^\s<>"）)]+')


def text_to_html_with_links(text):
    """
    プレーンテキストをHTMLに変換する。
    その際、本文中に含まれるURL(http/https)は <a href="..."> タグに変換し、
    クリックできるリンクとして復元する。改行は <br> に変換する。
    """
    escaped = html_lib.escape(text)
    linked = URL_PATTERN.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
    return linked.replace("\n", "<br>\n")


def create_draft_copy(imap, original_msg, new_body_text, new_subject):
    """
    テンプレートのFromを引き継ぎ、件名は新しいものを指定して下書きを作成する。
    本文はHTML形式で作成し、含まれるURLはクリックできるリンクとして保持する。
    """
    html_body = text_to_html_with_links(new_body_text)

    new_msg = MIMEText(html_body, "html", _charset="utf-8")
    new_msg["Subject"] = new_subject
    new_msg["From"] = original_msg.get("From")
    new_msg["To"] = original_msg.get("To")
    new_msg["Date"] = formatdate(localtime=True)

    drafts_folder = find_drafts_folder_name(imap)
    imap.append(
        drafts_folder,
        "",
        imaplib.Time2Internaldate(time.time()),
        new_msg.as_bytes(),
    )
