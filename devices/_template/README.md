# 设备插件模板

> **这里是「接一台新设备」的全部起点。** 复制、填六处、起服务 —— **核心一行不改**。
> 契约见 [`docs/DEVICE-PLUGIN.md`](../../docs/DEVICE-PLUGIN.md)。

## 复制

```bash
cp -a devices/_template devices/<name>          # 例：devices/demo
sed -i 's/<name>/demo/g' devices/<name>/bot.py  # 同时改掉 DEVICE 与文档里的占位符
mv devices/<name>/systemd/pihub-NAME.service devices/<name>/systemd/pihub-<name>.service
```

## 填六处（都在 `bot.py` 里标着 TODO）

| # | 项 | 在哪 |
|---|---|---|
| 1 | 设备名 | `DEVICE`（决定主题、单元名、ACL 用户） |
| 2 | 命令主题 | `CMD_TOPIC` |
| 3 | 回执主题 | `ACK_TOPIC` |
| 4 | 指令解析 | `parse_command(text) -> (cmd, arg)` |
| 5 | 回执渲染 | `describe_ack(payload) -> str` |
| 6 | 执行一步 | `execute(cmd, arg) -> dict` ← **唯一该出现设备协议的地方** |

**六处之外不需要改任何东西。** 需要改第七处，说明边界划错了 —— 先改边界，别在插件里打补丁。

## 还需要两样（不属于代码）

- **ACL**：把 `acl-fragment.conf` 的内容追加到 `/etc/mosquitto/aclfile`，改掉 `<name>`，
  再 `sudo mosquitto_passwd -c /etc/mosquitto/passwd <name>`（设备侧 TLS 接入时才需要）。
- **systemd**：`systemd/pihub-NAME.service` 改好路径后
  `sudo cp ... /etc/systemd/system/ && sudo systemctl daemon-reload && systemctl enable --now pihub-<name>`。

## 可选：让验收闸门也验这台设备

新建 `devices/<name>/selfcheck.sh`，实现 `<name>_selfcheck_device` 等钩子，
`acceptance_s9.sh` 会自动 source 并调用 —— **核心与验收脚本都不用改**。
契约见 [`docs/DEVICE-PLUGIN.md`](../../docs/DEVICE-PLUGIN.md)「三之二」。

## 自检（不上设备也能验）

模板只认一条 `ping`，正好用来证明「加个目录就通了」，全程走本机回环 broker：

```bash
cd <仓库根>
cp -a devices/_template devices/demo
sed -i 's/<name>/demo/g' devices/demo/bot.py

/home/pi/cooperate/pihub/.venv/bin/python devices/demo/bot.py &
sleep 1
mosquitto_pub -h 127.0.0.1 -t hub/command/demo -m '{"v":1,"id":"t1","cmd":"ping"}'
timeout 5 mosquitto_sub -h 127.0.0.1 -t hub/command/demo/ack -C 1
# 期望：{"v": 1, "id": "t1", "ok": true, "status": "pong"}

kill %1
rm -rf devices/demo          # 验完就删：模板不该常驻成第七个服务
```

这条自检通过 = **判据 3 成立**：新增设备只加了一个目录，`core/` 零改动。
