#!/usr/bin/env python3
"""Package PyInstaller dist/ output into standalone release archives.

Usage (after pyinstaller):
  python scripts/package_release.py --platform windows|macos|linux [--version X.Y.Z]

The version defaults to pi_manager/extras.py APP_VERSION when --version is omitted.

Two hard gates run before anything is written (docs/review/r2-build.md):
- B-5: the packaged binary's own ``--self-check`` must report the version being
  packaged, so a stale ``dist/`` can never be shipped under a new file name.
- B-2: archives must preserve symbolic links; macOS ``.app`` bundles rely on
  Frameworks<->Resources cross-links and a flattened archive breaks the ad-hoc
  signature (users then see "damaged, can't be opened", which is worse than
  unsigned because right-click -> Open no longer bypasses it).
"""
from __future__ import annotations

import argparse
import gzip
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


_APP_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*"([^"]+)"')
_SELF_CHECK_VERSION_RE = re.compile(r"^version=(.+)$", re.MULTILINE)
_ARCHIVE_STEM_RE = re.compile(r"^PiManager-v(?P<version>[0-9][^-]*)-")

# zip external_attr high 16 bits = st_mode; S_IFLNK | 0o777 marks a symlink
# entry (Info-ZIP / macOS Archive Utility / ditto all honour this).
_MODE_SYMLINK = 0o120777
_MODE_DIR = 0o040755
_MODE_FILE_EXEC = 0o100755
_MODE_FILE = 0o100644
_CREATE_SYSTEM_UNIX = 3


def get_app_version(project_root: Path = REPO_ROOT) -> str:
    """Read APP_VERSION from pi_manager/extras.py (single source of truth).

    Text-based extraction keeps this standalone packaging entry point free of
    pi_manager package imports (no GUI / heavy dependencies).
    """
    src = (project_root / "pi_manager" / "extras.py").read_text(encoding="utf-8")
    match = _APP_VERSION_RE.search(src)
    if not match:
        raise SystemExit("cannot extract APP_VERSION from pi_manager/extras.py")
    return match.group(1)


def resolve_repo_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return system


def _ensure_executable(path: Path) -> None:
    if not path.exists() or path.is_dir():
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _source_date_epoch() -> int | None:
    """Honour SOURCE_DATE_EPOCH so archives can be made reproducible (B-16)."""
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _entry_date_time(st_mtime: float) -> tuple[int, int, int, int, int, int]:
    epoch = _source_date_epoch()
    return time.localtime(epoch if epoch is not None else st_mtime)[:6]


def _atomic_archive(dst: Path, writer) -> None:
    """Write via ``<dst>.partial`` then ``os.replace``.

    Failing halfway must never leave a truncated archive behind that a later
    step (or a human) could mistake for a finished product (review §6.1).
    """
    staging = dst.with_name(dst.name + ".partial")
    if staging.exists():
        staging.unlink()
    try:
        writer(staging)
        os.replace(staging, dst)
    except BaseException:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
        raise


def iter_tree(src: Path):
    """Yield every entry below ``src``; symlinked directories are not descended.

    ``Path.rglob`` already refuses to recurse through symlinked directories, so
    the macOS Frameworks<->Resources cross-links cannot produce duplicates or
    an infinite walk. Symlinks are still yielded as entries themselves.
    """
    return sorted(src.rglob("*"))


