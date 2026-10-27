"""Extract posts (caption, date, counters, comments, media), followers and following from Instagram."""
import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from dashboard import generate_dashboard

BASE = Path(__file__).resolve().parent
UNAME = r'[A-Za-z0-9._]{1,30}'
HOSTS = {'www.instagram.com', 'instagram.com'}
CDN = ('cdninstagram.com', 'fbcdn.net')
META_DESC = ('meta[property="og:description"]', 'meta[name="description"]')
RESERVED = {'accounts', 'explore', 'direct', 'reels', 'reel', 'p', 'stories',
            'about', 'legal', 'privacy', 'terms', 'challenge', 'settings'}
FOLLOW = {'followers': r'\b(seguidores|followers)\b', 'following': r'\b(seguidos|following)\b'}
BENFORD_MIN_SAMPLE = 200
BENFORD_CANDIDATES = 250
WARNINGS = ('try again later', 'inténtalo de nuevo más tarde', 'intentá de nuevo más tarde',
            'we restrict certain activity', 'restringimos cierta actividad')
COUNT_PATTERN = r'\d+(?:[.,\s\u00a0\u202f]\d+)*(?:\s*(?:mil|[kKmM]))?'
METRIC_LABELS = {
    'likes': r'likes?|me gusta',
    'comments': r'comments?|comentarios?',
    'reposts': r'reposts?|republicar|republicaci(?:ón|ones)|republicad[oa]s?|veces republicad[oa]'}
FIELDS = {
    'posts': ['url', 'caption', 'date', 'screenshot', 'media_kind', 'likes', 'comments',
              'comments_collected', 'comments_status', 'reposts', 'interactions',
              'interactions_observed', 'interactions_approximate', 'interactions_checked_utc',
              'status', 'method'],
    'followers': ['username', 'url', 'followers_count', 'followers_count_approximate',
                  'following_count', 'following_count_approximate', 'counts_status', 'counts_checked_utc'],
    'following': ['username', 'url'],
    'comments': ['post_url', 'username', 'text', 'date', 'url']}
COMMENT_LOAD_MORE = ('button[aria-label*="more comments" i], button[aria-label*="más comentarios" i], '
                     '[role="button"][aria-label*="more comments" i], [role="button"][aria-label*="más comentarios" i], '
                     'button:has(svg[aria-label*="more comments" i]), button:has(svg[aria-label*="más comentarios" i])')

# Text of the page outside posts/comment lists, so user comments never trigger a false restriction.
BODY_JS = r"""() => {
    let text = document.body.innerText || '';
    document.querySelectorAll('article, main ul').forEach(e => { text = text.replace(e.innerText || '', ''); });
    return text;
}"""
# Scroll the closest scrollable ancestor of the last visible element matching `sel`.
SCROLL_JS = r"""sel => {
    let el = [...document.querySelectorAll(sel)].filter(e => e.getClientRects().length).pop();
    while (el) {
        if (el.clientHeight > 100 && el.scrollHeight > el.clientHeight + 10 &&
            /auto|scroll/.test(getComputedStyle(el).overflowY)) {
            const before = el.scrollTop;
            const next = Math.min(before + Math.max(300, el.clientHeight * 0.8),
                                  el.scrollHeight - el.clientHeight);
            if (next > before + 1) {
                el.scrollTop = next;
                return true;
            }
        }
        el = el.parentElement;
    }
    window.scrollBy(0, Math.max(500, innerHeight * 0.8));
    return false;
}"""
GRID_JS = ("(els) => els.filter(e => e.querySelector('img, video') && e.getClientRects().length)"
           ".map(e => e.href)")
