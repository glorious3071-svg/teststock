#!/usr/bin/env python3
"""Quick smoke test for TushareClient."""

from tushare_client import create_client


def main() -> None:
    client = create_client()

    # SDK mode
    df = client.daily(
        ts_code="000001.SZ",
        start_date="20260101",
        end_date="20260110",
    )
    print("[SDK] daily rows:", len(df))
    print(df.head())

    # HTTP mode
    data = client.query_http(
        "daily",
        params={
            "ts_code": "000001.SZ",
            "start_date": "20260101",
            "end_date": "20260110",
        },
    )
    print("[HTTP] response keys:", list(data.keys()))


if __name__ == "__main__":
    main()
