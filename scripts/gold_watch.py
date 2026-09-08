#!/usr/bin/env python3
"""黄金盯盘 + Bark 推送（全免费）
每5分钟被 GitHub Actions 调用一次：
  1. 拉国内上金所基准（东方财富：Au99.99 日盘 / Au(T+D) 夜盘，按北京时间自动切换）
  2. 基准拿不到时降级为 国际XAU/31.1034768*汇率*系数
  3. 积存估算 = 基准 + 银行加点(offset)
  4. 穿越盯盘线就推 Bark 到 iPhone（穿越触发＋15分钟防抖），状态写回 gold-push-state.json
环境变量：
  BARK_KEY     Bark 的 device key（必填，放 GitHub Secrets）
  BARK_URL     自建 bark server 则填，否则默认 https://api.day.app
  TH_HIGH      默认 961（兼容老用法；设了 LEVELS 则以 LEVELS 为准）
  TH_LOW       默认 935（同上）
  LEVELS       多级盯盘，格式“价格:方向:文案”用英文分号隔开，方向 high=涨到 / low=跌破
               默认 "965:high:到965清仓线，剩下的全走，回APP确认1秒价;961:high:到961按计划卖25g;935:low:破935止损纪律，半天站不回走25g;955:low:跌破955走弱提醒，935破了再按纪律走"
  FLAP_MIN     防抖分钟数，默认 15：在线上来回抖，15分钟内只推一次（兼容 COOLDOWN_MIN 老参数）
  BANK_OFFSET  银行加点 = 银行APP卖出价 - 上金所基准（默认 0，看板校准后把值抄过来）
  FACTOR       降级公式系数（默认 1）
"""
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

G = 31.1034768
ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "gold-push-state.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) gold-watch/1.0"}


def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_sge(secid):
    """东方财富报价，返回元/克。f43 现价(分->元需/100)。"""
    url = (
        "https://push2delay.eastmoney.com/api/qt/stock/get?secid=" + secid
        + "&fields=f43,f44,f45,f46,f57,f58,f60,f86"
    )
    try:
        j = http_json(url)
    except Exception as e:
        print(f"SGE {secid} fail: {e}", flush=True)
        return None
    d = (j or {}).get("data") or {}
    try:
        p = float(d.get("f43") or 0) / 100
    except Exception:
        return None
    if not (0 < p < 100000):
        return None
    return p


def fetch_xau():
    try:
        j = http_json("https://api.gold-api.com/price/XAU")
        p = float((j or {}).get("price") or 0)
        if p > 0:
            return p, "gold-api.com"
    except Exception as e:
        print(f"XAU gold-api fail: {e}", flush=True)
    try:
        j = http_json("https://data-asg.goldprice.org/dbXRates/USD")
        items = (j or {}).get("items") or []
        u = next((x for x in items if x.get("curr") == "USD"), {})
        p = float(u.get("xauPrice") or 0)
        if p > 0:
            return p, "goldprice.org"
    except Exception as e:
        print(f"XAU fallback fail: {e}", flush=True)
    return None, None


def fetch_fx():
    try:
        j = http_json("https://open.er-api.com/v6/latest/USD")
        r = float(((j or {}).get("rates") or {}).get("CNY") or 0)
        return r if r > 0 else None
    except Exception as e:
        print(f"FX fail: {e}", flush=True)
        return None


def beijing_hour():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).hour


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"high_at": 0, "low_at": 0}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2) + "\n")


DEFAULT_LEVELS = (
    "965:high:到965清仓线，剩下的全走，回APP确认1秒价;"
    "963:high:到963接近清仓线，准备走剩下的;"
    "960:high:到960总体回本附近，按计划卖25g;"
    "957:high:回到957成本线，解套一半可先走25g;"
    "955:low:跌破955走弱提醒，935破了再按纪律走;"
    "953:low:跌破953继续走弱，盯住935;"
    "949:low:跌破949离935一步之遥，管住手等确认;"
    "935:low:破935止损纪律，半天站不回走25g"
)


def parse_levels():
    """返回 [(价格, 方向, 文案)]。优先 LEVELS，否则用 TH_HIGH/TH_LOW 拼两档（兼容老配置）。"""
    raw = os.getenv("LEVELS", "").strip()
    if not raw and (os.getenv("TH_HIGH") or os.getenv("TH_LOW")):
        th_high = os.getenv("TH_HIGH", "961").strip() or "961"
        th_low = os.getenv("TH_LOW", "935").strip() or "935"
        raw = (
            f"{th_high}:high:按计划卖25g，回APP确认1秒价;"
            f"{th_low}:low:半天站不回走25g"
        )
    if not raw:
        raw = DEFAULT_LEVELS
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        lv_s, _, rest = part.partition(":")
        direction, _, msg = rest.partition(":")
        try:
            lv = float(lv_s.strip())
        except ValueError:
            print(f"LEVELS 跳过非法项：{part}", flush=True)
            continue
        direction = direction.strip().lower()
        if direction not in ("high", "low") or not msg.strip():
            print(f"LEVELS 跳过非法项：{part}", flush=True)
            continue
        out.append((lv, direction, msg.strip()))
    return out


def migrate_state(st):
    """老 state 只有 high_at/low_at，搬到按档位的 levels 里，保留冷却语义。"""
    lv = st.setdefault("levels", {})
    if "high_at" in st:
        st["_old_high_at"] = st.pop("high_at")
    if "low_at" in st:
        st["_old_low_at"] = st.pop("low_at")
    return st


