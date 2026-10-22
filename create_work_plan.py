"""
【作業計画作成スクリプト】
以下をGmailから読み取り、メールテンプレートをコピーして作業計画メールを作成する。
  1. Drafts（下書きフォルダ）の「【作業計画】テンプレ」メール(作業計画メールのテンプレートです)
     → このメールテンプレートをコピーして作業計画メールを作成(件名は「【作業計画】yyMMdd」)
  2. Draftsフォルダ内の「オンライン講座 MM/DD〜MM/DD」メール(Webサイトから取得した週間スケジュール情報はこのメールの本文に保存されています)
     → 1で作成した作業計画メールの「■本日の作業計画」セクションに、翌通所日のスケジュール情報を挿入

想定する運用タイミング:
    - 通所日の16時頃に実行する。

休みの扱い:
    - 土日は作業計画メールの作成がスキップされます
    - 月～金の間で施設閉所日が存在する場合、その日のスケジュール情報は「（休み）」と記載され、作業計画メール作成がスキップされます
    - 「オンライン講座 MM/DD〜MM/DD」メールの本文に翌通所日の日付のスケジュール情報が見つからない場合も作成せず終了する

「翌通所日」の決め方:
    - 今日より後で、週間スケジュール上「（休み）」ではない直近の日とする。
      (例: 金曜に実行すると翌週月曜分、月曜が休みなら休みでない直近の日の分を作る。
       暦の上の「明日」ではないので、週末をまたぐ場合も正しく作成される)

前提:
    - weekly_schedule_to_draft.py が事前に実行され、
      「オンライン講座 MM/DD〜MM/DD」という件名の下書きが存在していること
    - 「【作業計画】テンプレ」という件名の下書き(テンプレート)が存在していること
    - このスクリプトはブラウザ操作が発生しない(自動でGmailが読み書きされる)
"""

import imaplib
import os
import sys
from datetime import date

from daily_report_common import (
    HOLIDAY_TEXT,
    create_draft_copy,
    find_drafts_folder_name,
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

WORK_PLAN_TEMPLATE_SUBJECT = "【作業計画】テンプレ"

# 作業計画メールの宛先(複数の場合はカンマ区切りで指定)
HEADING_MORNING = "・午前中"


def main():
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

    try:
        drafts_folder = find_drafts_folder_name(imap)
        imap.select(drafts_folder)

        # 1. 週間スケジュールを取得
        schedule = get_latest_weekly_schedule(imap)

        # 2. 今日自体が休みなら、作業計画は作成せず終了する
        today = date.today()
        if schedule.get(today) == HOLIDAY_TEXT:
            print(f"今日({today.strftime('%m/%d')})は休みのため、作業計画は作成しません。")
            return

        # 3. 次の稼働日(今日より後で、休みではない直近の日)とその内容を決定
        next_item = get_next_content_skipping_holidays(schedule, today)
        if next_item is None:
            print(
                "次の稼働日の予定が週間スケジュールに見つからないため、作業計画は作成しません。"
                "(weekly_schedule_to_draft.pyの実行が必要な可能性があります)"
            )
            return
        target_day, target_content = next_item

        # 4. テンプレートを取得し、「・午前中」の次に次の稼働日のスケジュールを挿入
        template_msg = get_draft_by_subject(imap, WORK_PLAN_TEMPLATE_SUBJECT)
        template_body = get_body_text(template_msg)
        new_body = insert_block_after_heading(template_body, HEADING_MORNING, f"◇{target_content}")

        # 5. 新しい下書きとして保存(テンプレート自体は変更しない)
        # 件名の日付は、作業計画の対象日(次の稼働日)とする
        new_subject = f"【作業計画】{target_day.strftime('%y%m%d')}"
        create_draft_copy(imap, template_msg, new_body, new_subject)

        print(f"作業計画の下書き「{new_subject}」を作成しました。挿入内容: {target_content}")

    finally:
        imap.logout()


if __name__ == "__main__":
    main()
