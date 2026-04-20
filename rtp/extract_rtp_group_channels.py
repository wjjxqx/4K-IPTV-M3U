import base64
import json
import os
import re
from html import unescape

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://blog.cqshushu.com/multicast-iptv"
TEMPLATE_DIR = "rtp"
PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆",
]


def clean_text(raw: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", raw)).replace("\xa0", " ").strip()


def encrypt_token(token: str) -> str:
    cipher = AES.new(b"cQshuShu88888888", AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(token.encode("utf-8"), AES.block_size))).decode("utf-8")


def parse_ajax_cfg(home_html: str) -> dict:
    m = re.search(r"var\s+multicastIptvAjax\s*=\s*(\{.*?\});", home_html, flags=re.S)
    if not m:
        raise RuntimeError("未找到 multicastIptvAjax 配置")
    return json.loads(m.group(1))


def parse_region_code_map(home_html: str) -> dict:
    sm = re.search(r'<select\s+name="region"[^>]*>(.*?)</select>', home_html, flags=re.S)
    if not sm:
        raise RuntimeError("未找到 region 下拉框")
    mapping = {}
    for code, name_html in re.findall(r'<option\s+value="([^"]*)"\s*[^>]*>(.*?)</option>', sm.group(1), flags=re.S):
        if not code.strip():
            continue
        mapping[clean_text(name_html)] = code.strip()
    return mapping


def parse_list_rows(fragment_html: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", fragment_html, flags=re.S | re.I):
        ip_anchor = re.search(
            r'<a[^>]*class="[^"]*ip-link[^"]*"[^>]*data-p="([^"]+)"[^>]*>\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+)\s*</a>',
            tr,
            flags=re.S | re.I,
        )
        if not ip_anchor:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
        if len(tds) < 6:
            continue
        rows.append(
            {
                "p_token": ip_anchor.group(1),
                "ip_port": ip_anchor.group(2),
                "status": clean_text(tds[5]),
            }
        )
    return rows


def choose_one_ip(rows: list[dict]) -> dict | None:
    for row in rows:
        if "新上线" in row["status"]:
            return row
    for row in rows:
        if "存活" in row["status"]:
            return row
    return None


def parse_s_token(detail_html: str) -> str | None:
    m = re.search(r'data-s="([^"]+)"', detail_html, flags=re.S | re.I)
    if m:
        return m.group(1)
    m = re.search(r'href="[^"]*[?&]s=([^"&]+)[^"]*"', detail_html, flags=re.S | re.I)
    if m:
        return m.group(1)
    return None


def parse_channel_lines(channel_html: str) -> list[str]:
    lines = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", channel_html, flags=re.S | re.I):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
        if len(tds) < 3:
            continue
        name = clean_text(tds[1])
        play_url = clean_text(tds[2])
        if not name or not play_url:
            continue
        m = re.search(r"/(udp|rtp|igmp)/(\d+\.\d+\.\d+\.\d+:\d+)", play_url, flags=re.I)
        if not m:
            continue
        lines.append(f"{name},{m.group(1).lower()}/{m.group(2)}")
    return lines


def detect_province(filename: str) -> str | None:
    for p in PROVINCES:
        if p in filename:
            return p
    return None


def fetch_channels_for_province(session: requests.Session, ajax_cfg: dict, region_code_map: dict, province: str) -> tuple[list[str], str]:
    region_code = region_code_map.get(province)
    if not region_code:
        return [], "region_code_not_found"

    token_plain = ajax_cfg["token"]
    list_payload = {
        "action": "multicast_iptv_ajax",
        "action_type": "list",
        "page_num": 1,
        "limit": 20,
        "region": region_code,
        "search": "",
        "nonce": ajax_cfg["nonce"],
        "token": encrypt_token(token_plain),
    }
    list_json = session.post(ajax_cfg["ajaxUrl"], data=list_payload, timeout=20).json()
    list_html = list_json.get("data", {}).get("html", "")
    if not list_html:
        return [], "list_empty"

    picked = choose_one_ip(parse_list_rows(list_html))
    if not picked:
        return [], "no_new_or_alive"

    detail_payload = {
        "action": "multicast_iptv_ajax",
        "action_type": "detail",
        "p": picked["p_token"],
        "nonce": ajax_cfg["nonce"],
        "token": encrypt_token(token_plain),
    }
    detail_json = session.post(ajax_cfg["ajaxUrl"], data=detail_payload, timeout=20).json()
    detail_html = detail_json.get("data", {}).get("html", "")
    if not detail_html:
        return [], "detail_empty"
    token_plain = detail_json.get("data", {}).get("new_token", token_plain)

    s_token = parse_s_token(detail_html)
    if not s_token:
        return [], "s_token_not_found"

    channels_payload = {
        "action": "multicast_iptv_ajax",
        "action_type": "channels",
        "s": s_token,
        "nonce": ajax_cfg["nonce"],
        "token": encrypt_token(token_plain),
    }
    channels_json = session.post(ajax_cfg["ajaxUrl"], data=channels_payload, timeout=20).json()
    channels_html = channels_json.get("data", {}).get("html", "")
    if not channels_html:
        return [], "channels_empty"
    lines = parse_channel_lines(channels_html)
    if not lines:
        return [], "channel_lines_empty"
    return lines, "ok"


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_dir = os.path.join(script_dir, TEMPLATE_DIR)
    if not os.path.isdir(template_dir):
        raise RuntimeError(f"模板目录不存在: {template_dir}")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    home_html = session.get(BASE_URL, timeout=20).text
    ajax_cfg = parse_ajax_cfg(home_html)
    region_code_map = parse_region_code_map(home_html)

    template_files = [f for f in os.listdir(template_dir) if f.endswith(".txt")]
    if not template_files:
        print("未找到待更新 txt 文件。")
        return

    summary = {"ok": 0, "failed": 0}
    for filename in template_files:
        province = detect_province(filename)
        out_path = os.path.join(template_dir, filename)

        # 先清空，再重新写入（按需求）
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("")

        if not province:
            print(f"[skip] {filename}: 未识别省份")
            continue

        try:
            lines, status = fetch_channels_for_province(session, ajax_cfg, region_code_map, province)
            if lines:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                summary["ok"] += 1
                print(f"[ok] {filename}: {len(lines)} lines")
            else:
                summary["failed"] += 1
                print(f"[fail] {filename}: {status}")
        except Exception as exc:
            summary["failed"] += 1
            print(f"[fail] {filename}: exception={exc}")

    print(f"done ok={summary['ok']} failed={summary['failed']}")


if __name__ == "__main__":
    main()
