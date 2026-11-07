"""Publish the next reviewed learner ZIP once its exact YouTube video is public."""

import json
import os
import re
import sys
from base64 import b64decode, b64encode
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CHANNEL_ID = 'UC3PsooDxvFG0aEBe4JVtAbg'
INITIAL_PART = 8
PUBLIC_CHECK_VIDEO = 'aQCqgKIEpHA'
TAG = re.compile(r'learner-p(\d{2,})-r([1-9]\d*)', re.ASCII)
REVIEW = re.compile(r'<!-- learner-review (\{[^\n]+\}) -->\r?\n\r?\n')
PUBLIC_VIDEO = re.compile(r'<!-- learner-release video: ([\w-]{11}) -->', re.ASCII)
INDEX_PATH = '/contents/release-index.json'


def version(release):
    tag = TAG.fullmatch(release.get('tag_name', ''))
    return tuple(map(int, tag.groups())) if tag else None


def asset_name(part, revision):
    return f'Networking-Studio-Learner-Parts-01-{part:02d}-r{revision}.zip'


def read_json(url, service, headers=None, method='GET', body=None, allow_missing=False):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers=headers or {}, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404 and allow_missing:
            return None
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
        if check_only:
            if not public_check(PUBLIC_CHECK_VIDEO):
                raise RuntimeError('YouTube did not return the known public Part 8 video on Bitten Tech.')
            print('YouTube Data API verified public Part 8 on the Bitten Tech channel.')
        return
    video_id, digest, notes = read_review(draft)
    if not public_check(video_id):
        print('Waiting for the staged video to be public on Bitten Tech.')
        return
    if check_only:
        print('The next draft passed the ZIP and YouTube checks. Check-only mode made no changes.')
        return
    body = f'<!-- learner-release video: {video_id} -->\n\n{notes}\n\nSHA-256 `{digest}`'.strip()
    result = github(f'/releases/{draft["id"]}', 'PATCH',
                    {'body': body, 'draft': False, 'make_latest': 'true'})
    if result['draft'] or result['tag_name'] != draft['tag_name']:
        raise RuntimeError('GitHub did not confirm publication of the reviewed release.')
    print(f'Published {result["tag_name"]}.')


def sync_release_index(github):
    records = []
    for release in get_releases(github):
        release_version = version(release)
        if release['draft'] or not release_version:
            continue
        part, revision = release_version
        match = PUBLIC_VIDEO.match(release.get('body') or '')
        video = match.group(1) if match else PUBLIC_CHECK_VIDEO if release_version == (INITIAL_PART, 1) else None
        assets = release['assets']
        if (not video or len(assets) != 1 or assets[0]['name'] != asset_name(part, revision)
                or assets[0]['state'] != 'uploaded'
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', assets[0].get('digest') or '')):
            raise RuntimeError(f'Published release {release["tag_name"]} lacks verified index metadata.')
        records.append({'part': part, 'revision': revision, 'tag': release['tag_name'],
                        'youtube': f'https://www.youtube.com/watch?v={video}',
                        'release': release['html_url'], 'download': assets[0]['browser_download_url'],
                        'sha256': assets[0]['digest'].removeprefix('sha256:')})
    records.sort(key=lambda item: (item['part'], item['revision']))
    if not records:
        return
    data = (json.dumps({'releases': records}, indent=2) + '\n').encode('utf-8')
    current = github(INDEX_PATH, allow_missing=True)
    if current and b64decode(current['content']) == data:
        return
    payload = {'message': f'Index learner releases through Part {records[-1]["part"]}',
               'content': b64encode(data).decode('ascii')}
    if current:
        payload['sha'] = current['sha']
    github(INDEX_PATH, 'PUT', payload)
    print('Updated the public release index.')


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    headers = {'Accept': 'application/vnd.github+json',
               'Authorization': f'Bearer {os.environ["GH_TOKEN"]}',
               'X-GitHub-Api-Version': '2022-11-28'}

    def github(path, method='GET', body=None, allow_missing=False):
        return read_json(f'https://api.github.com/repos/{repo}{path}', 'GitHub', headers, method, body, allow_missing)

    def check_public(video_id):
        key = os.environ.get('YOUTUBE_API_KEY')
        if not key:
            raise RuntimeError('Configure the YOUTUBE_API_KEY repository secret.')
        return video_is_public(video_id, key)

    check_only = os.environ.get('CHECK_ONLY') == 'true'
    publish_next(github, check_public, check_only)
    if not check_only:
        sync_release_index(github)


if __name__ == '__main__':
    try:
        main()
    except (KeyError, TypeError, RuntimeError) as error:
        sys.exit(str(error))
