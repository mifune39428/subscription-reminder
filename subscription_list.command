#!/bin/bash

# =========================================================
# サブスク更新前リマインダー（手動実行用）
#   ダブルクリックすると、いま見つかっているサブスクの一覧を出します。
#   メールは送りません。届くメールの見本は report_preview.html に書き出します。
# =========================================================

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"

clear
python3 "$HERE/watch.py" --list
python3 "$HERE/watch.py" --dry-run --quiet
echo ""
echo "このウィンドウは閉じて構いません。"
