"""Focused checks for local indexing and source references."""

import os
import tempfile
import unittest
from unittest.mock import patch

import file_knowledge
import uploads


class FileKnowledgeTest(unittest.TestCase):
    def test_search_finds_late_content_with_source(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(uploads, 'UPLOADS_DIR', folder), patch.object(uploads, 'INDEX_PATH', os.path.join(folder, 'index.json')), patch.object(file_knowledge, 'DB_PATH', os.path.join(folder, 'knowledge.db')):
                item = uploads.save_upload('长资料.txt', ('前言 ' * 2000 + '验收标准是库存为零时拒绝借阅').encode())
                self.assertIsNotNone(item)
                file_knowledge.index_upload(item['id'], item['name'])
                result = file_knowledge.answer('验收标准', [item['id']])
                self.assertTrue(result['sources'])
                self.assertEqual(result['sources'][0]['name'], '长资料.txt')
                self.assertIn('验收标准', result['sources'][0]['content'])
                self.assertIn('库存为零', file_knowledge.context_for_task('验收标准', [item['id']]))
                self.assertTrue(file_knowledge.answer('这些资料里哪里提到了验收标准', [item['id']])['sources'])
                file_knowledge.remove_upload(item['id'])
                self.assertEqual(file_knowledge.search('验收标准'), [])


if __name__ == '__main__':
    unittest.main()