# Every comment has a timestamp linking to /p/<code>/c/<id>/: use it to locate each comment block.
READ_COMMENTS_JS = r"""() => {
    const root = document.querySelector('article') || document.querySelector('main') || document.body;
    const anchors = [...root.querySelectorAll('a[href*="/c/"]')].filter(a =>
        /\/(p|reel)\/[^/]+\/c\/\d+/.test(a.getAttribute('href') || '') && a.querySelector('time'));
    return anchors.map(anchor => {
        let item = anchor;
        while (item.parentElement && item.parentElement !== root &&
               item.parentElement.querySelectorAll('a[href*="/c/"] time').length === 1) {
            item = item.parentElement;
        }
        const id = ((anchor.getAttribute('href') || '').match(/\/c\/(\d+)/) || [])[1];
        const user = [...item.querySelectorAll('a[href]')].find(a =>
            /^\/[A-Za-z0-9._]{1,30}\/$/.test(a.getAttribute('href') || ''));
        const username = user ? user.getAttribute('href').replace(/\//g, '') : null;
        const texts = [...item.querySelectorAll('span[dir="auto"]')]
            .filter(s => !s.closest('a, button, [role="button"]'))
            .map(s => s.innerText.trim()).filter(t => t && t !== username);
        const time = anchor.querySelector('time');
        return {id, username, text: texts[0] || null,
                date: time ? time.getAttribute('datetime') : null, url: anchor.href};
    });
}"""
# Counter labels from buttons; the SVG aria-label is joined with the number next to icon-only buttons.
LABELS_JS = '''els => els.filter(e => e.getClientRects().length).flatMap(e => {
    const text = (e.innerText || '').trim();
    const svg = e.querySelector('svg[aria-label]');
    const svgLabel = svg ? svg.getAttribute('aria-label') : '';
    return [e.getAttribute('aria-label') || '', text, svgLabel && text ? svgLabel + ' ' + text : ''];
})'''

class InstagramRestriction(RuntimeError):
    """Stop extraction when Instagram reports a restriction."""

def username_from_url(url):
    parts = urlsplit(url)
    name = parts.path.strip('/')
    return name if parts.hostname in HOSTS and re.fullmatch(UNAME, name) and name not in RESERVED else None

def normalize_post_url(url):
    parts = urlsplit(url)
    match = re.fullmatch(r'/(?:[A-Za-z0-9._]+/)?(p|reel)/([A-Za-z0-9_-]+)/?', parts.path)
    return f'https://www.instagram.com/{match[1]}/{match[2]}/' if parts.hostname in HOSTS and match else None

def get_text(locator):
    return locator.first.inner_text().strip() if locator.count() else ''

def meta(page, *selectors):
    """Yield (selector, content) for each meta tag that has content."""
    for selector in selectors:
        element = page.locator(selector).first
        content = element.get_attribute('content') if element.count() else None
        if content:
            yield selector, content

def valid_date(value):
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
    except (ValueError, AttributeError):
        return None

def scroll(page, selector):
    return page.evaluate(SCROLL_JS, selector)

def check_access(page):
    if any(p in urlsplit(page.url).path for p in ('/challenge/', '/checkpoint/', '/accounts/login')):
        raise InstagramRestriction('Instagram requires authentication or verification.')
    body = page.evaluate(BODY_JS).lower()
    if any(w in body for w in WARNINGS):
        raise InstagramRestriction('Instagram reports a temporary restriction. Extraction stopped.')

def open_page(page, url):
    page.goto(url, wait_until='domcontentloaded')
    page.locator('main, article, header').first.wait_for(state='visible')
    check_access(page)

def wait_for_session(page, context, timeout=180):
    print('Waiting for an Instagram session (up to 180 seconds). Sign in through the browser if needed.')
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if page.is_closed():
            raise RuntimeError('The browser window was closed.')
        path = urlsplit(page.url).path
        if any(p in path for p in ('/challenge/', '/checkpoint/')):
            raise RuntimeError('Instagram requires verification. Complete it and run the program again.')
        has_session = any(c['name'] == 'sessionid' and c['value']
                          for c in context.cookies('https://www.instagram.com/'))
        if has_session and not any(p in path for p in ('/accounts/login', '/accounts/onetap')):
            check_access(page)
            return
        page.wait_for_timeout(1000)
    raise TimeoutError('No active session was detected before the timeout.')

def relationship_link(page, username, kind):
    candidates = page.get_by_role('link', name=re.compile(FOLLOW[kind], re.I))
    for i in range(candidates.count()):
        if candidates.nth(i).is_visible():
            return candidates.nth(i)
    return page.locator(f'a[href="/{username}/{kind}/"]').first

def extract_bio(page):
    """Read only the quoted segment after the counters in the profile metadata."""
    for selector, content in meta(page, *META_DESC):
        match = re.search(r':\s*["“](.*)["”]\s*$', content, re.S)
        if match:
            bio = match[1].strip()
            return {'bio': bio or None, 'bio_method': selector,
                    'bio_status': 'metadata_extracted_unverified' if bio else 'empty_in_metadata'}
    return {'bio': None, 'bio_method': 'metadata', 'bio_status': 'not_detected_automatically'}

