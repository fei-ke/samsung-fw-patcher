FROM debian:bookworm-slim

# 安装依赖：lz4, tar, unzip
RUN apt-get update && apt-get install -y --no-install-recommends \
    lz4 \
    tar \
    unzip \
    binutils \
    && rm -rf /var/lib/apt/lists/*

# 将 bin 目录内的可执行文件和脚本放到容器的可执行路径中
COPY bin/* /usr/local/bin/
RUN chmod +x /usr/local/bin/*

# 将 oneui7 目录复制到容器根目录
COPY oneui7 /oneui7

# 设置默认的工作目录为 /workdir
WORKDIR /workdir

# 默认入口设置为 patch.sh
ENTRYPOINT ["patch.sh"]
