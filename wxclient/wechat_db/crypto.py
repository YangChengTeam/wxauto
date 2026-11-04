"""微信 4.x 本地数据库：只读取密钥、只读解密（不注入、不写盘、不改微信任何东西）。

移植自劳小智仓 ``lxz_yingshen/scripts/runtime/wechat_local_probe.py``（映身的本地读取后端），
其布局 / 密钥匹配基于 328336690/wechat-decrypt 44427c4，运行时密钥定位改编自
TANGandXUE/wcdb-key-tool 79f1b5b（MIT）。

做的事只有两件，都是**只读**的（AGENTS.md §5.3 写明允许、仍禁注入/伪装进程）：

- ``scan_keys``：``ReadProcessMemory`` 读 Weixin.exe 内存，找出能通过页校验的 SQLCipher 密钥；
- ``decrypt_database_bytes``：按页 HMAC 校验 + AES 解密成内存里的 SQLite 镜像，WAL 只取已提交帧。

★ 密钥、明文**绝不**落盘、绝不进日志；日志只有计数。
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import struct
import time
from ctypes import wintypes as wt
from pathlib import Path

import structlog
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_log = structlog.get_logger(__name__)

PAGE = 4096

def verify_page(key, page, number, salt):
    if len(key) != 32 or len(page) != PAGE or len(salt) != 16:
        return False
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 58 for v in salt), 2, 32)
    start = 16 if number == 1 else 0
    actual = hmac.new(mac_key, page[start:4032] + struct.pack('<I', number), 'sha512').digest()
    return hmac.compare_digest(actual, page[4032:])

def committed_wal_pages(data):
    if not data:
        return {}, None
    if len(data) < 32:
        raise ValueError('Incomplete WAL header')
    magic, version, size = struct.unpack_from('>III', data)
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != PAGE:
        raise ValueError('Unsupported WAL format')
    order = '<' if magic == 0x377f0682 else '>'
    def checksum(block, seed=(0, 0)):
        first, second = seed
        words = struct.unpack(order + 'I' * (len(block) // 4), block)
        for i in range(0, len(words), 2):
            first = (first + words[i] + second) & 0xffffffff
            second = (second + words[i+1] + first) & 0xffffffff
        return first, second
    state = checksum(data[:24])
    if state != struct.unpack_from('>II', data, 24):
        raise ValueError('WAL header checksum failed')
    pending, committed, database_size = {}, {}, None
    for offset in range(32, len(data) - 4119, 4120):
        header, page = data[offset:offset+24], data[offset+24:offset+4120]
        number, commit = struct.unpack_from('>II', header)
        if header[8:16] != data[16:24]:
            break  # stale frames from an earlier WAL generation
        state = checksum(header[:8] + page, state)
        if state != struct.unpack_from('>II', header, 16) or number < 1:
            raise ValueError('WAL frame checksum failed')
        pending[number] = bytes(page)
        if commit:
            committed.update(pending)
            pending.clear()
            database_size = commit
    return {n: p for n, p in committed.items() if n <= database_size}, database_size

def decrypt_database_bytes(source, key) -> bytes:
    """Return an authenticated snapshot without writing plaintext to disk."""
    source = Path(source)
    if source.stat().st_size == 0 or source.stat().st_size % PAGE:
        raise ValueError('Invalid database page length')
    wal = Path(str(source) + '-wal')
    before = (source.stat().st_mtime_ns, source.stat().st_size, wal.stat().st_mtime_ns if wal.exists() else 0)
    frames, committed_size = committed_wal_pages(wal.read_bytes() if wal.exists() else b'')
    image = bytearray()
    with source.open('rb') as reader:
        salt = reader.read(16)
        reader.seek(0)
        total = committed_size or source.stat().st_size // PAGE
        for number in range(1, total + 1):
            original = reader.read(PAGE)
            page = frames.get(number, original)
            if not verify_page(key, page, number, salt):
                raise ValueError('Database page authentication failed')
            decoder = Cipher(algorithms.AES(key), modes.CBC(page[4016:4032])).decryptor()
            body = decoder.update(page[16 if number == 1 else 0:4016]) + decoder.finalize()
            image.extend((b'SQLite format 3' + bytes(1) if number == 1 else b'') + body + bytes(80))
    after = (source.stat().st_mtime_ns, source.stat().st_size, wal.stat().st_mtime_ns if wal.exists() else 0)
    if before != after:
        raise ValueError('Database changed during snapshot; retry')
    return bytes(image)


class ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wt.DWORD), ('usage', wt.DWORD), ('pid', wt.DWORD),
                ('heap', ctypes.c_size_t), ('module', wt.DWORD), ('threads', wt.DWORD),
                ('parent', wt.DWORD), ('priority', wt.LONG), ('flags', wt.DWORD), ('exe', wt.WCHAR * 260)]

class MemoryInfo(ctypes.Structure):
    _fields_ = [('base', ctypes.c_void_p), ('allocation', ctypes.c_void_p),
                ('allocation_protect', wt.DWORD), ('region_size', ctypes.c_size_t),
                ('state', wt.DWORD), ('protect', wt.DWORD), ('type', wt.DWORD)]

def scan_runtime_cipher(regions, read, pages):
    # Adapted from TANGandXUE/wcdb-key-tool 79f1b5b (MIT).
    name = b'com.Tencent.WCDB.Config.Cipher'
    mask = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
    def chunks():
        for base, size in regions:
            for offset in range(0, size, 1024 * 1024):
                data = read(base + offset, min(1024 * 1024 + 128, size - offset))
                if data:
                    yield base + offset, data
    names = set()
    for base, data in chunks():
        pos = data.find(name)
        while pos >= 0:
            names.add(base + pos)
            pos = data.find(name, pos + 1)
    found, seen = {}, set()
    for base, data in chunks() if names else []:
        for address in names:
            pattern = struct.pack('<QQ', address, len(name))
            pos = data.find(pattern)
            while pos >= 0:
                node = read(base + pos - 16, 64)
                if len(node) >= 48:
                    pointer = struct.unpack_from('<Q', node, 40)[0]
                    if 65536 <= pointer < 0x800000000000 and pointer not in seen:
                        seen.add(pointer)
                        obj = read(pointer + 136, 24)
                        if len(obj) == 24:
                            blob_ptr, length = struct.unpack_from('<QQ', obj, 8)
                            if 0 < length <= 1024 and 65536 <= blob_ptr < 0x800000000000:
                                blob = read(blob_ptr, length)
                                decoded = bytes(v ^ mask[i % len(mask)] for i, v in enumerate(blob))
                                for match in re.finditer(rb"[xX]'([0-9a-fA-F]{64,192})'", decoded):
                                    token = match.group(1)
                                    key = bytes.fromhex(token[:64].decode())
                                    for salt, page in pages.items():
                                        if salt not in found and verify_page(key, page, 1, salt):
                                            found[salt] = key
                pos = data.find(pattern, pos + 1)
    _log.info('读微信运行时密钥配置', 配置名=len(names), 配置对象=len(seen), 验证通过=len(found))
    return found

def scan_keys(pages, timeout=60):
    if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError('Windows x64 Python is required')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    api.CreateToolhelp32Snapshot.restype = wt.HANDLE
    api.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.CloseHandle.argtypes = [wt.HANDLE]
    api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    api.OpenProcess.restype = wt.HANDLE
    api.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
    api.VirtualQueryEx.restype = ctypes.c_size_t
    api.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError('Cannot enumerate processes')
    pids = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.exe.lower() == 'weixin.exe':
                pids.append(entry.pid)
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    if not pids:
        raise ValueError('请先打开并登录电脑版微信，再点击重新连接。')
    found, examined = {}, set()
    deadline = time.monotonic() + timeout
    pattern = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
    opened = 0
    regions = scanned_bytes = patterns = 0
    for pid in pids:
        handle = api.OpenProcess(0x410, False, pid)
        if not handle:
            continue
        opened += 1
        try:
            address = 0
            info = MemoryInfo()
            runtime_regions = []
            while api.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                base, size = info.base or 0, info.region_size
                if info.state == 0x1000 and not info.protect & 0x100 and info.protect & 0xff in (2, 4, 8, 32, 64, 128):
                    runtime_regions.append((base, size))
                if base + size <= address:
                    break
                address = base + size
            # 读运行时配置这一步每个进程单独计时，不吃后面暴力扫的总时限：真机 2026-11-04 客户端里
            # 主进程这一步被别的启动线程拖过了 60 秒，后面几个进程一读就空，整次扫钥失败。
            runtime_deadline = time.monotonic() + timeout
            def read_runtime(address, length, handle=handle, runtime_deadline=runtime_deadline):  # bind: closure outlives the loop vars
                if time.monotonic() >= runtime_deadline:
                    return b''
                buffer, count = ctypes.create_string_buffer(length), ctypes.c_size_t()
                if api.ReadProcessMemory(handle, address, buffer, length, ctypes.byref(count)):
                    return buffer.raw[:count.value]
                return b''
            found.update(scan_runtime_cipher(runtime_regions, read_runtime, pages))
            if len(found) >= len(pages):
                break  # 全部验证通过（常见：主进程的运行时配置一次就齐），后面的进程不用再开
            address = 0
            while time.monotonic() < deadline and len(found) < len(pages):
                if not api.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                    break
                regions += 1
                base, size = info.base or 0, info.region_size
                if info.state == 0x1000 and not info.protect & 0x100 and info.protect & 0xff in (2, 4, 8, 32, 64, 128):
                    offset, tail = 0, b''
                    while offset < size and time.monotonic() < deadline:
                        length = min(1024 * 1024, size - offset)
                        buffer, count = ctypes.create_string_buffer(length), ctypes.c_size_t()
                        ok = api.ReadProcessMemory(handle, base + offset, buffer, length, ctypes.byref(count))
                        scanned_bytes += count.value
                        data = tail + buffer.raw[:count.value] if ok else b''
                        for match in pattern.finditer(data):
                            patterns += 1
                            token = match.group(1)
                            if token in examined:
                                continue
                            examined.add(token)
                            if len(token) % 2:
                                continue
                            key = bytes.fromhex(token[:64].decode())
                            selected = [bytes.fromhex(token[-32:].decode())] if len(token) >= 96 else list(pages)
                            for salt in selected:
                                if salt in pages and salt not in found and verify_page(key, pages[salt], 1, salt):
                                    found[salt] = key
                        tail = data[-200:]
                        offset += length
                next_address = base + size
                if next_address <= address:
                    break
                address = next_address
        finally:
            api.CloseHandle(handle)
    if not opened:
        raise PermissionError('Cannot read WeChat process; run this probe with matching Windows permissions')
    _log.info('扫描微信进程内存找数据库密钥', 进程=opened, 内存段=regions, 读取MiB=scanned_bytes // 1048576, 候选=patterns, 验证通过=len(found))
    return found
