#!/usr/bin/env python3
"""
三星官方固件自动化补丁脚本（KernelSU LKM + 原厂内核去 RKP/KDP）：
1. BL：替换 abl.elf.lz4、禁用 vbmeta 校验，打包为 patched_BL.tar
2. AP：提取 KMI、获取匹配的最新 KernelSU .ko 并修补 init_boot.img，
       使用 samsung-lkm-boot-patcher 修补原厂 boot.img，打包为 ksu.tar
3. CSC：提取分区表 .pit 文件到工作目录
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ABL = Path("/oneui7/abl.elf.lz4")
KSU_LATEST = "https://github.com/tiann/KernelSU/releases/latest"
KSU_API_LATEST = "https://api.github.com/repos/tiann/KernelSU/releases/latest"
COMPRESSED_KERNEL_ERR = "Kernel payload is not an uncompressed ARM64 Linux Image"
KMI_RE = re.compile(rb"Linux version (\d+\.\d+)(?:\.\d+)?-([a-zA-Z0-9]+)")


def die(msg: str) -> None:
    sys.exit(f"[-] Error: {msg}")


def run(*cmd: str | Path, cwd: Path | None = None) -> None:
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def find_archive(work_dir: Path, prefix: str) -> Path:
    for p in sorted(work_dir.iterdir()):
        if p.is_file() and p.name.startswith(f"{prefix}_") and (
            p.name.endswith(".tar") or p.name.endswith(".tar.md5")
        ):
            return p
    die(f"{prefix} file ({prefix}_*.tar or {prefix}_*.tar.md5) not found in {work_dir}.")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "samsung-fw-patcher"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def download_ko(url: str, dest: Path) -> bool:
    try:
        data = fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"[!] HTTP {exc.code}: {url}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[!] Failed to download {url}: {exc}", file=sys.stderr)
        return False
    if not data.startswith(b"\x7fELF"):
        return False
    dest.write_bytes(data)
    return True


def resolve_ksu_ko(work_dir: Path, tmp_dir: Path, kmi: str) -> str:
    """获取匹配 KMI 的 KernelSU LKM (.ko) 模块并放到 tmp 目录。"""
    # 1. 优先使用工作目录中的本地 .ko（忽略 x86）
    for p in sorted(work_dir.iterdir()):
        name = p.name.lower()
        if p.is_file() and name.endswith(".ko") and kmi.lower() in name and "x86" not in name:
            print(f"[+] Found local ko file: {p.name}")
            shutil.copy2(p, tmp_dir / p.name)
            return p.name

    # 2. 本地缺失时，直接从官方 latest Release 按常见命名下载（不消耗 API 配额）
    print(f"[*] Downloading latest KernelSU module for {kmi}...")
    for name in (f"lkm-aarch64-{kmi}_kernelsu.ko", f"{kmi}_kernelsu.ko"):
        url = f"{KSU_LATEST}/download/{name}"
        print(f"[*] Trying {url}...")
        if download_ko(url, tmp_dir / name):
            print(f"[+] Downloaded {name}")
            return name

    # 3. 直接命名未命中时，回退到 GitHub Releases API 动态匹配 aarch64 / 非 x86 的 .ko 资产
    print("[*] Querying GitHub Releases API...")
    try:
        assets = json.loads(fetch(KSU_API_LATEST)).get("assets", [])
    except Exception as exc:
        die(f"Failed to query {KSU_API_LATEST}: {exc}")

    candidates = [
        a for a in assets
        if a["name"].endswith(".ko")
        and kmi in a["name"]
        and not any(x in a["name"].lower() for x in ("x86", "riscv"))
    ]
    candidates.sort(key=lambda a: "aarch64" not in a["name"].lower())
    if candidates and download_ko(candidates[0]["browser_download_url"], tmp_dir / candidates[0]["name"]):
        name = candidates[0]["name"]
        print(f"[+] Downloaded {name}")
        return name

    die(f"Failed to download KernelSU .ko for {kmi} from {KSU_LATEST}.")


def patch_bl(work_dir: Path, tmp_dir: Path) -> None:
    """处理 BL 包：替换 abl.elf.lz4、禁用 vbmeta 校验，输出 patched_BL.tar。"""
    # 1. 查找并解包 BL 固件
    bl_file = find_archive(work_dir, "BL")
    print(f"[+] Found BL file: {bl_file.name}")

    bl_dir = tmp_dir / "BL"
    bl_dir.mkdir()
    run("tar", "-xf", bl_file, "-C", bl_dir)

    # 2. 替换 abl.elf.lz4（优先工作目录，缺失时回退到容器内置 One UI 7 默认文件）
    abl_src = work_dir / "abl.elf.lz4"
    if not abl_src.is_file():
        abl_src = DEFAULT_ABL
    if not abl_src.is_file():
        die(f"abl.elf.lz4 not found in {work_dir} or {DEFAULT_ABL.parent}.")
    print(f"[*] Replacing abl.elf.lz4 with {abl_src}...")
    (bl_dir / "abl.elf.lz4").unlink(missing_ok=True)
    shutil.copy2(abl_src, bl_dir / "abl.elf.lz4")

    # 3. 解压 vbmeta.img.lz4 并禁用校验
    vbmeta_lz4 = bl_dir / "vbmeta.img.lz4"
    vbmeta_img = bl_dir / "vbmeta.img"
    if not vbmeta_lz4.is_file():
        die("vbmeta.img.lz4 not found in BL archive.")
    run("lz4", "-d", vbmeta_lz4, vbmeta_img)
    vbmeta_lz4.unlink()
    run("vbmeta-disable-verification", vbmeta_img)

    # 4. 重新打包为 patched_BL.tar
    print("[*] Packaging patched_BL.tar...")
    run("tar", "-cf", work_dir / "patched_BL.tar", *[p.name for p in sorted(bl_dir.iterdir())], cwd=bl_dir)


def patch_ap(work_dir: Path, tmp_dir: Path) -> None:
    """处理 AP 包：修补 init_boot.img（KernelSU）与原厂 boot.img（去 RKP/KDP），输出 ksu.tar。"""
    # 1. 查找 AP 固件并提取解压 init_boot.img 和 boot.img
    ap_file = find_archive(work_dir, "AP")
    print(f"[+] Found AP file: {ap_file.name}")

    run("tar", "-xf", ap_file, "-C", tmp_dir, "init_boot.img.lz4", "boot.img.lz4")
    for name in ("init_boot.img", "boot.img"):
        lz4_file = tmp_dir / f"{name}.lz4"
        run("lz4", "-d", lz4_file, tmp_dir / name)
        lz4_file.unlink()

    # 2. 解包 boot.img 提取裸内核并解析 KMI 版本（同时保留解包产物供压缩内核回退使用）
    unpack_dir = tmp_dir / "boot_unpack"
    unpack_dir.mkdir()
    run("magiskboot", "unpack", tmp_dir / "boot.img", cwd=unpack_dir)

    match = KMI_RE.search((unpack_dir / "kernel").read_bytes())
    if not match:
        die("Failed to extract KMI version from stock kernel.")
    kmi = f"{match.group(2).decode()}-{match.group(1).decode()}"
    print(f"[+] Extracted KMI: {kmi}")

    # 3. 获取对应 KMI 的 KernelSU .ko 模块并用 ksud 修补 init_boot.img
    ko_file = resolve_ksu_ko(work_dir, tmp_dir, kmi)
    print("[*] Patching init_boot.img with ksud...")
    run("ksud", "boot-patch", "-b", "init_boot.img", "--module", ko_file, "--out-name", "init_boot-patched.img", cwd=tmp_dir)

    # 4. 使用 samsung-lkm-boot-patcher 修补原厂 boot.img（关闭 UH / RKP / KDP）
    #    优先原地修补 boot.img 保留原厂布局；遇到压缩内核时回退到修补解包后的 kernel 并重新打包
    print("[*] Patching stock boot.img with patch_samsung_boot_rkp_kdp.py...")
    boot_img = tmp_dir / "boot.img"
    boot_patched = tmp_dir / "boot-patched.img"
    res = subprocess.run(
        ["patch_samsung_boot_rkp_kdp.py", str(boot_img), str(boot_patched)],
        text=True,
        stderr=subprocess.PIPE,
    )
    if res.stderr:
        sys.stderr.write(res.stderr)
    if res.returncode != 0:
        if COMPRESSED_KERNEL_ERR not in (res.stderr or ""):
            sys.exit(res.returncode)
        print("[*] Compressed kernel detected; falling back to magiskboot unpack/repack...")
        run("patch_samsung_boot_rkp_kdp.py", "kernel", "kernel-patched", cwd=unpack_dir)
        (unpack_dir / "kernel-patched").replace(unpack_dir / "kernel")
        run("magiskboot", "repack", boot_img, cwd=unpack_dir)
        (unpack_dir / "new-boot.img").replace(boot_patched)

    # 5. 将修补后的 init_boot.img 与 boot.img 打包为 ksu.tar
    print("[*] Packaging ksu.tar...")
    (tmp_dir / "init_boot-patched.img").replace(tmp_dir / "init_boot.img")
    boot_patched.replace(boot_img)
    run("tar", "-cf", work_dir / "ksu.tar", "init_boot.img", "boot.img", cwd=tmp_dir)


def extract_pit(work_dir: Path) -> None:
    """从 CSC 固件包中提取 .pit 分区表文件到工作目录。"""
    csc_file = find_archive(work_dir, "CSC")
    print(f"[+] Found CSC file: {csc_file.name}")

    entries = subprocess.check_output(["tar", "-tf", str(csc_file)], text=True).splitlines()
    pit_file = next((e for e in entries if e.endswith(".pit")), None)
    if not pit_file:
        die("No .pit file found in CSC archive.")
    print(f"[*] Extracting {pit_file}...")
    run("tar", "-xf", csc_file, pit_file, cwd=work_dir)


def main() -> None:
    # 解析工作目录并初始化临时目录 tmp
    work_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if not work_dir.is_dir():
        die(f"Cannot access directory: {work_dir}")
    print(f"[*] Working directory: {work_dir}")

    tmp_dir = work_dir / "tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir()

    # 依次执行 BL 修补、AP 修补与 CSC 分区表提取
    patch_bl(work_dir, tmp_dir)
    patch_ap(work_dir, tmp_dir)
    extract_pit(work_dir)
    print("[+] All patching steps completed successfully!")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
