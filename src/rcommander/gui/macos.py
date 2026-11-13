"""macOS integration via ctypes (no pyobjc dependency).

When run from a plain Python interpreter, macOS names the app after the
executable ("python3.14") in the Dock, the Cmd-Tab switcher and the menu bar.
We fix that in two steps:

  * before NSApplication starts: patch CFBundleName in the main bundle's
    info dictionary (the menu bar title reads it);
  * after it has checked in with LaunchServices: set the LS display name,
    which is what the Dock and Cmd-Tab show. This is the private
    _LSSetApplicationInformationItem that Chromium uses to name its helper
    processes.

Every step is best-effort. On failure it logs at debug level and the app
keeps the interpreter's name.
"""

import ctypes
import ctypes.util
import logging
import sys

log = logging.getLogger(__name__)

_UTF8 = 0x08000100
_kLSDefaultSessionID = -2


def _libs():
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ls = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreServices.framework/CoreServices")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    return objc, cf, ls


def _msg(objc, restype, *argtypes):
    """A correctly-typed objc_msgSend (needed on arm64: no varargs ABI)."""
    addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(addr)


def _cfstr(cf, s):
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8)


def set_bundle_name(name):
    """Call before QApplication is created."""
    if sys.platform != "darwin":
        return
    try:
        objc, cf, _ = _libs()
        sel = objc.sel_registerName
        send0 = _msg(objc, ctypes.c_void_p)
        bundle = send0(objc.objc_getClass(b"NSBundle"), sel(b"mainBundle"))
        for getter in (b"localizedInfoDictionary", b"infoDictionary"):
            info = send0(bundle, sel(getter))
            if not info:
                continue
            responds = _msg(objc, ctypes.c_bool, ctypes.c_void_p)
            if not responds(info, sel(b"respondsToSelector:"), sel(b"setObject:forKey:")):
                continue
            setter = _msg(objc, None, ctypes.c_void_p, ctypes.c_void_p)
            setter(info, sel(b"setObject:forKey:"), _cfstr(cf, name), _cfstr(cf, "CFBundleName"))
    except Exception as ex:  # cosmetic only
        log.debug("set_bundle_name failed: %s", ex)


def set_display_name(name):
    """Call after QApplication exists (the process has checked in with LS)."""
    if sys.platform != "darwin":
        return False
    try:
        _, cf, ls = _libs()
        get_asn = ls._LSGetCurrentApplicationASN
        get_asn.restype = ctypes.c_void_p
        set_item = ls._LSSetApplicationInformationItem
        set_item.restype = ctypes.c_int32
        set_item.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p]
        key = ctypes.c_void_p.in_dll(ls, "_kLSDisplayNameKey").value
        asn = get_asn()
        if not asn or not key:
            return False
        return set_item(_kLSDefaultSessionID, asn, key, _cfstr(cf, name), None) == 0
    except Exception as ex:
        log.debug("set_display_name failed: %s", ex)
        return False
