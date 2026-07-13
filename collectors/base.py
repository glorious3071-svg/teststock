"""Base collector abstractions."""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime

import pymysql

from collectors.models import CollectResult, RawArticle
from collectors.storage import finish_run_log, insert_articles, mirror_to_news_flash, start_run_log


class BaseCollector(ABC):
    name: str
    category: str
    tier: str = "flash"
    request_sleep: float = 1.0
    mirror_legacy: bool = False

    @abstractmethod
    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        raise NotImplementedError

    def run(
        self,
        conn: pymysql.connections.Connection,
        *,
        run_id: str | None = None,
        since: datetime | None = None,
        dry_run: bool = False,
    ) -> CollectResult:
        run_id = run_id or str(uuid.uuid4())
        log_id = start_run_log(conn, run_id, self.name) if not dry_run else 0
        result = CollectResult(collector=self.name)

        try:
            articles = self.fetch(since=since)
            result.fetched = len(articles)
            insert_result = insert_articles(conn, articles, dry_run=dry_run)
            result.inserted = insert_result.inserted
            result.skipped_dup = insert_result.skipped_dup
            if self.mirror_legacy and not dry_run and articles:
                mirror_to_news_flash(conn, articles)
        except Exception as exc:
            result.error_msg = str(exc)[:2000]
        finally:
            if not dry_run and log_id:
                finish_run_log(conn, log_id, result)
            if self.request_sleep > 0:
                time.sleep(self.request_sleep)

        return result


__all__ = ["BaseCollector", "RawArticle", "CollectResult"]
