# -*- coding: utf-8 -*-
"""定时快照推送：中国石油现价 + 杭州天气。

**为什么需要它（而不是靠 agent 的 cron）：**
    agent 的 cron 是「到点叫醒 agent，agent 再调工具转述」。一旦模型侧超时/空回复，
    这条推送就没了 —— 用户要的是**确定的投递**。

    更关键的是节奏：`monitor` 是 60 秒一轮（`poll_interval_trading`），agent 的 cron
    最快也就分钟级。而涨跌停是**秒级事件** —— 从涨停到开板可能就几十秒，
    靠 agent 轮询抓不住。

    ⇒ 把它做成和 `monitor` **同一个容器、同一个 MQTT 连接**里的一个线程：
      · 60 秒 tick 由 monitor 主循环驱动（它已经在那儿轮询了）
      · 快照到点就发，不依赖任何外部唤醒
      · 涨跌停告警走 monitor 原生路径（`hub/alert/stock/*` → smalltv bot → 企微）

**只发一次**：用 SQLite `config` 表记 `last_sent_date:<key>`，
跨进程重启也不会重复发。
"""
import sqlite3
import time
from datetime import datetime

from core import datasource as ds

# ---------------- 推送计划（改这里就行） ----------------
# 每项：(key, 触发时刻 HH:MM, 构建函数名)
#   星期一到星期五有效（周末不推 —— 不是交易日）
PLAN = [
    ("oil_price", "09:30", "build_oil_price"),
    ("hangzhou_weather", "09:30", "build_hangzhou_weather"),
]

WEEKDAYS_ONLY = True
# 杭州（中国天气网城市代码）
HANGZHOU_CODE = "101210101"
# 中国石油
OIL_SYMBOL = "sh601857"
OIL_LABEL = "中国石油"