def profile(page, username):
    open_page(page, f'https://www.instagram.com/{username}/')
    stats = re.compile(r'seguidores|followers', re.I)
    page.locator('header').filter(has_text=stats).first.wait_for(state='visible')
    found = username_from_url(page.url)
    if not found or found.lower() != username.lower():
        raise RuntimeError('The URL does not match the requested profile.')
    check_access(page)
    data = {'username': username, 'url': page.url,
            'visible_header': get_text(page.locator('header').filter(has_text=stats))}
    for kind in FOLLOW:
        link = relationship_link(page, username, kind)
        data[f'{kind}_visible'] = get_text(link)
        titles = link.locator('[title]').evaluate_all(
            '(els) => els.map(e => e.getAttribute("title"))') if link.count() else []
        data[f'{kind}_total'] = next((count[0] for title in titles
                                     if (count := parse_count(title)) and not count[1]), None)
    return {**data, **extract_bio(page)}

def profile_count(profile_data, kind):
    """Return an exact header count or a clearly marked estimate from its visible label."""
    exact = profile_data.get(f'{kind}_total')
    if isinstance(exact, int) and not isinstance(exact, bool):
        return exact, False
    label = profile_data.get(f'{kind}_visible') or ''
    match = re.search(COUNT_PATTERN, label, re.I)
    parsed = parse_count(match[0]) if match else None
    return parsed if parsed is not None else (None, False)

def enrich_benford_followers(page, followers, checkpoint, candidate_limit=BENFORD_CANDIDATES):
    """Read public follower/following counts from each follower profile, saving progress."""
    candidates = followers[:candidate_limit]
    for index, row in enumerate(candidates, 1):
        username = row.get('username')
        if not username:
            continue
        first, second = row.get('followers_count'), row.get('following_count')
        if (isinstance(first, int) and first > 0 and isinstance(second, int) and second > 0):
            continue
        print(f'Benford sample {index}/{len(candidates)}: @{username}')
        try:
            details = profile(page, username)
            followers_count, followers_approx = profile_count(details, 'followers')
            following_count, following_approx = profile_count(details, 'following')
            row.update({
                'followers_count': followers_count,
                'followers_count_approximate': followers_approx,
                'following_count': following_count,
                'following_count_approximate': following_approx,
                'counts_status': 'complete' if followers_count is not None and following_count is not None else 'partial',
                'counts_checked_utc': datetime.now(timezone.utc).isoformat(),
            })
        except InstagramRestriction:
            raise
        except Exception as exc:
            check_access(page)
            row['counts_status'] = f'error: {type(exc).__name__}'
            print(f'Could not read public counters for @{username}: {type(exc).__name__}')
        checkpoint(list(followers), 'benford_counts_in_progress')
        page.wait_for_timeout(1200)

    complete = sum(
        isinstance(row.get('followers_count'), int) and row['followers_count'] > 0
        and isinstance(row.get('following_count'), int) and row['following_count'] > 0
        for row in candidates
    )
    status = ('benford_sample_ready' if complete >= BENFORD_MIN_SAMPLE
              else f'benford_sample_incomplete_{complete}_of_{BENFORD_MIN_SAMPLE}')
    checkpoint(list(followers), status)
    print(f'Benford sample: {complete}/{BENFORD_MIN_SAMPLE} profiles have both positive counters.')
    return status

def collect_items(page, read, move, limit, rounds, delay, checkpoint=None, stall_limit=5, cap=True):
    found, stalled = {}, 0
    for _ in range(rounds):
        check_access(page)
        previous = len(found)
        found.update(read())
        rows = list(found.values())[:limit] if cap else list(found.values())
        status = 'target_reached' if len(rows) >= limit else 'in_progress'
        if checkpoint:
            checkpoint(rows, status)
        print(f'Collected {len(rows)} records (target {limit}).')
        if status == 'target_reached':
            return rows, status
        stalled = stalled + 1 if len(found) == previous else 0
        if stalled >= stall_limit:
            return rows, 'no_new_records; completeness_unverified'
        move()
        page.wait_for_timeout(delay)
    return rows, 'scroll_limit; partial_result'

