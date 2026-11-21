"""APP_VERSION is semver and matches the newest CHANGELOG.md entry."""
import os
import re

import server

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_version_is_semver():
    assert re.fullmatch(r'\d+\.\d+\.\d+', server.APP_VERSION)


def test_changelog_top_entry_is_the_current_version():
    with open(os.path.join(REPO, 'CHANGELOG.md'), encoding='utf-8') as f:
        first = next(line for line in f if line.startswith('## '))
    assert first.split()[1] == server.APP_VERSION, 'bump APP_VERSION and CHANGELOG.md together'
