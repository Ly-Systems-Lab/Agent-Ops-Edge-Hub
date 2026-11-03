# SPDX-License-Identifier: Apache-2.0
"""配置加载 —— 凭据永远不进仓，只从外部 .env 读。

查找顺序：`--env PATH` > 环境变量 `WINAGENT_ENV` > 当前目录下的 `.env`。
证书类相对路径**相对于 .env 文件所在目录**解析 —— 这样整套凭据可以整体搬走。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

# 与 Pi 侧下发的接入说明保持同名，用户拿到的 .env.local 可以原样使用
KEY_HOST = "WIN_MQTT_HOST"
KEY_PORT = "WIN_MQTT_PORT"
KEY_USER = "WIN_MQTT_USER"
KEY_PASSWORD = "WIN_MQTT_PASSWORD"
KEY_CA = "WIN_MQTT_CA"
KEY_CERT = "WIN_MQTT_CERT"
KEY_KEY = "WIN_MQTT_KEY"

REQUIRED = (KEY_HOST, KEY_USER, KEY_PASSWORD)

DEFAULTS = {
    KEY_PORT: "8883",
    KEY_CA: "ca.crt",
}

# 证书/私钥按账号名推导：Pi 侧签发时就是 `<账号>.crt` / `<账号>.key`。
# 这样 .env 里只填账号与密码即可，少两个容易写错的键。
DEFAULT_CERT = "%s.crt"
DEFAULT_KEY = "%s.key"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    ca: Path
    cert: Path
    key: Path
    env_path: Path

    def describe(self) -> str:
        """给人看的一行摘要。**不含密码**，可以被日志/回执安全引用。"""
        return "%s:%d user=%s cert=%s" % (self.host, self.port, self.user, self.cert.name)


def _parse_env(path: Path) -> dict:
    data = {}
    # utf-8-sig：Windows 记事本存 UTF-8 会带 BOM，别让它污染第一个键名
    with path.open("r", encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ConfigError("%s 第 %d 行不是 KEY=VALUE：%r" % (path, lineno, line))
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def find_env(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError("--env 指定的文件不存在：%s" % p)
        return p.resolve()

    from_env = os.environ.get("WINAGENT_ENV", "").strip()
    if from_env:
        p = Path(from_env).expanduser()
        if not p.is_file():
            raise ConfigError("环境变量 WINAGENT_ENV 指向的文件不存在：%s" % p)
        return p.resolve()

    p = Path.cwd() / ".env"
    if p.is_file():
        return p.resolve()

    raise ConfigError(
        "找不到配置文件。用 --env <路径> 指定，或设置 WINAGENT_ENV，"
        "或在当前目录放一个 .env（样例见 .env.example）"
    )


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load(explicit: str | None = None) -> Config:
    env_path = find_env(explicit)
    data = _parse_env(env_path)
    base = env_path.parent

    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise ConfigError("%s 缺少必填项：%s" % (env_path, ", ".join(missing)))

    try:
        port = int(data.get(KEY_PORT) or DEFAULTS[KEY_PORT])
    except ValueError:
        raise ConfigError("%s 的 %s 不是整数：%r" % (env_path, KEY_PORT, data.get(KEY_PORT)))

    user = data[KEY_USER]
    ca = _resolve(base, data.get(KEY_CA) or DEFAULTS[KEY_CA])
    cert = _resolve(base, data.get(KEY_CERT) or DEFAULT_CERT % user)
    key = _resolve(base, data.get(KEY_KEY) or DEFAULT_KEY % user)

    for label, p in (("CA 证书", ca), ("客户端证书", cert), ("客户端私钥", key)):
        if not p.is_file():
            raise ConfigError("%s 不存在：%s" % (label, p))

    return Config(
        host=data[KEY_HOST],
        port=port,
        user=user,
        password=data[KEY_PASSWORD],
        ca=ca,
        cert=cert,
        key=key,
        env_path=env_path,
    )