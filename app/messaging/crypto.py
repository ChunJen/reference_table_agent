"""Callback 加解密 (AES-256-CBC, PKCS#7 block=32)."""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct

from Crypto.Cipher import AES


class ChatCryptoError(Exception):
    pass


class MsgCrypt:
    BLOCK = 32

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key) != 43:
            raise ChatCryptoError("AES key 長度需為 43")
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.receive_id = receive_id

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        parts = sorted([self.token, str(timestamp), str(nonce), encrypt])
        return hashlib.sha1("".join(parts).encode()).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        if self.signature(timestamp, nonce, encrypt) != msg_signature:
            raise ChatCryptoError("簽名驗證失敗")

    def decrypt(self, encrypt: str) -> str:
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt))
        pad = plain[-1]
        if pad < 1 or pad > self.BLOCK:
            raise ChatCryptoError("padding 錯誤")
        plain = plain[:-pad]
        msg_len = socket.ntohl(struct.unpack("I", plain[16:20])[0])
        msg = plain[20:20 + msg_len]
        rid = plain[20 + msg_len:].decode()
        if rid != self.receive_id:
            raise ChatCryptoError("receive_id 不符")
        return msg.decode("utf-8")

    def encrypt(self, text: str, nonce_bytes: bytes | None = None) -> str:
        msg = text.encode("utf-8")
        body = (nonce_bytes or os.urandom(16)) + struct.pack("I", socket.htonl(len(msg))) + msg \
            + self.receive_id.encode()
        pad = self.BLOCK - len(body) % self.BLOCK
        body += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(body)).decode()

    # --- convenience wrappers for the two callback shapes -------------------
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        self.verify(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str, post_xml: str) -> str:
        from .messages import extract_encrypt

        enc = extract_encrypt(post_xml)
        self.verify(msg_signature, timestamp, nonce, enc)
        return self.decrypt(enc)
