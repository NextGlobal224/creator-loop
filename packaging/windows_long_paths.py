"""Before PyInstaller Qt hooks, use extended names for bundled imports/DLLs.

Pinned PyInstaller 6.16's frozen finder caches its bundle roots before user
runtime hooks. Retain its original roots and add the I/O aliases, so PYZ lookup
and extension-module lookup refer to the same owned bundle. Canonical app/data
identities and executable paths stay unchanged; no registry or PATH probing.
"""

import os
import sys


def _extended(path):
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\.\\"):
        raise RuntimeError("Invalid frozen bundle device namespace")
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _configure():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes
    from ctypes import wintypes as w

    import pyimod02_importers

    aliases = pyimod02_importers._TOP_LEVEL_DIRECTORY_PATHS
    for path in list(aliases):
        alias = _extended(path)
        if alias not in aliases:
            aliases.append(alias)
    original = sys._MEIPASS
    bundle = _extended(original)
    # Qt canonicalizes extended plugin paths before LoadLibraryW. When the
    # volume provides an 8.3 name, use that same-directory alias for runtime
    # resources; never create a junction, map a drive or move user files.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel.GetShortPathNameW.restype = w.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetShortPathNameW(bundle, buffer, len(buffer))
    if 0 < length < len(buffer):
        short = buffer.value
        if short.startswith("\\\\?\\UNC\\"):
            short = "\\\\" + short[8:]
        elif short.startswith("\\\\?\\"):
            short = short[4:]
        if len(short) < len(original) and os.path.samefile(bundle, _extended(short)):
            bundle = short
            aliases.append(short)
            aliases.append(_extended(short))
    sys._MEIPASS = bundle
    paths = []
    for path in sys.path:
        paths.append(_bundle_import_path(path, original, bundle))
    sys.path[:] = paths
    sys.path_importer_cache.clear()
    # Keep these handles for the process lifetime; do not alter system settings.
    sys._creator_loop_dll_directories = [os.add_dll_directory(bundle)]


def _bundle_import_path(path, original, bundle):
    if not path or not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, original)
    except ValueError:
        # An entry on another drive is outside the owned bundle.
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    return os.path.join(bundle, relative)


_configure()
