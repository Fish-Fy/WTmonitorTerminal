"""Lossless 14-bit CJK transport for existing checksummed share packets."""
import base64
from ..i18n import tr

PREFIX = "WT5."
START = 0x4E00
BITS = 14
MASK = (1 << BITS) - 1


def encode(ascii_code):
    token = ascii_code[4:]
    packet = bytes([int(ascii_code[2])]) + base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
    padding = (-len(packet) * 8) % BITS
    chars = [chr(START + padding)]
    buffer = count = 0
    for byte in packet:
        buffer = (buffer << 8) | byte
        count += 8
        if count >= BITS:
            count -= BITS
            chars.append(chr(START + (buffer >> count)))
            buffer &= (1 << count) - 1
    if count:
        chars.append(chr(START + (buffer << padding)))
    return PREFIX + ''.join(chars)


def decode(code):
    values = [ord(char) - START for char in code[4:]]
    if len(values) < 2 or not 0 <= values[0] < BITS or any(not 0 <= v <= MASK for v in values[1:]):
        raise ValueError(tr('汉字分享码含有无效字符'))
    padding = values[0]
    if ((len(values) - 1) * BITS - padding) % 8 or values[-1] & ((1 << padding) - 1):
        raise ValueError(tr('汉字分享码长度或末尾内容无效'))
    result = bytearray()
    buffer = count = 0
    for value in values[1:]:
        buffer = (buffer << BITS) | value
        count += BITS
        while count >= 8:
            count -= 8
            result.append(buffer >> count)
            buffer &= (1 << count) - 1
    if padding >= 8:
        result.pop()
    if not result or result[0] not in (1, 2, 3, 4):
        raise ValueError(tr('汉字分享码版本无效'))
    return f'WT{result[0]}.' + base64.urlsafe_b64encode(result[1:]).decode('ascii').rstrip('=')
