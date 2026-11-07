"""Publish the next reviewed learner ZIP once its exact YouTube video is public."""

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CHANNEL_ID = 'UC3PsooDxvFG0aEBe4JVtAbg'
INITIAL_PART = 8
TAG = re.compile(r'learner-p(\d{2,})-r([1-9]\d*)', re.ASCII)
REVIEW = re.compile(r'<!-- learner-review (\{[^\n]+\}) -->\r?\n\r?\n')


def version(release):
    tag = TAG.fullmatch(release.get('tag_name', ''))
    return tuple(map(int, tag.groups())) if tag else None


def asset_name(part, revision):
    return f'Networking-Studio-Learner-Parts-01-{part:02d}-r{revision}.zip'


def read_json(url, service, headers=None, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers=headers or {}, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'{service} returned HTTP {error.code}.') from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f'Could not read a valid response from {service}.') from None


def get_releases(github):
    releases = []
    page = 1
    while True:
        batch = github(f'/releases?per_page=100&page={page}')
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def next_draft(releases):
    published = [version(item) for item in releases if not item['draft'] and version(item)]
    last = max(published, default=(INITIAL_PART - 1, 0))
    candidates = []
    for item in releases:
        current = version(item)
        if (item['draft'] and current and last < current and INITIAL_PART <= current[0] <= last[0] + 1
                and (item.get('body') or '').startswith('<!-- learner-review ')):
            candidates.append((current, item))
    versions = [current for current, _ in candidates]
    if len(set(versions)) != len(versions):
        raise RuntimeError('Multiple reviewed drafts share one version.')
    # Advance one part at a time. If multiple revisions are staged, use the newest.
    candidates.sort(key=lambda entry: (entry[0][0], -entry[0][1]))
    return candidates[0][1] if candidates else None


def read_review(draft):
    part, revision = version(draft)
    match = REVIEW.match(draft['body'])
    if not match:
        raise RuntimeError('The next draft has an invalid review record.')
    try:
        review = json.loads(match.group(1))
        video_id, digest = review['videoId'], review['sha256']
        if not re.fullmatch(r'[\w-]{11}', video_id, re.ASCII) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('The next draft has an invalid video ID or review checksum.') from None
    assets = draft['assets']
    if len(assets) != 1:
        raise RuntimeError('The reviewed draft must contain exactly one learner ZIP.')
    asset = assets[0]
    if (asset['name'] != asset_name(part, revision) or asset['state'] != 'uploaded'
            or asset.get('digest') != f'sha256:{digest}'):
        raise RuntimeError('The uploaded ZIP does not match the reviewed archive.')
    return video_id, digest, draft['body'][match.end():].strip()


def video_is_public(video_id, key):
    fields = 'items(id,snippet/channelId,status/privacyStatus)'
    query = urlencode({'id': video_id, 'part': 'snippet,status', 'fields': fields, 'key': key})
    result = read_json(f'https://www.googleapis.com/youtube/v3/videos?{query}', 'YouTube')
    return any(item.get('id') == video_id and item.get('snippet', {}).get('channelId') == CHANNEL_ID
               and item.get('status', {}).get('privacyStatus') == 'public' for item in result['items'])


def publish_next(github, public_check, check_only):
    draft = next_draft(get_releases(github))
    if draft is None:
        print('No reviewed draft is next in the release sequence.')
        return
    video_id, digest, notes = read_review(draft)
    if not public_check(video_id):
        print('Waiting for the staged video to be public on Bitten Tech.')
        return
    if check_only:
        print('The next draft passed the ZIP and YouTube checks. Check-only mode made no changes.')
        return
    body = f'{notes}\n\nSHA-256 `{digest}`'.strip()
    result = github(f'/releases/{draft["id"]}', 'PATCH',
                    {'body': body, 'draft': False, 'make_latest': 'true'})
    if result['draft'] or result['tag_name'] != draft['tag_name']:
        raise RuntimeError('GitHub did not confirm publication of the reviewed release.')
    print(f'Published {result["tag_name"]}.')


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    headers = {'Accept': 'application/vnd.github+json',
               'Authorization': f'Bearer {os.environ["GH_TOKEN"]}',
               'X-GitHub-Api-Version': '2022-11-28'}

    def github(path, method='GET', body=None):
        return read_json(f'https://api.github.com/repos/{repo}{path}', 'GitHub', headers, method, body)

    def check_public(video_id):
        key = os.environ.get('YOUTUBE_API_KEY')
        if not key:
            raise RuntimeError('Configure the YOUTUBE_API_KEY repository secret.')
        return video_is_public(video_id, key)

    publish_next(github, check_public, os.environ.get('CHECK_ONLY') == 'true')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, TypeError, RuntimeError) as error:
        sys.exit(str(error))
