# -*- coding: utf-8 -*-
"""Cross-platform helpers for Pi Manager (Windows / macOS / Linux)."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


# Windows FILE_ATTRIBUTE_REPARSE_POINT：文件是重解析点（含符号链接/junction）。
# 非 Windows 平台该常量无意义，但保留以供 is_reparse_point 早返回。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def windows_file_attributes(path: str | Path) -> int | None:
    """返回 Windows 文件属性位；非 Windows 或查询失败返回 None。

    用 GetFileAttributesW 不跟随符号链接地读取属性，供重解析点/符号链接检测。
    """
    if not is_windows():
        return None
    try:
        import ctypes

        func = ctypes.windll.kernel32.GetFileAttributesW
        func.argtypes = [ctypes.c_wchar_p]
        func.restype = ctypes.c_uint32
        value = func(str(path))
    except Exception:
        return None
    if value == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
        return None
    return int(value)


def is_reparse_point(path: str | Path) -> bool:
    """判断 path 是否为重解析点/符号链接（跨平台）。

    非返回 False。用 stat(follow_symlinks=False) + Windows 属性位双校验，
    防止符号链接劫持（写入符号链接会覆盖其指向的目标）。
    """
    try:
        st = os.stat(str(path), follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return False
    if not stat.S_ISREG(st.st_mode):
        # 非普通文件即视为需警惕（目录/字符设备/链接本身）
        return True
    attrs = windows_file_attributes(path)
    return attrs is not None and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


# --- Windows ACL 加固 --------------------------------------------------------
# 为什么需要这一层：POSIX 的 chmod 0600 在 Windows 上几乎是空操作（CPython 只映射
# 只读位），敏感文件（broker token / secrets.vault / pi-manager-helper.json）的实际
# 权限 100% 来自父目录的继承 ACE。历史实现（config_broker._restrict_windows_acl）
# 引用了并不存在的 ctypes.wintypes.PVOID，在设置 argtypes 时就抛 AttributeError，
# 又被 `except Exception: pass` 吞掉 —— 在任何 Windows 机器上都是彻底的 no-op，
# 而注释与历史修复记录都声称它生效了。这里重写为可用实现，并且**加固失败必须留下
# 日志**：安全加固静默失败伪装成成功，是本次审查发现的系统性反模式。
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_INHERITED_ACE = 0x10
_WRITE_DAC = 0x00040000
_READ_CONTROL = 0x00020000
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # 打开目录句柄必需
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # 绝不跟随符号链接去改别人的 ACL
_FILE_ALL_ACCESS = 0x001F01FF
_GRANT_ACCESS = 1
_NO_INHERITANCE = 0
_SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1


def _win_acl_api() -> dict:
    """惰性构建 Windows ACL 所需的 ctypes 原型与结构体。

    只在 Windows 上调用；任何失败都向上抛，由调用方记录日志（绝不静默吞掉）。
    注意 LocalFree 由 kernel32 导出，advapi32 并不导出它 —— 旧实现写成
    advapi32.LocalFree，即使修好 PVOID 也会在设置 argtypes 时再抛一次
    AttributeError。
    """
    import ctypes
    from ctypes import wintypes

    lpvoid = wintypes.LPVOID  # wintypes 没有 PVOID，只有 LPVOID
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, lpvoid,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, lpvoid, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG, lpvoid, lpvoid, ctypes.POINTER(lpvoid),
    ]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        lpvoid, lpvoid, lpvoid, lpvoid,
    ]
    advapi32.SetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(lpvoid), ctypes.POINTER(lpvoid),
        ctypes.POINTER(lpvoid), ctypes.POINTER(lpvoid),
        ctypes.POINTER(lpvoid),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        lpvoid, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [lpvoid, wintypes.DWORD, ctypes.POINTER(lpvoid)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [lpvoid, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", lpvoid),
            ("MultipleTrusteeOperation", wintypes.DWORD),
            ("TrusteeForm", wintypes.DWORD),
            ("TrusteeType", wintypes.DWORD),
            # TrusteeForm=TRUSTEE_IS_SID 时这里放的是 PSID 而不是字符串，
            # 因此声明为 LPVOID（旧实现声明 LPWSTR 再 cast，容易误导）。
            ("ptstrName", lpvoid),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", wintypes.DWORD),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", lpvoid), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    class ACL_HEADER(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD), ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER), ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    return {
        "ctypes": ctypes, "wintypes": wintypes, "lpvoid": lpvoid,
        "advapi32": advapi32, "kernel32": kernel32,
        "TRUSTEE_W": TRUSTEE_W, "EXPLICIT_ACCESS_W": EXPLICIT_ACCESS_W,
        "TOKEN_USER": TOKEN_USER, "ACL_HEADER": ACL_HEADER,
        "ACCESS_ALLOWED_ACE": ACCESS_ALLOWED_ACE,
    }


def _current_user_sid(api: dict) -> tuple:
    """取当前进程 token 的用户 SID。

    比「读文件属主」更准确：管理员账户创建的文件属主可能是 Administrators 组，
    那样加固出来的 ACL 就不是「仅当前用户」。SID 内存随 buffer 生命周期，
    调用方必须在 buffer 存活期间用完，故一并返回 buffer。
    """
    ctypes = api["ctypes"]
    wintypes = api["wintypes"]
    advapi32 = api["advapi32"]
    kernel32 = api["kernel32"]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(f"OpenProcessToken 失败: {ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)
        ):
            raise OSError(f"GetTokenInformation 失败: {ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(token)
    sid = ctypes.cast(buffer, ctypes.POINTER(api["TOKEN_USER"])).contents.User.Sid
    return sid, buffer


def _open_for_acl(api: dict, path: str | Path, access: int):
    """以不跟随重解析点的方式打开句柄；目录需要 BACKUP_SEMANTICS。"""
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if Path(path).is_dir():
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel32.CreateFileW(str(path), access, 0, None, _OPEN_EXISTING, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        raise OSError(f"CreateFileW 失败: {ctypes.get_last_error()}: {path}")
    return handle


def restrict_windows_acl(path: str | Path) -> bool:
    """把 path 的 DACL 收紧为「仅当前用户完全控制」，并阻断继承。

    实现要点：
    - 用 SetEntriesInAclW 构造一条显式 ACE（当前用户 / FILE_ALL_ACCESS）。
    - 用 PROTECTED_DACL_SECURITY_INFORMATION 让 DACL 受保护，父目录的继承 ACE
      被剥离；这正是旧实现 docstring 承诺、但从未生效的部分。
    - **绝不能传 NULL DACL**：Microsoft 明确说明 NULL DACL 授予所有本地用户完全
      访问，会把保护反转成放开（本项目历史上出过这个严重漏洞）。
    - 目录额外带 (OI)(CI) 继承标志，使后续新建的文件自动继承收紧后的权限。

    非 Windows 平台直接返回 False（不适用）。失败记 WARNING 并返回 False，
    调用方据此决定是否升级为更醒目的告警 —— 但绝不静默当作成功。
    """
    if not is_windows():
        return False
    try:
        api = _win_acl_api()
        ctypes = api["ctypes"]
        advapi32 = api["advapi32"]
        kernel32 = api["kernel32"]
        sid, _sid_buffer = _current_user_sid(api)
        handle = _open_for_acl(api, path, _WRITE_DAC | _READ_CONTROL)
        try:
            entry = api["EXPLICIT_ACCESS_W"]()
            entry.grfAccessPermissions = _FILE_ALL_ACCESS
            entry.grfAccessMode = _GRANT_ACCESS
            entry.grfInheritance = (
                _SUB_CONTAINERS_AND_OBJECTS_INHERIT
                if Path(path).is_dir()
                else _NO_INHERITANCE
            )
            entry.Trustee.pMultipleTrustee = None
            entry.Trustee.MultipleTrusteeOperation = 0
            entry.Trustee.TrusteeForm = _TRUSTEE_IS_SID
            entry.Trustee.TrusteeType = _TRUSTEE_IS_USER
            entry.Trustee.ptstrName = sid
            new_dacl = api["lpvoid"]()
            # OldAcl=None：从零构造，不合并任何既有 ACE。
            code = advapi32.SetEntriesInAclW(
                1, ctypes.byref(entry), None, ctypes.byref(new_dacl)
            )
            if code != 0 or not new_dacl:
                raise OSError(f"SetEntriesInAclW 失败: {code}")
            try:
                code = advapi32.SetSecurityInfo(
                    handle, _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None, None, new_dacl, None,
                )
                if code != 0:
                    raise OSError(f"SetSecurityInfo 失败: {code}")
            finally:
                kernel32.LocalFree(new_dacl)
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        logger.warning(
            "Windows ACL 加固失败（文件仍可能被同机其他账户访问）: %s: %s", path, exc
        )
        return False
    return True


def windows_dacl_summary(path: str | Path) -> dict | None:
    """读回 DACL 供验证/测试断言，非 Windows 或失败返回 None。

    返回 {"protected": bool, "null_dacl": bool, "ace_count": int,
    "inherited_ace_count": int, "trustee_sids": [str, ...]}。测试必须断言这些
    **实际效果**，而不是「函数没抛异常」—— 旧的 no-op 正是因为只有后者才潜伏至今。
    """
    if not is_windows():
        return None
    try:
        api = _win_acl_api()
        ctypes = api["ctypes"]
        wintypes = api["wintypes"]
        advapi32 = api["advapi32"]
        kernel32 = api["kernel32"]
        lpvoid = api["lpvoid"]
        handle = _open_for_acl(api, path, _READ_CONTROL)
        try:
            dacl = lpvoid()
            descriptor = lpvoid()
            code = advapi32.GetSecurityInfo(
                handle, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
                None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
            )
            if code != 0:
                raise OSError(f"GetSecurityInfo 失败: {code}")
            try:
                control = wintypes.WORD()
                revision = wintypes.DWORD()
                if not advapi32.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                ):
                    raise OSError(
                        f"GetSecurityDescriptorControl 失败: {ctypes.get_last_error()}"
                    )
                protected = bool(control.value & _SE_DACL_PROTECTED)
                if not dacl:
                    # NULL DACL：所有人完全访问。必须能被测试识别出来。
                    return {
                        "protected": protected, "null_dacl": True,
                        "ace_count": 0, "inherited_ace_count": 0, "trustee_sids": [],
                    }
                header = ctypes.cast(dacl, ctypes.POINTER(api["ACL_HEADER"])).contents
                sids: list[str] = []
                inherited = 0
                sid_offset = api["ACCESS_ALLOWED_ACE"].SidStart.offset
                for index in range(header.AceCount):
                    ace = lpvoid()
                    if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                        continue
                    typed = ctypes.cast(
                        ace, ctypes.POINTER(api["ACCESS_ALLOWED_ACE"])
                    ).contents
                    if typed.Header.AceFlags & _INHERITED_ACE:
                        inherited += 1
                    sid_ptr = ctypes.cast(ctypes.addressof(typed) + sid_offset, lpvoid)
                    text = wintypes.LPWSTR()
                    if advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
                        try:
                            sids.append(str(text.value))
                        finally:
                            kernel32.LocalFree(text)
                return {
                    "protected": protected, "null_dacl": False,
                    "ace_count": int(header.AceCount),
                    "inherited_ace_count": inherited, "trustee_sids": sids,
                }
            finally:
                kernel32.LocalFree(descriptor)
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        logger.debug("读取 DACL 失败: %s: %s", path, exc)
        return None


def current_user_sid_string() -> str:
    """当前进程用户的 SID 字符串（S-1-5-...）；非 Windows 或失败返回空串。"""
    if not is_windows():
        return ""
    try:
        api = _win_acl_api()
        ctypes = api["ctypes"]
        wintypes = api["wintypes"]
        sid, _buffer = _current_user_sid(api)
        text = wintypes.LPWSTR()
        if not api["advapi32"].ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return ""
        try:
            return str(text.value)
        finally:
            api["kernel32"].LocalFree(text)
    except Exception:
        return ""


def harden_private_path(path: str | Path) -> bool:
    """跨平台把敏感文件/目录收紧为「仅当前用户可读写」。

    POSIX 走 chmod 0600/0700；Windows 走 restrict_windows_acl（chmod 在
    Windows 上不产生任何访问控制效果）。返回是否真的完成了平台对应的加固。
    """
    target = Path(path)
    if is_windows():
        return restrict_windows_acl(target)
    try:
        os.chmod(target, 0o700 if target.is_dir() else 0o600)
    except OSError as exc:
        logger.warning("chmod 加固失败: %s: %s", target, exc)
        return False
    return True


def wt_escape(text: str) -> str:
    """为 Windows Terminal 转义参数中的分号。

    wt.exe 会对**自己的命令行**再解析一次，`;` 是它的子命令（新 pane/tab）分隔符。
    Python 的 list2cmdline 只保证参数原样传到 wt.exe，管不住 wt 的二次解析。

    实测（本机 Windows 11 + wt.exe，子进程回写 sys.argv 验证）：
        wt -d <dir> python probe.py "a;b" "c d"     -> 子进程只收到 ["a"]
        wt -d <dir> -- python probe.py "a;b" "c d"   -> 仍然只收到 ["a"]（-- 无效）
        wt new-tab -d <dir> -- python probe.py ...   -> 仍然只收到 ["a"]
        wt -d <dir> python probe.py "a\\;b" "c d"     -> 正确收到 ["a;b", "c d"]
    也就是说 `--` 分隔符在 wt 上不起作用，只有反斜杠转义有效。

    同一实测确认转义不会损坏其他内容：反斜杠 Windows 路径、含空格的参数、
    多行 --append-system-prompt、以 `--` 开头的参数都原样送达。
    """
    return text.replace(";", "\\;")


def platform_name() -> str:
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    if is_linux():
        return "linux"
    return sys.platform


def subprocess_no_window_kwargs() -> dict:
    if is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def open_path(path: str | Path, *, select_if_file: bool = False) -> bool:
    p = Path(path).expanduser()
    if not p.exists():
        try:
            if p.suffix:
                p.parent.mkdir(parents=True, exist_ok=True)
            else:
                p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建路径失败: %s: %s", p, exc)
            return False

    if is_windows():
        try:
            if select_if_file and p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p if p.exists() else p.parent))  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning("打开路径失败: %s: %s", p, exc)
            return False
        return True

    if is_macos():
        try:
            if select_if_file and p.is_file():
                subprocess.Popen(["open", "-R", str(p)])
            else:
                target = p if p.exists() else p.parent
                subprocess.Popen(["open", str(target)])
        except OSError as exc:
            logger.warning("打开路径失败: %s: %s", p, exc)
            return False
        return True

    target = p if p.is_dir() or not p.exists() else (p.parent if select_if_file and p.is_file() else p)
    target_s = str(target if target.exists() else p.parent)
    for args in (["xdg-open", target_s], ["gio", "open", target_s]):
        try:
            subprocess.Popen(args)
            return True
        except FileNotFoundError:
            continue
    logger.warning("未找到 xdg-open 或 gio，无法打开路径: %s", target_s)
    return False


def which_many(names: Iterable[str]) -> str | None:
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def npm_global_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()

    if is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            roots.append(Path(appdata) / "npm")
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            roots.append(Path(local) / "npm")
    else:
        roots.extend(
            [
                home / ".npm-global",
                home / ".local",
                Path("/usr/local"),
                Path("/opt/homebrew"),
            ]
        )
        nvm = Path(os.environ.get("NVM_DIR", str(home / ".nvm"))) / "versions" / "node"
        if nvm.exists():
            try:
                for v in sorted(nvm.iterdir(), reverse=True)[:6]:
                    roots.append(v)
            except OSError:
                pass
        roots.append(home / ".volta")

    for key in ("npm_config_prefix", "NPM_CONFIG_PREFIX", "PREFIX"):
        val = os.environ.get(key)
        if val:
            roots.append(Path(val))

    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        s = str(r)
        if s and s not in seen:
            seen.add(s)
            out.append(r)
    return out


def _pi_cli_js_candidates(root: Path, scope: str, name: str) -> list[Path]:
    """npm 全局根下 pi CLI 的候选路径（node_modules 与 lib/node_modules）。"""
    return [
        root / "node_modules" / scope / name / "dist" / "cli.js",
        root / "lib" / "node_modules" / scope / name / "dist" / "cli.js",
    ]


def find_pi_cli_js() -> Path | None:
    packages = (
        ("@earendil-works", "pi-coding-agent"),
        ("@mariozechner", "pi-coding-agent"),
    )
    for root in npm_global_roots():
        for scope, name in packages:
            for c in _pi_cli_js_candidates(root, scope, name):
                if c.is_file():
                    return c
    return None


def _is_safe_executable(path: str) -> bool:
    """POSIX 下校验可执行文件所有权与权限是否安全。

    - other-write 位被设置则视为不安全（他人可改写该可执行文件）。
    - 不属于当前用户、也不属于 root 时视为不安全。
    - 任何异常都返回 True，避免在校验不可用时阻塞主流程。
    """
    if is_windows():
        return True
    try:
        st = os.stat(path, follow_symlinks=False)
        if st.st_mode & stat.S_IWOTH:
            return False
        uid = os.getuid()
        if st.st_uid not in (uid, 0):
            return False
    except Exception:
        return True
    return True


def find_pi_command() -> str | None:
    which = shutil.which("pi")
    if which:
        if is_windows() and Path(which).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            # npm's Windows shims require a command shell, which re-parses
            # prompts and system messages containing newlines or metacharacters.
            # Execute the package CLI with Node directly whenever available.
            cli = find_pi_cli_js()
            node = shutil.which("node")
            if cli is not None and node:
                return f"NODECLI::{node}::{cli}"
        if not _is_safe_executable(which):
            logger.warning("跳过不安全的 pi 可执行文件（其他用户可写或非本用户拥有）: %s", which)
        else:
            return which

    for root in npm_global_roots():
        candidates = []
        if is_windows():
            candidates = [root / "pi.cmd", root / "pi.ps1", root / "pi"]
        else:
            candidates = [root / "bin" / "pi", root / "pi"]
        for p in candidates:
            if p.is_file():
                ps = str(p)
                if not _is_safe_executable(ps):
                    logger.warning("跳过不安全的 pi 可执行文件（其他用户可写或非本用户拥有）: %s", ps)
                    continue
                return ps

    cli = find_pi_cli_js()
    if cli is not None:
        node = shutil.which("node") or "node"
        return f"NODECLI::{node}::{cli}"
    return None


def list_terminal_options() -> list[tuple[str, str]]:
    if is_windows():
        return [
            ("auto", "自动"),
            ("wt", "Windows Terminal"),
            ("powershell", "PowerShell"),
            ("cmd", "命令提示符 cmd"),
        ]
    if is_macos():
        return [
            ("auto", "自动"),
            ("terminal", "终端.app"),
            ("iterm", "iTerm2（若已安装）"),
        ]
    return [
        ("auto", "自动"),
        ("xdg", "系统默认终端"),
        ("gnome", "GNOME Terminal"),
        ("konsole", "Konsole"),
        ("xterm", "xterm"),
    ]


def _linux_terminal_prefix(mode: str = "auto") -> tuple[str, list[str]] | None:
    ordered: list[tuple[str, list[str]]] = []
    if mode == "gnome":
        # gnome-terminal uses "--"; kgx (GNOME Console, its modern successor)
        # also uses "--" — it does NOT support "-e", which would swallow the
        # flag silently and never start the pi session.
        ordered = [("gnome-terminal", ["--"]), ("kgx", ["--"])]
    elif mode == "konsole":
        ordered = [("konsole", ["-e"])]
    elif mode == "xterm":
        ordered = [("xterm", ["-e"])]
    else:
        ordered = [
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("kgx", ["--"]),
            ("konsole", ["-e"]),
            ("xfce4-terminal", ["-e"]),
            ("mate-terminal", ["-e"]),
            ("tilix", ["-e"]),
            ("alacritty", ["-e"]),
            ("kitty", ["-e"]),
            ("xterm", ["-e"]),
        ]
    for name, extra in ordered:
        path = shutil.which(name)
        if path:
            return name, [path, *extra]
    return None


def launch_in_terminal(
    argv: list[str],
    workdir: str,
    terminal: str = "auto",
    env: dict[str, str] | None = None,
) -> str:
    from .core import proxy_reachable, strip_pyinstaller_runtime_env

    workdir = str(Path(workdir).expanduser())
    Path(workdir).mkdir(parents=True, exist_ok=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
        # Empty-string values explicitly remove a variable.
        for key, value in env.items():
            if value == "":
                full_env.pop(key, None)
    # A configured-but-stopped proxy makes every spawned session fail with
    # "Connection error"; drop unreachable proxy vars so the child connects
    # directly instead.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = full_env.get(var)
        if value and not proxy_reachable(value):
            full_env.pop(var, None)
    # Frozen onefile bookkeeping must not leak into pi / terminals; a later
    # PiManager.exe helper would then fail parent-exe validation.
    full_env = strip_pyinstaller_runtime_env(full_env)
    mode = (terminal or "auto").lower()
    if is_windows():
        return _launch_windows(argv, workdir, mode, full_env)
    if is_macos():
        return _launch_macos(argv, workdir, mode, full_env)
    return _launch_linux(argv, workdir, mode, full_env)


def _launch_windows(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    def cmd_quote(a: str) -> str:
        if not a:
            return '""'
        if any(ch in a for ch in ' \t"&<>|^') or "@" in a:
            return '"' + a.replace('"', '""') + '"'
        return a

    def ps_quote(a: str) -> str:
        return "'" + a.replace("'", "''") + "'"

    cmdline_cmd = " ".join(cmd_quote(x) for x in argv)
    cmdline_ps = "& " + " ".join(ps_quote(x) for x in argv)

    if mode == "auto":
        wt = shutil.which("wt")
        wt_path = wt or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe")
        mode = "wt" if wt_path and Path(wt_path).exists() else "cmd"

    if mode in {"wt", "windows-terminal"}:
        wt = shutil.which("wt") or str(
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
        )
        if wt and Path(wt).exists():
            # Pass each Pi argument directly to Windows Terminal. Going through
            # cmd /k corrupts quoted paths and multiline system prompts.
            subprocess.Popen(
                [wt, "-d", wt_escape(workdir), *(wt_escape(a) for a in argv)],
                cwd=workdir,
                env=env,
            )
            return f"Windows Terminal: {cmdline_cmd}"
        mode = "cmd"

    if mode == "cmd":
        # CREATE_NEW_CONSOLE provides the requested terminal without another
        # shell parsing pass. This also works when Windows delegates consoles
        # to Windows Terminal. We launch argv directly (no `cmd /c` wrapper) so
        # quoted paths and multiline system prompts are never re-parsed by a
        # nested shell. The console closes when pi exits; callers that need a
        # persistent window should use PowerShell mode (which passes -NoExit).
        subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return f"cmd: {cmdline_cmd}"

    ps = (
        f"Set-Location -LiteralPath {ps_quote(workdir)}; "
        f"Write-Host 'Starting Pi...' -ForegroundColor Cyan; "
        f"{cmdline_ps}"
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoExit",
            "-NoProfile",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-Command",
            ps,
        ],
        cwd=workdir,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    return f"PowerShell: {cmdline_ps}"


def _applescript_string(text: str) -> str:
    """Render a strict AppleScript string literal (repr is not a safe stand-in)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _launch_macos(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    changed_env = {
        key: value for key, value in env.items() if os.environ.get(key) != value
    }
    if changed_env:
        # Terminal.app may be an already-running app and therefore not inherit
        # the caller's environment. Keep the secret out of AppleScript and the
        # visible command line: a 0600 wrapper (invoked via sh, no exec bit
        # needed) self-deletes on start.
        # Use a private directory under the user config tree (~/.pi/agent/.tmp)
        # instead of the shared /tmp, where directory names could be enumerated
        # by other local users even with 0700 permissions.
        tmp_base = Path(os.path.expanduser("~")) / ".pi" / "agent" / ".tmp"
        try:
            tmp_base.mkdir(parents=True, exist_ok=True)
            private_dir = Path(tempfile.mkdtemp(prefix="pi-manager-", dir=str(tmp_base)))
        except Exception:
            # 创建私有基础目录失败（权限、跨平台差异等）时，回退到系统默认临时目录，
            # 不阻塞终端启动；后续 wrapper 仍受 O_EXCL / 0600 保护。
            private_dir = Path(tempfile.mkdtemp(prefix="pi-manager-"))
        wrapper = private_dir / "wrapper.sh"
        fd = os.open(str(wrapper), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # mkstemp already creates 0600; clamp explicitly before writing so
            # no wider umask window exists, and never follow symlinks.
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            lines = ["#!/bin/sh", "set -eu"]
            for key, value in changed_env.items():
                if not key or not key.replace("_", "").isalnum():
                    continue
                lines.append(f"export {key}={shlex.quote(value)}")
            # Terminal.app may already be running with leaked _PYI_* from a
            # previous frozen launch; the wrapper only exports diffs, so
            # explicitly drop bootloader bookkeeping.
            for key in os.environ:
                if key.startswith("_PYI_") and key.replace("_", "").isalnum():
                    lines.append(f"unset {key}")
            lines.extend(
                [
                    'rm -f -- "$0" 2>/dev/null || true',
                    f'rmdir -- {shlex.quote(str(private_dir))} 2>/dev/null || true',
                    f"cd {shlex.quote(workdir)}",
                    "exec " + " ".join(shlex.quote(a) for a in argv),
                    "",
                ]
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(wrapper, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            wrapper.unlink(missing_ok=True)
            # Path.rmdir 从来没有 missing_ok 参数（截至 3.13 仍是 rmdir(self)）。
            # 之前写成 rmdir(missing_ok=True) 会在这个异常处理器里抛 TypeError，
            # 把原始失败原因（磁盘满 / 无权限 / 沙箱）整个替换掉，raise 永不执行，
            # 而且 private_dir 目录还会残留。清理失败绝不能覆盖原始异常。
            try:
                private_dir.rmdir()
            except OSError:
                pass
            raise
        # The wrapper self-deletes when it runs; if the terminal launch fails
        # it never runs, so a detached janitor removes the secret-bearing file
        # shortly afterwards either way.
        try:
            subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 10; rm -f -- {shlex.quote(str(wrapper))}; rmdir -- {shlex.quote(str(private_dir))} 2>/dev/null || true"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        cmd = "sh " + shlex.quote(str(wrapper))
    else:
        cmd = "cd " + shlex.quote(workdir) + " && " + " ".join(shlex.quote(a) for a in argv)
    cmd_keep = cmd + "; echo; echo '[Pi Manager] session ended — press enter to close'; read _"

    if mode == "iterm":
        script = (
            'tell application "iTerm"\n'
            "  if (count of windows) = 0 then create window with default profile\n"
            "  tell current session of current window\n"
            f"    write text {_applescript_string(cmd_keep)}\n"
            "  end tell\n"
            "  activate\n"
            "end tell"
        )
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                env=env,
            )
            if r.returncode == 0:
                return f"iTerm2: {cmd}"
        except Exception:
            pass
        mode = "terminal"

    script = f'tell application "Terminal" to do script {_applescript_string(cmd_keep)}'
    subprocess.Popen(["osascript", "-e", script], env=env)
    try:
        subprocess.Popen(
            ["osascript", "-e", 'tell application "Terminal" to activate'],
            env=env,
        )
    except Exception:
        pass
    return f"Terminal.app: {cmd}"


def _launch_linux(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    cmd = "cd " + shlex.quote(workdir) + " && " + " ".join(shlex.quote(a) for a in argv)
    inner = cmd + "; echo; echo '[Pi Manager] session ended — press enter to close'; read _"
    bash_cmd = ["bash", "-lc", inner]

    found = _linux_terminal_prefix(mode if mode not in {"auto", "xdg", "system"} else "auto")
    if found:
        name, prefix = found
        subprocess.Popen(prefix + bash_cmd, cwd=workdir, env=env)
        return f"{name}: {cmd}"

    subprocess.Popen(argv, cwd=workdir, env=env, start_new_session=True)
    return f"detached: {cmd}"
