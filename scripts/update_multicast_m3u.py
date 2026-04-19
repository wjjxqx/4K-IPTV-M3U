#!/usr/bin/env python3
"""
Fetch multicast IPTV lists from iptv.cqshushu.com per province, validate streams,
write region M3U files (e.g. hubei4K.m3u) into the repo root or OUTPUT_DIR.

Requires Playwright browsers:  playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://iptv.cqshushu.com/"
MULTICAST_ENTRY = "https://iptv.cqshushu.com/index.php?t=multicast"

# province code, Chinese name, output slug (file: {slug}4K.m3u)
REGIONS: list[tuple[str, str, str]] = [
    ("hb", "湖北", "hubei"),
    ("nm", "内蒙古", "neimenggu"),
    ("sc", "四川", "sichuan"),
    ("bj", "北京", "beijing"),
    ("sd", "山东", "shandong"),
    ("he", "河北", "hebei"),
    ("tj", "天津", "tianjin"),
    ("js", "江苏", "jiangsu"),
    ("ah", "安徽", "anhui"),
    ("sn", "陕西", "shaanxi"),
    ("ha", "河南", "henan"),
    ("sh", "上海", "shanghai"),
    ("jl", "吉林", "jilin"),
    ("zj", "浙江", "zhejiang"),
    ("gd", "广东", "guangdong"),
    ("hi", "海南", "hainan"),
    ("hl", "黑龙江", "heilongjiang"),
    ("yn", "云南", "yunnan"),
    ("fj", "福建", "fujian"),
    ("cq", "重庆", "chongqing"),
    ("hn", "湖南", "hunan"),
    ("gz", "贵州", "guizhou"),
    ("tw", "台湾", "taiwan"),
    ("qh", "青海", "qinghai"),
    ("sx", "山西", "shanxi"),
    ("xj", "新疆", "xinjiang"),
    ("gx", "广西", "guangxi"),
    ("gs", "甘肃", "gansu"),
    ("jx", "江西", "jiangxi"),
    ("ln", "辽宁", "liaoning"),
    ("nx", "宁夏", "ningxia"),
    ("vn", "越南", "yuenan"),
    ("ru", "俄罗斯", "eluosi"),
]


@dataclass
class MulticastRow:
    token: str
    ip: str
    type_label: str
    online_at: str


def _parse_time(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def _sub_group(region_zh: str, channel_name: str) -> str:
    n = channel_name.upper()
    if "CCTV" in n or "央视" in channel_name or channel_name.startswith("中央"):
        return "央视"
    if "卫视" in channel_name:
        return "卫视"
    return "其他"


def _build_extinf(region_zh: str, channel: str) -> str:
    sub = _sub_group(region_zh, channel)
    return f'#EXTINF:-1 group-title="{region_zh}地区/{sub}",{channel}'


def _parse_m3u_text(text: str) -> list[tuple[str, str]]:
    """Return list of (channel_name, url) from raw M3U."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[tuple[str, str]] = []
    pending_name: str | None = None
    for ln in lines:
        if ln.startswith("#EXTINF"):
            pending_name = ln.rsplit(",", 1)[-1].strip()
        elif not ln.startswith("#") and pending_name:
            if ln.startswith("http://") or ln.startswith("https://"):
                out.append((pending_name, ln))
            pending_name = None
    return out


def _rewrite_m3u(region_zh: str, pairs: Iterable[tuple[str, str]]) -> str:
    lines = ["#EXTM3U"]
    for name, url in pairs:
        lines.append(_build_extinf(region_zh, name))
        lines.append(url)
    return "\n".join(lines) + "\n"


def _extract_channel_pairs_from_html(html: str, base_url: str) -> list[tuple[str, str]]:
    """Parse <a href=....m3u8>Title</a> pairs from list HTML."""
    pat = re.compile(
        r'<a[^>]+href=["\'](?P<href>[^"\']+\.m3u8[^"\']*)["\'][^>]*>(?P<title>[^<]+)</a>',
        re.I,
    )
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in pat.finditer(html):
        title = re.sub(r"\s+", " ", m.group("title")).strip()
        href = m.group("href").replace("&amp;", "&")
        if not title or "javascript:" in href.lower():
            continue
        url = href if href.startswith("http") else urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        pairs.append((title, url))
    return pairs


def _collect_m3u8_hrefs(html: str, base_url: str) -> list[str]:
    hrefs = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html, flags=re.I)
    hrefs += re.findall(r'href\s*=\s*"([^"]+\.m3u8[^"]*)"', html, flags=re.I)
    out: list[str] = []
    seen = set()
    for h in hrefs:
        u = h if h.startswith("http") else urljoin(base_url, h)
        u = u.replace("&amp;", "&")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _find_m3u_download_url(html: str, base_url: str) -> str | None:
    for m in re.finditer(r'href\s*=\s*"([^"]+)"', html, flags=re.I):
        href = m.group(1).replace("&amp;", "&")
        if ".m3u" in href.lower() and "javascript:" not in href.lower():
            return urljoin(base_url, href)
    for m in re.finditer(r"https?://[^\s\"'<>]+\.m3u(?:\?[^\s\"'<>]*)?", html, flags=re.I):
        return m.group(0)
    return None


def _probe_m3u8(context, url: str, timeout_ms: int) -> bool:
    try:
        r = context.request.head(url, timeout=timeout_ms)
        if r and r.ok:
            return True
    except Exception:
        pass
    try:
        r = context.request.get(
            url,
            timeout=timeout_ms,
            headers={"Range": "bytes=0-8191"},
        )
        if not r:
            return False
        if r.status >= 400:
            return False
        ct = (r.headers.get("content-type") or "").lower()
        body = r.body() or b""
        if "mpegurl" in ct or "m3u8" in ct or body.lstrip().startswith(b"#EXTM3U"):
            return True
    except Exception:
        return False
    return False


