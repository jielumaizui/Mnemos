import unittest
from datetime import datetime, timedelta

from core.kia.kairos import PeriodicDetector, TimeParser, TimeWindowType


class TestKairosTimeParser(unittest.TestCase):
    def test_no_time_intent_does_not_default_to_immediate(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        result = parser.parse("帮我看看这个方案")

        self.assertEqual(result.window, TimeWindowType.NO_TIME_INTENT)
        self.assertIsNone(result.days_until)
        self.assertIsNone(result.due_date)

    def test_should_load_no_time_intent_by_task_type(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))
        result = parser.parse("帮我看看这个方案")

        self.assertTrue(parser.should_load_now(result, task_type="coding"))
        self.assertTrue(parser.should_load_now(result, task_type="analysis"))
        self.assertTrue(parser.should_load_now(result, task_type="review"))
        self.assertFalse(parser.should_load_now(result, task_type="writing"))
        self.assertFalse(parser.should_load_now(result, task_type="strategy"))
        self.assertFalse(parser.should_load_now(result, task_type="unknown"))

    def test_fuzzy_time_uses_new_semantics(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        asap = parser.parse("尽快处理", task_type="coding")
        casual = parser.parse("有空时处理", task_type="strategy")
        not_urgent = parser.parse("这个不急", task_type="review")

        self.assertEqual(asap.days_until, 0)
        self.assertEqual(asap.window, TimeWindowType.IMMEDIATE)
        self.assertEqual(casual.days_until, 2)
        self.assertEqual(not_urgent.days_until, 2)

    def test_dynamic_relative_this_week_next_week_month_end(self):
        # 2026-05-27 is Wednesday. This week targets Friday, next week Monday.
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        this_week = parser.parse("本周完成")
        next_week = parser.parse("下周开始")
        month_end = parser.parse("本月底交付")

        self.assertEqual(this_week.days_until, 2)
        self.assertEqual(this_week.due_date.date(), datetime(2026, 5, 29).date())
        self.assertEqual(next_week.days_until, 5)
        self.assertEqual(next_week.due_date.date(), datetime(2026, 6, 1).date())
        self.assertEqual(month_end.days_until, 4)
        self.assertEqual(month_end.due_date.date(), datetime(2026, 5, 31).date())

    def test_compound_weekly_time(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        result = parser.parse("每周五下午3点 review")

        self.assertEqual(result.window, TimeWindowType.PERIODIC)
        self.assertTrue(result.is_periodic)
        self.assertEqual(result.period, "weekly")
        self.assertEqual(result.weekday, 4)
        self.assertEqual(result.hour, 15)
        self.assertEqual(result.minute, 0)
        self.assertIn("weekday_4", result.periodic_keywords)
        self.assertIn("hour_15", result.periodic_keywords)

    def test_specific_date_still_works(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        result = parser.parse("2026-06-10 之前完成")

        self.assertEqual(result.window, TimeWindowType.MEDIUM)
        self.assertEqual(result.days_until, 13)

    def test_week_after_next_keeps_legacy_meaning(self):
        parser = TimeParser(reference_time=datetime(2026, 5, 27, 9, 0))

        result = parser.parse("下下周再处理")

        self.assertEqual(result.days_until, 14)
        self.assertEqual(result.window, TimeWindowType.MEDIUM)


class TestPeriodicDetector(unittest.TestCase):
    def test_detects_periodic_with_two_records(self):
        detector = PeriodicDetector()
        start = datetime(2026, 5, 1, 9, 0)
        history = [
            {"task_type": "review", "created_at": start.isoformat()},
            {"task_type": "review", "created_at": (start + timedelta(days=7)).isoformat()},
        ]

        result = detector.detect("review", history)

        self.assertIsNotNone(result)
        self.assertEqual(result["period"], "weekly")
        self.assertEqual(result["avg_interval"], 7.0)
        self.assertGreater(result["confidence"], 0)
        self.assertEqual(result["variance_ratio"], 0.0)

    def test_rejects_unstable_period(self):
        detector = PeriodicDetector()
        history = [
            {"task_type": "review", "created_at": "2026-05-01T09:00:00"},
            {"task_type": "review", "created_at": "2026-05-08T09:00:00"},
            {"task_type": "review", "created_at": "2026-06-20T09:00:00"},
        ]

        self.assertIsNone(detector.detect("review", history))


if __name__ == "__main__":
    unittest.main()
