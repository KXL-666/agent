"""Plan routing and local-model privacy boundary."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_config
import main


class TaskPlanTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_plan_routes_file_and_software(self):
        document = self.client.post('/api/sprint/plan', json={
            'requirement': '生成 Word 总结',
            'task_kind': 'file',
            'execution_style': 'quick',
        })
        software = self.client.post('/api/sprint/plan', json={'requirement': '开发一个网页', 'task_kind': 'software'})
        self.assertEqual(document.status_code, 200)
        self.assertEqual(document.json()['output'], 'Word 文档')
        self.assertEqual(software.status_code, 200)
        self.assertIn('分工开发', software.json()['route'])

    def test_local_mode_rejects_cloud_backup(self):
        local = lambda roster: {'api_key': 'test', 'base_url': 'http://127.0.0.1:1234/v1'} if roster == 1 else {}
        cloud_backup = lambda roster: {'api_key': 'test', 'base_url': 'https://example.com/v1'} if roster == 1 else {}
        with patch.object(agent_config, 'resolve_llm', side_effect=local), patch.object(agent_config, 'resolve_llm_backup', side_effect=cloud_backup):
            self.assertFalse(main._local_model_ready())
            response = self.client.post('/api/sprint/plan', json={'requirement': '生成 Word 总结', 'task_kind': 'file', 'execution_mode': 'local'})
            self.assertEqual(response.status_code, 400)
        with patch.object(agent_config, 'resolve_llm', side_effect=local), patch.object(agent_config, 'resolve_llm_backup', return_value={}):
            self.assertTrue(main._local_model_ready())


if __name__ == '__main__':
    unittest.main()
