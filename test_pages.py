import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pages


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.bare = self.root / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.bare)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True)
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'true')]:
            pages.git(self.repo, 'config', key, value)
        pages.git(self.repo, 'remote', 'add', 'origin', str(self.bare))
        (self.repo / 'README.md').write_text('Source code\n')
        pages.git(self.repo, 'add', 'README.md')
        pages.git(self.repo, 'commit', '-m', 'Initial source')
        self.files = {'index.html': b'<h1>Public report</h1>', '.nojekyll': b''}

    def remote_head(self):
        return pages.git(self.repo, 'ls-remote', 'origin', 'refs/heads/gh-pages').stdout.decode().split()[0]

    def test_unsigned_publish_preserves_worktree_index_and_is_idempotent(self):
        (self.repo / 'README.md').write_text('Unrelated staged change\n')
        pages.git(self.repo, 'add', 'README.md')
        (self.repo / 'README.md').write_text('Also an unstaged change\n')
        before = pages.git(self.repo, 'status', '--porcelain=v1').stdout
        index = (self.repo / '.git/index').read_bytes()
        head = pages.git(self.repo, 'rev-parse', 'HEAD').stdout
        commit, pushed = pages.push_snapshot(self.repo, self.files)
        self.assertTrue(pushed)
        self.assertEqual(self.remote_head(), commit)
        self.assertNotIn(b'gpgsig ', pages.git(self.repo, 'cat-file', '-p', commit).stdout)
        self.assertEqual(pages.push_snapshot(self.repo, self.files), (commit, False))
        self.assertEqual(before, pages.git(self.repo, 'status', '--porcelain=v1').stdout)
        self.assertEqual(index, (self.repo / '.git/index').read_bytes())
        self.assertEqual(head, pages.git(self.repo, 'rev-parse', 'HEAD').stdout)
        self.assertEqual(pages.git(self.repo, 'config', '--local', 'commit.gpgsign').stdout.strip(), b'true')

    def test_removes_stale_generated_page_but_retains_history(self):
        first, _ = pages.push_snapshot(self.repo, {**self.files, 'page-2.html': b'Old page'})
        second, _ = pages.push_snapshot(self.repo, self.files)
        self.assertEqual(pages.git(self.repo, 'rev-parse', second + '^').stdout.decode().strip(), first)
        self.assertNotIn(b'page-2.html', pages.git(self.repo, 'ls-tree', '-r', '--name-only', second).stdout)
        self.assertEqual(pages.git(self.repo, 'show', first + ':page-2.html').stdout, b'Old page')

    def test_refuses_unmanaged_files_on_remote_branch(self):
        blob = pages.git(self.repo, 'hash-object', '-w', '--stdin', input=b'Keep this').stdout.strip()
        tree = pages.git(self.repo, 'mktree', input=b'100644 blob ' + blob + b'\tmanual.txt\n').stdout.strip().decode()
        commit = pages.git(self.repo, 'commit-tree', tree, input=b'Manual publication\n').stdout.strip().decode()
        pages.git(self.repo, 'push', 'origin', commit + ':refs/heads/gh-pages')
        with self.assertRaisesRegex(RuntimeError, 'unmanaged files'):
            pages.push_snapshot(self.repo, self.files)
        self.assertEqual(self.remote_head(), commit)

    def test_concurrent_publication_is_not_overwritten(self):
        parent, _ = pages.push_snapshot(self.repo, self.files)
        original = pages.git
        race_commit = None
        def racing(repo, *args, **kwargs):
            nonlocal race_commit
            if args[:1] == ('push',):
                tree = original(repo, 'rev-parse', parent + '^{tree}').stdout.strip().decode()
                race_commit = original(repo, 'commit-tree', tree, '-p', parent, input=b'Concurrent publisher\n').stdout.strip().decode()
                original(repo, 'push', 'origin', race_commit + ':refs/heads/gh-pages')
            return original(repo, *args, **kwargs)
        with patch('pages.git', side_effect=racing), self.assertRaises(subprocess.CalledProcessError):
            pages.push_snapshot(self.repo, {**self.files, 'index.html': b'Our newer report'})
        self.assertEqual(self.remote_head(), race_commit)

    def test_snapshot_rejects_private_files_and_symlinks(self):
        site = self.root / 'site';site.mkdir()
        for name in pages.ASSETS | {'index.html', '.nojekyll'}:
            p = site / name;p.parent.mkdir(exist_ok=True);p.write_bytes(b'Public')
        valid = pages.snapshot(site)
        manifest = json.loads(valid['publication.json'])
        self.assertEqual(manifest['files']['index.html'], hashlib.sha256(b'Public').hexdigest())
        private = site / 'history.sqlite3';private.write_text('private')
        with self.assertRaisesRegex(ValueError, 'Unexpected file'):
            pages.snapshot(site)
        private.unlink();(site / 'page-2.html').symlink_to(self.repo / 'README.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            pages.snapshot(site)

    def test_wait_checks_exact_commit_and_every_served_file(self):
        files = {**self.files, 'page-2.html': b'Archive'}
        builds = [[{'commit': 'other', 'status': 'built'}], [{'commit': 'target', 'status': 'built'}]]
        with patch('pages.github', side_effect=builds), patch('pages.time.sleep'), patch('pages.read_public', side_effect=[files['index.html'], files['page-2.html']]) as read:
            pages.wait_for_publication('owner/repo', 'target', files, 'https://example.test/report/', interval=0, log=lambda _: None)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all('deployment=target' in call.args[0] for call in read.call_args_list))

    def test_failed_build_does_not_pass_verification(self):
        with patch('pages.github', return_value=[{'commit': 'target', 'status': 'errored', 'error': {'message': 'Build error'}}]), self.assertRaisesRegex(ValueError, 'build failed'):
            pages.wait_for_publication('owner/repo', 'target', self.files, 'https://example.test/', interval=0)


if __name__ == '__main__':
    unittest.main()
