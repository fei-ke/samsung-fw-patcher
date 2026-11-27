# Samsung Firmware Patcher

本项目用于自动化对三星官方固件进行补丁和打包，主要包含禁用 `vbmeta` 校验、替换 `abl` 文件、在 `init_boot` 中注入 KernelSU，以及使用 [samsung-lkm-boot-patcher](https://github.com/fei-ke/samsung-lkm-boot-patcher) 修补原厂 `boot.img`（关闭 UH / RKP / KDP 保护，使原厂内核支持 KernelSU LKM 模式）。

## 准备文件

运行脚本前，请在工作目录中准备好以下官方固件文件：

1. **BL 固件文件**:
   - 文件名格式：`BL_*.tar` 或 `BL_*.tar.md5`
2. **AP 固件文件**:
   - 文件名格式：`AP_*.tar` 或 `AP_*.tar.md5`
3. **CSC 固件文件**:
   - 文件名格式：`CSC_*.tar` 或 `CSC_*.tar.md5`

> [!NOTE]
> - **KernelSU `.ko` 模块**：无需手动下载，脚本会自动从原厂内核提取 KMI 版本并从 [KernelSU Releases](https://github.com/tiann/KernelSU/releases/latest) 下载最新版本对应的 `aarch64` `.ko` 文件；若工作目录中已存在匹配该 KMI 版本的 `.ko` 文件，则会优先使用本地文件。
> - **`abl.elf.lz4`**：脚本优先使用工作目录中的 `abl.elf.lz4` 替换 BL 包里的同名文件。工作目录没有该文件时，使用运行环境中的 `/oneui7/abl.elf.lz4`。两处都缺失时，脚本会报错退出。

---

## 运行步骤

建议使用 Docker 进行构建和运行，以避免环境依赖问题。

### 1. 构建 Docker 镜像

在项目根目录下执行以下命令构建镜像：

```bash
docker build -t samsung-fw-patcher .
```

### 2. 运行补丁脚本

将所有准备好的文件放入一个目录（例如当前目录的 `workspace` 文件夹），然后运行容器，将该目录挂载至容器的 `/workdir`：

```bash
docker run --rm -v /path/to/workspace:/workdir samsung-fw-patcher
```

---

## 输出文件

运行成功后，会在挂载的工作目录中生成以下文件：

1. **`patched_BL.tar`**:
   - 包含已禁用校验的 `vbmeta.img` 以及被替换后的 `abl.elf.lz4`。
2. **`ksu.tar`**:
   - 包含通过 `ksud` 注入了 KernelSU 的 `init_boot.img`，以及通过 `samsung-lkm-boot-patcher` 修补（关闭 UH / RKP / KDP）后的原厂 `boot.img`。
3. **`*.pit`**:
   - 从 CSC 固件中自动检索并提取出的分区布局配置文件（如 `PA3Q_CHN_OPENX.pit`）。
