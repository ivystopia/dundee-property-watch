"""Publish only generated public files to a dedicated GitHub Pages branch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ASSETS = {'assets/pico.jade.min.css', 'assets/report.css', 'assets/theme.js', 'assets/pico-LICENSE.txt', 'assets/favicon.svg'}


def public_path(name):
    return name in ASSETS | {'index.html', '.nojekyll', 'publication.json'} or bool(re.fullmatch(r'page-[1-9][0-9]*\.html', name))


def snapshot(directory):
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Generated site contains a symlink')
        if not path.is_file():
            continue
        name = path.relative_to(directory).as_posix()
        if not public_path(name):
            raise ValueError(f'Unexpected file in generated site: {name}')
        if name != 'publication.json':
            files[name] = path.read_bytes()
    if not {'index.html', '.nojekyll', *ASSETS} <= files.keys():
        raise ValueError('Generated report or required assets are missing')
    manifest = {'schema_version': 1, 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files['publication.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    return files


def git(repo, *args, env=None, input=None, check=True):
    # The scheduled task must not open pinentry or depend on an unlocked PGP key.
    command = ['git', '-c', 'commit.gpgsign=false', '-c', 'credential.helper=',
               '-c', 'credential.helper=!gh auth git-credential', '-C', str(repo), *args]
    return subprocess.run(command, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', **(env or {})},
                          input=input, capture_output=True, check=check, timeout=90)


def push_snapshot(repo, files, branch='gh-pages'):
    ref = 'refs/heads/' + branch
    found = git(repo, 'ls-remote', '--exit-code', '--heads', 'origin', ref, check=False)
    if found.returncode not in {0, 2}:
        raise RuntimeError('Could not read the publication branch: ' + found.stderr.decode(errors='replace'))
    parent = found.stdout.decode().split()[0] if found.returncode == 0 else None
    if parent:
        git(repo, 'fetch', '--no-tags', 'origin', ref)
        previous = git(repo, 'ls-tree', '-r', '--name-only', parent).stdout.decode().splitlines()
        unexpected = [name for name in previous if not public_path(name)]
        if unexpected:
            raise RuntimeError('Publication branch contains unmanaged files; preserving them: ' + ', '.join(unexpected))
    # An isolated index leaves the user's branch, staged changes and worktree alone.
    with tempfile.TemporaryDirectory(prefix='dundee-pages-index-') as temp:
        env = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git(repo, 'read-tree', '--empty', env=env)
        for name, data in sorted(files.items()):
            if not public_path(name):
                raise ValueError('Refusing to publish a non-public path')
            blob = git(repo, 'hash-object', '-w', '--stdin', input=data).stdout.decode().strip()
            git(repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{name}', env=env)
        tree = git(repo, 'write-tree', env=env).stdout.decode().strip()
    if parent and git(repo, 'rev-parse', parent + '^{tree}').stdout.decode().strip() == tree:
        return parent, False
    args = ['commit-tree', tree]
    if parent:
        args += ['-p', parent]
    commit = git(repo, *args, input=b'Publish Dundee property report\n').stdout.decode().strip()
    # A concurrent remote update rejects this ordinary push. Never force or rewrite it.
    git(repo, 'push', 'origin', f'{commit}:{ref}')
    return commit, True


def github(path):
    result = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('GitHub API read failed: ' + result.stderr.strip())
    return json.loads(result.stdout)


def read_public(url):
    request = Request(url, headers={'Cache-Control': 'no-cache', 'User-Agent': 'DundeePropertyWatch/1.0'})
    with urlopen(request, timeout=30) as response:
        return response.read()


def wait_for_publication(repository, commit, files, url, *, timeout=600, interval=15, log=print):
    deadline = time.monotonic() + timeout
    previous = None
    last_error = 'Deployment has not appeared yet'
    while time.monotonic() < deadline:
        try:
            builds = github(f'repos/{repository}/pages/builds?per_page=10')
            build = next((row for row in builds if row.get('commit') == commit), None)
            status = build.get('status') if build else 'queued'
            if status != previous:
                log(f'GitHub Pages {commit[:12]}: {status}')
                previous = status
            if status == 'errored':
                raise ValueError('GitHub Pages build failed: ' + str(build.get('error')))
            if status == 'built':
                for name, expected in files.items():
                    if name == '.nojekyll':
                        continue  # This build-control dotfile is intentionally not served.
                    actual = read_public(url.rstrip('/') + '/' + name + '?deployment=' + commit)
                    if actual != expected:
                        raise RuntimeError('Live content does not yet match: ' + name)
                log(f'Verified {len(files) - 1} public files at {url}')
                return
        except ValueError:
            raise
        except (RuntimeError, OSError) as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    raise RuntimeError('Timed out verifying GitHub Pages; publication remains pending. ' + last_error)


def publish_site(repo, directory, *, log=print):
    config = json.loads((repo / 'publication-settings.json').read_text())
    repository, branch, url = config['repository'], config['branch'], config['url']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or branch != 'gh-pages':
        raise ValueError('Unexpected publication repository or branch')
    remote = git(repo, 'remote', 'get-url', 'origin').stdout.decode().strip()
    if remote not in {f'https://github.com/{repository}.git', f'git@github.com:{repository}.git'}:
        raise ValueError('Git origin does not match the configured publication repository')
    site = github(f'repos/{repository}/pages')
    if site.get('source') != {'branch': branch, 'path': '/'} or site.get('build_type', 'legacy') != 'legacy':
        raise ValueError('GitHub Pages is not configured for the expected publication branch')
    if site.get('html_url', '').rstrip('/') != url.rstrip('/') or urlsplit(url).scheme != 'https':
        raise ValueError('GitHub Pages URL differs from the configured public URL')
    files = snapshot(directory)
    commit, pushed = push_snapshot(repo, files, branch)
    log(('Pushed' if pushed else 'Reusing') + ' publication ' + commit[:12])
    wait_for_publication(repository, commit, files, url, log=log)
    return {'commit': commit, 'url': url, 'files': len(files) - 1}
