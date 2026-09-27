"""The asset list's search box: name words plus filters like tris>2000 or type:texture."""

import operator
import re


def is_unreadable(stats):
    """Stats of an asset that failed to decode or has nothing in it."""
    if not stats:
        return False
    return stats.get("info") == "?" or stats.get("size") == 0 or stats.get("w") == 0 or stats.get("h") == 0

FILTER_HELP = """Search by name, or add filters (no spaces around the sign):
  tris>1000     triangles (models)
  verts<500     vertices (models)
  size>1mb      data size (kb, mb, gb)
  w>=512 h>=512 width / height (textures, sprites)
  type:model    type (model, texture, sprite, text, audio, file)
Example:  gun tris>2000 size<5mb"""

FILTER_FIELDS = {"tris": "tris", "verts": "verts", "size": "size",
                 "w": "w", "width": "w", "h": "h", "height": "h"}

FILTER_TOKEN = re.compile(r"^([a-z]+)(>=|<=|>|<|=|:)(.+)$", re.I)

FILTER_OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le, "=": operator.eq}

FILTER_UNITS = {"": 1, "k": 1e3, "m": 1e6, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}

TYPE_ALIASES = {"mesh": "model", "tex": "texture", "texture2d": "texture", "textasset": "text",
                "sound": "audio", "other": "file"}

def parse_number(text):
    match = re.fullmatch(r"([\d.]+)\s*([a-z]*)", text.lower())
    if not match or match.group(2) not in FILTER_UNITS:
        raise ValueError(text)
    return float(match.group(1)) * FILTER_UNITS[match.group(2)]

class AssetFilter:
    """Parsed search box text: plain words + field conditions like tris>1000."""

    def __init__(self, text):
        self.words, self.conditions, self.type = [], [], None
        for token in text.split():
            match = FILTER_TOKEN.match(token)
            key = match.group(1).lower() if match else ""
            if match and key == "type":
                value = match.group(3).lower()
                self.type = TYPE_ALIASES.get(value, value)
                continue
            if match and key in FILTER_FIELDS and match.group(2) != ":":
                try:
                    self.conditions.append((FILTER_FIELDS[key], FILTER_OPS[match.group(2)],
                                            parse_number(match.group(3))))
                    continue
                except ValueError:
                    pass
            self.words.append(token.lower())

    def matches(self, name, kind, stats):
        low = name.lower()
        if any(word not in low for word in self.words):
            return False
        if self.type and not kind.startswith(self.type):
            return False
        for stat, op, number in self.conditions:
            value = (stats or {}).get(stat)
            if value is None or not op(value, number):
                return False
        return True
