"""Native credential protection and a private, persistent Linux data key."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet, InvalidToken


class LoginStorageError(Exception):
    pass


class FileCredentialCipher:
    """Publish one 0600 key atomically; never replace a lost or invalid key."""
    def __init__(self, path, *, create):
        self.path = Path(path)
        try:
            if create and not self.path.exists():
                with NamedTemporaryFile(dir=self.path.parent, prefix='.credentials-') as temporary:
                    temporary.write(Fernet.generate_key())
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    try:
                        os.link(temporary.name, self.path)
                    except FileExistsError:
                        pass  # Another process already published the directory's key.
            self.cipher = Fernet(self.path.read_bytes())
        except (OSError, ValueError) as error:
            raise LoginStorageError('无法读取本机加密密钥 credentials.key；请恢复原密钥并检查目录权限，原数据库已保留。') from error

    def encrypt(self, data):
        return self.cipher.encrypt(data)

    def decrypt(self, data):
        try:
            return self.cipher.decrypt(data)
        except InvalidToken as error:
            raise LoginStorageError('本机加密数据无法解密；请使用对应的 credentials.key，原数据已保留。Windows 登录态需在原用户下读取。') from error


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    if os.environ.get('LINGXI_ANDROID') == '1':
        from java import jclass
        vault = jclass('cn.pppyl.lingxiworkspace.CredentialVault')
        return bytes(vault.decrypt(data) if decrypt else vault.encrypt(data))
    if os.name != 'nt':
        raise LoginStorageError('当前系统不支持原生登录态加密。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(b'lingxi-web/shared-app-login/v1')
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = [ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)]
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = [ctypes.byref(source), 'Lingxi shared APP sessions', ctypes.byref(entropy), None, None, 1, ctypes.byref(output)]
    function.restype = wintypes.BOOL
    if not function(*args):
        raise LoginStorageError('无法读取本机加密登录态，请使用原 Windows 用户登录或重新验证账号。' if decrypt
                                else '本机登录态加密失败，账号暂时仅保留在内存中。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, wintypes.HLOCAL))
