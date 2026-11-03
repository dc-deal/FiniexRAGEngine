"""A mail's stories as corpus articles (#138) — the one place their ids and fields are decided.

An id is derived from the mail's Message-ID, the story's index and the extract version: stable, so a
re-delivered story is a no-op in the store, and different per version, so a re-extraction adds its own
rows while the older version's rows can be found and retired.
"""
from datetime import datetime
from typing import List

from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.types.mail_types import MailStory


def story_url(source_id: str, message_id: str, index: int) -> str:
    """A non-web locator for one story: the source, the mail, the story's position in it."""
    return f'mail://{source_id}/{message_id.strip().strip("<>")}#{index}'


def story_article_id(source_id: str, message_id: str, index: int, extract_version: int) -> str:
    return Article.make_id(story_url(source_id, message_id, index),
                           guid=f'{message_id.strip().lower()}#{index}#v{extract_version}')


def story_articles(source: SourceConfig, message_id: str, sent_at: datetime, extract_version: int,
                   stories: List[MailStory], fetched_at: datetime) -> List[Article]:
    """One article per story; `published_at` is the mail's sending time — when the stories were current."""
    return [Article(article_id=story_article_id(source.source_id, message_id, index, extract_version),
                    source_id=source.source_id, source_weight=source.weight,
                    url=story_url(source.source_id, message_id, index),
                    title=story.headline, summary=story.facts, language='en',
                    published_at=sent_at, fetched_at=fetched_at)
            for index, story in enumerate(stories)]
