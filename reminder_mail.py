"""更新が近いサブスクを知らせるHTMLメールを組み立てて送る。

Gmailで崩れないよう table + inline style だけで書く（画像もCSSクラスも使わない）。
ブログレポート配信と同じ作りにそろえてある。
"""

from __future__ import annotations

import html
import json
import smtplib
import subprocess
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BG = "#f4f5f7"
CARD = "#ffffff"
LINE = "#e3e5e8"
TEXT = "#20242b"
MUTED = "#767b85"
DANGER = "#c0392b"
WARN = "#c77700"
OK = "#2e7d5b"

_STATUS_COLOR = {
    "解約候補": DANGER,
    "判定不能": MUTED,
    "利用中": OK,
    "解約済み？": MUTED,
    "除外": MUTED,
}


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _yen(entry: dict) -> str:
    if entry["currency"] == "JPY":
        return f"¥{entry['amount']:,.0f}"
    return f"{entry['currency']} {entry['amount']:,.2f}"


def _used_label(entry: dict) -> str:
    if entry["unused_days"] is None:
        return "利用状況わからず"
    if entry["unused_days"] <= 0:
        return "今日使った"
    return f"{entry['unused_days']}日前に利用"


def _usage_label(entry: dict) -> str:
    """続けるか決める材料になる「どのくらい使ったか」。"""
    web = entry.get("web_usage")
    if web and web.get("label"):
        return web["label"]
    visits = entry.get("visits_30d")
    if visits:
        return f"30日で{visits}回アクセス"
    if entry.get("signals"):
        return "アクセスの記録なし"
    return "測れていない"


def _signals_label(entry: dict) -> str:
    if not entry.get("signals"):
        return "—"
    parts = [f"{s['kind']} {s['last']:%m/%d}（{s['detail']}）" for s in entry["signals"]]
    return " / ".join(parts)


def _cell(content: str, **style) -> str:
    css = ";".join(f"{k.replace('_', '-')}:{v}" for k, v in style.items())
    return f'<td style="{css}">{content}</td>'


def _section(title: str, body: str, note: str = "") -> str:
    if not body:
        return ""
    note_html = (
        f'<div style="font-size:12px;color:{MUTED};margin:0 0 10px">{_esc(note)}</div>'
        if note else ""
    )
    return (
        f'<tr><td style="padding:22px 22px 0">'
        f'<div style="font-size:15px;font-weight:700;color:{TEXT};margin:0 0 6px">{_esc(title)}</div>'
        f"{note_html}{body}</td></tr>"
    )


