#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小车（曼波语音小车）指令解析：企微文本 → hub/command/car 的 {cmd}。

三条链路互不干扰（各自独立文件 + 独立主题）：
  command.py      → hub/command/smalltv   {action, value}    C3 小电视
  p4_command.py   → hub/command/p4        {v, id, cmd, arg}  P4 控制面板
  car_command.py  → hub/command/car       {v, id, cmd}       曼波小车（本文件）

★ 2026-10-30 用户定：**只做最小验证**，仅四类指令 ——
   露一手（转 3 圈 + EG.mp3 一遍）/ 转一圈 / 我要听歌 / 停止·停下
  前进后退等单点运动**刻意不接入**（用户原话：现在也没打算把这些加入企微控制）。

解析返回 cmd 字符串，无法识别返回 None。
"""
import re

# ★ 机器人显示名可能带空格（P4 那次踩过：名字 "ESP32 P4" 导致 @\S+ 只吃掉 @ESP32）。
#   这里同样采用「从 @ 起剥到能识别出指令为止」的策略，见 _strip_at()。
_AT_RE = re.compile(r"[@＠]\S+")


def _normalize(text):
    """归一化：全角转半角空白、去零宽字符、小写、去首尾空白。"""
    if not text:
        return ""
    t = text.replace("\u200b", "").replace("\ufeff", "")
    t = t.replace("\u3000", " ")
    t = t.strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def _strip_mentions(text):
    """把开头的 @xxx 逐步剥掉（机器人名可能含空格）。"""
    t = text.strip()
    for _ in range(4):
        m = re.match(r"^[@＠]\S+\s*", t)
        if not m:
            break
        t = t[m.end():].strip()
    return t


# 关键词 → cmd。顺序有意义：停止类最优先（保命）。
_RULES = [
    # ---- 停止（最高优先级，任何含这两个词的都当停止） ----
    (("停止", "停下", "停车", "急停", "别动", "stop"), "stop"),
    # ---- 露一手 ----
    (("露一手", "露个手", "表演", "炫技", "showoff"), "showoff"),
    # ---- 转一圈 ----
    (("转一圈", "转个圈", "转圈圈", "转圈", "spin"), "spin"),
    # ---- 听歌 ----
    (("我要听歌", "听歌", "放歌", "播放歌曲", "music"), "music"),
    # ---- 帮助 ----
    (("帮助", "help", "指令", "说明", "菜单"), "help"),
]

# 精确匹配（整句相等）。「停止」这类短词已在 _RULES 里覆盖，
# 但要防「停止音乐」之类未来扩展时被误当成全停，预留此表。
_EXACT = {}


def parse_command(text):
    """企微文本 → cmd 字符串（stop/showoff/spin/music/help）或 None。"""
    t = _normalize(text)
    if not t:
        return None
    t = _strip_mentions(t)
    if not t:
        return None

    if t in _EXACT:
        return _EXACT[t]

    for keywords, cmd in _RULES:
        for kw in keywords:
            if kw in t:
                return cmd
    return None


HELP_TEXT = ("小车可用指令：\n"
             "- 露一手（原地转 3 圈 + 播放 EG 音乐）\n"
             "- 转一圈\n"
             "- 我要听歌\n"
             "- 停止 / 停下\n"
             "- 帮助")


# 指令 → 中文名（回显用）
CMD_NAMES = {
    "stop": "停止",
    "showoff": "露一手",
    "spin": "转一圈",
    "music": "我要听歌",
    "help": "帮助",
}


def describe_ack(payload):
    """把 ack 报文转成一句人话（给企微回显）。"""
    action = payload.get("action") or payload.get("cmd") or ""
    name = CMD_NAMES.get(action, action)
    status = payload.get("status", "")
    ok = payload.get("ok", True)
    text = payload.get("text") or ""

    if status == "error" or ok is False:
        err = payload.get("error", "")
        if err == "unknown_cmd":
            return "小车没听懂这条指令。\n" + HELP_TEXT
        return "小车执行失败：%s" % (text or err or "未知错误")

    if text:
        return text

    if action == "stop":
        return "已停止"
    if action == "showoff":
        return "露一手完成（转 3 圈 + EG 音乐）"
    if action == "spin":
        return "转一圈完成"
    if action == "music":
        return "开始播放歌库"
    return name or "完成"


if __name__ == "__main__":
    # 自测
    cases = [
        "露一手", "@小车 露一手", "转一圈", "转个圈", "我要听歌",
        "停止", "停下", "@ESP32 小车 停止", "帮助", "前进", "",
    ]
    for c in cases:
        print("%-20r -> %s" % (c, parse_command(c)))
