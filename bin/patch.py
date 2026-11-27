#!/usr/bin/env python3
"""Automate Samsung firmware patching for KernelSU (LKM) with stock kernel."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

DEFAULT_ABL_PATH = Path("/oneui7/abl.elf.lz4")
KSU_LATEST_URL = "https://github.com/tiann/KernelSU/releases/latest"
KSU_API_LATEST_URL = "https://api.github.com/repos/tiann/KernelSU/releases/latest"
COMPRESSED_KERNEL_ERR = "Kernel payload is not an uncompressed ARM64 Linux Image"
KMI_PATTERN = re.compile(rb"Linux version (\d+\.\d+)(?:\.\d+)?-([a-zA-Z0-9]+)")
USER_AGENT = "samsung-fw-patcher"


class PatchError(RuntimeError):
    """Raised when a firmware patching step fails."""


def run_cmd(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    capture_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            text=True,
            stderr=subprocess.PIPE if capture_stderr else None,
        )
    except subprocess.CalledProcessError as exc:
        if capture_stderr and exc.stderr:
            sys.stderr.write(exc.stderr)
        raise PatchError(f"Command failed (exit {exc.returncode}): {' '.join(cmd)}") from exc


def find_firmware_archive(work_dir: Path, prefix: str) -> Path:
    matches = sorted(
        p
        for p in work_dir.iterdir()
        if p.is_file()
        and p.name.startswith(f"{prefix}_")
        and (p.name.endswith(".tar") or p.name.endswith(".tar.md5"))
    )
    if not matches:
        raise PatchError(
            f"{prefix} file ({prefix}_*.tar or {prefix}_*.tar.md5) not found in working directory."
        )
    return matches[0]


def safe_extract_tar(archive: Path, dest_dir: Path, members: Optional[Iterable[str]] = None) -> None:
    dest_resolved = dest_dir.resolve()
    wanted = set(members) if members is not None else None
    found: set[str] = set()

    with tarfile.open(archive, "r:") as tf:
        for member in tf.getmembers():
            raw_posix = PurePosixPath(member.name)
            if raw_posix.is_absolute():
                raise PatchError(f"Unsafe absolute path in archive {archive.name}: {member.name}")
            clean_name = raw_posix.as_posix()
            if clean_name == ".":
                continue
            if wanted is not None:
                if clean_name not in wanted:
                    continue
                found.add(clean_name)

            target_path = (dest_dir / clean_name).resolve()
            if not target_path.is_relative_to(dest_resolved):
                raise PatchError(f"Unsafe path in archive {archive.name}: {member.name}")

            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise PatchError(f"Failed to read {member.name} from {archive.name}")
                with src, target_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            elif member.issym():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if target_path.exists() or target_path.is_symlink():
                    target_path.unlink()
                target_path.symlink_to(member.linkname)

    if wanted is not None:
        missing = sorted(wanted - found)
        if missing:
            raise PatchError(f"Missing {', '.join(missing)} in {archive.name}")


def create_odin_tar(output_path: Path, files: Iterable[Path]) -> None:
    with tarfile.open(output_path, "w", format=tarfile.GNU_FORMAT) as tf:
        for file_path in files:
            tf.add(file_path, arcname=file_path.name)


def decompress_lz4(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise PatchError(f"{src.name} not found.")
    run_cmd(["lz4", "-d", str(src), str(dst)])
    src.unlink()


def patch_bl(work_dir: Path, tmp_dir: Path) -> None:
    print("[*] Searching for BL file...")
    bl_file = find_firmware_archive(work_dir, "BL")
    print(f"[+] Found BL file: {bl_file.name}")

    bl_dir = tmp_dir / "BL"
    bl_dir.mkdir(parents=True, exist_ok=True)
    print("[*] Extracting BL file to tmp/BL...")
    safe_extract_tar(bl_file, bl_dir)

    abl_file = work_dir / "abl.elf.lz4"
    if not abl_file.is_file():
        abl_file = DEFAULT_ABL_PATH
    if not abl_file.is_file():
        raise PatchError(f"abl.elf.lz4 not found in {work_dir} or {DEFAULT_ABL_PATH.parent}.")

    print(f"[*] Replacing abl.elf.lz4 with {abl_file}...")
    dst_abl = bl_dir / "abl.elf.lz4"
    if dst_abl.exists():
        dst_abl.unlink()
    shutil.copy2(abl_file, dst_abl)

    print("[*] Decompressing vbmeta.img.lz4...")
    vbmeta_lz4 = bl_dir / "vbmeta.img.lz4"
    vbmeta_img = bl_dir / "vbmeta.img"
    if not vbmeta_lz4.is_file():
        raise PatchError("vbmeta.img.lz4 not found in extracted BL files.")
    decompress_lz4(vbmeta_lz4, vbmeta_img)

    print("[*] Patching vbmeta.img...")
    run_cmd(["vbmeta-disable-verification", str(vbmeta_img)])

    print("[*] Packaging patched_BL.tar...")
    create_odin_tar(work_dir / "patched_BL.tar", sorted(bl_dir.iterdir()))


def extract_kmi(kernel_path: Path) -> str:
    data = kernel_path.read_bytes()
    match = KMI_PATTERN.search(data)
    if not match:
        raise PatchError("Failed to extract KMI version from stock kernel.")
    version = match.group(1).decode("ascii")
    android = match.group(2).decode("ascii")
    return f"{android}-{version}"


def http_request(url: str, *, method: str = "GET", retries: int = 3, delay: float = 2.0) -> tuple[str, bytes]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = b"" if method == "HEAD" else resp.read()
                return resp.geturl(), body
        except urllib.error.HTTPError as exc:
            # Fail fast on client errors (e.g., 404); retry transient 408/429/5xx
            if exc.code not in (408, 429) and exc.code < 500:
                raise
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < retries:
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def download_file(url: str, dest: Path) -> bool:
    try:
        _, data = http_request(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        print(f"[!] HTTP {exc.code} downloading {url}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[!] Failed downloading {url}: {exc}", file=sys.stderr)
        return False

    if not data or not data.startswith(b"\x7fELF"):
        return False
    dest.write_bytes(data)
    return True


def is_ignored_arch(filename: str) -> bool:
    lower = filename.lower()
    return any(arch in lower for arch in ("x86_64", "x86", "riscv64"))


def resolve_ksu_module(work_dir: Path, tmp_dir: Path, kmi: str) -> str:
    print(f"[*] Searching for local .ko file matching KMI version ({kmi})...")
    local_matches = sorted(
        p
        for p in work_dir.iterdir()
        if p.is_file() and p.name.endswith(".ko") and kmi in p.name and not is_ignored_arch(p.name)
    )
    if local_matches:
        local_ko = local_matches[0]
        print(f"[+] Found local matching ko file: {local_ko.name}")
        shutil.copy2(local_ko, tmp_dir / local_ko.name)
        return local_ko.name

    print(f"[*] Local .ko file not found. Downloading latest KernelSU module for {kmi}...")
    try:
        effective_url, _ = http_request(KSU_LATEST_URL, method="HEAD", retries=2, delay=1.0)
        latest_tag = effective_url.rstrip("/").rsplit("/", 1)[-1]
        if latest_tag and latest_tag != "latest":
            print(f"[*] Latest KernelSU release: {latest_tag}")
    except Exception:
        pass

    for candidate in (f"lkm-aarch64-{kmi}_kernelsu.ko", f"{kmi}_kernelsu.ko"):
        download_url = f"{KSU_LATEST_URL}/download/{candidate}"
        print(f"[*] Trying {download_url}...")
        if download_file(download_url, tmp_dir / candidate):
            print(f"[+] Downloaded {candidate}")
            return candidate

    print("[*] Direct download name not matched; querying GitHub Releases API...")
    try:
        _, raw_json = http_request(KSU_API_LATEST_URL)
        assets = json.loads(raw_json.decode("utf-8")).get("assets", [])
    except Exception as exc:
        raise PatchError(
            f"Failed to download KernelSU .ko file matching KMI ({kmi}) from {KSU_LATEST_URL}: {exc}"
        ) from exc

    ko_assets = [
        a
        for a in assets
        if a.get("name", "").endswith(".ko")
        and kmi in a.get("name", "")
        and not is_ignored_arch(a.get("name", ""))
    ]
    preferred = [
        a for a in ko_assets if "aarch64" in a["name"].lower() or "arm64" in a["name"].lower()
    ]
    selected = (preferred or ko_assets or [None])[0]
    if selected:
        asset_name = selected["name"]
        asset_url = selected["browser_download_url"]
        print(f"[*] Downloading {asset_name} from {asset_url}...")
        if download_file(asset_url, tmp_dir / asset_name):
            print(f"[+] Downloaded {asset_name}")
            return asset_name

    raise PatchError(
        f"Failed to download KernelSU .ko file matching KMI version ({kmi}) from {KSU_LATEST_URL}."
    )


def patch_stock_boot(tmp_dir: Path, boot_unpack_dir: Path) -> Path:
    boot_img = tmp_dir / "boot.img"
    boot_patched = tmp_dir / "boot-patched.img"

    print("[*] Patching stock boot.img with patch_samsung_boot_rkp_kdp.py...")
    result = subprocess.run(
        ["patch_samsung_boot_rkp_kdp.py", str(boot_img), str(boot_patched)],
        text=True,
        stderr=subprocess.PIPE,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)

    if result.returncode == 0:
        return boot_patched

    if COMPRESSED_KERNEL_ERR in (result.stderr or ""):
        print(
            "[*] Compressed kernel detected; falling back to patching unpacked kernel and repacking boot.img..."
        )
        run_cmd(
            ["patch_samsung_boot_rkp_kdp.py", "kernel", "kernel-patched"],
            cwd=boot_unpack_dir,
        )
        (boot_unpack_dir / "kernel-patched").replace(boot_unpack_dir / "kernel")
        run_cmd(["magiskboot", "repack", str(boot_img)], cwd=boot_unpack_dir)
        (boot_unpack_dir / "new-boot.img").replace(boot_patched)
        return boot_patched

    raise PatchError(f"patch_samsung_boot_rkp_kdp.py failed with exit code {result.returncode}.")


def patch_ap(work_dir: Path, tmp_dir: Path) -> None:
    print("[*] Searching for AP file...")
    ap_file = find_firmware_archive(work_dir, "AP")
    print(f"[+] Found AP file: {ap_file.name}")

    print("[*] Extracting init_boot.img.lz4 and boot.img.lz4 to tmp...")
    safe_extract_tar(ap_file, tmp_dir, members=("init_boot.img.lz4", "boot.img.lz4"))

    print("[*] Decompressing init_boot.img.lz4 and boot.img.lz4...")
    decompress_lz4(tmp_dir / "init_boot.img.lz4", tmp_dir / "init_boot.img")
    decompress_lz4(tmp_dir / "boot.img.lz4", tmp_dir / "boot.img")

    print("[*] Unpacking boot.img to extract stock kernel...")
    boot_unpack_dir = tmp_dir / "boot_unpack"
    boot_unpack_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(["magiskboot", "unpack", str(tmp_dir / "boot.img")], cwd=boot_unpack_dir)

    print("[*] Extracting KMI version from stock kernel...")
    kmi = extract_kmi(boot_unpack_dir / "kernel")
    print(f"[+] Extracted KMI: {kmi}")

    ko_file = resolve_ksu_module(work_dir, tmp_dir, kmi)

    print("[*] Patching init_boot.img with ksud...")
    run_cmd(
        [
            "ksud",
            "boot-patch",
            "-b",
            "init_boot.img",
            "--module",
            ko_file,
            "--out-name",
            "init_boot-patched.img",
        ],
        cwd=tmp_dir,
    )

    boot_patched = patch_stock_boot(tmp_dir, boot_unpack_dir)

    print("[*] Packaging ksu.tar into working directory...")
    final_init_boot = tmp_dir / "init_boot.img"
    final_boot = tmp_dir / "boot.img"
    (tmp_dir / "init_boot-patched.img").replace(final_init_boot)
    boot_patched.replace(final_boot)
    create_odin_tar(work_dir / "ksu.tar", (final_init_boot, final_boot))


def extract_pit(work_dir: Path) -> None:
    print("[*] Searching for CSC file...")
    csc_file = find_firmware_archive(work_dir, "CSC")
    print(f"[+] Found CSC file: {csc_file.name}")

    print("[*] Finding .pit file in CSC archive...")
    with tarfile.open(csc_file, "r:") as tf:
        pit_member = next(
            (m for m in tf.getmembers() if m.isfile() and m.name.endswith(".pit")),
            None,
        )
        if pit_member is None:
            raise PatchError("No file ending with .pit found in CSC archive.")

        pit_name = Path(pit_member.name).name
        print(f"[+] Found .pit file in archive: {pit_member.name}")
        print(f"[*] Extracting {pit_name} from CSC file...")
        src = tf.extractfile(pit_member)
        if src is None:
            raise PatchError(f"Failed to extract {pit_member.name} from CSC archive.")
        pit_dest = work_dir / pit_name
        with src, pit_dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    print(f"[+] Successfully extracted {pit_name} to working directory.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch Samsung official firmware (BL, AP, CSC) for KernelSU LKM with stock kernel."
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        default=".",
        help="Working directory containing BL/AP/CSC firmware archives (default: current directory)",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).resolve()
    if not work_dir.is_dir():
        print(f"[-] Error: Cannot access directory {args.work_dir}", file=sys.stderr)
        return 1

    os.chdir(work_dir)
    print(f"[*] Working directory: {work_dir}")

    print("[*] Creating temporary directory 'tmp'...")
    tmp_dir = work_dir / "tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        patch_bl(work_dir, tmp_dir)
        patch_ap(work_dir, tmp_dir)
        extract_pit(work_dir)
    except PatchError as exc:
        print(f"[-] Error: {exc}", file=sys.stderr)
        return 1

    print("[+] All patching steps completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