# ---------------- 已发记录 ----------------
def _conn(db_path):
    c = sqlite3.connect(db_path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def already_sent(db_path, key, day):
    with _conn(db_path) as c:
        row = c.execute("SELECT value FROM config WHERE key=?",
                        (f"last_sent_date:{key}",)).fetchone()
    return bool(row) and row["value"] == day


def mark_sent(db_path, key, day):
    with _conn(db_path) as c:
        c.execute("""INSERT INTO config(key,value,updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                  (f"last_sent_date:{key}", day, int(time.time())))


# ---------------- 内容构建 ----------------
def _fmt(v, suffix="", digits=2):
    if v is None:
        return "（数据缺失）"
    try:
        s = f"{float(v):.{digits}f}".rstrip("0")
        if s.endswith("."):
            s += "0"
        return s + suffix
    except (TypeError, ValueError):
        return str(v)


def build_oil_price():
    """中国石油现价。返回 (title, markdown) 或 (None, None)。"""
    q = ds.fetch_quote(OIL_SYMBOL)
    if not q or q.get("price") is None:
        return None, None
    price = q["price"]
    pct = q.get("pct")
    prev = q.get("prev")
    sign = "+" if (pct is not None and float(pct) >= 0) else ""
    ts = datetime.now().strftime("%H:%M")
    lines = [
        f"**📈 {OIL_LABEL}（601857）现价**",
        f"> 现价：¥{_fmt(price)}",
        f"> 昨收：¥{_fmt(prev)}",
        f"> 涨跌：{sign}{_fmt(pct, '%')}",
        f"> 时间：{ts}",
    ]
    return "oil_price", "\n".join(lines)


def build_hangzhou_weather():
    """杭州天气。返回 (title, markdown) 或 (None, None)。"""
    w = ds.fetch_weather(HANGZHOU_CODE)
    if not w:
        return None, None
    raw = w.get("raw") or {}
    lines = [
        "**🌤️ 杭州天气**",
        f"> 天气：{w.get('weather') or '（未知）'}",
        f"> 气温：{_fmt(w.get('temp'), '℃', digits=1)}",
        f"> 湿度：{w.get('humi', '（未知）')}%",
        f"> 气压：{w.get('press', '（未知）')} hPa",
        f"> 空气质量：AQI {w.get('aqi', '（未知）')}",
    ]
    wind_dir, wind_scale = raw.get("WD"), raw.get("WS")
    if wind_dir or wind_scale:
        lines.append(f"> 风：{wind_dir or ''} {wind_scale or ''}".rstrip())
    if raw.get("limitnumber"):
        lines.append(f"> 今日限行：{raw['limitnumber']}")
    lines.append(f"> 时间：{datetime.now().strftime('%H:%M')}")
    return "hangzhou_weather", "\n".join(lines)


BUILDERS = {
    "build_oil_price": build_oil_price,
    "build_hangzhou_weather": build_hangzhou_weather,
}


# ---------------- 调度 ----------------
# 补发窗口（分钟）：到点后若因重启/断网/取数失败错过了那一分钟，
# 在这么多分钟内仍允许补发。
#   · 太长 → 10:30 突然打一条「9:30 的行情」，误导
#   · 太短（0）→ 进程恰好在那分钟重启，当天就彻底没了
# 3 分钟够容错一次重启，又不至于把旧数据当新数据推。
GRACE_MIN = 3

# 已发记录（SQLite config 表，键前缀）
_SENT_KEY = "last_sent_date:{key}"


def due_keys(now=None):
    """返回此刻应当发送的 (key, builder_name) 列表。

    判据：当前时间在 [计划时刻, 计划时刻 + GRACE_MIN 分钟] 窗口内，
    且今天还没发过。

    ★ 为什么要窗口而不是「HH:MM 精确相等」（2026-11-25）：
      精确相等要求主循环刚好在那一分钟里跑一轮。交易时段 10 秒一轮没问题，
      但只要那一刻正好赶上进程重启 / mqtt 重连 / 系统卡顿，整天就漏了。
      窗口 + 已发标记的组合：宁可晚 1~2 分钟，也不要整天不发。
    """
    now = now or datetime.now()
    if WEEKDAYS_ONLY and now.weekday() >= 5:
        return []
    out = []
    for key, hhmm, builder in PLAN:
        try:
            h, mi = (int(x) for x in hhmm.split(":"))
        except (ValueError, AttributeError):
            continue
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        delta_min = (now - target).total_seconds() / 60.0
        if 0 <= delta_min <= GRACE_MIN:
            out.append((key, builder))
    return out


def run_snapshots(db_path, log, now=None):
    """由 monitor 主循环每轮调用。返回本次实际发出的条数。

    ⚠️ 失败不记已发 —— 下一轮（60 秒后）会重试，直到当天该分钟过去。
       但 HH:MM 只匹配一分钟，所以「同一时刻的重试」实际不存在：
       取数失败就是这一分钟没发出去，下一天再来。
       这是有意的：宁可缺一条，也不要在 10:31 补发一条「9:30 的数据」误导人。
    """
    sent = 0
    for key, builder_name in due_keys(now):
        day = (now or datetime.now()).strftime("%Y-%m-%d")
        if already_sent(db_path, key, day):
            continue
        builder = BUILDERS.get(builder_name)
        if builder is None:
            log(f"[snapshot] 未知构建器 {builder_name}，跳过")
            continue
        try:
            title, content = builder()
        except Exception as e:
            log(f"[snapshot] {key} 构建失败: {e}")
            continue
        if not content:
            log(f"[snapshot] {key} 取数失败，本轮不发（不补发）")
            continue
        # 走 MQTT 告警通道，由 smalltv bot 渲染推送 —— 复用已验证的通路
        if publish_snapshot(key, content):
            mark_sent(db_path, key, day)
            sent += 1
            log(f"[snapshot] {key} 已推送")
        else:
            log(f"[snapshot] {key} 发布失败")
    return sent


# 由 monitor 注入真实的 publish 函数（这样它就用同一条连接）
_PUBLISH = None


def bind_publish(fn):
    global _PUBLISH
    _PUBLISH = fn


def publish_snapshot(key, content):
    if _PUBLISH is None:
        return False
    return bool(_PUBLISH(key, content))
