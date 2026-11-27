FROM debian:bookworm-slim

# 安装依赖：lz4, tar, python3, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    lz4 \
    tar \
    python3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 下载 samsung-lkm-boot-patcher 脚本并放入可执行路径
ARG LKM_PATCHER_REF=main
ADD https://raw.githubusercontent.com/fei-ke/samsung-lkm-boot-patcher/${LKM_PATCHER_REF}/patch_samsung_boot_rkp_kdp.py /usr/local/bin/patch_samsung_boot_rkp_kdp.py

# 将 bin 目录内的可执行文件和脚本放到容器的可执行路径中
COPY bin/* /usr/local/bin/
RUN chmod 755 /usr/local/bin/* \
    && ln -sf /usr/local/bin/patch.py /usr/local/bin/patch.sh

# 将 oneui7 目录复制到容器根目录
COPY oneui7 /oneui7

# 设置默认的工作目录为 /workdir
WORKDIR /workdir

# 默认入口设置为 patch.py
ENTRYPOINT ["patch.py"]
