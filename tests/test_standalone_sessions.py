"""Standalone sessions have independent access and no user-visible project."""
import tempfile
import unittest
from pathlib import Path
from umbrous.persistence import db
from umbrous.history.store import HistoryStore, HistoryError
from umbrous.gate.access import AccessStore
from umbrous.reset import Reset
from umbrous.runloop.coordination import ChatLocks, RunRegistry


class StandaloneTests(unittest.TestCase):
    def test_session_lifecycle_and_scope_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'u.db'
            db.connect(path).close()
            history = HistoryStore(path)
            self.assertEqual(history.list_projects(), [])
            a, b = history.create_chat(), history.create_chat()
            self.assertNotEqual(a.project_id, b.project_id)
            with self.assertRaises(HistoryError):
                history.create_chat(a.project_id)
            self.assertEqual(history.list_projects(), [])
            access = AccessStore(path, Path(tmp) / "home")
            folder = Path(tmp) / 'source'
            folder.mkdir()
            (folder / 'keep.txt').write_text('keep')
            access.attach(a.project_id, str(folder), 'workspace_code')
            self.assertIsNone(access.workspace(b.project_id))
            self.assertEqual(access.workspace(a.project_id).profile, 'workspace_code')
            history.append_exchange(a.id, 'hello', 'hello', 'test', 'complete')
            history.set_archived(a.id, True)
            self.assertEqual([c.id for c in history.list_chats()], [b.id])
            self.assertEqual([c.id for c in history.list_chats(archived=True)], [a.id])
            reopened = HistoryStore(path)
            self.assertEqual(reopened.list_projects(), [])
            self.assertEqual(reopened.get_chat(a.id).messages, 2)
            Reset(path).delete_target('chat', a.id, ChatLocks(), RunRegistry())
            with db.connect(path) as conn:
                self.assertIsNone(conn.execute('SELECT id FROM projects WHERE id=?', (a.project_id,)).fetchone())
            self.assertEqual(reopened.get_chat(b.id).id, b.id)
            self.assertEqual((folder / 'keep.txt').read_text(), 'keep')
