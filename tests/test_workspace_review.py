import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import RunningRuntime
from umbrous.api.workspace_review import review_folder


class WorkspaceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'base')

    def test_actual_content_includes_staged_unstaged_added_deleted(self):
        (self.root / 'edit.txt').write_text('old\n')
        (self.root / 'delete.txt').write_text('delete\n')
        self.commit()
        (self.root / 'edit.txt').write_text('staged\n')
        self.git('add', 'edit.txt')
        (self.root / 'edit.txt').write_text('actual\n')
        (self.root / 'delete.txt').unlink()
        (self.root / 'new.txt').write_text('new\n')
        before = (self.root / '.git/index').read_bytes()
        out = review_folder(str(self.root))
        self.assertTrue(out['available'])
        by_path = {f['path']: f for f in out['files']}
        self.assertIn('-old\n+actual\n', by_path['edit.txt']['patch'])
        self.assertEqual(by_path['delete.txt']['status'], 'deleted')
        self.assertEqual(by_path['new.txt']['status'], 'untracked')
        self.assertEqual(before, (self.root / '.git/index').read_bytes())

    def test_mode_only_change_and_missing_final_newline(self):
        (self.root / 'mode.sh').write_text('echo hi\n')
        (self.root / 'text').write_text('old')
        self.commit()
        (self.root / 'mode.sh').chmod(0o755)
        (self.root / 'text').write_text('new')
        by_path = {f['path']: f for f in review_folder(str(self.root))['files']}
        self.assertIn('old mode 100644\nnew mode 100755', by_path['mode.sh']['patch'])
        self.assertIn('-old\n\\ No newline at end of file\n+new\n', by_path['text']['patch'])

    def test_unborn_binary_excluded_and_symlink(self):
        (self.root / 'new.txt').write_text('hello\n')
        (self.root / '.env').write_text('SECRET_DO_NOT_RETURN')
        (self.root / 'image.bin').write_bytes(b'\0binary')
        (self.root / 'link').symlink_to('/etc/passwd')
        out = review_folder(str(self.root))
        self.assertTrue(out['available'], out)
        self.assertNotIn('SECRET_DO_NOT_RETURN', json.dumps(out))
        by_path = {f['path']: f for f in out['files']}
        self.assertTrue(by_path['image.bin']['binary'])
        self.assertIn('symbolic link', by_path['link']['error'])
        self.assertNotIn('.env', by_path)

    def test_non_repo_and_subfolder_do_not_review_parent(self):
        child = self.root / 'child'
        child.mkdir()
        self.assertFalse(review_folder(str(child))['available'])
        with tempfile.TemporaryDirectory() as outside:
            self.assertFalse(review_folder(outside)['available'])

    def test_size_limit_is_explicit(self):
        (self.root / 'new.txt').write_text('large\n' * 20)
        with patch('umbrous.api.workspace_review.MAX_PATCH', 10):
            out = review_folder(str(self.root))
        self.assertTrue(out['truncated'])
        self.assertTrue(out['files'][0]['truncated'])
        self.assertEqual(len(out['files'][0]['patch']), 10)

    def test_git_clean_filter_is_never_executed(self):
        (self.root / 'file.txt').write_text('original\n')
        self.commit()
        (self.root / '.gitattributes').write_text('*.txt filter=malicious\n')
        marker = self.root / 'filter-executed'
        self.git('config', 'filter.malicious.clean', 'touch ' + str(marker))
        (self.root / 'file.txt').write_text('changed\n')
        out = review_folder(str(self.root))
        self.assertTrue(out['available'])
        self.assertFalse(marker.exists())

    def test_route_rejects_missing_chat_and_respects_read_permission(self):
        with RunningRuntime() as rt:
            cid = rt.new_chat()
            url = '/api/tasks/review?chat_id=' + cid
            self.assertFalse(rt.get_json(url)[1]['available'])
            rt.runtime.access.attach('prj_general', str(self.root), 'ask_before_read')
            status, out = rt.get_json(url)
            self.assertEqual(status, 200)
            self.assertIn('needs approval', out['message'])
            rt.runtime.access.set_profile('prj_general', 'workspace_read')
            self.assertTrue(rt.get_json(url)[1]['available'])
            self.assertEqual(rt.get_json('/api/tasks/review?chat_id=chat_notfound123')[0], 404)

    def test_revoked_grant_during_review_discards_content(self):
        with RunningRuntime() as rt:
            cid = rt.new_chat()
            rt.runtime.access.attach('prj_general', str(self.root), 'ask_before_read')
            grant = rt.runtime.access.grant('prj_general', 'fs.read')
            rt.runtime.access.grant('prj_general', 'fs.list')
            def revoke(root):
                rt.runtime.access.revoke('prj_general', grant.id)
                return dict(available=True, files=[{'patch': 'must not return'}])
            with patch('umbrous.api.task_routes.review_folder', side_effect=revoke):
                status, out = rt.get_json('/api/tasks/review?chat_id=' + cid)
            self.assertEqual(status, 200)
            self.assertFalse(out['available'])
            self.assertEqual(out['files'], [])
            self.assertIn('permissions changed', out['message'])

    def test_chat_override_restricts_project_read_access(self):
        with RunningRuntime() as rt:
            cid = rt.new_chat()
            rt.runtime.access.attach('prj_general', str(self.root), 'workspace_read')
            rt.runtime.history.set_access(cid, 'ask_before_read')
            with patch('umbrous.api.task_routes.review_folder') as review:
                status, out = rt.get_json('/api/tasks/review?chat_id=' + cid)
                review.assert_not_called()
            self.assertEqual(status, 200)
            self.assertFalse(out['available'])


if __name__ == '__main__':
    unittest.main()
