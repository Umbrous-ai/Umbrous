"""M3: workspace confinement. Every refusal the plan lists, plus the swap race."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from umbrous.tools import fs

SECRET = "OUTSIDE-SECRET-7f3a"


def make_fixture(base: Path) -> Path:
    """A small test workspace and, next to it, an 'outside' folder with a secret."""
    ws, out = base / "ws", base / "outside"
    (ws / "notes").mkdir(parents=True)
    out.mkdir()
    (out / "secret.txt").write_text(SECRET + "\n")
    (ws / "notes" / "plan.md").write_text("# Plan\nShip M3 on Friday.\nOwner: Freddy\n")
    (ws / "readme.txt").write_text("Umbrous test workspace.\nThe garden needs water.\n")
    (ws / "big.txt").write_text("x" * (fs.MAX_READ_BYTES + 10))
    (ws / "image.bin").write_bytes(b"\x89PNG\x00\x00\x01binary")
    (ws / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    (ws / ".env").write_text("API_KEY=sk-should-never-be-read\n")
    (ws / "notes" / ".ENV").write_text("SHOUTY=1\n")
    (ws / "deploy.PEM").write_text("-----BEGIN PRIVATE KEY-----\n")
    (ws / ".ｅｎｖ").write_text("fullwidth\n")                          # NFKC folds this to .env
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[remote]\n")
    os.symlink(out / "secret.txt", ws / "link.txt")                    # symlink, final component
    os.symlink(out, ws / "linkdir")                                     # symlink, intermediate folder
    os.link(out / "secret.txt", ws / "hard.txt")                        # hard link to an outside file
    os.mkfifo(ws / "pipe")                                              # FIFO
    return ws


class ConfinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ws = str(make_fixture(self.base))

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, *parts):
        return fs.read_file(self.ws, tuple(parts))

    def refused(self, code, *parts):
        with self.assertRaises(fs.FsError) as e:
            self.read(*parts)
        self.assertEqual(e.exception.code, code, parts)

    def test_reads_a_normal_file_with_lines_and_hash(self):
        r = self.read("notes", "plan.md")
        self.assertEqual(r.text.split("\n")[1], "Ship M3 on Friday.")
        self.assertEqual((r.first_line, r.last_line, r.total_lines), (1, 3, 3))
        self.assertEqual(len(r.sha256), 64)

    def test_line_window(self):
        r = fs.read_file(self.ws, ("notes", "plan.md"), start_line=2, max_lines=1)
        self.assertEqual((r.text, r.first_line, r.last_line, r.truncated), ("Ship M3 on Friday.", 2, 2, True))

    def test_symlink_final_component_is_refused(self):
        self.refused("symlink", "link.txt")

    def test_symlink_intermediate_folder_is_refused(self):
        self.refused("symlink", "linkdir", "secret.txt")

    def test_hard_link_to_outside_file_is_refused(self):
        self.refused("hard_link", "hard.txt")

    def test_fifo_is_refused_without_hanging(self):
        self.refused("not_regular", "pipe")

    def test_device_file_is_refused(self):
        dev = Path(self.ws) / "null"
        try:
            os.mknod(dev, 0o600 | 0o020000, os.makedev(1, 3))  # a character device like /dev/null
        except (PermissionError, OSError):
            self.skipTest("cannot create device files here")
        self.refused("not_regular", "null")

    def test_oversized_binary_and_non_utf8(self):
        self.refused("too_large", "big.txt")
        self.refused("binary", "image.bin")
        self.refused("binary", "latin1.txt")

    def test_folder_and_missing(self):
        self.refused("is_directory", "notes")
        self.refused("not_found", "nope.md")

    def test_excluded_names_in_any_case_and_unicode_form(self):
        for parts in ((".env",), (".ENV",), ("notes", ".ENV"), ("deploy.PEM",), (".ｅｎｖ",), (".git", "config"),
                      (".Git", "config"), ("id_RSA",), ("prod.env",), (".env.local",), ("AWS-credentials.json",),
                      (".config", "gcloud", "x")):
            self.assertTrue(fs.is_excluded(parts), parts)
        for parts in (("notes", "plan.md"), ("environment.md",), ("keys.txt",), ("gitignore.md",)):
            self.assertFalse(fs.is_excluded(parts), parts)

    def test_list_hides_excluded_and_marks_symlinks(self):
        r = fs.list_dir(self.ws, ())
        names = {n: k for n, k, _ in r.entries}
        self.assertNotIn(".env", names)
        self.assertNotIn(".git", names)
        self.assertNotIn("deploy.PEM", names)
        self.assertEqual(names["link.txt"], "symlink (not followed)")
        self.assertEqual(names["notes"], "folder")
        self.assertGreaterEqual(r.hidden, 4)

    def test_list_through_symlinked_folder_is_refused(self):
        with self.assertRaises(fs.FsError) as e:
            fs.list_dir(self.ws, ("linkdir",))
        self.assertEqual(e.exception.code, "symlink")

    def test_search_never_follows_links_or_reads_excluded(self):
        r = fs.search(self.ws, (), "secret")
        joined = " ".join(f"{h.path} {h.text}" for h in r.hits)
        self.assertNotIn(SECRET, joined)                       # not via link.txt, linkdir or hard.txt
        r = fs.search(self.ws, (), "sk-should-never")
        self.assertEqual(r.hits, [])                           # .env is never searched
        r = fs.search(self.ws, (), "garden")
        self.assertEqual([(h.path, h.line) for h in r.hits], [("readme.txt", 2)])

    def test_search_results_are_bounded(self):
        many = Path(self.ws) / "many"
        many.mkdir()
        for i in range(30):
            (many / f"f{i}.txt").write_text("needle\n" * 20)
        r = fs.search(self.ws, ("many",), "needle")
        self.assertEqual(len(r.hits), fs.MAX_SEARCH_RESULTS)
        self.assertTrue(r.truncated)

    def test_search_time_limit(self):
        r = fs.search(self.ws, (), "garden", deadline_s=0.0)
        self.assertTrue(r.timed_out)

    def test_workspace_root_that_is_a_symlink_is_refused(self):
        os.symlink(self.ws, self.base / "wslink")
        with self.assertRaises(fs.FsError):
            fs.read_file(str(self.base / "wslink"), ("readme.txt",))

    def test_swap_race_never_returns_outside_content(self):
        """While reads run, a thread keeps swapping a workspace file for a symlink
        to an outside file (atomically, with rename). Evidence, not proof."""
        ws = Path(self.ws)
        target = ws / "race.txt"
        target.write_text("SAFE\n")
        safe_src, link_src = ws / "race.safe", ws / "race.link"
        stop = threading.Event()

        def swapper():
            i = 0
            while not stop.is_set():
                i += 1
                if i % 2:
                    os.symlink(self.base / "outside" / "secret.txt", link_src)
                    os.replace(link_src, target)
                else:
                    safe_src.write_text("SAFE\n")
                    os.replace(safe_src, target)

        t = threading.Thread(target=swapper)
        t.start()
        seen = {"ok": 0, "refused": 0}
        try:
            for _ in range(3000):
                try:
                    r = fs.read_file(self.ws, ("race.txt",))
                    self.assertNotIn(SECRET, r.text)
                    self.assertEqual(r.text, "SAFE")
                    seen["ok"] += 1
                except fs.FsError as exc:
                    self.assertIn(exc.code, ("symlink", "not_found"))
                    seen["refused"] += 1
                s = fs.search(self.ws, (), "OUTSIDE")
                self.assertEqual(s.hits, [])
        finally:
            stop.set()
            t.join()
        self.assertGreater(seen["ok"], 0)
        self.assertGreater(seen["refused"], 0)  # the race really happened


if __name__ == "__main__":
    unittest.main()
