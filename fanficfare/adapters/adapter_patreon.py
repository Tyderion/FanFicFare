# -*- coding: utf-8 -*-

# Copyright 2011 Fanficdownloader team, 2018 FanFicFare team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import absolute_import
import json
import logging
import re
from datetime import datetime
from enum import Enum
from xml.sax.saxutils import escape, quoteattr

from .. import exceptions as exceptions
from .base_adapter import BaseSiteAdapter

logger = logging.getLogger(__name__)


class LinkTypes(Enum):
    COLLECTION = "collection"
    CREATOR = "creator"
    POST = "post"


class Metadata(Enum):
    LINKTYPE = "linktype"
    ID = "id"


def getClass():
    return PatreonAdapter


# Class name has to be unique.  Our convention is camel case the
# sitename with Adapter at the end.  www is skipped.
class PatreonAdapter(BaseSiteAdapter):
    ## Patreon URLs come in three shapes that map to our three link types:
    ##   /collection/<id>                     -> COLLECTION (group 1)
    ##   [<creator>/]posts/<slug>-<id>        -> POST       (group 2)
    ##   [c/|cw/]<creator>[/posts]            -> CREATOR    (group 3)
    raw_regex = r"""
        ^https?:\/\/(?:www\.)?patreon\.com\/
        (?:
                (?:collection\/(\d+))
            |
                (?:(?:[\w-]+\/)?posts\/(?:[\w-]+-)?(\d+))
            |
                (?:(?:cw?\/)?([A-Za-z0-9_-]+)(?:\/posts)?)
        ).*$
    """

    # The regex engine here can't handle multiline/verbose regexes, so
    # flatten it into a single line.
    url_regex = "".join(line.strip() for line in raw_regex.splitlines())

    # JSON-API query suffix shared by all api calls.
    API_VERSION = "json-api-version=1.0&json-api-use-default-includes=false"

    # A post listing carries the bodies too, so a chapter costs no request of its own.
    POST_FIELDS = "title,published_at,current_user_can_view,content,content_json_string"

    # In the creator page's bootstrap JSON, which newer pages escape inside a script string.
    CAMPAIGN_ID_RE = r'\\?"campaign\\?":\s*\{\\?"data\\?":\s*\{\\?"id\\?":\s*\\?"(\d+)'

    def __init__(self, config, url):
        BaseSiteAdapter.__init__(self, config, url)

        self.data = {}
        # post id -> body html, filled while listing.
        self.post_bodies = {}

        matched = re.match(self.url_regex, self.url)

        # Group 1 => collection id
        if matched.group(1) is not None:
            self.data[Metadata.ID] = matched.group(1)
            self.data[Metadata.LINKTYPE] = LinkTypes.COLLECTION
        # Group 2 => post id
        elif matched.group(2) is not None:
            self.data[Metadata.ID] = matched.group(2)
            self.data[Metadata.LINKTYPE] = LinkTypes.POST
        # Group 3 => creator (vanity) name
        elif matched.group(3) is not None:
            self.data[Metadata.ID] = matched.group(3)
            self.data[Metadata.LINKTYPE] = LinkTypes.CREATOR
        else:
            self.data[Metadata.ID] = None
            self.data[Metadata.LINKTYPE] = None

        self.story.setMetadata('storyId', self.data[Metadata.ID])

        # normalized story URL.
        linktype = self.data[Metadata.LINKTYPE]
        if linktype == LinkTypes.COLLECTION:
            self._setURL('https://%s/collection/%s' % (self.getSiteDomain(), self.data[Metadata.ID]))
        elif linktype == LinkTypes.CREATOR:
            self._setURL('https://%s/%s' % (self.getSiteDomain(), self.data[Metadata.ID]))
        elif linktype == LinkTypes.POST:
            self._setURL('https://%s/posts/%s' % (self.getSiteDomain(), self.data[Metadata.ID]))
        logger.debug("normalized url: %s", self.url)

        # Each adapter needs to have a unique site abbreviation.
        self.story.setMetadata("siteabbrev", "ptrn")

        # Patreon timestamps are ISO-8601; we only display the date part.
        self.dateformat = "%Y-%m-%d"

        ## Patreon authenticates via Cloudflare-protected JSON endpoints
        ## that a simple credential POST can't satisfy.  Auth is instead
        ## supplied as browser session cookies, e.g.:
        ##   fanficfare --mozilla-cookies cookies.txt -U <url>
        ## Those cookies are loaded into the shared cookiejar and sent
        ## with every request, so no in-adapter login step is needed.

    @staticmethod  # must be @staticmethod, don't remove it.
    def getSiteDomain():
        # The site domain.  Does have www here, if it uses it.
        return "www.patreon.com"

    @classmethod
    def getAcceptDomains(cls):
        return ["patreon.com"]

    @classmethod
    def getConfigSections(cls):
        "Only needs to be overriden if has additional ini sections."
        return ["patreon.com", cls.getSiteDomain(), "https://www.patreon.com"]

    @classmethod
    def getSiteExampleURLs(cls):
        return ("https://www.patreon.com/collection/123 "
                "https://www.patreon.com/posts/example-456 "
                "https://www.patreon.com/creator_name")

    def getSiteURLPattern(self):
        return self.url_regex

    ## ------------------------------------------------------------------
    ## JSON-API helpers
    ## ------------------------------------------------------------------

    def _api_get(self, api_url):
        "GET a JSON-API endpoint and return the decoded object."
        data = self.get_request(api_url)
        try:
            return json.loads(data)
        except ValueError:
            raise exceptions.FailedToDownload(
                "Patreon API returned non-JSON for %s" % api_url)

    @staticmethod
    def _post_id_from_url(url):
        "Pull the trailing numeric post id out of a Patreon post URL."
        m = re.search(r'(\d+)(?:[/?#].*)?$', url)
        return m.group(1) if m else None

    def _filters(self):
        ## One creator can run several series, so each book narrows the
        ## posts: {title, match, exclude, since}, all optional.  Hex-encoded
        ## JSON because the CLI splits -o on '=', and a regex can hold one.
        encoded = self.getConfig('patreon_filter')
        if not encoded:
            return {}
        try:
            return json.loads(bytes.fromhex(encoded).decode('utf-8'))
        except ValueError:
            raise exceptions.FailedToDownload("Unreadable patreon_filter option")

    def _posts_url(self, campaign_id, sort, extra=''):
        ## Patreon answers 400 to a collection filter without the campaign filter.
        return ('https://%s/api/posts?%s'
                '&filter[campaign_id]=%s'
                '&filter[contains_exclusive_posts]=true'
                '&filter[is_draft]=false'
                '%s&sort=%s&fields[post]=%s'
                ) % (self.getSiteDomain(), self.API_VERSION, campaign_id, extra, sort, self.POST_FIELDS)

    def _list_posts(self, url, stop_before=None):
        """Every post of a listing, following links.next to the end.  On a
        newest-first listing, stop_before (YYYY-MM-DD) ends it early."""
        posts = []
        seen = set()
        while url:
            if url in seen:
                raise exceptions.FailedToDownload("Patreon pagination loops at %s" % url)
            seen.add(url)
            response = self._api_get(url)
            for post in response.get('data', []):
                published = post.get('attributes', {}).get('published_at') or ''
                if stop_before and published[:10] < stop_before:
                    return posts
                posts.append(post)
            url = (response.get('links') or {}).get('next')
        return posts

    def _post_body(self, attrs):
        # Patrons usually get rendered html in 'content', free posts only
        # the ProseMirror document in 'content_json_string'.
        if attrs.get('content'):
            return attrs['content']
        if attrs.get('content_json_string'):
            return self._prosemirror_to_html(json.loads(attrs['content_json_string']))
        return ''

    @staticmethod
    def _has_text(body):
        return bool(re.sub(r'<[^>]+>|&nbsp;|\s', '', body))

    def _select_posts(self, posts):
        "The posts that become chapters, in the given order, with their bodies kept."
        filters = self._filters()
        try:
            match = re.compile(filters['match'], re.IGNORECASE) if filters.get('match') else None
            exclude = re.compile(filters['exclude'], re.IGNORECASE) if filters.get('exclude') else None
        except re.error as e:
            raise exceptions.FailedToDownload("Bad Patreon title filter: %s" % e)
        since = filters.get('since')

        selected = []
        for post in posts:
            attrs = post.get('attributes', {})
            title = attrs.get('title') or ''
            if match and not match.search(title):
                continue
            if exclude and exclude.search(title):
                continue
            if since and (attrs.get('published_at') or '')[:10] < since:
                continue
            # Never a chapter: an update reuses chapters by URL, so a
            # "locked" placeholder would stay in the book for good.
            if not attrs.get('current_user_can_view'):
                logger.warning("Patreon: skipping post %s, this account can't view it: %s", post['id'], title)
                continue
            body = self._post_body(attrs)
            if not self._has_text(body):
                logger.warning("Patreon: skipping post %s, it has no text: %s", post['id'], title)
                continue
            self.post_bodies[post['id']] = body
            selected.append(post)
        return selected

    def _add_chapters(self, posts):
        selected = self._select_posts(posts)
        if not selected:
            raise exceptions.FailedToDownload("No Patreon post passed the filters: %s" % self.url)

        datefmt = self.getConfig('datechapter_format',
                                 self.getConfig('datePublished_format', self.dateformat))
        dates = []
        for post in selected:
            attrs = post['attributes']
            chaptermeta = {}
            published = self._parse_date(attrs.get('published_at'))
            if published:
                dates.append(published)
                chaptermeta['date'] = published.strftime(datefmt)
            # Not attrs['url']: its slug follows the title, and chapters are keyed by URL.
            self.add_chapter(attrs.get('title') or ('Post %s' % post['id']),
                             'https://%s/posts/%s' % (self.getSiteDomain(), post['id']),
                             chaptermeta)
        if dates:
            self.story.setMetadata('datePublished', min(dates))
            self.story.setMetadata('dateUpdated', max(dates))

    def _set_author(self, campaign_id, attrs):
        self.story.setMetadata('author', attrs.get('name') or attrs.get('vanity') or 'Patreon')
        self.story.setMetadata('authorId', campaign_id)
        if attrs.get('url'):
            self.story.setMetadata('authorUrl', attrs['url'])

    def _parse_date(self, isostr):
        "Parse a Patreon ISO-8601 timestamp into a naive datetime."
        if not isostr:
            return None
        try:
            return datetime.strptime(isostr[:19], '%Y-%m-%dT%H:%M:%S')
        except (ValueError, TypeError):
            logger.warning("Patreon: could not parse date %r", isostr)
            return None

    ## ------------------------------------------------------------------
    ## Metadata + chapter list -- dispatches on link type.
    ## ------------------------------------------------------------------

    def extractChapterUrlsAndMetadata(self):
        linktype = self.data[Metadata.LINKTYPE]
        if linktype == LinkTypes.COLLECTION:
            self._extract_collection()
        elif linktype == LinkTypes.POST:
            self._extract_post()
        elif linktype == LinkTypes.CREATOR:
            self._extract_creator()
        else:
            raise exceptions.FailedToDownload(
                "Unrecognized Patreon URL: %s" % self.url)

    def _resolve_campaign(self, vanity, page):
        ## The first id on the page need not be this creator's: a logged-in
        ## page can also carry the account's memberships.  Another campaign
        ## here would fill the book with someone else's posts.
        for campaign_id in list(dict.fromkeys(re.findall(self.CAMPAIGN_ID_RE, page)))[:10]:
            campaign = self._api_get('https://%s/api/campaigns/%s?%s&fields[campaign]=name,url,vanity'
                                     % (self.getSiteDomain(), campaign_id, self.API_VERSION))
            attrs = campaign.get('data', {}).get('attributes', {})
            if (attrs.get('vanity') or '').lower() == vanity.lower():
                return campaign_id, attrs
        raise exceptions.FailedToDownload("Couldn't find the campaign on Patreon page: %s" % self.url)

    def _preserve_deleted_chapters(self):
        BaseSiteAdapter._preserve_deleted_chapters(self)
        ## The reader keeps its place by chapter index, so a book only grows
        ## at its end.  The base class can put old chapters after new ones,
        ## and a post added mid-collection would land mid-book.
        if not self.oldchaptersmap:
            return
        old_order = {url: i for i, url in enumerate(self.oldchaptersmap)}
        book_order = lambda chapter: old_order.get(chapter['url'], len(old_order))
        self.story.chapters.sort(key=book_order)
        self.chapterInfos.sort(key=book_order)
        for i, chapter in enumerate(self.story.chapters):
            chapter['number'] = i + 1
            chapter['index04'] = chapter['index'] = '%04d' % (i + 1)

    def _extract_collection(self):
        collection_id = self.data[Metadata.ID]
        ## One trimmed request returns the collection, its members
        ## (post_ids) and the owning campaign.
        api_url = (
            'https://%s/api/collection/%s?%s'
            '&include=posts.campaign'
            '&fields[post]=title,url,published_at,current_user_can_view'
            '&fields[campaign]=name,url,vanity'
            '&fields[collection]=title,description,post_ids,thumbnail'
        ) % (self.getSiteDomain(), collection_id, self.API_VERSION)

        try:
            response = self._api_get(api_url)
        except exceptions.HTTPErrorFFF as e:
            if e.status_code == 404:
                raise exceptions.StoryDoesNotExist(self.url)
            raise

        collection = response.get('data', {})
        attrs = collection.get('attributes', {})
        included = response.get('included', [])

        self.story.setMetadata('title', self._filters().get('title') or attrs.get('title') or ('Collection %s' % collection_id))
        ## Patreon exposes no completion flag for collections; they're
        ## ongoing serials by nature, so default to In-Progress.
        self.story.setMetadata('status', 'In-Progress')

        thumbnail = (attrs.get('thumbnail') or {}).get('url')

        # The campaign is the 'author' of the collection.
        campaigns = [x for x in included if x.get('type') == 'campaign']
        if campaigns:
            campaign_id = campaigns[0].get('id')
            self._set_author(campaign_id, campaigns[0].get('attributes', {}))
        else:
            self.story.setMetadata('author', 'Patreon')
            found = re.search(r'/campaign/(\d+)', thumbnail or '')
            if not found:
                raise exceptions.FailedToDownload(
                    "Couldn't find the campaign of Patreon collection: %s" % self.url)
            campaign_id = found.group(1)

        if attrs.get('description'):
            self.setDescription(self.url, attrs['description'])

        post_ids = set(str(pid) for pid in (attrs.get('post_ids') or []))

        # The paged listing, not 'included', carries the posts.  If it holds
        # one the collection doesn't, the filter wasn't honoured, and the
        # rest of the listing may be the creator's other series.
        posts = self._list_posts(self._posts_url(
            campaign_id, 'collection_order',
            '&filter[collection_id]=%s&filter[include_drops]=true' % collection_id))
        strangers = [p['id'] for p in posts if post_ids and p['id'] not in post_ids]
        if strangers:
            raise exceptions.FailedToDownload(
                "Patreon listed posts outside collection %s: %s" % (collection_id, strangers[:5]))

        # Publish order, not post_ids: real collections marked 'custom'
        # store theirs newest-first, and a serial reads oldest-first.
        posts.sort(key=lambda p: p.get('attributes', {}).get('published_at') or '')
        self._add_chapters(posts)

        if thumbnail:
            self.setCoverImage(self.url, thumbnail)

    def _extract_post(self):
        ## A single post is one chapter; the text comes from getChapterText.
        ## Most individual posts are patron-only, so this path is left for
        ## the auth-enabled pass.
        raise exceptions.FailedToDownload(
            "Patreon single-post downloads aren't implemented yet; "
            "use a /collection/<id> URL: %s" % self.url)

    def _extract_creator(self):
        vanity = self.data[Metadata.ID]
        try:
            page = self.get_request('https://%s/%s' % (self.getSiteDomain(), vanity))
        except exceptions.HTTPErrorFFF as e:
            if e.status_code == 404:
                raise exceptions.StoryDoesNotExist(self.url)
            raise
        campaign_id, attrs = self._resolve_campaign(vanity, page)
        self._set_author(campaign_id, attrs)

        filters = self._filters()
        self.story.setMetadata('title', filters.get('title') or attrs.get('name') or vanity)
        self.story.setMetadata('status', 'In-Progress')

        # Newest first so 'since' can end the walk early; chapters read oldest-first.
        posts = self._list_posts(self._posts_url(campaign_id, '-published_at'),
                                 stop_before=filters.get('since'))
        posts.reverse()
        self._add_chapters(posts)

    ## ------------------------------------------------------------------
    ## Chapter text -- a chapter is a Patreon post.
    ## ------------------------------------------------------------------

    def getChapterText(self, url):
        logger.debug("Getting chapter text from: %s", url)

        post_id = self._post_id_from_url(url)
        if not post_id:
            raise exceptions.FailedToDownload(
                "Couldn't find a post id in chapter URL: %s" % url)

        body = self.post_bodies.get(post_id)
        if body is None:
            raise exceptions.FailedToDownload(
                "Patreon post wasn't in the listing: %s" % url)

        soup = self.make_soup('<div class="patreon-post">%s</div>' % body)
        return self.utf8FromSoup(url, soup.find('div'))

    ## ------------------------------------------------------------------
    ## ProseMirror (TipTap) JSON document -> HTML
    ## ------------------------------------------------------------------

    def _prosemirror_to_html(self, node):
        node_type = node.get('type')
        children = node.get('content') or []
        inner = ''.join(self._prosemirror_to_html(child) for child in children)

        if node_type == 'doc':
            return inner
        if node_type == 'paragraph':
            return '<p>%s</p>' % inner if inner else '<p><br/></p>'
        if node_type == 'heading':
            level = min(max(int(node.get('attrs', {}).get('level', 3)), 1), 6)
            return '<h%d>%s</h%d>' % (level, inner, level)
        if node_type == 'text':
            return self._apply_marks(node.get('text', ''), node.get('marks') or [])
        if node_type in ('hard_break', 'hardBreak'):
            return '<br/>'
        if node_type in ('horizontal_rule', 'horizontalRule'):
            return '<hr/>'
        if node_type == 'blockquote':
            return '<blockquote>%s</blockquote>' % inner
        if node_type in ('bullet_list', 'bulletList'):
            return '<ul>%s</ul>' % inner
        if node_type in ('ordered_list', 'orderedList'):
            return '<ol>%s</ol>' % inner
        if node_type in ('list_item', 'listItem'):
            return '<li>%s</li>' % inner
        if node_type in ('code_block', 'codeBlock'):
            return '<pre><code>%s</code></pre>' % inner
        if node_type == 'image':
            src = node.get('attrs', {}).get('src')
            return '<img src=%s/>' % quoteattr(src) if src else ''

        # Unknown node: keep the children so we never silently drop text.
        logger.debug("Patreon: unhandled prosemirror node type %r", node_type)
        return inner

    @staticmethod
    def _apply_marks(text, marks):
        out = escape(text)
        for mark in marks:
            mark_type = mark.get('type')
            if mark_type in ('bold', 'strong'):
                out = '<b>%s</b>' % out
            elif mark_type in ('italic', 'em'):
                out = '<i>%s</i>' % out
            elif mark_type == 'underline':
                out = '<u>%s</u>' % out
            elif mark_type in ('strike', 'strikethrough', 'strikeThrough'):
                out = '<s>%s</s>' % out
            elif mark_type == 'code':
                out = '<code>%s</code>' % out
            elif mark_type == 'link':
                href = mark.get('attrs', {}).get('href')
                if href:
                    out = '<a href=%s>%s</a>' % (quoteattr(href), out)
        return out
