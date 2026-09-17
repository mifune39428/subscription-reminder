#!/bin/bash

# =========================================================
# サブスク更新前リマインダー — 初期設定
#   ダブルクリックすると、対話形式で設定を作ります。
# =========================================================

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"

clear
python3 "$HERE/setup_wizard.py"
code=$?

echo ""
if [ $code -ne 0 ]; then
    echo "❌ 設定が完了しませんでした。(コード: $code)"
fi
echo "このウィンドウは閉じて構いません。"
