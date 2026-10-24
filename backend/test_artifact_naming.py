"""交付文件的语义命名回归测试；不依赖模型或 Office 环境。"""

import unittest

from artifact_naming import semantic_filename, semantic_stem


class ArtifactNamingTest(unittest.TestCase):
    def test_composition_gets_its_subject_as_word_filename(self):
        self.assertEqual(semantic_filename("写一篇我的校园生活作文，生成word文档", "docx"), "我的校园生活.docx")

    def test_document_formats_share_the_same_subject(self):
        prompt = "制作关于人工智能发展的PPT，包含五页"
        for extension in ("docx", "pptx", "xlsx", "pdf"):
            with self.subTest(extension=extension):
                self.assertEqual(semantic_filename(prompt, extension), f"人工智能发展.{extension}")

    def test_project_and_code_package_use_a_readable_name(self):
        self.assertEqual(semantic_stem("开发一个图书管理系统"), "图书管理系统")
        self.assertEqual(semantic_filename("开发一个图书管理系统", "zip"), "图书管理系统.zip")

    def test_explicit_name_wins_and_illegal_windows_characters_are_removed(self):
        self.assertEqual(semantic_filename("生成报告，文件名叫《销售:复盘》", "pdf"), "销售复盘.pdf")
        self.assertEqual(semantic_stem("创建文档，文件名叫CON"), "项目")


if __name__ == "__main__":
    unittest.main()
