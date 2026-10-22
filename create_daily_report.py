"""
【日報作成スクリプト】
以下をGmailから読み取り、テンプレートをコピーした新しい下書きを作成する。
  1. Drafts（下書きフォルダ）の「【日報】テンプレ」メール(日報メール用のテンプレート)
     → このメールテンプレートをコピーして日報メールを作成(件名は「【日報】yyMMdd」)
  2. Sent（送信済みメールフォルダ）にある当日日付の作業計画メール（朝に送信したもの）
     → 朝に送信した作業計画メールの「■本日の作業計画」セクションの内容を、1で作成した日報メールの「■本日の実績」セクションに挿入
  3. Draftsフォルダ内の「オンライン講座 MM/DD〜MM/DD」メール(Webサイトから取得した週間スケジュール情報はこのメールの本文に保存される)
     → 1で作成した日報メールの「■次回の予定」セクションに、次回通所日のスケジュール情報を挿入

想定する運用タイミング:
    - 通所日の11時頃に実行する。

休みの扱い:
    - 土日は日報作成がスキップされます
    - 月～金の間で施設閉所日が存在する場合、その日のスケジュール情報は「（休み）」と記載され、日報作成がスキップされる
    - 日報メールに挿入する「■次回の予定」を探す際、直近のスケジュール情報が「（休み）」となっている場合は、
      直近の「（休み）」ではない日のスケジュール情報を取得する

前提:
    - weekly_schedule_to_draft.py が事前に実行され、「オンライン講座 MM/DD〜MM/DD」という件名の下書きが存在していること
    - 「【日報】テンプレ」という件名の下書き(テンプレート)が存在していること
    - 「【作業計画】」という件名で当日に送ったメールがsentフォルダに存在すること
    - このスクリプトはブラウザ操作が発生しない(自動でGmailが読み書きされる)
"""

import imaplib
import os
import sys
from datetime import date

from daily_report_common import (
    HOLIDAY_TEXT,
    create_draft_copy,
    extract_section_block,
    fetch_all_messages,
    find_drafts_folder_name,
    find_sent_folder_name,
    get_body_text,
    get_draft_by_subject,
    get_latest_weekly_schedule,
    get_next_content_skipping_holidays,
    insert_block_after_heading,
)

# ========== 設定項目 ==========
# GitHub Actions の Secrets(環境変数)から読み込む。
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
    print("環境変数 GMAIL_ADDRESS / GMAIL_APP_PASSWORD が設定されていません。", file=sys.stderr)
    sys.exit(1)

REPORT_TEMPLATE_SUBJECT = "【日報】テンプレ"
WORK_PLAN_SUBJECT_PREFIX = "【作業計画】"
HEADING_NEXT_SCHEDULE = "■次回の予定"

# 日報メールの宛先(複数の場合はカンマ区切りで指定)
HEADING_TODAYS_RESULT = "■本日の実績"
HEADING_TODAYS_PLAN = "■本日の作業計画"


def get_todays_work_plan_block(imap):
    """
    送信済みフォルダから、件名が WORK_PLAN_SUBJECT_PREFIX で始まり、
    かつ本日送信されたメールを探し、
    HEADING_TODAYS_PLAN セクションの内容を抜き出して返す。
    """
    sent_folder = find_sent_folder_name(imap)
    imap.select(sent_folder)

    messages = fetch_all_messages(imap)
    today = date.today()

    candidates = [
        (subject, msg_date, msg)
        for subject, msg_date, msg in messages
        if subject.startswith(WORK_PLAN_SUBJECT_PREFIX)
    ]
    if not candidates:
        raise RuntimeError(
            f"送信済みフォルダに件名が「{WORK_PLAN_SUBJECT_PREFIX}」のメールが見つかりません。"
        )

    todays_candidates = [c for c in candidates if c[1] is not None and c[1].date() == today]
    if todays_candidates:
        chosen = todays_candidates[0]
    else:
        candidates.sort(key=lambda x: x[1] or date.min, reverse=True)
        chosen = candidates[0]
        print(
            f"警告: 本日({today.strftime('%m/%d')})送信の「{WORK_PLAN_SUBJECT_PREFIX}」メールが"
            "見つからなかったため、最新のメールを使用します。"
        )

    _, _, msg = chosen
    body_text = get_body_text(msg)
    return extract_section_block(body_text, HEADING_TODAYS_PLAN)


def main():
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

    try:
        drafts_folder = find_drafts_folder_name(imap)
        imap.select(drafts_folder)

        # 1. 週間スケジュールを取得
        schedule = get_latest_weekly_schedule(imap)

        # 2. 今日自体が休みなら、日報は作成せず終了する
        today = date.today()
        if schedule.get(today) == HOLIDAY_TEXT:
            print(f"今日({today.strftime('%m/%d')})は休みのため、日報は作成しません。")
            return

        # 3. 次回の予定(休みをスキップした直近の内容)を決定
        next_item = get_next_content_skipping_holidays(schedule, today)
        if next_item is None:
            raise RuntimeError(
                "次回の予定が、取得済みの週間スケジュールに見つかりませんでした。"
                "月をまたぐ場合は、先に来月分のweekly_schedule_to_draft.pyを実行してください。"
            )
        next_day, next_schedule_text = next_item

        # 4. 本日送信済みの「作業計画」メールから、本日の実績に相当するブロックを取得
        work_plan_block = get_todays_work_plan_block(imap)

        # 5. テンプレートを取得し、両方を挿入
        # (Sentフォルダを選択した状態になっているため、Draftsに戻す)
        imap.select(drafts_folder)
        template_msg = get_draft_by_subject(imap, REPORT_TEMPLATE_SUBJECT)
        template_body = get_body_text(template_msg)
        new_body = insert_block_after_heading(template_body, HEADING_TODAYS_RESULT, work_plan_block)
        new_body = insert_block_after_heading(new_body, HEADING_NEXT_SCHEDULE, f"・{next_schedule_text}")

        # 6. 新しい下書きとして保存(テンプレート自体は変更しない)
        new_subject = f"【日報】{today.strftime('%y%m%d')}"
        create_draft_copy(imap, template_msg, new_body, new_subject)

        print(f"日報の下書き「{new_subject}」を作成しました。次回の予定({next_day.strftime('%m/%d')}分): {next_schedule_text}")

    finally:
        imap.logout()


if __name__ == "__main__":
    main()
