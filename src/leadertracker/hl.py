"""Thin client for Hyperliquid's public REST endpoints (stdlib only, no API key)."""

import json
import time
import urllib.error
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

_HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


class HLError(RuntimeError):
    pass


def info(payload: dict, timeout: int = 20, retries: int = 3):
    """POST to /info with backoff on rate limits and transient server errors."""
    body = json.dumps(payload).encode()
    delay = 2.0
    last_err = None

    for attempt in range(retries):
        req = urllib.request.Request(INFO_URL, data=body, headers=_HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 = rate limited, 5xx = transient. Anything else is our fault.
            if e.code != 429 and e.code < 500:
                detail = e.read()[:300].decode("utf-8", "replace")
                raise HLError(f"{payload.get('type')} -> HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e

        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2

    raise HLError(f"{payload.get('type')} failed after {retries} attempts: {last_err}")


def clearinghouse_state(address: str) -> dict:
    """Account value + open perp positions. Rate limit weight: 2."""
    return info({"type": "clearinghouseState", "user": address})


def candle_snapshot(coin: str, interval: str, start_time: int, end_time: int) -> list:
    """OHLC candles for one coin. Rate limit weight: 2.

    Verified live on 2026-08-20 (BTC, 1d): returns a plain list, oldest first,
    of

        {"t": 1787270400000, "T": 1787356799999, "s": "BTC", "i": "1d",
         "o": "73001.0", "c": "74605.0", "h": "75816.0", "l": "73001.0",
         "v": "24351.09053", "n": 140509}

    Prices are STRINGS, not numbers. `t` is the candle's open time and sits on
    a UTC boundary. Both ends of the window are inclusive - a 5-day range came
    back with 6 daily candles - and the last one is the still-forming candle
    for the current period, so its h/l/c keep moving until the period closes.
    """
    return info({
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": int(start_time),
            "endTime": int(end_time),
        },
    })


def user_role(address: str):
    """Account class: "user", "vault", "subAccount", "agent", or "missing".

    Verified live: returns {"role": "user"} for an ordinary wallet. Returns
    None if the response doesn't carry a role, which callers should treat as
    "unknown" rather than as a rejection.
    """
    data = info({"type": "userRole", "user": address})
    if isinstance(data, dict):
        role = data.get("role")
        if isinstance(role, str):
            return role
    return None


def total_account_value(address: str):
    """Total account equity: spot + every perp clearinghouse.

    NOT the same number as clearinghouseState's marginSummary.accountValue,
    which covers only the default perp clearinghouse. This one matches what
    the leaderboard reports, so it is the right quantity to compare against
    the $100k cohort floor. Returns None if it can't be determined.
    """
    data = info({"type": "portfolio", "user": address})
    if not isinstance(data, list):
        return None
    # [[period, {accountValueHistory: [[ts, "value"], ...], ...}], ...]
    best_ts, best_val = -1, None
    for entry in data:
        if len(entry) != 2 or not isinstance(entry[1], dict):
            continue
        for ts, val in entry[1].get("accountValueHistory") or []:
            if ts > best_ts:
                try:
                    best_ts, best_val = ts, float(val)
                except (TypeError, ValueError):
                    pass
    return best_val


# Verified empirically: /info caps this response at exactly 2000 fills.
# 4 of the first 14 cohort wallets hit the cap on a 7-day window, so paging
# is required or busy wallets silently lose history.
FILLS_PAGE_CAP = 2000


def _paged_fills(req_type: str, address: str, start_time: int, unwrap=None,
                 max_pages: int = 25, page_delay: float = 0.6) -> list:
    """Page a fills-style endpoint past its 2000-record cap."""
    out, seen = [], set()
    cursor = int(start_time)

    for _ in range(max_pages):
        page = info({"type": req_type, "user": address, "startTime": cursor})
        if not isinstance(page, list) or not page:
            break

        fresh = 0
        for item in page:
            f = unwrap(item) if unwrap else item
            if f is None:
                continue
            key = (f.get("tid"), f.get("time"), f.get("coin"), f.get("oid"))
            if key not in seen:
                seen.add(key)
                out.append(f)
                fresh += 1

        if len(page) < FILLS_PAGE_CAP:
            break

        # Re-request the boundary millisecond rather than skipping past it -
        # several fills can share one timestamp. The dedup above absorbs the
        # overlap; no progress means we're done.
        raw_times = [int((unwrap(i) if unwrap else i).get("time") or 0) for i in page]
        next_cursor = max(raw_times)
        if fresh == 0 or next_cursor < cursor:
            break
        cursor = next_cursor
        time.sleep(page_delay)

    return out


def user_fills_by_time(address: str, start_time: int) -> list:
    """Ordinary fills at or after start_time (ms epoch).

    Does NOT include TWAP child fills - see user_twap_slice_fills_by_time.
    Weight: 20 + more per 20 items returned, per page.
    """
    return _paged_fills("userFillsByTime", address, start_time)


def _unwrap_twap(item):
    """TWAP slices arrive as {"fill": {...}, "twapId": n}."""
    if not isinstance(item, dict):
        return None
    f = item.get("fill")
    if not isinstance(f, dict):
        return None
    return dict(f, twapId=item.get("twapId"))


def user_twap_slice_fills_by_time(address: str, start_time: int) -> list:
    """Child fills of TWAP orders.

    These are reported ONLY here - userFills and userFillsByTime omit them
    entirely. Verified on a cohort wallet that showed 1,706 BTC TWAP slices
    against 814 ordinary BTC fills; without these its position appeared to
    jump 186 BTC with no trade behind it, and its traded volume was
    understated by roughly two thirds.
    """
    return _paged_fills("userTwapSliceFillsByTime", address, start_time,
                        unwrap=_unwrap_twap)


def all_fills_by_time(address: str, start_time: int, delay: float = 0.0) -> list:
    """Every fill that moves a position: ordinary plus TWAP slices."""
    fills = user_fills_by_time(address, start_time)
    if delay:
        time.sleep(delay)
    return fills + user_twap_slice_fills_by_time(address, start_time)


def fetch_leaderboard() -> list:
    """The unofficial endpoint behind Hyperliquid's leaderboard page (~35MB)."""
    req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise HLError(f"Leaderboard fetch failed: {e}") from e

    if isinstance(data, dict) and "leaderboardRows" in data:
        return data["leaderboardRows"]
    if isinstance(data, list):
        return data
    raise HLError(f"Unexpected leaderboard response shape: {type(data)}")
