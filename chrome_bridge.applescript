-- Google Chrome の特定のタブを Python から操作するための橋渡し。
--
-- 使い方:
--   osascript chrome_bridge.applescript newtab <URL>        -> 新しいタブを開き、そのタブIDを返す
--   osascript chrome_bridge.applescript exec <タブID> <JSファイル> -> そのタブでJSを実行し、結果を文字列で返す
--   osascript chrome_bridge.applescript close <タブID>       -> そのタブを閉じる
--
-- JavaScript はファイル渡しにしている。-e で直接渡すとシェルとAppleScriptの
-- 二重エスケープになり、日本語や引用符を含むコードが必ず壊れるため。
--
-- 注意: exec は Chrome の「表示 > デベロッパー > Apple Events からの JavaScript を許可」
-- がオンでないと失敗する（オフのときはその旨のエラーがそのまま返る）。

on run argv
	set cmd to item 1 of argv
	if cmd is "newtab" then
		return openTab(item 2 of argv)
	else if cmd is "exec" then
		return execJS(item 2 of argv, item 3 of argv)
	else if cmd is "close" then
		return closeTab(item 2 of argv)
	end if
	error "unknown command: " & cmd
end run

on openTab(theURL)
	tell application "Google Chrome"
		if (count of windows) is 0 then
			make new window
		end if
		make new tab at end of tabs of front window with properties {URL:theURL}
		-- `make new tab` の戻り値から直接 `id of` を取ってはいけない。
		-- Chromeが読み込みで忙しいと、タブの参照そのものが返ってきて
		-- 「タイプをtextに変換できません (-1700)」で落ちる（実際に落ちた）。
		-- id の一覧は必ず数値のリストで返るので、その最後＝いま作ったタブを使う。
		delay 0.3
		set tabIds to id of every tab of front window
		return ((item -1 of tabIds) as number) as text
	end tell
end openTab

-- タブIDの比較を number で行っているのは、Chromeのタブ IDが AppleScript の integer の
-- 範囲を超えて real になり、integer との `is` 比較が必ず false になるため（実際に踏んだ）。
--
-- `repeat with t in tabs of w` を使ってはいけない。この書き方だと AppleScript は
-- 毎回「item N of every tab of window 1」を取りに行くので、**走査中に別のタブが
-- 閉じられるとインデックスがずれて -1719 で落ちる**
-- （「item 18 of every tab of item 1 of every window を取り出すことはできません」）。
-- 記事の執筆中にサムネ生成側がタブを閉じるだけで起きる。先にIDの一覧を取っておく。
on execJS(tabIdText, jsFile)
	set theJS to (read POSIX file jsFile as «class utf8»)
	set wantedId to tabIdText as number
	tell application "Google Chrome"
		repeat with w in windows
			set tabIds to {}
			try
				set tabIds to id of every tab of w
			end try
			repeat with i from 1 to (count of tabIds)
				if ((item i of tabIds) as number) = wantedId then
					-- ここは try で包まない。Apple Events からのJS実行が
					-- 許可されていないときのエラー文面を、呼び出し側が
					-- そのまま案内に使うため（chrome_js.py の APPLE_EVENTS_HINT）。
					set theResult to (execute (tab i of w) javascript theJS)
					if theResult is missing value then return ""
					return theResult as text
				end if
			end repeat
		end repeat
	end tell
	error "tab not found: " & tabIdText
end execJS

-- execJS と同じ理由で、タブ集合をその場で走査せずIDの一覧を先に取る。
on closeTab(tabIdText)
	set wantedId to tabIdText as number
	tell application "Google Chrome"
		repeat with w in windows
			set tabIds to {}
			try
				set tabIds to id of every tab of w
			end try
			repeat with i from 1 to (count of tabIds)
				if ((item i of tabIds) as number) = wantedId then
					try
						close (tab i of w)
						return "closed"
					end try
				end if
			end repeat
		end repeat
	end tell
	return "not-found"
end closeTab
