"""ログイン済みのChromeのタブでJavaScriptを実行するための共通ヘルパー。

サムネイル生成（gemini_thumbnail.py）と記事執筆（browser_writer.py）の両方が、
「ふだん使っているChromeのログイン状態のまま、Webサービスのページを直接操作する」
という同じやり方を使うので、その土台をここにまとめている。

ユーザー側の準備は1回だけ:
  Chrome の [表示] > [デベロッパー] > [Apple Events からの JavaScript を許可] をオンにする。

注意: Chromeの `execute javascript` は Promise を待てない。渡すJSはすべて同期処理にして、
「今どうなっているか」をPython側からポーリングすること。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(_HERE, "chrome_bridge.applescript")

# AppleScriptの戻り値として一度に受け取る文字数。
CHUNK = 60000

APPLE_EVENTS_HINT = (
    "ChromeでAppleScriptからのJavaScript実行が許可されていません。\n"
    "  Chromeのメニューバーから [表示] > [デベロッパー] > [Apple Events からの JavaScript を許可]\n"
    "  を一度だけオンにしてください（この設定はChromeに残ります）。"
)


class ChromeError(RuntimeError):
    """Chromeを操作できなかった。呼び出し側は従来の手動フローに切り替える。"""


def require_macos() -> None:
    if sys.platform != "darwin":
        raise ChromeError("Chrome経由の自動操作はmacOS専用です。")


def osa(*args: str) -> str:
    proc = subprocess.run(["osascript", BRIDGE, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "JavaScript" in err and ("Apple Event" in err or "AppleScript" in err):
            raise ChromeError(APPLE_EVENTS_HINT)
        raise ChromeError(f"Chromeの操作に失敗しました: {err}")
    out = proc.stdout.rstrip("\n")
    # タブIDが AppleScript の real になって "1.765179239E+9" の形で返ることがある。
    if args and args[0] == "newtab" and "E+" in out:
        out = str(int(float(out)))
    return out


def new_tab(url: str) -> str:
    require_macos()
    return osa("newtab", url)


def close_tab(tab_id: str) -> None:
    try:
        osa("close", tab_id)
    except ChromeError:
        pass


def exec_js(tab_id: str, js: str) -> str:
    """タブでJavaScriptを実行する。JSはファイル渡し（エスケープ事故を避けるため）。"""
    fd, path = tempfile.mkstemp(suffix=".js", prefix="chrome_js_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        return osa("exec", tab_id, path)
    finally:
        os.unlink(path)


def utf16_len(text: str) -> int:
    """JavaScriptの `String.length` と同じ数え方（絵文字などは2つ分）。

    Pythonの len() はコードポイント数なので、絵文字が入ると
    JS側の長さと食い違う（実測: JS 1207 に対し Python 1205）。
    受け取り漏れの判定はJS側の数え方に合わせる必要がある。
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


# チャンクの末尾がサロゲートペアの片割れにならないように削ってから返す。
# 分断すると単独サロゲートになって文字化けする。削った分は次のチャンクの先頭に回る。
_JS_CHUNK = """
(function () {
  var s = window.__chromeJsBuf || '';
  var part = s.substr(%d, %d);
  if (part.length && /[\\uD800-\\uDBFF]/.test(part.charAt(part.length - 1))) {
    part = part.slice(0, -1);
  }
  return part;
})()
"""


def read_long(tab_id: str, expression: str) -> str:
    """長い文字列を分割して受け取る。

    Apple Events の戻り値に何万文字も載せると不安定なので、いったんページ側の
    `window.__chromeJsBuf` に置いてから `substr` で切り出す。
    """
    total_raw = exec_js(
        tab_id,
        "(function(){ window.__chromeJsBuf = String(%s || ''); "
        "return String(window.__chromeJsBuf.length); })()" % expression,
    )
    try:
        total = int(total_raw)
    except ValueError:
        raise ChromeError(f"ページからの読み出しに失敗しました: {total_raw!r}") from None
    if total <= 0:
        return ""

    parts: list[str] = []
    offset = 0
    while offset < total:
        part = exec_js(tab_id, _JS_CHUNK % (offset, CHUNK))
        if not part:
            # 文字列の最後がサロゲートペアの片割れだと、切り出し側が毎回それを削るので
            # 空が返り続ける。残り1文字ならその1文字を捨てて終わる（実際に踏んだ:
            # weekly_expense の確定拠出年金の画面で「40/41文字」で止まった）。
            if total - offset <= 1:
                break
            raise ChromeError(f"読み出しが途中で止まりました（{offset}/{total}文字）。")
        parts.append(part)
        offset += utf16_len(part)
    if offset < total - 1:
        raise ChromeError(f"読み出しが途中で欠けました（{offset}/{total}文字）。")
    return "".join(parts)


def js_string(value: str) -> str:
    """JSソースに文字列を埋め込む（JSONはJSのリテラルとしてそのまま使える）。"""
    import json

    return json.dumps(value, ensure_ascii=False)
