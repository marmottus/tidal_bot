import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from pydantic.v1 import ValidationError
from rss_parser import RSSParser
from rss_parser.models.rss import RSS
from rss_parser.models.rss.item import Item

logger = logging.getLogger(__name__)


@dataclass
class RssEntry:
    title: str
    description: str
    links: list[str] = field(default_factory=list)
    pub_date: str | None = None
    notified: bool = field(default=False, compare=False)


ENTRIES_FILE = Path(__file__).parent.parent.parent / ".session/rss/entries.json"


class Rss:
    def __init__(self) -> None:
        self.entries: list[RssEntry] = []

    def load_entries(self) -> None:
        logger.info("Loading RSS entries from %s", ENTRIES_FILE)

        ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)

        if ENTRIES_FILE.exists():
            try:
                entries_json = json.loads(ENTRIES_FILE.read_text())
                self.entries = [RssEntry(**entry) for entry in entries_json["entries"]]
                logger.info("Loaded %d entries from cache", len(self.entries))
            except (json.JSONDecodeError, TypeError) as e:
                logger.error("Failed to load RSS entries: %s", e)

    def save_entries(self) -> None:
        logger.info("Saving RSS entries to %s", ENTRIES_FILE)
        with ENTRIES_FILE.open("w") as f:
            entries_json = {"entries": [asdict(entry) for entry in self.entries]}
            json.dump(entries_json, f, indent=4)
            logger.info("Saved %d entries to cache", len(self.entries))

    def parse_rss_entries(self) -> None:
        url = "https://eurovisionworld.com/feed"

        logger.info("Parsing RSS feed from %s", url)

        try:
            logger.debug("Fetching RSS feed from %s", url)
            response = requests.get(url, timeout=5)
            response.raise_for_status()
        except requests.HTTPError as e:
            logger.warning(
                "Failed to fetch album image from %s: %s",
                url,
                e,
            )
            return

        try:
            rss_content: RSS = RSSParser.parse(response.text)
        except ValidationError as e:
            logger.error("Failed to parse RSS feed: %s", e)
            return

        if rss_content.channel.items:
            items = rss_content.channel.items
            item: Item
            for item in items:
                if item.title.content is None or item.description.content is None:
                    continue

                links = item.links
                title = item.title.content
                description = item.description.content
                pub_date = item.pub_date.content if item.pub_date else None

                entry = RssEntry(
                    title=title,
                    description=description,
                    links=[l.content for l in links],
                    pub_date=pub_date,
                )

                if entry not in self.entries:
                    self.entries.append(entry)

        logger.info("Parsed %d entries from RSS feed", len(self.entries))