def collect_post_links(page, limit, checkpoint):
    def read():
        urls = page.locator('a[href]').evaluate_all(GRID_JS)
        return {url: url for raw in urls if (url := normalize_post_url(raw))}
    return collect_items(page, read, lambda: scroll(page, 'a[href*="/p/"], a[href*="/reel/"]'),
                         limit, 200, 2500, lambda rows, status: checkpoint(rows))

def mentions_author(text, username):
    return bool(username and re.search(r'(?<![\w.])' + re.escape(username) + r'(?![\w.])', text, re.I))

def caption_from_metadata(content, username):
    """Extract only the final quotation attributed to the requested author."""
    match = re.fullmatch(r'(.*?):\s*["“](.*)["”][.\s]*', content or '', re.S)
    if not match or not mentions_author(match[1], username):
        return None
    return match[2].strip() or None

def should_retry(row, incomplete=False):
    return row.get('status') == 'error' or (incomplete and (not row.get('caption') or not row.get('date')))

def structured_post(page, url):
    """Read post-specific public structured metadata, excluding related posts."""
    matches = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            refs = [value.get('url'), value.get('@id'), value.get('mainEntityOfPage')]
            refs = [r.get('@id') or r.get('url') if isinstance(r, dict) else r for r in refs]
            if any(isinstance(r, str) and normalize_post_url(r) == url for r in refs):
                matches.append(value)
            for key in ('@graph', 'mainEntity'):
                visit(value.get(key))

    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            visit(json.loads(raw))
        except (ValueError, TypeError):
            pass
    keys = ('articleBody', 'caption', 'description', 'interactionStatistic')
    return next((m for m in matches if any(m.get(k) for k in keys) or m.get('commentCount') is not None), {})