def lvl_entry(st, direction, level):
    """取某档的状态 {t: 上次推送时间, side: 上轮位置 hit/out/None}，兼容老 state。"""
    e = st.get("levels", {}).get(f"{direction[0]}:{level:g}") or {}
    t = e.get("t", 0)
    if not t:
        t = st.get("_old_high_at" if direction == "high" else "_old_low_at", 0)
    return float(t or 0), e.get("side")


def set_lvl(st, direction, level, t, side):
    st.setdefault("levels", {})[f"{direction[0]}:{level:g}"] = {"t": t, "side": side}


def breached(direction, bank, level):
    return bank >= level if direction == "high" else bank <= level


def decide_fire(st, bank, base, src, levels, now, flap):
    """穿越触发：只有“从线内穿到线外”才算新事件；在在线上待着不重复推。
    每轮每方向只推最极端的一档。返回 (fire, st)，fire 为 None 或
    (direction, level, title, body)。side 每次都更新，t 只在真推送时更新。"""
    fire = None
    for direction in ("high", "low"):
        ordered = sorted([lv for lv in levels if lv[1] == direction],
                         key=lambda x: x[0], reverse=(direction == "high"))
        cands = []
        for lv, _, msg in ordered:
            hit = breached(direction, bank, lv)
            side_now = "hit" if hit else "out"
            t_last, side_prev = lvl_entry(st, direction, lv)
            fresh = hit and side_prev != "hit"  # 含首次见到的已穿线（side None）
            set_lvl(st, direction, lv, t_last, side_now)
            if fresh:
                if now - t_last > flap:
                    cands.append((lv, msg))
                else:
                    print(f"{direction} {lv:g} 刚穿但在防抖内，吞掉", flush=True)
        if cands and fire is None:
            # 同方向穿多档只推最极端：high 取最高，low 取最低
            lv, msg = (max if direction == "high" else min)(cands, key=lambda x: x[0])
            verb = "到线" if direction == "high" else "破线"
            fire = (direction, lv, f"黄金{verb} {lv:g}",
                    f"积存估算¥{bank:.1f}（基准{base:.1f}·{src}），{msg}")
    return fire, st


def bark_push(base, key, title, body, group="gold-duo"):
    q = urllib.parse.quote
    url = f"{base.rstrip('/')}/{key}/{q(title)}/{q(body)}?group={q(group)}&sound=minuet"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        print("bark:", r.status, r.read().decode("utf-8", "ignore")[:200], flush=True)


def env_float(name, default):
    """Actions 传空变量会变成空字符串，这里一律按缺失处理，防 ValueError 炸整轮。"""
    try:
        return float((os.getenv(name) or "").strip())
    except ValueError:
        return default


def main():
    flap = env_float("FLAP_MIN", env_float("COOLDOWN_MIN", 15)) * 60
    offset = env_float("BANK_OFFSET", 0)
    factor = env_float("FACTOR", 1)
    bark_key = os.getenv("BARK_KEY", "").strip()
    bark_url = os.getenv("BARK_URL", "https://api.day.app").strip()
    dry = os.getenv("DRY_RUN", "") == "1"
    levels = parse_levels()
    if not levels:
        print("没有可用盯盘档位，退出", flush=True)
        return 2

    now = time.time()
    h = beijing_hour()
    night = (h >= 20 or h < 2) or (2 <= h < 9) or (15 <= h < 20)

    sge_day = fetch_sge("118.AU9999")
    sge_td = fetch_sge("118.AUTD")
    if night or (sge_day is None):
        base, src = (sge_td, "Au(T+D)") if sge_td else (sge_day, "Au99.99(休)"), None
        base, src = base[0], base[1]
    else:
        base, src = sge_day, "Au99.99日盘"
    if base is None:
        print("SGE 全失败，走国际降级公式", flush=True)
        xau, xs = fetch_xau()
        fx = fetch_fx()
        if not xau or not fx:
            print("所有源都失败，本轮跳过", flush=True)
            return 2
        base, src = xau / G * fx * factor, f"XAU降级({xs})"

    bank = base + offset
    highs = sorted([lv for lv in levels if lv[1] == "high"])
    lows = sorted([lv for lv in levels if lv[1] == "low"])
    print(f"base={base:.2f}({src}) offset={offset:+.2f} bank={bank:.2f} bj_h={h}", flush=True)
    print("档位 high=" + ",".join(f"{lv:g}" for lv, _, _ in highs)
          + " low=" + ",".join(f"{lv:g}" for lv, _, _ in lows), flush=True)

    st = migrate_state(load_state())
    fire, st = decide_fire(st, bank, base, src, levels, now, flap)

    if not fire:
        if not dry and bark_key:
            save_state(st)  # side 变化落盘，下轮才能判断穿越；dry 绝不写盘
        print("无新穿越，不推送", flush=True)
        return 0
    direction, lv, title, body = fire
    if dry or not bark_key:
        print(f"DRY/无KEY不真推：{title} | {body}", flush=True)
        if not bark_key:
            print("注意：BARK_KEY 为空，去仓库 Secrets 里添加（重装Bark App会换key，旧key会失效）", flush=True)
        return 0
    try:
        bark_push(bark_url, bark_key, title, body)
    except Exception as e:
        print(f"Bark推送失败：{e}", flush=True)
        print("排查：1) Secrets 里 BARK_KEY 是否最新key（重装App后旧key作废）；"
              "2) 测试地址浏览器能通否：https://api.day.app/<你的key>/测试/通否", flush=True)
        return 1
    set_lvl(st, direction, lv, now, "hit")
    save_state(st)
    print("已推送并更新状态", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
