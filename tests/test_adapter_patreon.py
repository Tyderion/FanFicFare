import json
from urllib.parse import parse_qs, urlsplit

import pytest

from fanficfare import exceptions
from fanficfare.adapters.adapter_patreon import PatreonAdapter, short_chapter_title
from fanficfare.configurable import Configuration

DOMAIN = "https://www.patreon.com"


def post(pid, title, day, can_view=True, content=None, doc="default"):
    if doc == "default":
        doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Body of %s" % pid}]}]}
    return {
        "id": pid,
        "type": "post",
        "attributes": {
            "title": title,
            "url": "%s/creator/posts/%s-%s" % (DOMAIN, title.lower().replace(" ", "-"), pid),
            "published_at": "2026-01-%02dT10:00:00.000+00:00" % day,
            "current_user_can_view": can_view,
            "content": content,
            "content_json_string": json.dumps(doc) if doc else None,
        },
    }


class FakeApi:
    """Answers the adapter's requests; pages holds the /api/posts responses, in cursor order."""

    AUTHOR = {"name": "The Author", "url": DOMAIN + "/creator", "vanity": "creator"}

    def __init__(self, pages, collection=None, creator_page='<script>"pageBootstrap":{"campaign":{"data":{"id":"77"}}}</script>', campaigns=None):
        self.pages = pages
        self.collection = collection
        self.creator_page = creator_page
        self.campaigns = campaigns or {"77": self.AUTHOR}
        self.requests = []

    def page_url(self, index):
        return "%s/api/posts?page[cursor]=%d" % (DOMAIN, index)

    def __call__(self, url, **_):
        self.requests.append(url)
        path = urlsplit(url).path
        if path == "/creator":
            return self.creator_page
        if path.startswith("/api/campaigns/"):
            campaign_id = path.rsplit("/", 1)[1]
            return json.dumps({"data": {"id": campaign_id, "attributes": self.campaigns[campaign_id]}})
        if path.startswith("/api/collection/"):
            return json.dumps(self.collection)
        assert path == "/api/posts", url
        index = int(parse_qs(urlsplit(url).query).get("page[cursor]", ["0"])[0])
        body = {"data": self.pages[index]}
        if index + 1 < len(self.pages):
            body["links"] = {"next": self.page_url(index + 1)}
        return json.dumps(body)


def make_adapter(url, api, filters=None):
    config = Configuration(["www.patreon.com"], "epub")
    config.add_section("overrides")
    if filters is not None:
        config.set("overrides", "patreon_filter", json.dumps(filters).encode("utf-8").hex())
    adapter = PatreonAdapter(config, url)
    adapter.get_request = api
    return adapter


def titles(adapter):
    return [c["title"] for c in adapter.get_chapters()]


CREATOR = DOMAIN + "/creator/posts"


@pytest.mark.parametrize("url", [DOMAIN + "/creator", CREATOR, CREATOR + "/", DOMAIN + "/c/creator", DOMAIN + "/cw/creator/posts", DOMAIN + "/creator?l=de"])
def test_creator_url_shapes_normalize(url):
    assert make_adapter(url, FakeApi([[]])).url == DOMAIN + "/creator"


@pytest.mark.parametrize("url", [DOMAIN + "/cw/creator/posts/title-123", DOMAIN + "/c/creator/collections/5"])
def test_a_deeper_path_is_not_the_creators_whole_feed(url):
    with pytest.raises(exceptions.InvalidStoryURL):
        make_adapter(url, FakeApi([[]]))


def test_collection_url_keeps_its_query_tolerance():
    assert make_adapter(DOMAIN + "/collection/9?view=condensed", FakeApi([[]])).url == DOMAIN + "/collection/9"