def parse_count(value):
    """Return (count, is_approximate); ambiguous values stay missing."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().lower()
    if suffix := re.fullmatch(r'(\d+(?:[.,]\d{1,2})?)\s*(mil|k|m)', text):
        return int(float(suffix[1].replace(',', '.')) * (1000000 if suffix[2] == 'm' else 1000)), True
    if re.fullmatch(r'\d+|\d{1,3}(?:[.,\s\u00a0\u202f]\d{3})+', text):
        return int(re.sub(r'\D', '', text)), False
    return None

def labeled_counts(text):
    """Accept whole counter labels only, not numbers embedded in captions."""
    result, text = {}, text.strip()
    for field, label in METRIC_LABELS.items():
        match = (re.fullmatch(r'(?:view all|ver los|ver todos los|ver todos)?\s*(' + COUNT_PATTERN
                              + r')\s+(?:' + label + r')', text, re.I)
                 or re.fullmatch(r'(?:' + label + r')\s*:?\s*(' + COUNT_PATTERN + r')', text, re.I))
        if match and (parsed := parse_count(match[1])) is not None:
            result[field] = parsed
    return result

def extract_interactions(page, structured, username):
    """Read reported counters (likes, comments, reposts); unavailable counts stay None."""
    values = dict.fromkeys(METRIC_LABELS)
    sources, approximations = {}, {}

    def accept(field, parsed, source):
        if parsed is not None and values[field] is None:
            values[field], approximations[field] = parsed
            sources[field] = source

    stats = structured.get('interactionStatistic', [])
    for item in [stats] if isinstance(stats, dict) else stats if isinstance(stats, list) else []:
        if not isinstance(item, dict):
            continue
        action = item.get('interactionType', '')
        if isinstance(action, dict):
            action = action.get('@type') or action.get('@id') or ''
        field = {'LikeAction': 'likes', 'CommentAction': 'comments'}.get(
            str(action).rstrip('/').split('/')[-1].split('#')[-1])
        if field:
            accept(field, parse_count(item.get('userInteractionCount')), 'post JSON-LD')
    accept('comments', parse_count(structured.get('commentCount')), 'post JSON-LD')
    for selector, content in meta(page, *META_DESC):
        prefix = re.split(r'\s+[-–—]\s+', content, maxsplit=1)[0]
        if prefix == content or not mentions_author(content[len(prefix):], username):
            continue
        for field, label in METRIC_LABELS.items():
            match = re.search(r'(?:^|,\s+|;\s*)(' + COUNT_PATTERN + r')\s+(?:' + label
                              + r')(?=,\s+|;\s*|$)', prefix, re.I)
            if match:
                accept(field, parse_count(match[1]), selector)
    # DOM fallback restricted to post counter links and action buttons.
    labels = page.locator('article a[href*="/liked_by/"], main a[href*="/liked_by/"], '
                          'article section button, article section [role="button"], '
                          'main section button, main section [role="button"]').evaluate_all(LABELS_JS)
    for label in labels:
        for field, parsed in labeled_counts(label).items():
            accept(field, parsed, 'visible post counter')
    known = [v for v in values.values() if v is not None]
    values.update(interactions=sum(known) if len(known) == len(METRIC_LABELS) else None,
                  interactions_observed=sum(known) if known else None,
                  interactions_approximate=any(approximations.values()),
                  interaction_sources=sources, interaction_approximations=approximations,
                  interactions_checked_utc=datetime.now(timezone.utc).isoformat())
    return values

def collect_comments(page, limit):
    """Stop at the target, preserving any additional comments already loaded."""
    def read():
        return {item['id']: {key: item.get(key) for key in FIELDS['comments'] if key != 'post_url'}
                for item in page.evaluate(READ_COMMENTS_JS) if item.get('id') and item.get('text')}
    def move():
        button = page.locator(COMMENT_LOAD_MORE).first
        if button.count() and button.is_visible():
            button.click()
        else:
            scroll(page, 'a[href*="/c/"]')
    return collect_items(page, read, move, limit, 40, 1800, stall_limit=4, cap=False)

def media_path(output, url, suffix):
    normalized = normalize_post_url(url)
    if not normalized:
        raise ValueError('Invalid Instagram post URL.')
    path = output / 'media' / (urlsplit(normalized).path.strip('/').split('/')[-1] + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def download_post_image(page, url, output):
    """Save the post's displayed CDN image; never fetch unrelated page URLs."""
    candidates = page.locator('article img, main img').evaluate_all('''elements => elements
        .filter(e => e.getClientRects().length && e.naturalWidth >= 300 && e.clientHeight >= 150)
        .map(e => e.currentSrc || e.src)''')
    candidates = [c for _, c in meta(page, 'meta[property="og:image"]')] + candidates
    for source in dict.fromkeys(candidates):
        if not isinstance(source, str):
            continue
        parsed = urlsplit(source)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in CDN):
            continue
        response = None
        try:
            response = page.context.request.get(source, timeout=15000, max_redirects=0)
            ext = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}.get(
                response.headers.get('content-type', '').split(';')[0].strip())
            body = response.body() if response.ok and ext else b''
            if not body or len(body) > 20 * 1024 * 1024:
                continue
            destination = media_path(output, url, '_image' + ext)
            destination.write_bytes(body)
            return destination.relative_to(output).as_posix()
        except Exception:
            continue
        finally:
            if response is not None:
                response.dispose()
    return None

def capture_post(page, url, output):
    """Screenshot the visible post media (or the viewport as a last resort)."""
    destination = media_path(output, url, '.jpg')
    candidates = page.locator('article video, article img, main video, main img')
    for i in range(min(candidates.count(), 20)):
        candidate = candidates.nth(i)
        box = candidate.bounding_box()
        if box and box['width'] >= 200 and box['height'] >= 150:
            try:
                candidate.scroll_into_view_if_needed()
                page.wait_for_function('''element => element.tagName === 'IMG'
                    ? element.complete && element.naturalWidth > 0
                    : element.readyState >= 2''', arg=candidate.element_handle(), timeout=8000)
                candidate.screenshot(path=str(destination), type='jpeg', quality=80, timeout=10000)
                return f'media/{destination.name}'
            except PlaywrightTimeoutError:
                continue
    page.screenshot(path=str(destination), type='jpeg', quality=80, timeout=10000)
    return f'media/{destination.name}'

def attempt(page, operation, fallback, errors=None):
    """Optional extraction failures must not discard the rest of a post."""
    try:
        return operation()
    except InstagramRestriction:
        raise
    except Exception as exc:
        check_access(page)
        message = f'{type(exc).__name__}: {exc}'
        print(message)
        if errors is not None:
            errors.append(message)
        return fallback

def save_media(page, url, output):
    image = download_post_image(page, url, output)
    return (image, 'downloaded_image') if image else (capture_post(page, url, output), 'screenshot')