def _table(entries: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(
        f'<th align="left" style="font-size:12px;color:{MUTED};font-weight:600;'
        f'padding:6px 8px;border-bottom:1px solid {LINE}">{_esc(label)}</th>'
        for label, _ in columns
    )
    body = []
    for entry in entries:
        cells = []
        for _, key in columns:
            value = _render_cell(entry, key)
            # 金額や周期が「月/額」のように折れると読みにくいので折り返さない
            nowrap = ("white-space:nowrap;"
                      if key in ("amount", "cycle", "renewal", "last_charge", "status") else "")
            cells.append(
                f'<td style="font-size:13px;color:{TEXT};padding:7px 8px;{nowrap}'
                f'border-bottom:1px solid {LINE};vertical-align:top">{value}</td>'
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{CARD};border:1px solid {LINE};border-radius:6px;border-collapse:collapse">'
        f"<tr>{head}</tr>{''.join(body)}</table>"
    )


def _render_cell(entry: dict, key: str) -> str:
    if key == "name":
        name = _esc(entry["name"])
        if entry.get("cancel_url"):
            name = f'<a href="{_esc(entry["cancel_url"])}" style="color:{TEXT}">{name}</a>'
        subject = entry["subjects"][0] if entry.get("subjects") else ""
        return f'{name}<div style="font-size:11px;color:{MUTED}">{_esc(subject[:38])}</div>'
    if key == "amount":
        monthly = entry.get("monthly")
        cycle_days = entry.get("cycle_days")
        if monthly and cycle_days and cycle_days > 31:
            # 年契約・半年契約は「月あたりいくらか」も出さないと比べられない
            return (f'{_yen(entry)}<div style="font-size:11px;color:{MUTED}">'
                    f'月あたり ¥{monthly:,.0f}</div>')
        return _yen(entry)
    if key == "cycle":
        return _esc(entry.get("cycle", ""))
    if key == "renewal":
        when = entry.get("next_renewal")
        if not when:
            if entry.get("manual"):
                return (f'<span style="color:{WARN}">未設定</span>'
                        f'<div style="font-size:11px;color:{MUTED}">日付を入れると通知</div>')
            return "—"
        days = entry.get("days_to_renewal")
        suffix = f'<div style="font-size:11px;color:{MUTED}">{days}日後</div>' if days is not None else ""
        return f"{when:%Y-%m-%d}{suffix}"
    if key == "last_charge":
        if entry.get("manual"):
            return "—"  # 手動登録は課金履歴が無い
        return f'{entry["last_charge"]:%Y-%m-%d}'
    if key == "status":
        color = _STATUS_COLOR.get(entry["status"], MUTED)
        return (
            f'<span style="color:{color};font-weight:700">{_esc(entry["status"])}</span>'
            f'<div style="font-size:11px;color:{MUTED}">{_esc(_used_label(entry))}</div>'
        )
    if key == "usage":
        web = entry.get("web_usage") or {}
        label = _esc(_usage_label(entry))
        extra = ""
        if web.get("examples"):
            extra = (f'<div style="font-size:11px;color:{MUTED}">'
                     f'{_esc("・".join(web["examples"][:2])[:46])}</div>')
        elif web.get("note"):
            extra = f'<div style="font-size:11px;color:{MUTED}">{_esc(web["note"])}</div>'
        elif entry.get("usage_note"):
            extra = f'<div style="font-size:11px;color:{MUTED}">{_esc(entry["usage_note"])}</div>'
        return f"{label}{extra}"
    if key == "signals":
        return f'<span style="font-size:11px;color:{MUTED}">{_esc(_signals_label(entry))}</span>'
    if key == "confidence":
        return _esc(entry.get("confidence", ""))
    return ""


def build_html(data: dict, config: dict) -> tuple[str, str]:
    """(件名, HTML) を返す。"""
    now = data["generated_at"]
    days_before = int(config["notify"].get("days_before_renewal", 5))
    subs = data["subscriptions"]

    upcoming = [s for s in subs
                if s["days_to_renewal"] is not None and 0 <= s["days_to_renewal"] <= days_before
                and s["status"] != "除外"]
    cancel_candidates = [s for s in subs if s["status"] == "解約候補"]

    def monthly_by_currency(entries: list[dict]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for entry in entries:
            if entry["status"] == "除外" or not entry.get("monthly"):
                continue
            totals[entry["currency"]] = totals.get(entry["currency"], 0) + entry["monthly"]
        return totals

    totals = monthly_by_currency(subs)
    monthly = totals.get("JPY", 0)
    other_currency = [f"{c} {v:,.2f}" for c, v in sorted(totals.items()) if c != "JPY"]
    unsure_totals = monthly_by_currency(data.get("unsure", []))
    unsure_monthly = unsure_totals.get("JPY", 0)

    if upcoming:
        head = upcoming[0]
        days = head["days_to_renewal"]
        when = "今日" if days == 0 else f"{days}日後"
        subject = f"[サブスク] {when} {head['name']} {_yen(head)} が更新されます"
        if len(upcoming) > 1:
            subject += f" ほか{len(upcoming) - 1}件"
    elif cancel_candidates:
        subject = f"[サブスク] 使っていないかもしれない契約が{len(cancel_candidates)}件あります"
    else:
        subject = f"[サブスク] 契約中 {len(subs)}件（更新間近なし）"

    def tile(label: str, value: str, color: str = TEXT, sub: str = "") -> str:
        sub_html = f'<div style="font-size:11px;color:{MUTED}">{sub}</div>' if sub else ""
        return (
            f'<td width="33%" style="padding:0">'
            f'<div style="font-size:11px;color:{MUTED}">{label}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{color}">{value}</div>'
            f"{sub_html}</td>"
        )

    summary = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        + tile("契約中", f"{len(subs)}件")
        + tile("月あたり合計", f"¥{monthly:,.0f}",
               sub=" / ".join(filter(None, [
                   ("＋" + "・".join(other_currency)) if other_currency else "",
                   f"年{monthly * 12:,.0f}円",
                   f"要確認込み ¥{monthly + unsure_monthly:,.0f}" if unsure_monthly else "",
               ])))
        + tile("解約候補", f"{len(cancel_candidates)}件",
               DANGER if cancel_candidates else TEXT)
        + "</tr></table>"
    )

    sections = [
        _section(f"あと{days_before}日以内に更新されるもの",
                 _table(upcoming, [("サービス", "name"), ("金額", "amount"),
                                   ("更新日", "renewal"), ("今月の利用", "usage"),
                                   ("状況", "status")])
                 if upcoming else ""),
        _section("使っていないかもしれない契約",
                 _table(cancel_candidates, [("サービス", "name"), ("金額", "amount"),
                                            ("周期", "cycle"), ("更新日", "renewal"),
                                            ("今月の利用", "usage"), ("利用シグナル", "signals")])
                 if cancel_candidates else "",
                 f'最後に使ってから{config["judgement"]["unused_days"]}日以上たっているもの。'),
        _section("契約中のサブスク",
                 _table(subs, [("サービス", "name"), ("金額", "amount"), ("周期", "cycle"),
                               ("更新日", "renewal"), ("今月の利用", "usage"),
                               ("状況", "status")])),
        _section("サブスクかもしれないもの（要確認）",
                 _table(data.get("unsure", []), [("サービス", "name"), ("金額", "amount"),
                                                 ("周期", "cycle"), ("更新日", "renewal"),
                                                 ("今月の利用", "usage"),
                                                 ("最終課金", "last_charge")]),
                 "課金が2回しか見つかっていない、または本文に「月額」等の手がかりがないもの。"
                 "更新日が空のものは overrides.json の manual に書いた契約で、"
                 "watch.py --set-renewal で日付を入れると通知の対象になる。"),
        _section("止まっていそうな契約",
                 _table(data.get("ended", []), [("サービス", "name"), ("金額", "amount"),
                                                ("周期", "cycle"), ("最終課金", "last_charge")]),
                 "2周期ぶん以上、課金メールが届いていないもの。解約済みならこのままで問題なし。"),
    ]

    notes = data.get("notes") or []
    notes_html = ""
    if notes:
        items = "".join(f"<li style='margin:2px 0'>{_esc(n)}</li>" for n in notes)
        notes_html = _section("注意", f'<ul style="font-size:12px;color:{MUTED};margin:0;padding-left:18px">{items}</ul>')

    html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:18px 0">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:100%;background:{CARD};border:1px solid {LINE};border-radius:10px;font-family:-apple-system,'Hiragino Sans','Helvetica Neue',sans-serif">
  <tr><td style="padding:22px 22px 4px">
    <div style="font-size:19px;font-weight:700;color:{TEXT}">サブスク管理</div>
    <div style="font-size:12px;color:{MUTED};margin-top:2px">{now:%Y年%m月%d日} 時点 / 請求メール{data.get('record_count', 0)}通を確認</div>
  </td></tr>
  <tr><td style="padding:16px 22px">{summary}</td></tr>
  {''.join(sections)}
  {notes_html}
  <tr><td style="padding:20px 22px 22px">
    <div style="font-size:11px;color:{MUTED};border-top:1px solid {LINE};padding-top:10px">
      Gmailの請求メールから自動で作っています。判定がおかしいものは overrides.json に書けば直せます。
    </div>
  </td></tr>
</table>
</td></tr></table></body></html>"""
    return subject, html_doc


def notify_macos(entries: list[dict]) -> None:
    """デスクトップ通知も出す。メールは埋もれるので、更新の直前だけ画面にも出す。"""
    if not entries:
        return
    head = entries[0]
    days = head.get("days_to_renewal")
    when = "今日" if days == 0 else f"{days}日後"
    title = f"サブスク更新 {when}"
    body = f"{head['name']} {_yen(head)}"
    if len(entries) > 1:
        body += f" ほか{len(entries) - 1}件"
    usage = _usage_label(head)
    if usage not in ("測れていない", "アクセスの記録なし"):
        body += f"（{usage}）"
    script = (
        f'display notification {json.dumps(body, ensure_ascii=False)} '
        f'with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def send(subject: str, body_html: str, settings: dict) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = settings["sender_email"]
    message["To"] = settings["receiver_email"]
    message.attach(MIMEText("HTMLメールです。", "plain", "utf-8"))
    message.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP(settings.get("smtp_server", "smtp.gmail.com"),
                      int(settings.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(settings["sender_email"], settings["sender_password"])
        server.send_message(message)
