# 底座镜像（base only）
# ------------------------------------------------------------
# 这个镜像装什么：core/（config / paths / cache / datasource / wecom_base /
#   mqtt_guard / monitor / init_db）。
# 这个镜像**不装**什么：devices/ 下任何插件、任何企微 bot、api(5000)、devctl、
#   win-agent。边界依据 docs/ARCHITECTURE.md「底座四层」：
#   容器边界 = 总线 + 核心，不放设备、不放入口。
#
# ⚠️ 网络必须 host：core/config.py 默认 mqtt_host=127.0.0.1，
#    bridge 网络下容器里的回环是容器自己，连不到宿主 mosquitto。
#
# 构建（在 repo 根）：
#   docker build -f docker/Dockerfile -t agent-ops-edge-hub-base:latest .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /opt/pihub

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只拷底座的两块：core/ 与入口脚本
COPY core/ ./core/
COPY docker/entrypoint.sh ./
RUN chmod +x entrypoint.sh

# 运行时数据 / 配置挂载点（DB、.env 由宿主提供，一律不进镜像）
VOLUME ["/opt/pihub/data", "/opt/pihub/logs"]

# 底座镜像不起常驻服务：由 systemd unit / compose 决定起哪个 core 进程。
# 直接 `docker run <img>` 时给一个可诊断的默认动作。
ENTRYPOINT ["./entrypoint.sh"]
CMD ["--help"]