def extract_post(page, url, username=None, output=None, comments_limit=0):
    open_page(page, url)
    try:  # navigation may finish before the post is rendered
        page.wait_for_function('''() => !!document.querySelector(
            'article h1, main h1, article time[datetime], main time[datetime]')''', timeout=8000)
    except PlaywrightTimeoutError:
        check_access(page)
    main_content = page.locator('article')
    if not main_content.count():
        main_content = page.locator('main')
    date = main_content.locator('time[datetime]')
    structured = structured_post(page, url)
    caption = get_text(main_content.locator('h1')) or None
    method = 'DOM article/main h1' if caption else 'caption_not_detected'
    if not caption:
        value = structured.get('articleBody') or structured.get('caption') or structured.get('description')
        if isinstance(value, str) and value.strip():
            caption, method = value.strip(), 'post JSON-LD'
    if not caption:
        for selector, content in meta(page, *META_DESC):
            if caption := caption_from_metadata(content, username):
                method = selector + '; quotation attributed to the username; check for truncation'
                break
    extracted_date = (date.first.get_attribute('datetime') if date.count() else None) \
        or valid_date(structured.get('datePublished')) \
        or next((d for _, v in meta(page, 'meta[property="article:published_time"]',
                                    'meta[property="og:published_time"]') if (d := valid_date(v))), None)
    interactions = extract_interactions(page, structured, username)
    screenshot, media_kind = attempt(page, lambda: save_media(page, url, output), (None, None)) if output else (None, None)
    comments, comments_status = attempt(page, lambda: collect_comments(page, comments_limit), ([], 'error')) if comments_limit else ([], 'skipped')
    return {**interactions, 'url': url, 'caption': caption, 'date': extracted_date,
            'screenshot': screenshot, 'media_kind': media_kind, 'comments_list': comments,
            'comments_collected': len(comments), 'comments_status': comments_status,
            'comments_checked_utc': datetime.now(timezone.utc).isoformat() if comments_limit > 0 else None,
            'status': 'extracted_check_truncation' if caption else 'caption_not_detected',
            'method': method + '; date from DOM time or publication metadata'}

def extract_relationships(page, username, kind, checkpoint, limit=100):
    open_page(page, f'https://www.instagram.com/{username}/')
    relationship_link(page, username, kind).click()
    dialog = page.get_by_role('dialog').last
    dialog.wait_for(state='visible')
    page.wait_for_timeout(2000)
    def read():
        urls = dialog.locator('a[href]').evaluate_all('(els) => els.map(e => e.href)')
        return {name.lower(): {'username': name, 'url': f'https://www.instagram.com/{name}/'}
                for url in urls if (name := username_from_url(url)) and name.lower() != username.lower()}
    def move():
        scroll(page, '[role="dialog"] a[href]')
        links = dialog.locator('a[href]')
        for index in range(links.count() - 1, -1, -1):
            candidate = links.nth(index)
            if candidate.is_visible():
                candidate.hover()
                page.mouse.wheel(0, 700)
                return
        dialog.hover()
        page.mouse.wheel(0, 700)
    rows, status = collect_items(page, read, move, limit, 500, 3500,
                                 checkpoint, stall_limit=12)
    if not rows:
        raise RuntimeError('No profile candidates detected; an empty list is not confirmed.')
    return rows, status

def safe_cell(value):
    """Neutralize spreadsheet formulas."""
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value

def save_results(data, output):
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / 'data.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    destination = output / 'data.json'
    for attempt_index in range(5):
        try:
            temporary.replace(destination)
            break
        except PermissionError:
            if attempt_index == 4:
                raise
            time.sleep(0.25 * (attempt_index + 1))
    rows = {'posts': data['posts'], 'followers': data['followers'], 'following': data['following'],
            'comments': [{'post_url': p['url'], **c} for p in data['posts'] for c in p.get('comments_list') or []]}
    for name, fields in FIELDS.items():
        with (output / f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: safe_cell(row.get(k)) for k in fields} for row in rows[name])