def _extract_rows(page) -> list[MulticastRow]:
    section = page.locator('section[aria-label="组播源列表"]')
    rows = section.locator("tbody tr")
    n = rows.count()
    result: list[MulticastRow] = []
    for i in range(n):
        tr = rows.nth(i)
        try:
            onclick = tr.locator("a.ip-link").get_attribute("onclick") or ""
            m = re.search(r"gotoIP\('([^']+)'\s*,\s*'multicast'\)", onclick)
            if not m:
                continue
            token = m.group(1)
            ip = tr.locator("a.ip-link").inner_text().strip()
            tds = tr.locator("td")
            type_label = ""
            online_at = ""
            for j in range(tds.count()):
                cell = tds.nth(j)
                label = (cell.get_attribute("data-label") or "").strip()
                txt = cell.inner_text().strip()
                if label.startswith("类型"):
                    type_label = txt
                if label.startswith("上线时间"):
                    online_at = txt
            result.append(MulticastRow(token=token, ip=ip, type_label=type_label, online_at=online_at))
        except Exception:
            continue
    return result


def _pick_row(rows: list[MulticastRow], region_zh: str) -> MulticastRow | None:
    if not rows:
        return None
    # Prefer rows whose 类型 mentions the province name
    tagged = [r for r in rows if region_zh in r.type_label]
    pool = tagged if tagged else rows
    # Newest 上线时间 first
    pool.sort(key=lambda r: _parse_time(r.online_at), reverse=True)
    # Prefer 新上线 if status column exists (optional)
    return pool[0]


def process_region(page, context, code: str, region_zh: str, slug: str, args) -> str | None:
    page.goto(MULTICAST_ENTRY, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page.wait_for_timeout(1500)
    try:
        page.select_option("#provinceSelect", code)
    except Exception:
        print(f"[skip] {region_zh}: province select failed", file=sys.stderr)
        return None
    page.wait_for_load_state("networkidle", timeout=args.timeout_ms)
    page.wait_for_timeout(800)
    try:
        page.select_option("#limitSelect", str(args.per_page))
    except Exception:
        pass
    page.wait_for_timeout(500)

    rows = _extract_rows(page)
    row = _pick_row(rows, region_zh)
    if not row:
        print(f"[skip] {region_zh}: no multicast rows", file=sys.stderr)
        return None

    detail_url = f"{BASE.rstrip('/')}/index.php?p={row.token}&t=multicast"
    page.goto(detail_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page.wait_for_timeout(1200)
    html = page.content()

    # 查看频道列表 — button or link (new tab or in-page)
    for name in ("查看频道列表", "频道列表"):
        loc = page.get_by_text(name, exact=False)
        if loc.count() == 0:
            continue
        try:
            with page.expect_popup(timeout=8000) as pop:
                loc.first.click()
            newp = pop.value
            newp.wait_for_load_state("domcontentloaded", timeout=args.timeout_ms)
            html = newp.content()
            newp.close()
            break
        except Exception:
            try:
                loc.first.click()
                page.wait_for_timeout(2500)
                html = page.content()
                break
            except Exception:
                continue

    m3u_url = _find_m3u_download_url(html, page.url)
    pairs: list[tuple[str, str]] | None = None

    if m3u_url:
        try:
            r = context.request.get(m3u_url, timeout=args.timeout_ms)
            if r.ok:
                raw = r.text()
                pairs = _parse_m3u_text(raw)
        except Exception:
            pairs = None

    if not pairs:
        anchor_pairs = _extract_channel_pairs_from_html(html, page.url)
        if anchor_pairs:
            if any(
                _probe_m3u8(context, url, args.probe_timeout_ms)
                for _, url in anchor_pairs[: args.test_top_n]
            ):
                pairs = anchor_pairs

    if not pairs:
        m3u8s = _collect_m3u8_hrefs(html, page.url)
        tested: list[str] = []
        for u in m3u8s[: args.test_top_n]:
            if _probe_m3u8(context, u, args.probe_timeout_ms):
                tested.append(u)
                break
        if not tested:
            print(f"[skip] {region_zh}: no playable m3u8 in first {args.test_top_n}", file=sys.stderr)
            return None
        u = tested[0]
        path = urlparse(u).path.split("/")[-1].split("?")[0] or "live"
        ch = path.replace(".m3u8", "") or "live"
        pairs = [(ch, u)]

    return _rewrite_m3u(region_zh, pairs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output-dir",
        default=os.environ.get("GITHUB_WORKSPACE", "."),
        help="Directory to write *4K.m3u files",
    )
    ap.add_argument("--timeout-ms", type=int, default=120000)
    ap.add_argument("--probe-timeout-ms", type=int, default=15000)
    ap.add_argument("--per-page", type=int, default=10, help="Rows per page (site select limit)")
    ap.add_argument("--test-top-n", type=int, default=8, help="How many m3u8 URLs to probe")
    ap.add_argument("--regions", default="", help="Comma province codes, e.g. hb,sc (default: all)")
    args = ap.parse_args()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    want = {x.strip().lower() for x in args.regions.split(",") if x.strip()}
    regions = [(c, z, s) for c, z, s in REGIONS if not want or c in want]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        for code, zh, slug in regions:
            path = out_dir / f"{slug}4K.m3u"
            try:
                text = process_region(page, context, code, zh, slug, args)
                if text:
                    path.write_text(text, encoding="utf-8")
                    print(f"[ok] {path.name} ({zh})")
                time.sleep(0.8)
            except Exception as e:
                print(f"[err] {zh}: {e}", file=sys.stderr)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
