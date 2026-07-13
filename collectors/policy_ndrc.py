"""NDRC policy documents — incremental first-page scrape per section."""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

from collectors.base import BaseCollector
from collectors.dedup import html_to_text, normalize_text
from collectors.models import RawArticle

NDRC_BASE = "https://www.ndrc.gov.cn"
NDRC_ZCFB = f"{NDRC_BASE}/xxgk/zcfb"
USER_AGENT = "Mozilla/5.0 (compatible; teststock-ndrc-collector/1.0)"
FETCH_TIMEOUT = 30

SECTIONS: dict[str, tuple[str, str]] = {
    "fzggwl": ("发展改革委令", "国家发展改革委"),
    "ghxwj": ("规范性文件", "国家发展改革委"),
    "gg": ("公告", "国家发展改革委"),
    "tz": ("通知", "国家发展改革委"),
}

LIST_LINK_RE = re.compile(
    r'<li><a\s+href=["\'](\./\d{6}/t(\d{8})_\d+\.html)["\'][^>]*title=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)


class NdrcPolicyCollector(BaseCollector):
    name = "ndrc_policy"
    category = "policy"
    tier = "daily"
    request_sleep = 0.8

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        articles: list[RawArticle] = []
        errors: list[str] = []
        for section, (ptype, puborg) in SECTIONS.items():
            try:
                articles.extend(self._scrape_section(section, ptype, puborg, since))
            except OSError as exc:
                errors.append(f"{section}:{exc}")
            time.sleep(0.4)
        if not articles:
            articles = self._fallback_from_db(since)
        if not articles and errors:
            raise OSError("; ".join(errors))
        return articles

    def _fallback_from_db(self, since: datetime | None) -> list[RawArticle]:
        """When ndrc.gov.cn is unreachable, reuse recent npr_policy rows."""
        import pymysql

        from collectors.dedup import html_to_text
        from db.connection import mysql_config

        conn = pymysql.connect(**mysql_config())
        try:
            with conn.cursor() as cur:
                if since:
                    cur.execute(
                        """
                        SELECT pubtime, title, url, puborg, ptype, content_html
                        FROM npr_policy
                        WHERE pubtime >= %s
                        ORDER BY pubtime DESC LIMIT 50
                        """,
                        (since,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT pubtime, title, url, puborg, ptype, content_html
                        FROM npr_policy
                        ORDER BY pubtime DESC LIMIT 50
                        """
                    )
                rows = cur.fetchall()
        finally:
            conn.close()

        out: list[RawArticle] = []
        for pubtime, title, url, puborg, ptype, content_html in rows:
            body = html_to_text(content_html) if content_html else None
            out.append(
                RawArticle(
                    source="ndrc",
                    category=self.category,
                    title=normalize_text(str(title)),
                    body_text=body,
                    pub_time=pubtime,
                    url=url,
                    author=puborg,
                    fetch_status="ok" if body else "partial",
                    extra_json={"ptype": ptype, "fallback": "npr_policy"},
                )
            )
        return out

    def _scrape_section(
        self,
        section: str,
        ptype: str,
        puborg: str,
        since: datetime | None,
    ) -> list[RawArticle]:
        base_url = f"{NDRC_ZCFB}/{section}"
        html = self._get(f"{base_url}/")
        if not html:
            raise OSError(f"{section} list page unreachable")

        articles: list[RawArticle] = []
        for rel_url, date_str, title in LIST_LINK_RE.findall(html)[:8]:
            title = normalize_text(title)
            if not title:
                continue
            url = f"{base_url}/{rel_url.lstrip('./')}"
            pub_time = datetime.strptime(date_str, "%Y%m%d")
            if since and pub_time < since:
                continue
            body = self._fetch_body(url)
            articles.append(
                RawArticle(
                    source="ndrc",
                    category=self.category,
                    title=title,
                    body_text=body,
                    pub_time=pub_time,
                    url=url,
                    author=puborg,
                    fetch_status="ok" if body else "partial",
                    extra_json={"section": section, "ptype": ptype, "puborg": puborg},
                )
            )
        return articles

    def _fetch_body(self, url: str) -> str | None:
        html = self._get(url)
        if not html:
            return None
        for pattern in (
            r'(?is)<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>',
            r'(?is)<div[^>]*id="zoom"[^>]*>(.*?)</div>',
            r'(?is)<div[^>]*class="TRS_Editor"[^>]*>(.*?)</div>',
        ):
            m = re.search(pattern, html)
            if m:
                text = html_to_text(m.group(1))
                if text:
                    return text
        return None

    def _get(self, url: str) -> str | None:
        proxy = os.getenv("ALL_PROXY") or os.getenv("all_proxy")
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            return None
        for enc in ("utf-8", "gbk", "gb2312"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
