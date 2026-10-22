"""本机敏感信息加密：API Key 落盘前用 Windows DPAPI 加密（绑定当前用户，其他账户无法解密）

- Windows：crypt32 的 CryptProtectData/CryptUnprotectData，无第三方依赖；
- 非 Windows 或加解密失败：回退明文存储（仅本机自用场景）。
- 密文统一加 "dpapi:" 前缀，便于区分明文旧数据并自动迁移。
"""

import base64
import os

_PREFIX = "dpapi:"


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_value(plain: str) -> str:
    """加密字符串；失败时原样返回（不阻塞保存）"""
    if not plain or is_encrypted(plain):
        return plain
    if os.name != "nt":
        return plain
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
            return plain
        try:
            cipher = ctypes.string_at(dst.pbData, dst.cbData)
            return _PREFIX + base64.b64encode(cipher).decode("ascii")
        finally:
            if dst.pbData:
                kernel32.LocalFree(ctypes.c_void_p(dst.pbData))
    except Exception as error:  # noqa: BLE001 —— 加密失败不阻塞配置保存
        print(f"[SecretBox] 加密失败，本次按明文保存：{error}")
        return plain


def decrypt_value(value: str) -> str:
    """解密字符串；非密文原样返回（兼容明文旧数据），解密失败返回空串"""
    if not is_encrypted(value):
        return value
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
