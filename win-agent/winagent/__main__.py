# SPDX-License-Identifier: Apache-2.0
"""winagent 命令行入口。

    python -m winagent doctor                     # 自检：TLS + 建连 + 一条不动机器的命令
    python -m winagent list                       # 看设备与指令表
    python -m winagent p4 music                   # 打开并播放
    python -m winagent p4 music close             # 关闭音乐
    python -m winagent car help                   # 连通性自检（不动机器）
    python -m winagent --json car spin            # 机器可读输出

退出码：0 成功 / 2 配置 / 3 TLS / 4 连接 / 5 订阅 / 6 超时 / 7 设备执行失败
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, errors
from .config import ConfigError, load
from .link import Result, send, tls_probe

# 每条指令的元组是（说明, 可用 arg, 默认等回执秒数）。
# ⚠️ 默认超时**按指令**给，不是一个全局 8 秒：
#    2026-11-03 实测 car/showoff 的 ack 要 **22.0 s** 才回来（整段动作跑完才回），
#    用 8 秒会必然超时，而且报的是"设备没回"—— 分类就错了。
DEVICES = {
    "p4": {
        "topic": "hub/command/p4",
        "note": "一次性同步回执。ok:true = 命令已执行到位，不等于正在出声",
        "commands": {
            "music": ("打开并播放第 1 首", ["play", "open", "close", "stop"], 8.0),
            "aichat": ("打开小智", ["open", "close", "stop"], 8.0),
            "close_all": ("关闭全部后台（幂等）", [], 8.0),
        },
    },
    "car": {
        "topic": "hub/command/car",
        "note": "只回一条终态 ack，耗时随动作走（showoff 实测 22 s）",
        "commands": {
            "help": ("返回指令表（不动机器）", [], 8.0),
            "spin": ("原地转一圈（实测 2.4 s）", [], 10.0),
            "showoff": ("露一手：转 3 圈 + 播 EG 音乐（实测 22 s）", [], 40.0),
            "music": ("播放歌库（实测 0.09 s）", [], 8.0),
            "stop": ("停止 / 急停", [], 8.0),
        },
    },
}


# --------------------------------------------------------------------------- 输出

def _emit_json(result: Result) -> None:
    print(json.dumps({
        "ok": result.ok,
        "stage": result.stage,
        "device": result.device,
        "cmd": result.cmd,
        "id": result.id,
        "topic": result.topic,
        "rtt_ms": round(result.rtt_ms, 1),
        "ack": result.ack,
        "interims": result.interims,
        "detail": result.detail,
    }, ensure_ascii=False, indent=2))


def _emit_human(result: Result) -> None:
    head = "✓" if result.ok else "✗"
    if result.ok:
        line = "%s %s %s  %.0f ms" % (head, result.device, result.cmd, result.rtt_ms)
        ack = result.ack or {}
        extra = []
        if "closed" in ack:
            extra.append("关闭了 %s 个 app" % ack["closed"])
        if ack.get("status") and ack["status"] != "done":
            extra.append("status=%s" % ack["status"])
        if extra:
            line += "  (" + "，".join(extra) + ")"
        print(line)
        if ack.get("text"):
            for ln in str(ack["text"]).splitlines():
                print("   " + ln)
        if ack.get("status") not in (None, "done") and not result.interims:
            print("   注：这是中间回执，动作可能仍在执行（加 --wait-final 继续等终态）")
        return

    print("%s %s %s  失败 [%s]" % (head, result.device, result.cmd, result.stage))
    if result.detail:
        print("   %s" % result.detail)
    hint = errors.HINTS.get(result.stage)
    if hint:
        print("   提示：%s" % hint)
    if result.ack:
        print("   ack：%s" % json.dumps(result.ack, ensure_ascii=False))


# --------------------------------------------------------------------------- 子命令

def cmd_list(args) -> int:
    if args.json:
        print(json.dumps({
            "devices": {
                name: {
                    "topic": d["topic"],
                    "note": d["note"],
                    "commands": {c: {"desc": v[0], "args": v[1], "timeout": v[2]}
                                 for c, v in d["commands"].items()},
                }
                for name, d in DEVICES.items()
            }
        }, ensure_ascii=False, indent=2))
        return 0

    for name, d in DEVICES.items():
        print("%s  →  %s" % (name, d["topic"]))
        print("   %s" % d["note"])
        for cmd, (desc, argvs, tmo) in d["commands"].items():
            tail = ("  arg: " + " / ".join(argvs)) if argvs else ""
            print("   %-10s %s%s  [默认等 %gs]" % (cmd, desc, tail, tmo))
        print()
    return 0


def cmd_send(args) -> int:
    device = args.device
    table = DEVICES[device]["commands"]

    if not args.cmd:
        print("用法：winagent %s <cmd> [arg]" % device)
        print()
        print("%s  →  %s" % (device, DEVICES[device]["topic"]))
        for cmd, (desc, argvs, tmo) in table.items():
            tail = ("  arg: " + " / ".join(argvs)) if argvs else ""
            print("  %-10s %s%s  [默认等 %gs]" % (cmd, desc, tail, tmo))
        return 0

    if args.cmd not in table:
        # 不拦 —— 让设备自己回 unknown_cmd，那是一次诚实的往返。
        # 但要在 stderr 提醒：多半是打错字。
        print("⚠ 本地指令表里没有 %r（可用：%s）—— 仍会发出去，设备应回 unknown_cmd"
              % (args.cmd, ", ".join(table)), file=sys.stderr)

    try:
        cfg = load(args.env)
    except ConfigError as e:
        if args.json:
            print(json.dumps({"ok": False, "stage": errors.CONFIG, "detail": str(e)},
                             ensure_ascii=False, indent=2))
        else:
            print("✗ 配置错误：%s" % e)
            print("   提示：%s" % errors.HINTS[errors.CONFIG])
        return errors.EXIT_CODES[errors.CONFIG]

    # 显式给了 --timeout 就用它；否则按该指令的默认值（showoff 40 s，其余 8 s）
    timeout = args.timeout
    if timeout is None:
        timeout = table[args.cmd][2] if args.cmd in table else 8.0

    result = send(
        cfg,
        device=device,
        cmd=args.cmd,
        arg=args.arg,
        timeout=timeout,
        wait_final=args.wait_final,
        final_timeout=args.final_timeout,
        command_id=args.id,
    )

    if args.json:
        _emit_json(result)
    else:
        _emit_human(result)
    return result.exit_code


def cmd_doctor(args) -> int:
    try:
        cfg = load(args.env)
    except ConfigError as e:
        if args.json:
            print(json.dumps({"ok": False, "stage": errors.CONFIG, "detail": str(e)},
                             ensure_ascii=False, indent=2))
        else:
            print("✗ 配置错误：%s" % e)
        return errors.EXIT_CODES[errors.CONFIG]

    report = {"config": cfg.describe(), "tls": None, "probe": None}

    # ---- 1. TLS ----------------------------------------------------------
    try:
        info = tls_probe(cfg)
        report["tls"] = info
        if not args.json:
            print("① TLS")
            print("   版本   : %s" % info["version"])
            print("   套件   : %s" % info["cipher"])
            print("   broker : %s" % info["subject"])
            print("   SAN    : %s" % ", ".join(info["san"]))
            print("   有效期 : %s → %s" % (info["not_before"], info["not_after"]))
            print("   主机名 : ✓ 证书覆盖 %s" % cfg.host)
    except Exception as e:  # noqa: BLE001
        report["tls"] = {"error": "%s: %s" % (type(e).__name__, e)}
        if args.json:
            print(json.dumps({"ok": False, "stage": errors.TLS, **report},
                             ensure_ascii=False, indent=2))
        else:
            print("① TLS  ✗ %s: %s" % (type(e).__name__, e))
            print("   提示：%s" % errors.HINTS[errors.TLS])
        return errors.EXIT_CODES[errors.TLS]

    if args.no_probe:
        if args.json:
            print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
        else:
            print("\n② 命令往返  已跳过（--no-probe）")
        return 0

    # ---- 2. 一条不动机器的命令 -------------------------------------------
    result = send(cfg, device="car", cmd="help", timeout=args.timeout)
    report["probe"] = {
        "ok": result.ok, "stage": result.stage, "rtt_ms": round(result.rtt_ms, 1),
        "detail": result.detail, "ack": result.ack,
    }
    if args.json:
        print(json.dumps({"ok": result.ok, **report}, ensure_ascii=False, indent=2))
    else:
        print("\n② 命令往返（car/help —— 不动机器）")
        if result.ok:
            print("   ✓ %.0f ms" % result.rtt_ms)
            if result.ack and result.ack.get("text"):
                for ln in str(result.ack["text"]).splitlines()[:6]:
                    print("     " + ln)
        else:
            print("   ✗ [%s] %s" % (result.stage, result.detail))
            hint = errors.HINTS.get(result.stage)
            if hint:
                print("     提示：%s" % hint)
    return result.exit_code


# --------------------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    # 顶层与子命令都认 --env / --json；子命令里用 SUPPRESS，
    # 否则子解析器的默认值会把顶层已解析的值覆盖掉。
    top = argparse.ArgumentParser(
        prog="winagent",
        description="以「指令源」身份接入 Pi Hub MQTT 总线（mTLS）",
        epilog="退出码：0 成功 / 2 配置 / 3 TLS / 4 连接 / 5 订阅 / 6 超时 / 7 设备执行失败",
    )
    top.add_argument("--version", action="version", version="winagent %s" % __version__)
    top.add_argument("--env", metavar="PATH", help="凭据文件路径（默认 $WINAGENT_ENV 或 ./.env）")
    top.add_argument("--json", action="store_true", help="输出机器可读的 JSON")

    sub = top.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--env", metavar="PATH", default=argparse.SUPPRESS,
                       help="凭据文件路径")
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="输出机器可读的 JSON")
        return p

    common(sub.add_parser("list", help="列出设备与指令表")).set_defaults(func=cmd_list)

    p_doctor = common(sub.add_parser("doctor", help="自检：TLS + 建连 + 不动机器的往返"))
    p_doctor.add_argument("--no-probe", action="store_true", help="只做 TLS 自检，不发命令")
    p_doctor.add_argument("--timeout", type=float, default=8.0)
    p_doctor.set_defaults(func=cmd_doctor)

    for name in DEVICES:
        p = common(sub.add_parser(name, help="给 %s 发指令" % name))
        p.add_argument("cmd", nargs="?", help="指令名（省略则显示该设备的指令表）")
        p.add_argument("arg", nargs="?", help="可选参数，如 close / open")
        p.add_argument("--timeout", type=float, default=None,
                       help="等回执的秒数（默认按指令取：showoff 40 s，其余 8 s）")
        p.add_argument("--wait-final", action="store_true",
                       help="预留：若有中间回执则继续等终态。"
                            "当前车侧在 MQTT 上只回一条终态 ack，等于没作用")
        p.add_argument("--final-timeout", type=float, default=40.0,
                       help="等终态的总秒数（默认 40）")
        p.add_argument("--id", dest="id", default=None, help="自定义命令 id（默认自动生成）")
        p.set_defaults(func=cmd_send, device=name)

    return top


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())