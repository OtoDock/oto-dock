"""Colours as the agents write them, one grammar for every format.

Accepted: hex with or without ``#`` (3, 6 or 8 digits; 8 digits are
RRGGBBAA, or AARRGGBB where a format documents ARGB, as XLSX does), the CSS
named colours, ``rgb()``/``rgba()``, ``hsl()``/``hsla()``/``hsv()``, a list
``[r, g, b]`` or ``[r, g, b, a]`` (fractions when every value is within 0..1,
so ``[1, 1, 1]`` is white, else 0-255), and the Office theme slots
(``accent1``..``accent6``, ``dk1 lt1 dk2 lt2 hlink folHlink`` and their
Office names) with an optional tint or shade. Each format maps a theme
colour its own way; a PDF or an image has no theme and refuses one.
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass

from results import Code, OpError, short_repr

# CSS Color Module Level 4 named colours.
CSS_NAMES = {
    "aliceblue": "F0F8FF", "antiquewhite": "FAEBD7", "aqua": "00FFFF", "aquamarine": "7FFFD4",
    "azure": "F0FFFF", "beige": "F5F5DC", "bisque": "FFE4C4", "black": "000000",
    "blanchedalmond": "FFEBCD", "blue": "0000FF", "blueviolet": "8A2BE2", "brown": "A52A2A",
    "burlywood": "DEB887", "cadetblue": "5F9EA0", "chartreuse": "7FFF00", "chocolate": "D2691E",
    "coral": "FF7F50", "cornflowerblue": "6495ED", "cornsilk": "FFF8DC", "crimson": "DC143C",
    "cyan": "00FFFF", "darkblue": "00008B", "darkcyan": "008B8B", "darkgoldenrod": "B8860B",
    "darkgray": "A9A9A9", "darkgreen": "006400", "darkgrey": "A9A9A9", "darkkhaki": "BDB76B",
    "darkmagenta": "8B008B", "darkolivegreen": "556B2F", "darkorange": "FF8C00", "darkorchid": "9932CC",
    "darkred": "8B0000", "darksalmon": "E9967A", "darkseagreen": "8FBC8F", "darkslateblue": "483D8B",
    "darkslategray": "2F4F4F", "darkslategrey": "2F4F4F", "darkturquoise": "00CED1",
    "darkviolet": "9400D3", "deeppink": "FF1493", "deepskyblue": "00BFFF", "dimgray": "696969",
    "dimgrey": "696969", "dodgerblue": "1E90FF", "firebrick": "B22222", "floralwhite": "FFFAF0",
    "forestgreen": "228B22", "fuchsia": "FF00FF", "gainsboro": "DCDCDC", "ghostwhite": "F8F8FF",
    "gold": "FFD700", "goldenrod": "DAA520", "gray": "808080", "green": "008000",
    "greenyellow": "ADFF2F", "grey": "808080", "honeydew": "F0FFF0", "hotpink": "FF69B4",
    "indianred": "CD5C5C", "indigo": "4B0082", "ivory": "FFFFF0", "khaki": "F0E68C",
    "lavender": "E6E6FA", "lavenderblush": "FFF0F5", "lawngreen": "7CFC00", "lemonchiffon": "FFFACD",
    "lightblue": "ADD8E6", "lightcoral": "F08080", "lightcyan": "E0FFFF",
    "lightgoldenrodyellow": "FAFAD2", "lightgray": "D3D3D3", "lightgreen": "90EE90",
    "lightgrey": "D3D3D3", "lightpink": "FFB6C1", "lightsalmon": "FFA07A", "lightseagreen": "20B2AA",
    "lightskyblue": "87CEFA", "lightslategray": "778899", "lightslategrey": "778899",
    "lightsteelblue": "B0C4DE", "lightyellow": "FFFFE0", "lime": "00FF00", "limegreen": "32CD32",
    "linen": "FAF0E6", "magenta": "FF00FF", "maroon": "800000", "mediumaquamarine": "66CDAA",
    "mediumblue": "0000CD", "mediumorchid": "BA55D3", "mediumpurple": "9370DB",
    "mediumseagreen": "3CB371", "mediumslateblue": "7B68EE", "mediumspringgreen": "00FA9A",
    "mediumturquoise": "48D1CC", "mediumvioletred": "C71585", "midnightblue": "191970",
    "mintcream": "F5FFFA", "mistyrose": "FFE4E1", "moccasin": "FFE4B5", "navajowhite": "FFDEAD",
    "navy": "000080", "oldlace": "FDF5E6", "olive": "808000", "olivedrab": "6B8E23",
    "orange": "FFA500", "orangered": "FF4500", "orchid": "DA70D6", "palegoldenrod": "EEE8AA",
    "palegreen": "98FB98", "paleturquoise": "AFEEEE", "palevioletred": "DB7093",
    "papayawhip": "FFEFD5", "peachpuff": "FFDAB9", "peru": "CD853F", "pink": "FFC0CB",
    "plum": "DDA0DD", "powderblue": "B0E0E6", "purple": "800080", "rebeccapurple": "663399",
    "red": "FF0000", "rosybrown": "BC8F8F", "royalblue": "4169E1", "saddlebrown": "8B4513",
    "salmon": "FA8072", "sandybrown": "F4A460", "seagreen": "2E8B57", "seashell": "FFF5EE",
    "sienna": "A0522D", "silver": "C0C0C0", "skyblue": "87CEEB", "slateblue": "6A5ACD",
    "slategray": "708090", "slategrey": "708090", "snow": "FFFAFA", "springgreen": "00FF7F",
    "steelblue": "4682B4", "tan": "D2B48C", "teal": "008080", "thistle": "D8BFD8",
    "tomato": "FF6347", "turquoise": "40E0D0", "violet": "EE82EE", "wheat": "F5DEB3",
    "white": "FFFFFF", "whitesmoke": "F5F5F5", "yellow": "FFFF00", "yellowgreen": "9ACD32",
}

THEME_SLOTS = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4",
               "accent5", "accent6", "hlink", "folHlink")
_THEME_NAMES = {slot.lower(): slot for slot in THEME_SLOTS}
_THEME_NAMES.update({
    "text1": "dk1", "background1": "lt1", "text2": "dk2", "background2": "lt2",
    "hyperlink": "hlink", "followedhyperlink": "folHlink",
})
THEME_KEY = "theme"
TINT_KEY = "tint"
SHADE_KEY = "shade"
_THEME_DICT_KEYS = frozenset({THEME_KEY, TINT_KEY, SHADE_KEY})

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_FUNC_RE = re.compile(r"^(rgba?|hsla?|hsv)\(\s*([^)]*)\)$", re.IGNORECASE)
_PERCENT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")
_FORMS = ("a hex code such as #1F4E79, a CSS name such as navy, rgb(31, 78, 121), "
          "[31, 78, 121], or a theme slot such as accent1")
_HEX_DIGITS_SHORT = 3
_HEX_DIGITS_RGBA = 8
_RGB_FUNCS = frozenset({"rgb", "rgba"})
_HSL_FUNCS = frozenset({"hsl", "hsla"})


@dataclass(frozen=True)
class Color:
    """A parsed colour: an RGB value (ints 0-255) with an optional alpha
    (0..1), or a theme slot with an optional tint or shade (0..1)."""

    rgb: tuple[int, int, int] | None = None
    alpha: float | None = None
    theme: str | None = None
    tint: float | None = None
    shade: float | None = None

    def _require_rgb(self) -> tuple[int, int, int]:
        if self.rgb is None:
            raise OpError(Code.BAD_VALUE, f"the theme colour '{self.theme}' has no fixed RGB value here",
                          hint="Give a hex colour such as #1F4E79.")
        return self.rgb

    @property
    def hex(self) -> str:
        r, g, b = self._require_rgb()
        return f"{r:02X}{g:02X}{b:02X}"

    @property
    def argb(self) -> str:
        alpha = 255 if self.alpha is None else round(self.alpha * 255)
        return f"{alpha:02X}{self.hex}"

    @property
    def fractions(self) -> tuple[float, float, float]:
        r, g, b = self._require_rgb()
        return (r / 255, g / 255, b / 255)


def _bad(value, hint: str = "") -> OpError:
    return OpError(Code.BAD_VALUE, f"{short_repr(value)} is not a colour", hint=hint or f"Give {_FORMS}.")


def _channel_255(text: str, value) -> int:
    pct = _PERCENT_RE.match(text)
    try:
        number = float(pct.group(1)) * 255 / 100 if pct else float(text)
    except ValueError:
        raise _bad(value) from None
    if not 0 <= number <= 255:
        raise _bad(value, "Each channel is 0-255 or 0%-100%.")
    return round(number)


def _unit_fraction(text: str, value, *, percent_only: bool = False) -> float:
    pct = _PERCENT_RE.match(text)
    try:
        number = float(pct.group(1)) / 100 if pct else float(text)
    except ValueError:
        raise _bad(value) from None
    if percent_only and not pct:
        number /= 100
    if not 0 <= number <= 1:
        raise _bad(value, "Saturation, lightness, value and alpha are 0-100% (or 0-1).")
    return number


def _from_hex(digits: str, *, argb: bool) -> Color:
    if len(digits) == _HEX_DIGITS_SHORT:
        digits = "".join(c * 2 for c in digits)
    alpha = None
    if len(digits) == _HEX_DIGITS_RGBA:
        if argb:
            alpha, digits = int(digits[:2], 16) / 255, digits[2:]
        else:
            alpha, digits = int(digits[6:], 16) / 255, digits[:6]
    rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
    return Color(rgb=rgb, alpha=alpha)


def _from_function(name: str, body: str, value) -> Color:
    parts = [p.strip() for p in re.split(r"[,\s/]+", body.strip()) if p.strip()]
    name = name.lower()
    if len(parts) not in (3, 4):
        raise _bad(value)
    alpha = _unit_fraction(parts[3], value) if len(parts) == 4 else None
    if name in _RGB_FUNCS:
        return Color(rgb=tuple(_channel_255(p, value) for p in parts[:3]), alpha=alpha)
    try:
        hue = (float(parts[0].removesuffix("deg")) % 360) / 360
    except ValueError:
        raise _bad(value) from None
    second = _unit_fraction(parts[1], value, percent_only=True)
    third = _unit_fraction(parts[2], value, percent_only=True)
    if name in _HSL_FUNCS:
        r, g, b = colorsys.hls_to_rgb(hue, third, second)
    else:
        r, g, b = colorsys.hsv_to_rgb(hue, second, third)
    return Color(rgb=(round(r * 255), round(g * 255), round(b * 255)), alpha=alpha)


def _from_list(items: list, value) -> Color:
    if len(items) not in (3, 4) or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in items
    ):
        raise _bad(value, "A list colour is [r, g, b] or [r, g, b, a].")
    channels = items[:3]
    alpha = items[3] if len(items) == 4 else None
    if alpha is not None and not 0 <= alpha <= 1:
        raise _bad(value, "Alpha is 0-1.")
    alpha = None if alpha is None else float(alpha)
    if all(0 <= v <= 1 for v in channels):
        return Color(rgb=tuple(round(v * 255) for v in channels), alpha=alpha)
    if all(isinstance(v, int) and 0 <= v <= 255 for v in channels):
        return Color(rgb=tuple(channels), alpha=alpha)
    raise _bad(value, "Give every channel as a fraction 0-1 or every channel as an int 0-255.")


def _from_theme(name: str, value, *, tint=None, shade=None) -> Color:
    slot = _THEME_NAMES.get(name.lower())
    if slot is None:
        raise _bad(value, f"Theme slots are {', '.join(THEME_SLOTS)}.")
    if tint is not None and shade is not None:
        raise _bad(value, "Give a tint or a shade, not both.")
    for amount in (tint, shade):
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))
                                   or not 0 <= amount <= 1):
            raise _bad(value, "A tint or shade is a number from 0 to 1.")
    return Color(theme=slot, tint=None if tint is None else float(tint),
                 shade=None if shade is None else float(shade))


def parse_color(value, *, argb: bool = False, allow_theme: bool = True) -> Color:
    """``value`` as a :class:`Color`, or ``OpError(BAD_VALUE)`` naming the
    accepted forms. ``argb`` reads 8 hex digits as AARRGGBB;
    ``allow_theme=False`` refuses a theme slot."""
    if isinstance(value, Color):
        color = value
    elif isinstance(value, str):
        text = value.strip()
        hex_match = _HEX_RE.match(text)
        func_match = _FUNC_RE.match(text)
        if hex_match:
            color = _from_hex(hex_match.group(1), argb=argb)
        elif func_match:
            color = _from_function(func_match.group(1), func_match.group(2), value)
        elif text.lower() in CSS_NAMES:
            color = _from_hex(CSS_NAMES[text.lower()], argb=False)
        elif text.lower() in _THEME_NAMES:
            color = _from_theme(text, value)
        else:
            raise _bad(value)
    elif isinstance(value, (list, tuple)):
        color = _from_list(list(value), value)
    elif isinstance(value, dict):
        unknown = set(value) - _THEME_DICT_KEYS
        if unknown or THEME_KEY not in value or not isinstance(value[THEME_KEY], str):
            raise _bad(value, 'A theme colour is {"theme": "accent1"} with an optional "tint" or "shade" (0-1).')
        color = _from_theme(value[THEME_KEY], value, tint=value.get(TINT_KEY), shade=value.get(SHADE_KEY))
    else:
        raise _bad(value)
    if color.theme is not None and not allow_theme:
        raise OpError(Code.BAD_VALUE, f"{short_repr(value)} is a theme colour, and only Office documents have a theme",
                      hint="Give a hex colour such as #1F4E79.")
    return color