def test_an_undated_post_does_not_end_the_walk():
    undated = post("9", "Undated", 1)
    undated["attributes"]["published_at"] = None
    adapter = make_adapter(CREATOR, FakeApi([[undated, post("3", "Three", 3)]]), {"since": "2026-01-02"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Three"]


def test_a_rate_limit_is_reported_as_itself_not_as_a_missing_campaign():
    class Limited(FakeApi):
        def __call__(self, url, **_):
            if urlsplit(url).path == "/api/campaigns/77":
                raise exceptions.HTTPErrorFFF(url, 429, "slow down")
            return FakeApi.__call__(self, url)

    with pytest.raises(exceptions.HTTPErrorFFF):
        make_adapter(CREATOR, Limited([[post("1", "One", 1)]])).extractChapterUrlsAndMetadata()


def test_many_memberships_and_an_unreadable_campaign_dont_hide_the_creator():
    class Unreadable(FakeApi):
        def __call__(self, url, **_):
            if urlsplit(url).path == "/api/campaigns/0":
                raise exceptions.HTTPErrorFFF(url, 404, "gone")
            return FakeApi.__call__(self, url)

    ids = [str(i) for i in range(15)] + ["77"]
    page = " ".join('"campaign":{"data":{"id":"%s"}}' % i for i in ids)
    campaigns = dict({i: {"vanity": "other"} for i in ids}, **{"77": FakeApi.AUTHOR})
    adapter = make_adapter(CREATOR, Unreadable([[post("1", "One", 1)]], creator_page=page, campaigns=campaigns))
    adapter.extractChapterUrlsAndMetadata()
    assert adapter.story.getMetadata("authorId") == "77"


def test_creator_listing_follows_every_page_and_reads_oldest_first():
    api = FakeApi([[post("3", "Three", 3), post("2", "Two", 2)], [post("1", "One", 1)]])
    adapter = make_adapter(CREATOR, api, {"title": "My Book"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["One", "Two", "Three"]
    assert adapter.story.getMetadata("title") == "My Book"
    assert adapter.story.getMetadata("author") == "The Author"


def test_collection_listing_is_paged_and_reads_in_publish_order():
    collection = {
        "data": {"id": "9", "attributes": {"title": "Coll", "post_ids": [2, 1, 3]}},
        "included": [{"type": "campaign", "id": "77", "attributes": {"name": "The Author"}}],
    }
    api = FakeApi([[post("1", "One", 1)], [post("2", "Two", 2), post("3", "Three", 3)]], collection)
    adapter = make_adapter(DOMAIN + "/collection/9", api)
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["One", "Two", "Three"]
    first_listing = next(r for r in api.requests if "/api/posts" in r)
    assert "filter[campaign_id]=77" in first_listing and "filter[collection_id]=9" in first_listing


def test_a_post_outside_the_collection_fails_instead_of_mixing():
    collection = {
        "data": {"id": "9", "attributes": {"title": "Coll", "post_ids": [1]}},
        "included": [{"type": "campaign", "id": "77", "attributes": {}}],
    }
    adapter = make_adapter(DOMAIN + "/collection/9", FakeApi([[post("1", "One", 1), post("2", "Other series", 2)]], collection))
    with pytest.raises(exceptions.FailedToDownload, match="outside collection"):
        adapter.extractChapterUrlsAndMetadata()


def test_the_campaign_must_belong_to_the_creator():
    api = FakeApi([[post("1", "One", 1)]],
                  creator_page='"campaign":{"data":{"id":"5"}} "pageBootstrap":{"campaign":{"data":{"id":"77"}}}',
                  campaigns={"5": {"vanity": "someone-else"}, "77": FakeApi.AUTHOR})
    adapter = make_adapter(CREATOR, api)
    adapter.extractChapterUrlsAndMetadata()
    assert adapter.story.getMetadata("authorId") == "77"


def test_no_campaign_of_this_creator_is_an_error():
    api = FakeApi([[post("1", "One", 1)]], campaigns={"77": {"vanity": "someone-else"}})
    with pytest.raises(exceptions.FailedToDownload, match="find the campaign"):
        make_adapter(CREATOR, api).extractChapterUrlsAndMetadata()


@pytest.mark.parametrize("site_ids, expected", [
    (["4"], ["1", "2", "3", "4"]),  # no old chapter survives: FanFicFare alone gives 4,1,2,3
    (["1", "9", "2", "3"], ["1", "2", "3", "9"]),  # a post added mid-collection goes to the end
    (["1", "3", "4"], ["1", "2", "3", "4"]),  # a deleted post keeps its place
])
def test_old_chapters_keep_their_place_and_new_ones_go_last(site_ids, expected):
    adapter = make_adapter(CREATOR, FakeApi([[]]))
    adapter.configuration.set("overrides", "update_preserve_deleted_chapters", "true")
    url = lambda pid: DOMAIN + "/posts/%s" % pid
    adapter.oldchaptersmap = {url(p): adapter.make_soup("<div>old %s</div>" % p).find("div") for p in ["1", "2", "3"]}
    adapter.oldchaptersdata = {url(p): {"origchapter": p, "title": p, "url": url(p)} for p in ["1", "2", "3"]}
    for pid in site_ids:
        adapter.add_chapter(pid, url(pid))
        adapter.story.addChapter({"url": url(pid), "title": pid, "origtitle": pid, "toctitle": pid, "html": "x"}, False)

    adapter._preserve_deleted_chapters()

    assert [c["url"].rsplit("/", 1)[1] for c in adapter.story.chapters] == expected
    assert [c["url"].rsplit("/", 1)[1] for c in adapter.get_chapters()] == expected
    assert [c["number"] for c in adapter.story.chapters] == [1, 2, 3, 4]
    assert adapter.story.chapters[-1]["index04"] == "0004"


def test_filters_all_apply_and_series_do_not_mix():
    posts = [
        post("6", "Book A: Chapter 3", 6),
        post("5", "Book B: Chapter 2", 5),
        post("4", "book a: announcement", 4),
        post("3", "Book A: Chapter 2", 3),
        post("2", "Book A: Chapter 1", 2),
        post("1", "Book B: Chapter 1", 1),
    ]
    adapter = make_adapter(CREATOR, FakeApi([posts]), {"match": "^book a", "exclude": "announcement", "since": "2026-01-03"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Chapter 2", "Chapter 3"]
    assert [c["url"].rsplit("/", 1)[1] for c in adapter.get_chapters()] == ["3", "6"]


def test_a_tag_is_passed_to_patreons_own_filter():
    api = FakeApi([[post("1", "Chapter 1 - No story name here", 1)]])
    adapter = make_adapter(CREATOR, api, {"title": "Aurora", "tag": "Story - Aurora"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Chapter 1 - No story name here"]
    assert any("filter[tag]=Story%20-%20Aurora" in r for r in api.requests)


def test_since_stops_the_walk_early():
    api = FakeApi([[post("3", "Three", 3), post("1", "One", 1)], [post("0", "Never fetched", 1)]])
    adapter = make_adapter(CREATOR, api, {"since": "2026-01-02"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Three"]
    assert not any("page[cursor]=1" in r for r in api.requests)


def test_locked_and_textless_posts_never_become_chapters():
    posts = [
        post("4", "Attachment only", 4, doc=None),
        post("3", "Image only", 3, content='<p><img src="x"/>&nbsp;</p>', doc=None),
        post("2", "Locked", 2, can_view=False, doc=None),
        post("1", "Readable", 1),
    ]
    adapter = make_adapter(CREATOR, FakeApi([posts]))
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Readable"]


def test_nothing_selected_is_an_error():
    with pytest.raises(exceptions.FailedToDownload, match="passed the filters"):
        make_adapter(CREATOR, FakeApi([[post("1", "One", 1)]]), {"match": "nope"}).extractChapterUrlsAndMetadata()


def test_bad_regex_is_an_error():
    with pytest.raises(exceptions.FailedToDownload, match="title filter"):
        make_adapter(CREATOR, FakeApi([[post("1", "One", 1)]]), {"match": "("}).extractChapterUrlsAndMetadata()


def test_chapter_url_survives_a_retitle():
    before = make_adapter(CREATOR, FakeApi([[post("1", "Old title", 1)]]))
    after = make_adapter(CREATOR, FakeApi([[post("1", "New title", 1)]]))
    before.extractChapterUrlsAndMetadata()
    after.extractChapterUrlsAndMetadata()
    assert before.get_chapters()[0]["url"] == after.get_chapters()[0]["url"] == DOMAIN + "/posts/1"


def test_chapter_text_comes_from_the_listing_without_another_request():
    api = FakeApi([[post("1", "One", 1)]])
    adapter = make_adapter(CREATOR, api)
    adapter.extractChapterUrlsAndMetadata()
    count = len(api.requests)
    assert "Body of 1" in adapter.getChapterText(DOMAIN + "/posts/1")
    assert len(api.requests) == count


@pytest.mark.parametrize("raw, book, expected", [
    ("Black Apron - Max Level Archmaid - Chapter Seventy-Five - The Chosen Path", "Black Apron", "Chapter 75 - The Chosen Path"),
    ("Black Apron - - Max Level Archmaid - Chapter Two - False", "Black Apron", "Chapter 2 - False"),
    ("Black Apron - Max Level Archmaid - Chapter Sixty- Six - On-y Off-y", "Black Apron", "Chapter 66 - On-y Off-y"),
    ("Black Apron - Max Level Archmaid", "Black Apron", "Max Level Archmaid"),
    ("Throne of Time: Chapter 1", "Throne of Time", "Chapter 1"),
    ("Throne of Time - Chapter 75: Unexpected Foundations", "Throne of Time", "Chapter 75 - Unexpected Foundations"),
    ("Throne of Time - Chapter 78-2: The Sequences of the Library", "Throne of Time", "Chapter 78-2 - The Sequences of the Library"),
    ("Gifted - Chapter 71 ", "Gifted", "Chapter 71"),
    ("Gifted - Chapter 120.1", "Gifted", "Chapter 120.1"),
    ("Aura Overload 1.1 - A New Skin", "Aura Overload", "Chapter 1.1 - A New Skin"),
    ("Aura 1.57 - Following the Trail", "Aura Overload", "Chapter 1.57 - Following the Trail"),
    ("Aura Overload - 2.32 Tempting Fate", "Aura Overload", "Chapter 2.32 - Tempting Fate"),
    ("Aurora of the Rainbow Rings - Prologue", "Aurora of the Rainbow Rings", "Prologue"),
    ("Chapter 1 - Reincarnation", "Aurora of the Rainbow Rings", "Chapter 1 - Reincarnation"),
    ("Chapter Two - Three Dogs", "X", "Chapter 2 - Three Dogs"),
    ("Chapter One Hundred Five - Late", "X", "Chapter 105 - Late"),
    ("Bonus Chapter - Beach Day", "X", "Bonus Chapter - Beach Day"),
    ("Aura Overload's Thursday Update", "Aura Overload", "Aura Overload's Thursday Update"),
    ("Aura Overload", "Aura Overload", "Aura Overload"),
    ("Some Ordinary Title", "", "Some Ordinary Title"),
])
def test_short_chapter_title(raw, book, expected):
    assert short_chapter_title(raw, book) == expected


def test_chapters_get_short_titles_but_filters_see_the_full_one():
    posts = [post("2", "Other Series - Chapter 1", 2), post("1", "My Book - Chapter 1 - Start", 1)]
    adapter = make_adapter(CREATOR, FakeApi([posts]), {"title": "My Book", "match": "^My Book"})
    adapter.extractChapterUrlsAndMetadata()
    assert titles(adapter) == ["Chapter 1 - Start"]
