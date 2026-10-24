"""交付正文纯净度检查；不依赖模型或文档渲染器。"""

import unittest

from artifact_content import meta_leaks, payload_meta_leaks, strip_document_meta


class ArtifactContentTest(unittest.TestCase):
    def test_removes_composition_word_count_and_explanatory_tail(self):
        source = (
            "# 我的校园生活\n\n清晨的校园很安静。\n\n"
            "我终于明白了坚持的意义。\n\n"
            "（全文约 800 字）\n\n"
            "**说明**：以上为作文正文，可直接复制到 Word 中使用。文中人物、分数、事件均为示例性素材。\n\n---"
        )
        self.assertEqual(strip_document_meta(source), "# 我的校园生活\n\n清晨的校园很安静。\n\n我终于明白了坚持的意义。")

    def test_business_explanation_and_quoted_text_are_preserved(self):
        source = "# 财务报告\n\n## 说明\n\n本月收入按到账日期统计。"
        self.assertEqual(strip_document_meta(source), source)
        self.assertEqual(meta_leaks(source), [])

    def test_nested_slide_and_sheet_text_is_checked(self):
        payload = {"slides": [{"heading": "方案", "bullets": ["实施分三步。"],
                               "note": "以上为示例内容，可直接复制到 Word 中使用。"}]}
        self.assertTrue(payload_meta_leaks(payload))
        self.assertFalse(payload_meta_leaks({"sheets": [{"name": "说明", "rows": [["数据来源", "用户输入"]]}]}))


if __name__ == "__main__":
    unittest.main()
