"""API keys: Windows DPAPI or an OS keyring reference. Never fall back to plaintext."""

import base64
import os
import uuid

_PREFIX = "dpapi:"
_KEYRING_PREFIX = "keyring:"
_SERVICE = "AI-Agile-Office"


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith((_PREFIX, _KEYRING_PREFIX))


def _keyring_backend():
    """Accept native OS stores only; do not select plaintext third-party backends."""
    try:
        import keyring
        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        for candidate in candidates:
            if type(candidate).__module__ in ("keyring.backends.macOS", "keyring.backends.SecretService", "keyring.backends.kwallet") and candidate.priority > 0:
                return candidate
    except Exception:
        pass
    raise ValueError("系统安全密钥存储不可用。请安装并解锁 macOS Keychain、Secret Service 或 KWallet；不会明文保存 API Key。")


def delete_value(value: str) -> None:
    """Best-effort cleanup of an unreferenced OS keyring entry; DPAPI has no entry."""
    if not value or not value.startswith(_KEYRING_PREFIX):
        return
    try:
        _keyring_backend().delete_password(_SERVICE, value[len(_KEYRING_PREFIX):])
    except Exception:
        # A locked store may retain an orphan reference; never delete config to
        # compensate and never print provider errors that might include secrets.
        pass


def encrypt_value(plain: str) -> str:
    """Protect a key; errors reject the save instead of returning plaintext."""
    if not plain or is_encrypted(plain):
        return plain
    if os.name != "nt":
        backend = _keyring_backend()
        reference = _KEYRING_PREFIX + uuid.uuid4().hex
        try:
            backend.set_password(_SERVICE, reference[len(_KEYRING_PREFIX):], plain)
            if backend.get_password(_SERVICE, reference[len(_KEYRING_PREFIX):]) != plain:
                raise ValueError("keyring read-back failed")
            return reference
        except Exception:
            delete_value(reference)
            raise ValueError("系统密钥存储写入失败，API Key 未保存。请解锁密钥存储后重试。") from None
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

        data = plain.encode("utf-8")
        src = Blob(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p))
        dst = Blob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int  # type: ignore[assignment]
        if not crypt32.CryptProtectData(
            ctypes.byref(src), "office-api-key", None, 0, None, 0, ctypes.byref(dst)
        ):
            raise ValueError("DPAPI encryption failed")
        try:
            cipher = ctypes.string_at(dst.pbData, dst.cbData)
            return _PREFIX + base64.b64encode(cipher).decode("ascii")
        finally:
            if dst.pbData:
                kernel32.LocalFree(ctypes.c_void_p(dst.pbData))
    except Exception:
        raise ValueError("Windows DPAPI 加密失败，API Key 未保存；不会改用明文。") from None


def decrypt_value(value: str) -> str:
    """解密字符串；非密文原样返回（兼容明文旧数据），解密失败返回空串"""
    if not is_encrypted(value):
        return value
    if value.startswith(_KEYRING_PREFIX):
        try:
            return _keyring_backend().get_password(_SERVICE, value[len(_KEYRING_PREFIX):]) or ""
        except Exception:
            return ""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

        cipher = base64.b64decode(value[len(_PREFIX):].encode("ascii"))
        src = Blob(len(cipher), ctypes.cast(ctypes.c_char_p(cipher), ctypes.c_void_p))
        dst = Blob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(Blob), ctypes.POINTER(wintypes.LPCWSTR), ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int  # type: ignore[assignment]
        if not crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, 0, None, 0, ctypes.byref(dst)
        ):
            print("[SecretBox] 解密失败：密文可能属于其他用户，已视为空 Key")
            return ""
        try:
            return ctypes.string_at(dst.pbData, dst.cbData).decode("utf-8")
        finally:
            if dst.pbData:
                kernel32.LocalFree(ctypes.c_void_p(dst.pbData))
    except Exception as error:  # noqa: BLE001
        print(f"[SecretBox] 解密异常：{error}")
        return ""
