#!/bin/bash

# 任意命令失败则立即终止脚本
set -e
set -o pipefail

# 从参数解析工作目录并进入，没有的话默认当前目录
WORK_DIR="${1:-.}"



# 获取工作目录的绝对路径并进入该目录
cd "$WORK_DIR" || { echo "[-] Error: Cannot change directory to $WORK_DIR" >&2; exit 1; }
WORK_DIR="$(pwd)"

echo "[*] Working directory: $WORK_DIR"




# 在工作目录建立一个 tmp 临时目录
echo "[*] Creating temporary directory 'tmp'..."
rm -rf tmp
mkdir -p tmp

# 查找 BL_开头，tar 或者 tar.md5 结尾的文件，没有的话报错
echo "[*] Searching for BL file..."
BL_FILE=$(find . -maxdepth 1 \( -name "BL_*.tar" -o -name "BL_*.tar.md5" \) -print -quit)
if [ -z "$BL_FILE" ]; then
    echo "[-] Error: BL file (BL_*.tar or BL_*.tar.md5) not found in working directory." >&2
    exit 1
fi
echo "[+] Found BL file: $BL_FILE"

# 使用 tar 解压这个文件到临时目录内 BL 目录
echo "[*] Extracting BL file to tmp/BL..."
mkdir -p tmp/BL
tar -xf "$BL_FILE" -C tmp/BL

# 优先使用工作目录中的 ABL，缺失时使用 One UI 7 的默认文件
ABL_FILE="$WORK_DIR/abl.elf.lz4"
if [ ! -f "$ABL_FILE" ]; then
    ABL_FILE="/oneui7/abl.elf.lz4"
fi
if [ ! -f "$ABL_FILE" ]; then
    echo "[-] Error: abl.elf.lz4 not found in $WORK_DIR or /oneui7." >&2
    exit 1
fi
echo "[*] Replacing abl.elf.lz4 with $ABL_FILE..."
rm -f tmp/BL/abl.elf.lz4
cp "$ABL_FILE" tmp/BL/

# 使用 lz4 命令解压 vbmeta.img.lz4，然后删除 vbmeta.img.lz4
echo "[*] Decompressing vbmeta.img.lz4..."
if [ ! -f "tmp/BL/vbmeta.img.lz4" ]; then
    echo "[-] Error: vbmeta.img.lz4 not found in extracted BL files." >&2
    exit 1
fi
lz4 -d tmp/BL/vbmeta.img.lz4 tmp/BL/vbmeta.img
rm -f tmp/BL/vbmeta.img.lz4

# 使用 vbmeta-disable-verification vbmeta.img patch vbmeta.img
echo "[*] Patching vbmeta.img..."
vbmeta-disable-verification tmp/BL/vbmeta.img

# BL 文件打包为 tar文件，命名为 patched_BL.tar，放到工作目录
echo "[*] Packaging patched_BL.tar..."
(
    cd tmp/BL
    tar -cf "$WORK_DIR/patched_BL.tar" *
)

# 查找 AP_ 开头 tar 或者 tar.md5 结尾的文件，没有的话报错
echo "[*] Searching for AP file..."
AP_FILE=$(find . -maxdepth 1 \( -name "AP_*.tar" -o -name "AP_*.tar.md5" \) -print -quit)
if [ -z "$AP_FILE" ]; then
    echo "[-] Error: AP file (AP_*.tar or AP_*.tar.md5) not found in working directory." >&2
    exit 1
fi
echo "[+] Found AP file: $AP_FILE"

# 从这个文件提取 init_boot.img.lz4 和 boot.img.lz4 文件到临时目录
echo "[*] Extracting init_boot.img.lz4 and boot.img.lz4 to tmp..."
tar -xf "$AP_FILE" -C tmp init_boot.img.lz4 boot.img.lz4

# 使用 lz4 解压这两个.lz4 文件
echo "[*] Decompressing init_boot.img.lz4 and boot.img.lz4..."
lz4 -d tmp/init_boot.img.lz4 tmp/init_boot.img
lz4 -d tmp/boot.img.lz4 tmp/boot.img
# 清理临时目录下的 lz4 文件
rm -f tmp/init_boot.img.lz4 tmp/boot.img.lz4

# 使用 magiskboot unpack boot.img 解压 boot 文件以提取 stock kernel
echo "[*] Unpacking boot.img to extract stock kernel..."
mkdir -p tmp/boot_unpack
(
    cd tmp/boot_unpack
    magiskboot unpack ../boot.img
)

