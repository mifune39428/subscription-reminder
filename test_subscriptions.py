"""サブスク判定の回帰テスト。実際に誤判定した請求メールの並びをそのまま再現している。

    python3 -m unittest test_subscriptions -v

判定（subscriptions.build）を触ったら必ず走らせること。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta

import subscriptions

CONFIG = {"judgement": {"unused_days": 60, "min_charges_for_cycle": 2}}


def charge(domain: str, subject: str, date: str, value: float,
           hint: bool | None = None, currency: str = "JPY") -> dict:
    return {
        "domain": domain,
        "subject": subject,
        "date": f"{date}T12:00:00+09:00",
        "billing": True,
        "amount": {"value": value, "currency": currency},
        "recurring_hint": hint,
    }


def notice(domain: str, subject: str, date: str) -> dict:
    """金額の無いお知らせ（加入通知・キャンセル通知）。"""
    return {"domain": domain, "subject": subject, "date": f"{date}T12:00:00+09:00",
            "billing": False, "amount": None}


class JudgementTest(unittest.TestCase):
    def setUp(self):
        # 手動登録（overrides.json）の影響を受けないよう、空の場所を指す
        self._orig = subscriptions.OVERRIDES_PATH
        self._tmp = tempfile.mkdtemp()
        subscriptions.OVERRIDES_PATH = os.path.join(self._tmp, "none.json")

    def tearDown(self):
        subscriptions.OVERRIDES_PATH = self._orig

    def run_build(self, records: list[dict]) -> dict:
        return subscriptions.build(CONFIG, records, [], {}, {}, {})

    def active(self, result: dict, domain: str) -> list[dict]:
        return [s for s in result["subscriptions"] if s["domain"] == domain]

    def listed_anywhere(self, result: dict, domain: str) -> list[dict]:
        return [s for key in ("subscriptions", "unsure", "ended")
                for s in result[key] if s["domain"] == domain]

    def test_amazon_books_of_similar_price_are_not_a_subscription(self):
        """¥690・¥700・¥727の本が「月額¥727」と出た（2026-09-13）。"""
        records = [
            charge("amazon.co.jp", "Amazon.co.jpでのご注文: 放課後のアイドルには秘密がある 8 (ヤングアニマルコミックス)", "2026-03-31", 690),
            charge("amazon.co.jp", "Amazon.co.jpでのご注文: 予想どおりに不合理　行動経済学が明かす", "2026-08-15", 700, hint=True),
            charge("amazon.co.jp", "Amazon.co.jpでのご注文: 国境のない生き方　－私をつくった本と旅－（小学館新書）", "2026-09-11", 727),
            # Amazonにはサブスク関連のお知らせも届く。これを根拠に本の注文をサブスクにしてはいけない
            notice("amazon.co.jp", "Kindle Unlimited：自動更新キャンセルのお知らせ", "2026-06-03"),
        ]
        self.assertEqual(self.listed_anywhere(self.run_build(records), "amazon.co.jp"), [])

    def test_same_manga_series_bought_monthly_is_still_a_purchase(self):
        """巻数だけ違う同シリーズを毎月同じ値段で買っても、注文確認はサブスクではない。"""
        records = [
            charge("amazon.co.jp", f"Amazon.co.jpでのご注文: 放課後のアイドルには秘密がある {n} (ヤングアニマルコミックス)", d, 690)
            for n, d in [(6, "2026-06-10"), (7, "2026-07-10"), (8, "2026-08-10"), (9, "2026-09-10")]
        ]
        self.assertEqual(self.listed_anywhere(self.run_build(records), "amazon.co.jp"), [])

    def test_xserver_missing_one_monthly_mail_stays_monthly(self):
        """7/21の請求メールが届かず、6/21→8/21 の61日から「2か月」と出た。"""
        subject = "【 XServer アカウント】ご利用料金自動更新処理のご連絡 ({})"
        dates = ["2025-12-21", "2026-01-21", "2026-02-21", "2026-03-21",
                 "2026-04-21", "2026-05-21", "2026-06-21", "2026-08-21"]
        records = [charge("xserver.ne.jp", subject.format(104300000 + i), d, 1320)
                   for i, d in enumerate(dates)]
        found = self.active(self.run_build(records), "xserver.ne.jp")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["cycle"], "月額")
        self.assertEqual(found[0]["missed_charges"], 1)
        self.assertEqual(round(found[0]["monthly"]), 1320)

    def test_claude_pro_with_fx_drift_and_signup_notice(self):
        """$22の請求が¥3,700→¥3,637と動く。件名には定期課金の語が無い。"""
        records = [
            notice("anthropic.com", "Your Pro subscription is confirmed", "2026-07-16"),
            charge("anthropic.com", "Your receipt from Anthropic, PBC #2775-9584-1550", "2026-07-16", 3700, hint=False),
            charge("anthropic.com", "Your receipt from Anthropic, PBC #2079-5461-1535", "2026-08-16", 3637, hint=False),
        ]
        found = self.active(self.run_build(records), "anthropic.com")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["cycle"], "月額")
        self.assertEqual(found[0]["amount"], 3637)

    def test_claude_pro_stays_confirmed_after_third_charge(self):
        """3回目が来ると「間隔2回」側の判定に移る。本文に月額の語が無くても落ちないこと。"""
        records = [
            notice("anthropic.com", "Your Pro subscription is confirmed", "2026-07-16"),
            charge("anthropic.com", "Your receipt from Anthropic, PBC #1", "2026-07-16", 3700, hint=False),
            charge("anthropic.com", "Your receipt from Anthropic, PBC #2", "2026-08-16", 3637, hint=False),
            charge("anthropic.com", "Your receipt from Anthropic, PBC #3", "2026-09-16", 3650, hint=False),
        ]
        found = self.active(self.run_build(records), "anthropic.com")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "高")

    def test_voicy_uses_only_recent_run_after_price_change(self):
        """550→330→550と値段が動いた。いまの¥550は月額として残る。"""
        subject = "プレミアムリスナーを更新しました"
        records = [charge("voicy.jp", subject, d, 550)
                   for d in ["2025-11-04", "2025-12-01", "2026-01-05",
                             "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-03"]]
        records += [charge("voicy.jp", subject, d, 330)
                    for d in ["2026-02-02", "2026-03-02", "2026-04-01"]]
        result = self.run_build(records)
        current = [s for s in self.active(result, "voicy.jp") if s["amount"] == 550]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["cycle"], "月額")
        self.assertEqual(current[0]["first_charge"].date().isoformat(), "2026-05-01")

    def test_apple_song_purchases_at_same_price_are_not_confirmed(self):
        """¥255の曲を2か月おきに買っていた。本文に定期課金の語は無い。"""
        records = [charge("apple.com", "Apple からの領収書です", d, 255, hint=False)
                   for d in ["2026-04-12", "2026-06-07", "2026-08-02"]]
        records.append(charge("apple.com", "Apple からの領収書です", "2026-05-21", 1020, hint=False))
        result = self.run_build(records)
        self.assertEqual(self.active(result, "apple.com"), [])

    def test_two_random_same_price_charges_without_evidence_are_dropped(self):
        records = [charge("shop.example.com", "決済完了のお知らせ", "2026-08-01", 1500),
                   charge("shop.example.com", "決済完了のお知らせ", "2026-08-30", 1500)]
        self.assertEqual(self.listed_anywhere(self.run_build(records), "shop.example.com"), [])

    def test_yearly_domain_renewal_is_candidate(self):
        records = [charge("netowl.jp", "【ネットオウル】請求書発行のお知らせ [ ご利用サービス料金更新請求 ]",
                          (datetime.now() - timedelta(days=270)).strftime("%Y-%m-%d"), 2047)]
        result = self.run_build(records)
        unsure = [s for s in result["unsure"] if s["domain"] == "netowl.jp"]
        self.assertEqual(len(unsure), 1)
        self.assertEqual(round(unsure[0]["monthly"]), round(2047 / 12))


class DetectCycleTest(unittest.TestCase):
    def d(self, *days: str) -> list[datetime]:
        return [datetime.fromisoformat(x) for x in days]

    def test_true_two_month_cycle_is_not_read_as_monthly_with_gaps(self):
        cycle = subscriptions._detect_cycle(self.d("2026-01-10", "2026-03-11", "2026-05-10", "2026-07-10"))
        self.assertEqual(cycle["label"], "2か月")
        self.assertEqual(cycle["missed"], 0)

    def test_irregular_gaps_have_no_cycle(self):
        cycle = subscriptions._detect_cycle(self.d("2026-01-01", "2026-01-20", "2026-03-15", "2026-03-19"))
        self.assertIsNone(cycle["label"])


if __name__ == "__main__":
    unittest.main()
