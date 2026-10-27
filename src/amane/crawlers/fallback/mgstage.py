"""番号 → MGStage 官方图源直链构造.

image.mgstage.com 按 ``{folder}/{cid}/{num}/pb_e_{cid}-{num}.jpg`` (横版大图) 与
``pf_e_...`` (竖版小图) 存放同一部作品; ``cid`` 为品牌编号加系列名, 无法从番号推出,
由系列表提供. 只覆盖素人系列, 未知系列返回空列表.
"""

import re

_IMAGE_BASE = "https://image.mgstage.com/images"

#: 素人系列 → (品牌编号, 目录名).
_SERIES_MAP: dict[str, tuple[str, str]] = {
    "otim": ("393", "onetime"),
    "chuc": ("201", "firststar"),
    "gerk": ("302", "guerrilla"),
    "luxu": ("259", "luxutv"),
    "onez": ("013", "onemore"),
    "onex": ("013", "onemore"),
    "mfc": ("435", "doc"),
    "ara": ("261", "ara"),
}

_DIGIT_PREFIX_RE = re.compile(r"^(\d+)([a-z]+)$")


def _series_entries(number: str) -> list[tuple[str, str, str]]:
    """番号 → [(系列, 品牌编号, 目录名)]; 支持 ``259LUXU-1111`` 与 ``LUXU-1111`` 两种写法."""
    cleaned = number.upper().strip().replace(" ", "")
    head, separator, digits = cleaned.rpartition("-")
    if not separator or not digits.isdigit():
        return []

    heads = [head.lower()]
    digit_prefixed = _DIGIT_PREFIX_RE.match(head.lower())
    if digit_prefixed is not None:
        heads.append(digit_prefixed.group(2))

    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for series in heads:
        entry = _SERIES_MAP.get(series)
        if entry is not None and entry not in seen:
            seen.add(entry)
            entries.append((series, entry[0], entry[1]))
    return entries


def _number_variants(digits: str) -> list[str]:
    """站点目录同时存在补零与去零两种编号写法, 两种都作为候选."""
    stripped = str(int(digits))
    return [digits] if stripped == digits else [digits, stripped]


def _candidates(number: str, prefix: str) -> list[str]:
    _, _, digits = number.upper().strip().replace(" ", "").rpartition("-")
    urls: list[str] = []
    for series, brand_id, folder in _series_entries(number):
        cid = f"{brand_id}{series}"
        for variant in _number_variants(digits):
            url = f"{_IMAGE_BASE}/{folder}/{cid}/{variant}/{prefix}_e_{cid}-{variant}.jpg"
            if url not in urls:
                urls.append(url)
    return urls


def cover_candidates(number: str) -> list[str]:
    """横版大图 (pb_e) 候选."""
    return _candidates(number, "pb")


def poster_candidates(number: str) -> list[str]:
    """竖版小图 (pf_e) 候选."""
    return _candidates(number, "pf")