# 从 stock kernel 中提取 KMI 版本
echo "[*] Extracting KMI version from stock kernel..."
KMI=$(strings tmp/boot_unpack/kernel | grep 'Linux version' | head -n 1 | sed -E 's/.*Linux version ([0-9]+\.[0-9]+)(\.[0-9]+)?-([a-zA-Z0-9]+).*/\3-\1/')
if [ -z "$KMI" ]; then
    echo "[-] Error: Failed to extract KMI version from stock kernel." >&2
    exit 1
fi
echo "[+] Extracted KMI: $KMI"

# 查找匹配 KMI 版本的 .ko 文件
echo "[*] Searching for .ko file matching KMI version ($KMI)..."
KO_FILE=$(find . -maxdepth 1 -name "*${KMI}*.ko" -print -quit)
if [ -z "$KO_FILE" ]; then
    echo "[-] Error: No .ko file matching KMI version ($KMI) found in working directory." >&2
    exit 1
fi
KO_FILE="$(basename "$KO_FILE")"
echo "[+] Found matching ko file: $KO_FILE"

# 将匹配的 .ko 文件拷贝到 tmp 目录
cp "$KO_FILE" tmp/

# 使用 ksud patch： ksud boot-patch -b init_boot.img --module "$KO_FILE" --out-name init_boot-patched.img
echo "[*] Patching init_boot.img with ksud..."
(
    cd tmp
    ksud boot-patch -b init_boot.img --module "$KO_FILE" --out-name init_boot-patched.img
)

# 使用 samsung-lkm-boot-patcher 修补原厂 boot.img（关闭 UH / RKP / KDP）
# 优先直接修补原厂 boot.img；若遇到压缩内核，则回退到修补解包后的 kernel 并重新打包
echo "[*] Patching stock boot.img with patch_samsung_boot_rkp_kdp.py..."
PATCH_ERR="$(mktemp)"
if patch_samsung_boot_rkp_kdp.py tmp/boot.img tmp/boot-patched.img 2>"$PATCH_ERR"; then
    cat "$PATCH_ERR" >&2
    rm -f "$PATCH_ERR"
elif grep -q "Kernel payload is not an uncompressed ARM64 Linux Image" "$PATCH_ERR"; then
    cat "$PATCH_ERR" >&2
    rm -f "$PATCH_ERR"
    echo "[*] Compressed kernel detected; falling back to patching unpacked kernel and repacking boot.img..."
    (
        cd tmp/boot_unpack
        patch_samsung_boot_rkp_kdp.py kernel kernel-patched
        mv kernel-patched kernel
        magiskboot repack ../boot.img
        mv new-boot.img ../boot-patched.img
    )
else
    cat "$PATCH_ERR" >&2
    rm -f "$PATCH_ERR"
    exit 1
fi

# 将 patch 后的 init_boot-patched.img 和 boot-patched.img 重命名为 init_boot.img 和 boot.img 然后一起打包为 ksu.tar，放到工作目录去
echo "[*] Packaging ksu.tar into working directory..."
mv tmp/init_boot-patched.img tmp/init_boot.img
mv tmp/boot-patched.img tmp/boot.img
(
    cd tmp
    tar -cf "$WORK_DIR/ksu.tar" init_boot.img boot.img
)

# 查找 CSC_开头， tar或者 .tar.md5 结尾的文件，并从中提取出 .pit 结尾的文件放到工作目录
echo "[*] Searching for CSC file..."
CSC_FILE=$(find . -maxdepth 1 \( -name "CSC_*.tar" -o -name "CSC_*.tar.md5" \) -print -quit)
if [ -z "$CSC_FILE" ]; then
    echo "[-] Error: CSC file (CSC_*.tar or CSC_*.tar.md5) not found in working directory." >&2
    exit 1
fi
echo "[+] Found CSC file: $CSC_FILE"

# 在 CSC 文件内寻找以 .pit 结尾的文件名
echo "[*] Finding .pit file in CSC archive..."
PIT_FILE=$(tar -tf "$CSC_FILE" | grep '\.pit$' | head -n 1)
if [ -z "$PIT_FILE" ]; then
    echo "[-] Error: No file ending with .pit found in CSC archive." >&2
    exit 1
fi
echo "[+] Found .pit file in archive: $PIT_FILE"

# 从中提取出该 .pit 文件放到工作目录
echo "[*] Extracting $PIT_FILE from CSC file..."
tar -xf "$CSC_FILE" "$PIT_FILE"
if [ ! -f "$PIT_FILE" ]; then
    echo "[-] Error: Failed to extract $PIT_FILE from CSC archive." >&2
    exit 1
fi
echo "[+] Successfully extracted $PIT_FILE to working directory."

echo "[+] All patching steps completed successfully!"
