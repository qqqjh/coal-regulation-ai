from __future__ import annotations

import unittest
from types import SimpleNamespace

import numeric_compare


class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content

    def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(completions=_FakeCompletions(content))


class NumericCompareGroundingTests(unittest.TestCase):
    def test_rejects_rule_value_hallucinated_from_pending_text(self):
        response = """{
          "pairs": [{
            "parameter": "采煤机内喷雾工作压力",
            "constraint_type": "lower_limit",
            "pending": {"value": 0.5, "value_high": null, "unit": "MPa", "quote": "内喷雾工作压力不得小于0.5 MPa"},
            "rule": {"value": 0.5, "value_high": null, "unit": "MPa", "quote": "内喷雾工作压力不得小于"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "内喷雾工作压力不得小于0.5 MPa",
            "内喷雾工作压力不得小于 , 外喷雾工作压力不得小于 ,",
        )

        self.assertFalse(result["has_pairs"])
        self.assertEqual(1, result["rejected_pair_count"])
        self.assertEqual(
            "rule_value_not_in_quote", result["rejected_pairs"][0]["reason"]
        )

    def test_rejects_restore_condition_paired_with_trigger_rule(self):
        response = """{
          "pairs": [{
            "parameter": "工作面甲烷浓度",
            "constraint_type": "trigger_threshold",
            "pending": {"value": 1.5, "value_high": null, "unit": "%", "quote": "瓦斯浓度低于1.5%时，方可开启照明灯"},
            "rule": {"value": 1.5, "value_high": null, "unit": "%", "quote": "甲烷浓度达到1.5%时必须停止工作、切断电源"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "工作面瓦斯浓度低于1.5%时，方可开启照明灯。",
            "甲烷浓度达到1.5%时必须停止工作、切断电源。",
        )

        self.assertFalse(result["has_pairs"])
        self.assertEqual(
            "control_phase_mismatch", result["rejected_pairs"][0]["reason"]
        )

    def test_accepts_grounded_restore_pair_and_compares_by_code(self):
        response = """{
          "pairs": [{
            "parameter": "工作面甲烷复电浓度",
            "constraint_type": "restore_threshold",
            "pending": {"value": 1.5, "value_high": null, "unit": "%", "quote": "低于1.5%时方可开启照明灯"},
            "rule": {"value": 1.0, "value_high": null, "unit": "%", "quote": "复电浓度<1.0%"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "工作面瓦斯浓度低于1.5%时方可开启照明灯。",
            "采煤工作面复电浓度<1.0%。",
        )

        self.assertTrue(result["has_pairs"])
        self.assertEqual("不合规", result["overall"])
        self.assertTrue(result["details"][0]["evidence_grounded"])
        self.assertEqual("later_trigger", result["details"][0]["relation"])

    def test_recovers_unique_pending_sentence_when_llm_rewords_quote(self):
        response = """{
          "pairs": [{
            "parameter": "工作面甲烷停工浓度",
            "constraint_type": "trigger_threshold",
            "pending": {"value": 1.8, "value_high": null, "unit": "%", "quote": "工作面甲烷浓度达到1.8%时停止工作"},
            "rule": {"value": 1.5, "value_high": null, "unit": "%", "quote": "甲烷浓度达到1.5%时必须停止工作"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "【待审数值句1】当工作面瓦斯浓度≥1.8%时，必须停止工作；\n"
            "【待审数值句2】当瓦斯浓度≥1.5%时，必须停止工作。",
            "甲烷浓度达到1.5%时必须停止工作。",
        )

        self.assertTrue(result["has_pairs"])
        self.assertEqual("不合规", result["overall"])
        self.assertEqual(
            "当工作面瓦斯浓度≥1.8%时，必须停止工作；",
            result["details"][0]["pending_quote"],
        )

    def test_repairs_llm_value_from_unique_grounded_value_and_unit(self):
        response = """{
          "pairs": [{
            "parameter": "工作面甲烷停工浓度",
            "constraint_type": "trigger_threshold",
            "pending": {"value": 1.5, "value_high": null, "unit": "%", "quote": "距煤墙10m范围瓦斯浓度≥1.8%时停止工作"},
            "rule": {"value": 1.5, "value_high": null, "unit": "%", "quote": "甲烷浓度达到1.5%时必须停止工作"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "距煤墙10m范围瓦斯浓度≥1.8%时停止工作。",
            "甲烷浓度达到1.5%时必须停止工作。",
        )

        self.assertTrue(result["has_pairs"])
        self.assertEqual("不合规", result["overall"])
        self.assertIn("1.8%", result["details"][0]["explanation"])

    def test_rejects_relative_percentage_as_absolute_concentration(self):
        response = """{
          "pairs": [{
            "parameter": "工作面甲烷报警浓度",
            "constraint_type": "trigger_threshold",
            "pending": {"value": 20, "value_high": null, "unit": "%", "quote": "报警浓度按法规报警浓度下调20%执行"},
            "rule": {"value": 1.0, "value_high": null, "unit": "%", "quote": "报警浓度≥1.0%"}
          }]
        }"""
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "报警浓度按法规报警浓度下调20%执行。",
            "报警浓度≥1.0%。",
        )

        self.assertFalse(result["has_pairs"])
        self.assertEqual(
            "relative_percentage_mismatch",
            result["rejected_pairs"][0]["reason"],
        )

    def test_restore_condition_uses_runtime_table_restore_column(self):
        response = """{
          "pairs": [{
            "parameter": "采煤工作面开启照明浓度",
            "constraint_type": "trigger_threshold",
            "pending": {"value": 1.5, "value_high": null, "unit": "%", "quote": "低于1.5%时方可开启照明灯"},
            "rule": {"value": 1.5, "value_high": null, "unit": "%", "quote": "采煤工作面 | ≥1.0 | ≥1.5 | <1.0 | 全部电源"}
          }]
        }"""
        rule_text = (
            "设置地点 | 报警浓度/% | 断电浓度/% | 复电浓度/% | 断电范围\n"
            "采煤工作面 | ≥1.0 | ≥1.5 | <1.0 | 全部电源"
        )
        result = numeric_compare.numeric_check(
            _FakeClient(response),
            "model",
            "工作面瓦斯浓度低于1.5%时方可开启照明灯。",
            rule_text,
        )

        self.assertTrue(result["has_pairs"])
        self.assertEqual("不合规", result["overall"])
        self.assertEqual(
            "restore_threshold", result["details"][0]["constraint_type"]
        )
        self.assertIn("法规 1%", result["details"][0]["explanation"])


if __name__ == "__main__":
    unittest.main()
