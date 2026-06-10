"""UTF-8-only encodings registry for minimal static Linux builds.

Only utf-8 (and common aliases) are supported. No locale-based encoding
selection and no other codec modules are loaded.
"""

import codecs as _codecs
from _codecs import _normalize_encoding

from . import aliases as _aliases_module

__all__ = ['CodecRegistryError', 'normalize_encoding', 'search_function']


class CodecRegistryError(LookupError, SystemError):
    pass


def normalize_encoding(encoding):
    if isinstance(encoding, bytes):
        encoding = str(encoding, 'ascii')
    return _normalize_encoding(encoding)


def _is_utf8(name):
    norm = normalize_encoding(name)
    if norm in ('utf_8', 'utf8'):
        return True
    aliased = _aliases_module.aliases.get(norm)
    if aliased is None:
        aliased = _aliases_module.aliases.get(norm.replace('.', '_'))
    return aliased in ('utf_8', 'utf8')


def _utf8_decode(input, errors='strict'):
    return _codecs.utf_8_decode(input, errors, True)


class _IncrementalEncoder(_codecs.IncrementalEncoder):
    def encode(self, input, final=False):
        return _codecs.utf_8_encode(input, self.errors)[0]


class _IncrementalDecoder(_codecs.BufferedIncrementalDecoder):
    _buffer_decode = _codecs.utf_8_decode


class _StreamWriter(_codecs.StreamWriter):
    encode = _codecs.utf_8_encode


class _StreamReader(_codecs.StreamReader):
    decode = _codecs.utf_8_decode


_UTF8_CODEC = _codecs.CodecInfo(
    name='utf-8',
    encode=_codecs.utf_8_encode,
    decode=_utf8_decode,
    incrementalencoder=_IncrementalEncoder,
    incrementaldecoder=_IncrementalDecoder,
    streamreader=_StreamReader,
    streamwriter=_StreamWriter,
)


def search_function(encoding):
    if _is_utf8(encoding):
        return _UTF8_CODEC
    return None


_codecs.register(search_function)