def _write_zip(src: Path, dst: Path, arc_root: str | None) -> None:
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in iter_tree(src):
            rel = path.relative_to(src)
            arc = f"{arc_root}/{rel.as_posix()}" if arc_root else rel.as_posix()
            st = path.lstat()
            # Order matters: is_dir()/is_file() follow symlinks, so a symlink
            # must be classified first or it gets materialised / dropped (B-2).
            if path.is_symlink():
                info = zipfile.ZipInfo(arc)
                info.date_time = _entry_date_time(st.st_mtime)
                info.create_system = _CREATE_SYSTEM_UNIX
                info.external_attr = _MODE_SYMLINK << 16
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, os.readlink(path))
                continue
            if path.is_dir():
                info = zipfile.ZipInfo(arc + "/")
                info.date_time = _entry_date_time(st.st_mtime)
                info.create_system = _CREATE_SYSTEM_UNIX
                info.external_attr = (_MODE_DIR << 16) | 0x10  # MS-DOS directory bit
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, b"")
                continue
            info = zipfile.ZipInfo(arc)
            info.date_time = _entry_date_time(st.st_mtime)
            info.create_system = _CREATE_SYSTEM_UNIX
            info.compress_type = zipfile.ZIP_DEFLATED
            # Preserve executable bit for Unix unzip tools
            executable = os.access(path, os.X_OK) or path.name in {"PiManager", "run-PiManager.sh"}
            info.external_attr = (_MODE_FILE_EXEC if executable else _MODE_FILE) << 16
            with path.open("rb") as fh:
                zf.writestr(info, fh.read())


def zip_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    """Zip a directory tree, preserving symlinks, directories and the exec bit."""
    _atomic_archive(dst, lambda staging: _write_zip(src, staging, arc_root))


def tar_gz_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        name = ti.name.replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if ti.isfile() and (
            base == "PiManager" or name.endswith("/PiManager") or base.endswith(".sh")
        ):
            ti.mode = 0o755
        return ti

    epoch = _source_date_epoch()

    def _write(staging: Path) -> None:
        # Explicit GzipFile so SOURCE_DATE_EPOCH can zero out the gzip header
        # timestamp; mtime=None keeps the previous "w:gz" behaviour.
        with staging.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=epoch) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tf:
                    tf.add(src, arcname=arc_root or src.name, filter=_filter)

    _atomic_archive(dst, _write)


# --------------------------------------------------------------------------- #
# B-2: symlink fidelity
# --------------------------------------------------------------------------- #


def source_symlinks(src: Path) -> set[str]:
    return {
        path.relative_to(src).as_posix()
        for path in iter_tree(src)
        if path.is_symlink()
    }


def archive_symlinks(archive: Path, arc_root: str | None = None) -> set[str]:
    """Relative paths stored as symlink entries inside a zip archive."""
    prefix = f"{arc_root}/" if arc_root else ""
    found: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                name = info.filename
                if prefix and name.startswith(prefix):
                    name = name[len(prefix):]
                found.add(name)
    return found


def verify_archive_symlinks(src: Path, archive: Path, arc_root: str | None = None) -> list[str]:
    """Fail loudly when the archive lost symlinks that exist in the source tree."""
    expected = source_symlinks(src)
    if not expected:
        return []
    actual = archive_symlinks(archive, arc_root)
    missing = sorted(expected - actual)
    if missing:
        head = ", ".join(missing[:5])
        return [
            f"archive lost {len(missing)}/{len(expected)} symlink(s) "
            f"(macOS .app signature would break): {head}"
        ]
    return []