def finalize_run(context, data, output):
    """Save results and build the dashboard even if the driver disconnects during cleanup."""
    posts = data['posts']
    data['summary'] = {'posts': len(posts),
                       'with_caption': sum(bool(p.get('caption')) for p in posts),
                       'with_date': sum(bool(p.get('date')) for p in posts),
                       'comments_collected': sum(p.get('comments_collected', 0) for p in posts),
                       'followers_collected': len(data['followers']),
                       'following_collected': len(data['following'])}
    save_results(data, output)
    try:
        context.close()
    except Exception as exc:
        data['errors'].append(f'Browser cleanup: {type(exc).__name__}: {exc}')
        print('Warning: the browser could not close normally. Results have been saved.')
        save_results(data, output)
    finally:
        try:
            runs = {(BASE / 'results').resolve(), (BASE / 'resultados').resolve()}
            generate_dashboard(None if output.parent.resolve() in runs else output.parent)
        except OSError as exc:
            print(f'Could not generate the dashboard: {exc}')

def preserve_post(row, previous):
    if not previous:
        return row
    for key in ('caption', 'date', 'screenshot', 'media_kind'):
        if not row.get(key) and previous.get(key):
            row[key] = previous[key]
    # Keep previously collected comments when collection is skipped or fails.
    old = {item.get('url') or (item.get('username'), item.get('text')): item
           for item in previous.get('comments_list') or []}
    old.update({item.get('url') or (item.get('username'), item.get('text')): item
                for item in row.get('comments_list') or []})
    row['comments_list'] = list(old.values())
    row['comments_collected'] = len(old)
    if row.get('caption') and row['status'] == 'caption_not_detected':
        row['status'] = 'extracted_check_truncation'
    return row

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--username', required=True, help='Target profile username, without @.')
    ap.add_argument('--output', type=Path, default=BASE / 'results', help='Directory for extraction runs.')
    ap.add_argument('--resume', type=Path, help='Path to the JSON file of an earlier run.')
    ap.add_argument('--posts-only', '--skip-relationships', action='store_true', dest='skip_relationships')
    ap.add_argument('--relationships-only', action='store_true',
                    help='Skip posts; combine with --resume to preserve existing posts.')
    ap.add_argument('--retry-incomplete', action='store_true',
                    help='On resume, retry posts with a missing caption or date.')
    ap.add_argument('--refresh-interactions', action='store_true',
                    help='Revisit saved posts to refresh counters and comments.')
    ap.add_argument('--no-screenshots', action='store_true', help='Skip post screenshots.')
    ap.add_argument('--benford', action='store_true',
                    help='Collect at least 200 follower profiles with public follower/following counts for a Benford report.')
    ap.add_argument('--limit', type=int, default=50, help='Posts to extract (1-50).')
    ap.add_argument('--relationship-limit', type=int, default=100, help='Records per relationship list (1-100).')
    ap.add_argument('--comments-limit', type=int, default=25,
                    help='Minimum comments to collect per post (0 disables, max 200).')
    args = ap.parse_args()
    for flag, low, high in (('limit', 1, 50), ('relationship_limit', 1, 100), ('comments_limit', 0, 200)):
        if not low <= getattr(args, flag) <= high:
            ap.error(f'--{flag.replace("_", "-")} must be between {low} and {high}.')
    if args.relationships_only and args.skip_relationships:
        ap.error('--relationships-only cannot be combined with --posts-only.')
    if args.benford and args.skip_relationships:
        ap.error('--benford requires collecting follower profiles; remove --posts-only.')
    username = args.username.lstrip('@')
    if not re.fullmatch(UNAME, username):
        ap.error('Invalid username.')
    output = args.output / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    data = {'extracted_utc': datetime.now(timezone.utc).isoformat(), 'profile': {},
            'post_target': args.limit, 'relationship_limit': args.relationship_limit,
            'comments_limit': args.comments_limit, 'benford_enabled': args.benford,
            'benford_minimum': BENFORD_MIN_SAMPLE, 'posts': [], 'followers': [], 'following': [],
            'statuses': {}, 'errors': []}
    if args.resume:
        try:
            previous = json.loads(args.resume.read_text(encoding='utf-8'))
            if previous.get('profile', {}).get('username', '').lower() != username.lower():
                ap.error('The username does not match the run being resumed.')
            for field in ('posts', 'followers', 'following'):
                if not isinstance(previous.get(field), list):
                    raise ValueError(f'Invalid field: {field}')
            data.update(previous, post_target=args.limit, relationship_limit=args.relationship_limit,
                        comments_limit=args.comments_limit, benford_enabled=args.benford,
                        benford_minimum=BENFORD_MIN_SAMPLE,
                        resumed_utc=datetime.now(timezone.utc).isoformat())
            output = args.resume.resolve().parent
        except (OSError, ValueError) as exc:
            ap.error(f'Cannot resume: {exc}')
    session_dir = BASE / ('.sesion_instagram' if (BASE / '.sesion_instagram').exists() else '.instagram_session')
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(session_dir), headless=False, viewport={'width': 1280, 'height': 900})
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15000)
        try:
            page.goto('https://www.instagram.com/', wait_until='domcontentloaded')
            wait_for_session(page, context)
            data['profile'] = profile(page, username)
            save_results(data, output)

            saved_followers = {
                (row.get('username') or '').lower(): row
                for row in data.get('followers', []) if row.get('username')
            }

            def checkpoint(field, rows, status=None):
                if field == 'followers':
                    merged = []
                    for row in rows:
                        previous = saved_followers.get((row.get('username') or '').lower(), {})
                        merged_row = {**previous, **row}
                        merged.append(merged_row)
                        if merged_row.get('username'):
                            saved_followers[merged_row['username'].lower()] = merged_row
                    rows = merged
                data[field] = rows
                if status:
                    status_field = ('benford' if field == 'followers' and status.startswith('benford_')
                                    else field)
                    data['statuses'][status_field] = status
                save_results(data, output)

            links = data.get('pending_links', [])
            if args.relationships_only:
                links, status = [], data['statuses'].get('post_links', 'skipped_by_option')
            elif len(links) < args.limit:
                links, status = collect_post_links(page, args.limit, lambda rows: checkpoint('pending_links', rows))
            else:
                status = 'links_restored'
            links = links[:args.limit]
            data['statuses']['post_links'] = status
            for i, url in enumerate(links, 1):
                prev = next((r for r in data['posts'] if r.get('url') == url), None)
                has_shot = bool(prev and prev.get('screenshot') and (output / prev['screenshot']).is_file())
                if (prev and prev.get('interactions_checked_utc') and not args.refresh_interactions
                        and not should_retry(prev, args.retry_incomplete)
                        and (not args.comments_limit or prev.get('comments_checked_utc'))
                        and (args.no_screenshots or has_shot)):
                    continue
                print(f'Post {i}/{len(links)}: {url}')
                row = attempt(page, lambda: extract_post(page, url, username,
                              None if args.no_screenshots else output, args.comments_limit),
                              {'url': url, 'status': 'error', 'method': 'DOM'}, data['errors'])
                row = preserve_post(row, prev)
                data['posts'] = [r for r in data['posts'] if r.get('url') != url] + [row]
                save_results(data, output)
            for kind in FOLLOW:
                if args.skip_relationships:
                    data['statuses'].setdefault(kind, 'skipped_by_option')
                    continue

                try:
                    relationship_limit = (max(args.relationship_limit, BENFORD_CANDIDATES)
                                          if args.benford and kind == 'followers'
                                          else args.relationship_limit)
                    rows, status = extract_relationships(page, username, kind,
                                                         lambda rows, status: checkpoint(kind, rows, status),
                                                         relationship_limit)
                except InstagramRestriction:
                    raise
                except Exception as exc:
                    check_access(page)
                    data['errors'].append(f'{kind}: {type(exc).__name__}: {exc}')
                    checkpoint(kind, data[kind], 'error; partial_result')
                    continue
                if kind == 'followers' and args.benford:
                    rows = [{**saved_followers.get((row.get('username') or '').lower(), {}), **row}
                            for row in rows]
                    checkpoint(kind, rows, status)
                    enrich_benford_followers(page, data[kind],
                                              lambda updated, state: checkpoint('followers', updated, state))
                    continue
                checkpoint(kind, rows, status)
        except (Exception, KeyboardInterrupt) as exc:
            data['errors'].append(f'{type(exc).__name__}: {exc}')
            if isinstance(exc, KeyboardInterrupt):
                print('Extraction interrupted from the terminal (KeyboardInterrupt). Progress has been saved.')
            else:
                print(f'Extraction stopped. Progress has been saved: {type(exc).__name__}: {exc}')
        finally:
            finalize_run(context, data, output)
    print(f'Files saved to: {output}')
    print('Review statuses and errors in data.json. A saved list may still be incomplete.')
    print('Summary:', data.get('summary', {}))
    print(f'Local dashboard: {BASE / "dashboard.html"}')

if __name__ == '__main__':
    main()
