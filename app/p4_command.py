#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 设备指令解析（企微固定指令 → hub/command/p4 的 {cmd,arg}）。

与 command.py（C3 小电视）完全独立、互不干扰：
  - command.py  → hub/command/smalltv   {action, value}
  - p4_command  → hub/command/p4        {v, id, cmd, arg?}

固件命令表（Windows 侧确认，2026-10-28）：
  {"cmd":"music"}                打开音乐 + 播第 1 首
  {"cmd":"music","arg":"open"}   只打开，不出声
  {"cmd":"music","arg":"close"}  关掉，音乐停，回桌面
  {"cmd":"aichat"}               打开小智
  {"cmd":"aichat","arg":"close"} 关掉小智，回桌面
  {"cmd":"close_all"}            关闭/清理后台，全部关掉（2026-10-28 Windows 侧新增）

arg 只接受 open / close，打错回 bad_arg（不静默无效）。
幂等：app 本就没跑，发 close 也回 ok:true。
判据不靠返回值：发完 close 会回查运行表，还在就回 close_failed。

解析返回 (cmd, arg)，arg 为 None 时表示不带该字段；无法识别返回 (None, None)。
"""

# 帮助文本（与 C3 保持同风格：纯文本、不依赖 markdown 渲染）
HELP_TEXT = ("P4 可用指令：\n"
             "- 播放音乐（打开并播放第 1 首）\n"
             "- 打开音乐（只打开，不出声）\n"
             "- 停止音乐 / 关闭音乐\n"
             "- 打开小智 / 启动小智\n"
             "- 关闭小智 / 退出小智\n"
             "- 关闭 / 清理后台（全部关闭回桌面）\n"
             "- 帮助")

# 关键词 → (cmd, arg)。顺序有意义：先匹配更具体的「关/停」，再匹配「开/放」。
_RULES = [
    # ---- 全局清理 ----
    # ★ 必须排在音乐/小智之前："关闭全部" 里含 "关闭"，若音乐规则先匹配会被误当成关音乐。
    # 只收录**不含歧义**的说法；单说 "关闭" / "关掉" 太容易误伤（"关闭音乐" 也含 "关闭"），
    # 因此「关闭」仅在整句等于它本身时才命中，见下方 _EXACT。
    (("关闭全部", "清理后台", "关闭所有", "全部关闭", "全部关掉", "清理全部",
      "清理一下后台"),
     ("close_all", None)),
    # ---- 音乐 ----
    (("停止音乐", "关闭音乐", "关掉音乐", "停音乐", "关音乐"), ("music", "close")),
    (("音乐停止", "音乐关闭"), ("music", "close")),
    (("打开音乐", "开启音乐", "只打开音乐", "音乐打开"), ("music", "open")),
    # 「播放音乐」= 打开 + 播放第 1 首（固件的默认语义，不能与 open 混淆）
    (("播放音乐", "放音乐", "听音乐", "来首歌", "放首歌"), ("music", None)),
    # ---- 小智 ----
    (("关闭小智", "退出小智", "关掉小智", "关小智", "退小智", "停止小智", "关小智ai"),
     ("aichat", "close")),
    (("打开小智", "启动小智", "开启小智", "开小智", "进入小智", "打开小智ai", "启动小智ai"),
     ("aichat", None)),
]

_HELP_WORDS = ("帮助", "help", "指令", "说明", "怎么用", "菜单")

# 精确匹配表：只在「整句归一后完全等于」时才命中，避免短词误伤长指令。
# 典型场景："关闭" 要对 close_all，但 "关闭音乐" 不能被它抢走。
_EXACT = {
    "关闭": ("close_all", None),
    "关闭。": ("close_all", None),
    "清理": ("close_all", None),
    "关掉": ("close_all", None),
}


def _normalize(text):
    r"""去掉 @机器人 前缀、全角空格、零宽字符，转小写。

    ★ 2026-10-28 踩过的坑：机器人显示名是「ESP32 P4」（含空格），
      而原来的 `re.sub(r"@\S+", " ", text)` 只能吃掉 `@ESP32`，
      会剩下 ` P4 关闭` —— 于是「关闭」这种靠整句精确匹配的指令永远不命中
      （而「清理后台」靠包含匹配所以正常，掩盖了这个 bug）。
    现在的做法：从 `@` 起，持续剥离，直到剩下的文本以某个已知关键词开头为止。
    具体分两步：先按空白拆词，把开头连续那些看起来像机器人名的 token 去掉，
    留下的就是真正的指令正文。
    """
    if not text:
        return ""
    t = text.replace("\u3000", " ").replace("\u200b", "").strip()
    if t.startswith("@"):
        # 拆词后，丢掉开头的 @token（可能被空格切成多段，如 "@ESP32 P4 关闭"）
        parts = t.split()
        # 把开头的 @xxx 以及紧跟其后、明显属于名字残余的 token 一起剥掉。
        # 判据：从第 0 个 token 起，只要【剩余部分还不是有效指令】且【当前 token 不像指令开头】
        # 就继续剥。简化实现：找到第一个能命中任意规则/精确表的位置。
        for i in range(len(parts)):
            rest = " ".join(parts[i:]).lower()
            if _looks_like_command(rest):
                t = rest
                break
        else:
            t = ""
    return t.strip().lower()


def _looks_like_command(s):
    """探测：这段文本是不是（已经去干净前缀的）指令正文。"""
    if not s:
        return False
    for w in _HELP_WORDS:
        if w in s:
            return True
    if s.rstrip("。.!！?？ ") in _EXACT:
        return True
    for keywords, _r in _RULES:
        for kw in keywords:
            if kw in s:
                return True
    return False


def parse_command(text):
    """对外入口：群消息 → (cmd, arg) 或 ('help', None) 或 (None, None)。

    刻意「整句包含即命中」，而不是精确等于 —— 用户很可能说
    「帮我播放音乐」「小智打开一下」。但同时要求：
      先查 close 类规则，再查 open/默认类规则（避免「关闭音乐」被「放音乐」抢走）。
    """
    t = _normalize(text)
    if not t:
        return None, None
    for word in _HELP_WORDS:
        if word in t:
            return "help", None
    exact = _EXACT.get(t.rstrip("。.!！?？ "))
    if exact:
        return exact
    for keywords, result in _RULES:
        for kw in keywords:
            if kw in t:
                return result
    return None, None


def describe_ack(payload):
    """ack 报文 → 人类可读回显（企微侧最终呈现）。

    payload 形如：
      {"v":1,"id":"cmd-1","cmd":"music","ok":true}
      {"v":1,"id":"cmd-2","cmd":"aichat","ok":false,"error":"not_installed"}

    error 取值见 docs/P4-PI-AGENT-SPEC.md §11.2。
    """
    cmd = payload.get("cmd", "")
    ok = payload.get("ok")
    err = payload.get("error", "")

    name = {"music": "音乐", "aichat": "小智 AI", "close_all": "全部后台"}.get(
        cmd, cmd or "未知指令")

    if ok:
        if cmd == "close_all":
            return "P4：已关闭全部后台应用，回到桌面"
        return f"P4：{name} 指令已执行"

    # 失败分支 —— 每种都给出「用户下一步该干什么」，而不只是报错原文
    if err == "not_installed":
        return f"P4 上未安装「{name}」"
    if err == "bad_arg":
        return "指令参数有误（Pi 侧映射 bug，已记录日志）"
    if err == "close_failed":
        if cmd == "close_all":
            return "P4 清理后台失败 —— 仍有应用在运行，请重试或到 P4 上手动关闭"
        return f"关闭「{name}」失败 —— 应用仍在运行，请重试或到 P4 上手动关闭"
    if err:
        return f"P4 执行失败：{err}"
    return f"P4 执行「{name}」失败（未返回错误码）"