def _macos_archive(app: Path, target: Path) -> str:
    """Archive a ``.app`` with Apple's ditto, falling back to the zip writer.

    ``ditto -c -k --sequesterRsrc --keepParent`` is Apple's documented way to
    zip a signed bundle: it keeps symlinks, resource forks and extended
    attributes. ``zip_dir`` is symlink-safe too and stays as the fallback for
    machines without ditto.
    """
    ditto = shutil.which("ditto")
    if ditto:
        staging = target.with_name(target.name + ".partial")
        if staging.exists():
            staging.unlink()
        result = subprocess.run(
            [ditto, "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(staging)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and staging.is_file():
            os.replace(staging, target)
            return "ditto"
        if staging.exists():
            staging.unlink()
        print(
            f"warning: ditto failed ({result.returncode}): {result.stderr.strip()}; "
            "falling back to the symlink-preserving zip writer",
            file=sys.stderr,
        )
    zip_dir(app, target, arc_root=app.name)
    return "zipfile"


def verify_macos_signature(app: Path, archive: Path, strict: bool) -> list[str]:
    """Extract the archive and run ``codesign --verify`` on the result.

    Only meaningful on macOS with codesign present; the symlink check above is
    the portable gate that actually catches the B-2 regression.
    """
    codesign = shutil.which("codesign")
    if not codesign or sys.platform != "darwin":
        return []
    unpack = shutil.which("ditto")
    scratch = archive.with_name(archive.name + ".verify")
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        if unpack:
            extract = subprocess.run(
                [unpack, "-x", "-k", str(archive), str(scratch)],
                check=False,
                capture_output=True,
                text=True,
            )
            if extract.returncode != 0:
                return [f"cannot unpack archive for signature verification: {extract.stderr.strip()}"]
        else:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(scratch)
        extracted = scratch / app.name
        if not extracted.is_dir():
            return [f"unpacked archive has no {app.name}"]
        result = subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(extracted)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = (
                "codesign --verify failed on the unpacked archive: "
                f"{(result.stderr or result.stdout).strip()}"
            )
            if strict:
                return [message]
            print(f"warning: {message}", file=sys.stderr)
        return []
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# B-5: version gate
# --------------------------------------------------------------------------- #


def dist_binary(dist: Path, plat: str) -> Path | None:
    if plat == "windows":
        # Windows only ships the onefile product, so this is the only layout
        # the version gate needs to know about.
        candidates = [dist / "PiManager.exe"]
    elif plat == "macos":
        candidates = [dist / "PiManager.app" / "Contents" / "MacOS" / "PiManager"]
    else:
        candidates = [dist / "PiManager" / "PiManager"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def parse_self_check_version(stdout: str) -> str | None:
    """Exact ``version=<X>`` line match; substring matching let 1.8.6 pass as
    1.8.60 in the old smoke test (B-13)."""
    match = _SELF_CHECK_VERSION_RE.search(stdout or "")
    return match.group(1).strip() if match else None


def verify_binary_version(binary: Path, expected: str, timeout: int) -> list[str]:
    """Run ``<binary> --self-check`` and require the reported version to match.

    Closes the "file name 1.8.6 / content 1.8.4" hole: the old script only
    checked that dist/PiManager.exe existed and never read its version (B-5).
    """
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("QT_OPENGL", "software")
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            [str(binary), "--self-check"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"{binary.name} --self-check timed out after {timeout}s"]
    except OSError as exc:
        return [f"cannot execute {binary}: {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-6:] if detail else []
        return [
            f"{binary.name} --self-check failed (exit {proc.returncode})",
            *[f"  {line}" for line in tail],
        ]
    found = parse_self_check_version(proc.stdout)
    if found is None:
        return [f"{binary.name} --self-check printed no 'version=' line"]
    if found != expected:
        return [
            f"packaged binary reports version={found} but --version says {expected}; "
            "rebuild dist/ before packaging"
        ]
    print(f"version gate: {binary.name} reports version={found}")
    return []


def prune_stale_archives(out: Path, version: str) -> list[Path]:
    """Delete PiManager archives of other versions left in the output dir.

    ``release-assets/`` is not cleaned between local builds, so ``gh release
    upload release-assets/*`` (or a manual upload) would ship the previous
    version alongside the new one (review §5.4). Only PiManager archives are
    touched; the independently versioned .vsix is left alone.
    """
    removed: list[Path] = []
    for path in sorted(out.glob("PiManager-v*")):
        if not path.is_file():
            continue
        if path.suffix not in {".zip", ".gz", ".partial", ".sha256"} and not path.name.endswith(".tar.gz"):
            continue
        match = _ARCHIVE_STEM_RE.match(path.name)
        if match and match.group("version") == version:
            continue
        path.unlink()
        removed.append(path)
    return removed


def normalize_arch(machine: str | None = None) -> str | None:
    """Map platform.machine() onto the release naming vocabulary.

    Returning None instead of silently degrading to an arch-less name keeps
    "PiManager-vX-macos.zip" / a mislabelled "linux-x64" on arm64 out of the
    release (review §6.2).
    """
    value = (machine if machine is not None else platform.machine()).lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    if value in {"x86_64", "amd64", "x64"}:
        return "x64"
    return None


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path, digest: str = "") -> Path:
    """Emit ``<archive>.sha256`` in the usual ``<hash>  <name>`` format (§4.4 P1)."""
    target = path.with_name(path.name + ".sha256")
    target.write_text(f"{digest or sha256_file(path)}  {path.name}\n", encoding="utf-8")
    return target


def write_run_notes(
    out_dir: Path,
    plat: str,
    version: str,
    archive_name: str = "",
    archive_sha256: str = "",
) -> Path:
    if plat == "windows":
        text = f"""PiManager v{version} (Windows x64 便携版)

独立运行说明：
1. 解压本 zip，得到单个 PiManager.exe（自包含，无需 Python / Node）
2. 双击 PiManager.exe 即可运行；首次启动需解压到临时目录，稍慢属正常
3. 可把 PiManager.exe 单独拷贝到任意目录 / U 盘便携使用

可选：安装官方 Pi CLI 以启动完整会话
  npm install -g @earendil-works/pi-coding-agent

自检：
  PiManager.exe --self-check
"""
    elif plat == "macos":
        text = f"""PiManager v{version} (macOS)

独立运行说明：
1. 解压 zip（建议用「归档实用工具」或 ditto -x -k，以保留 .app 内的符号链接）
2. 将 PiManager.app 拖到「应用程序」或任意文件夹
3. 首次打开：右键 → 打开（未签名时需在「隐私与安全性」中允许）
4. 若仍提示「已损坏 / 无法打开」，先移除隔离标记：
   xattr -dr com.apple.quarantine /path/to/PiManager.app

注意：
- 请使用与本机架构匹配的包（Apple Silicon 用 arm64 包）
- 完整 Pi 会话仍需本机安装官方 pi CLI

自检：
  PiManager.app/Contents/MacOS/PiManager --self-check
"""
    else:
        tarball = archive_name or f"PiManager-v{version}-linux-x64.tar.gz"
        text = f"""PiManager v{version} (Linux)

独立运行说明：
1. tar -xzf {tarball}
2. ./PiManager/PiManager
   或 ./PiManager/run-PiManager.sh
3. 保持目录完整（可执行文件与 _internal 等同级依赖不要拆开）

若启动报缺库，按发行版安装常见 GUI 依赖，例如 Debian/Ubuntu：
  sudo apt-get install -y libgl1 libegl1 libxkbcommon0 libxcb-cursor0 libdbus-1-3 libfontconfig1

完整 Pi 会话仍需本机安装官方 pi CLI：
  npm install -g @earendil-works/pi-coding-agent

自检：
  ./PiManager/PiManager --self-check
"""
    if archive_name and archive_sha256:
        text += f"""
完整性校验（SHA-256，与随包的 {archive_name}.sha256 一致）：
  {archive_sha256}  {archive_name}
  Windows : certutil -hashfile {archive_name} SHA256
  macOS   : shasum -a 256 {archive_name}
  Linux   : sha256sum -c {archive_name}.sha256
"""
    path = out_dir / f"RUN-{plat}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default=detect_platform())
    parser.add_argument("--version", default=get_app_version())
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--out", default="release-assets")
    parser.add_argument(
        "--self-check-timeout",
        type=int,
        default=300,
        help="timeout for the packaged binary's --self-check version gate",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="skip the binary version gate (debug only; never in a release)",
    )
    parser.add_argument(
        "--no-prune-stale",
        action="store_true",
        help="keep PiManager archives of other versions found in --out",
    )
    parser.add_argument(
        "--strict-sign",
        action="store_true",
        help="macOS: treat codesign / codesign --verify failures as fatal",
    )
    parser.add_argument(
        "--no-checksums",
        action="store_true",
        help="do not emit <archive>.sha256 next to each archive",
    )
    args = parser.parse_args()

    dist = resolve_repo_path(args.dist)
    out = resolve_repo_path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = args.version
    plat = args.platform.lower()
    written: list[Path] = []

    if plat not in {"windows", "macos", "linux"}:
        print(f"unsupported platform: {plat}", file=sys.stderr)
        return 2

    # ---- gate 1: the binary in dist/ must be the version we are packaging ----
    if not args.skip_version_check:
        binary = dist_binary(dist, plat)
        if binary is None:
            print(
                f"no packaged binary under {dist} for platform={plat}"
                "（请先用 PyInstaller 打包）",
                file=sys.stderr,
            )
            return 1
        problems = verify_binary_version(binary, version, args.self_check_timeout)
        if problems:
            for line in problems:
                print(f"FAIL: {line}", file=sys.stderr)
            return 1
    else:
        print("warning: --skip-version-check given; packaging without a version gate", file=sys.stderr)

    if not args.no_prune_stale:
        for path in prune_stale_archives(out, version):
            print(f"pruned stale artifact {path.name}")

    if plat == "windows":
        # 只产便携版（单文件 onefile）：用户要求不留文件夹版（dir）。
        one_src = dist / "PiManager.exe"
        if not one_src.is_file():
            print("dist/PiManager.exe not found（请先用 PyInstaller PiManagerOneFile.spec 打包）", file=sys.stderr)
            return 1
        target = out / f"PiManager-v{version}-windows-x64-onefile.zip"

        def _write_onefile(staging: Path) -> None:
            with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(one_src, arcname="PiManager.exe")

        _atomic_archive(target, _write_onefile)
        written.append(target)
    elif plat == "macos":
        app = dist / "PiManager.app"
        if not app.is_dir():
            print("PiManager.app not found", file=sys.stderr)
            return 1
        binary = app / "Contents" / "MacOS" / "PiManager"
        _ensure_executable(binary)
        # Ad-hoc sign for local consistency (not a Developer ID signature).
        # Signing before archiving is correct only because the archive writers
        # below preserve symlinks; otherwise the signature breaks on unpack.
        if shutil.which("codesign"):
            result = subprocess.run(
                ["codesign", "--force", "--sign", "-", str(app)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                message = f"codesign failed: {result.stderr.strip()}"
                if args.strict_sign:
                    print(f"FAIL: {message}", file=sys.stderr)
                    return 1
                print(f"warning: {message}", file=sys.stderr)
        arch = normalize_arch()
        if arch is None:
            print(
                f"FAIL: unrecognised macOS architecture {platform.machine()!r}; "
                "refusing to publish an arch-less archive name",
                file=sys.stderr,
            )
            return 1
        target = out / f"PiManager-v{version}-macos-{arch}.zip"
        method = _macos_archive(app, target)
        print(f"macos archive writer: {method}")
        problems = verify_archive_symlinks(app, target, arc_root=app.name)
        problems += verify_macos_signature(app, target, strict=args.strict_sign)
        if problems:
            for line in problems:
                print(f"FAIL: {line}", file=sys.stderr)
            target.unlink(missing_ok=True)
            return 1
        written.append(target)
    else:
        dir_src = dist / "PiManager"
        if not dir_src.is_dir():
            print("dist/PiManager not found", file=sys.stderr)
            return 1
        binary = dir_src / "PiManager"
        _ensure_executable(binary)
        launcher = dir_src / "run-PiManager.sh"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
            'cd "$HERE"\n'
            'exec "$HERE/PiManager" "$@"\n',
            encoding="utf-8",
        )
        _ensure_executable(launcher)
        arch = normalize_arch()
        if arch is None:
            print(
                f"FAIL: unrecognised Linux architecture {platform.machine()!r}; "
                "the archive name would misreport the target",
                file=sys.stderr,
            )
            return 1
        target = out / f"PiManager-v{version}-linux-{arch}.tar.gz"
        tar_gz_dir(dir_src, target, arc_root="PiManager")
        written.append(target)

    archives = list(written)
    digests: dict[str, str] = {}
    if not args.no_checksums:
        for path in archives:
            digests[path.name] = sha256_file(path)
            written.append(write_checksum(path, digests[path.name]))

    primary = archives[0].name if archives else ""
    notes = write_run_notes(
        out,
        plat,
        version,
        archive_name=primary,
        archive_sha256=digests.get(primary, ""),
    )
    written.append(notes)

    for path in written:
        size = path.stat().st_size if path.is_file() else 0
        print(f"wrote {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
