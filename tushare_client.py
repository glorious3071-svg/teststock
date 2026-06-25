"""
Tushare client for teajoin.com proxy.

Supports SDK mode (recommended) and direct HTTP mode.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import requests
import tushare as ts
from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://teajoin.com"


class TushareClient:
    """Wrapper around Tushare Pro API via teajoin proxy."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        load_env: bool = True,
    ) -> None:
        if load_env:
            load_dotenv()

        self.api_key = api_key or os.getenv("TUSHARE_API_KEY")
        self.base_url = (base_url or os.getenv("TUSHARE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

        if not self.api_key:
            raise ValueError("API key is required. Set TUSHARE_API_KEY or pass api_key=.")

        self._pro: ts.pro_api | None = None

    def _init_pro(self) -> ts.pro_api:
        if self._pro is not None:
            return self._pro

        ts.set_token(self.api_key)
        pro = ts.pro_api()
        pro._DataApi__token = self.api_key
        pro._DataApi__http_url = self.base_url
        # teajoin docs also reference the single-underscore attribute name.
        pro._DataApi_token = self.api_key

        self._pro = pro
        return pro

    @property
    def pro(self) -> ts.pro_api:
        """Underlying tushare pro_api instance."""
        return self._init_pro()

    def query_http(
        self,
        api_name: str,
        params: dict[str, Any] | None = None,
        fields: str | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Call API via direct HTTP POST."""
        payload: dict[str, Any] = {
            "api_name": api_name,
            "token": self.api_key,
            "params": params or {},
        }
        if fields:
            payload["fields"] = fields

        response = requests.post(self.base_url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def query(
        self,
        api_name: str,
        params: dict[str, Any] | None = None,
        fields: str | None = None,
    ) -> pd.DataFrame:
        """Call API via SDK and return a DataFrame."""
        kwargs = dict(params or {})
        if fields:
            kwargs["fields"] = fields

        method = getattr(self.pro, api_name)
        return method(**kwargs)

    def pro_bar(self, **kwargs: Any) -> pd.DataFrame:
        """Module-level ts.pro_bar() requires explicit api=pro."""
        return ts.pro_bar(api=self.pro, **kwargs)

    # Common shortcuts
    def daily(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.daily(**kwargs)

    def weekly(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.weekly(**kwargs)

    def monthly(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.monthly(**kwargs)

    def daily_basic(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.daily_basic(**kwargs)

    def stock_basic(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.stock_basic(**kwargs)

    def trade_cal(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.trade_cal(**kwargs)

    def index_daily(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.index_daily(**kwargs)

    def moneyflow(self, **kwargs: Any) -> pd.DataFrame:
        return self.pro.moneyflow(**kwargs)


def create_client(
    api_key: str | None = None,
    base_url: str | None = None,
) -> TushareClient:
    """Factory helper."""
    return TushareClient(api_key=api_key, base_url=base_url)
