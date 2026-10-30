import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
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

    def test_wait_checks_manifest_then_every_served_file_without_api(self):
        files = {**self.files, 'page-2.html': b'Archive', 'publication.json': b'New manifest'}
        replies = [b'Old manifest', files['publication.json'], files['index.html'], files['page-2.html']]
        with patch('pages.time.sleep'), patch('pages.read_public', side_effect=replies) as read:
            pages.wait_for_publication('target', files, 'https://example.test/report/', interval=0, log=lambda _: None)
        self.assertEqual(read.call_count, 4)
        self.assertTrue(all('deployment=target' in call.args[0] for call in read.call_args_list))
        self.assertIn('/publication.json?', read.call_args_list[0].args[0])
        self.assertIn('/page-2.html?', read.call_args_list[-1].args[0])

    def test_snapshot_publishes_only_named_checksummed_thumbnail_files(self):
        site = self.root / 'site';site.mkdir()
        for name in pages.ASSETS | {'index.html', '.nojekyll'}:
            path = site / name;path.parent.mkdir(exist_ok=True);path.write_bytes(b'Public')
        image = BytesIO()
        Image.new('RGB', (640, 427), 'green').save(image, format='JPEG')
        jpeg = image.getvalue()
        name = 'images/' + hashlib.sha256(jpeg).hexdigest() + '.jpg'
        photo = site / name;photo.parent.mkdir();photo.write_bytes(jpeg)
        files = pages.snapshot(site)
        self.assertEqual(files[name], jpeg)
        self.assertIn(name, json.loads(files['publication.json'])['files'])
        photo.write_bytes(b'Corrupt or private contents')
        with self.assertRaisesRegex(ValueError, 'thumbnail content or checksum'):
            pages.snapshot(site)
        for invalid in ['images/history.sqlite3', 'images/photo.jpg', 'images/../history.sqlite3', 'images/' + 'a'*64 + '.svg']:
            self.assertFalse(pages.public_path(invalid), invalid)

    def test_stale_archive_cannot_pass_verification(self):
        files = {**self.files, 'page-2.html': b'New archive'}
        with patch('pages.time.monotonic', side_effect=[0, 0, 1, 2]), patch('pages.time.sleep'), patch('pages.read_public', side_effect=[files['index.html'], b'Old archive']), self.assertRaisesRegex(RuntimeError, 'publication remains pending.*page-2.html'):
            pages.wait_for_publication('target', files, 'https://example.test/', timeout=2, interval=0)

    def test_publication_uses_normal_git_and_public_verification(self):
        config = {'repository': 'owner/repo', 'branch': 'gh-pages', 'url': 'https://owner.github.io/repo/'}
        (self.repo / 'publication-settings.json').write_text(json.dumps(config))
        remote = subprocess.CompletedProcess([], 0, stdout=b'git@github.com:owner/repo.git\n')
        with patch('pages.git', return_value=remote), patch('pages.snapshot', return_value=self.files), patch('pages.push_snapshot', return_value=('target', True)) as push, patch('pages.wait_for_publication') as verify:
            result = pages.publish_site(self.repo, self.root / 'site', log=lambda _: None)
        push.assert_called_once_with(self.repo, self.files, 'gh-pages')
        verify.assert_called_once_with('target', self.files, config['url'], log=unittest.mock.ANY)
        self.assertEqual(result['commit'], 'target')


if __name__ == '__main__':
    unittest.main()
