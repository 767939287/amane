"""番号 → DMM 官方 CDN 图片直链构造.

awsimgsrc 图床按 ``digital/video/{cid}/{cid}ps.jpg`` (竖版) 与 ``...{cid}pl.jpg`` (横版)
存放同一份图. ``cid`` 由厂牌前缀、系列名与补零编号拼接, 前缀无法从番号推出, 因此由静态
前缀表与学习表提供; 学习表见 `prefix_learn.py`.

只覆盖 digital 路径: mono 老片的 cid 属于独立编号空间, 推导出的前缀会污染新片候选.
"""

import re
from collections.abc import Sequence
from typing import NamedTuple

from ..sites.dmm_cdn import HIGH_RES_CDN, PICS_CDN

_DIGITAL_PATH = "digital/video"

#: 无码番号的 DMM 前缀, 命中即不生成候选 (DMM 只收有码).
_UNCENSORED_PREFIXES = ("FC2", "HEYZO", "1PONDO", "CARIB", "10MUCH", "200GANA", "PACO", "MKD", "MIUM")

#: 厂牌前缀 → 系列名. 空串表示 cid 直接以系列名开头.
_PREFIX_GROUPS: dict[str, list[str]] = {
    "": [
        "adn",
        "bf",
        "cawd",
        "cnd",
        "dasd",
        "dvdms",
        "ebod",
        "eyan",
        "gdhh",
        "hibl",
        "hmn",
        "hnd",
        "hntd",
        "ipit",
        "ipvr",
        "ipx",
        "ipzz",
        "jue",
        "jufd",
        "juk",
        "jul",
        "jux",
        "juy",
        "juq",
        "kawd",
        "meyd",
        "miab",
        "miad",
        "mibd",
        "mide",
        "midv",
        "mifd",
        "mtsp",
        "mudr",
        "mukd",
        "mvsd",
        "mymd",
        "nima",
        "ofje",
        "onsd",
        "pred",
        "rki",
        "sone",
        "sora",
        "ssis",
        "ssni",
        "waaa",
    ],
    "1": [
        "dandy",
        "dism",
        "dldss",
        "dvdes",
        "fcdss",
        "fset",
        "fsdss",
        "gs",
        "hunt",
        "kmhrs",
        "mmgh",
        "rct",
        "rctd",
        "sdab",
        "sdam",
        "sdde",
        "sdjs",
        "sdmf",
        "sdmm",
        "sdms",
        "sdmt",
        "sdmu",
        "sdfk",
        "sdnm",
        "star",
        "stars",
        "start",
        "svdvd",
        "sw",
        "vandr",
    ],
    "3": ["wanz"],
    "13": ["ayb", "gg", "gvg", "gvh", "ovg"],
    "17": ["bkd"],
    "18": ["momj", "ntrd"],
    "41": ["dok"],
    "42": ["sma"],
    "49": ["avop", "madm"],
    "55": ["t28"],
    "77": ["cre"],
    "118": ["onez"],
    "143": ["ppd", "umd"],
    "433": ["mbd"],
    "436": ["abf"],
    "5642": ["hodv"],
    "h_068": ["mxgs"],
    "h_113": ["ggg"],
    "h_205": ["ssnd"],
    "h_491": ["fone"],
    "h_1100": ["hzgd"],
    "h_1240": ["milk"],
    "h_1324": ["skmj"],
    "h_1371": ["zmen"],
    "h_1374": ["ksvr"],
    "h_1454": ["bdsr", "husr"],
    "h_189": ["ymd"],
    "h_237": ["nact"],
    "h_910": ["vrtm"],
    "h_995": ["bokd"],
}

#: 同名系列跨厂商或跨编号段时前缀不同, 作为附加候选.
_EXTRA_PREFIXES: dict[str, list[str]] = {
    "sw": ["h_113"],
    "bdsr": ["57"],
    "husr": ["57"],
    "sma": ["83"],
}

#: 以编号阈值切换前缀的系列: 阈值、阈值以内前缀、阈值以上前缀.
_SPECIAL_THRESHOLDS: dict[str, tuple[int, str, str]] = {
    "avop": (168, "", "1"),
    "gigl": (643, "h_860", ""),
    "ekdv": (655, "49", ""),
}

#: 静态表未收录系列的候选前缀, 按命中概率排序.
_COMMON_PREFIXES: list[str] = ["", "1", "13", "49", "436", "118", "55", "57", "83", "5642"]

#: 系列名本身含数字 (如 t28), 与「字母系列 + 数字编号」的通用形态分开识别.
_DIGIT_SERIES: list[str] = sorted(
    {series for members in _PREFIX_GROUPS.values() for series in members if any(ch.isdigit() for ch in series)},
    key=len,
    reverse=True,
)

_CID_URL_RE = re.compile(r"/digital/video/([^/?#]+)/")
_NUMBER_RE = re.compile(r"^([a-z]+)(\d+)$")


class NumberParts(NamedTuple):
    series: str
    num: int
    padded: str


def parse_number(number: str) -> list[NumberParts]:
    """番号 → (系列, 编号, 五位补零编号); 无法解析时返回空列表."""
    cleaned = number.lower().strip().replace("-", "").replace(" ", "")
    for series in _DIGIT_SERIES:
        if not cleaned.startswith(series):
            continue
        rest = cleaned[len(series) :]
        if rest.isdigit() and rest:
            return [NumberParts(series, int(rest), f"{int(rest):05d}")]
    match = _NUMBER_RE.match(cleaned)
    if match is None:
        return []
    series, digits = match.group(1), match.group(2)
    return [NumberParts(series, int(digits), f"{int(digits):05d}")]


def series_of(number: str) -> str | None:
    """番号的系列名 (小写); 无法解析时返回 None. 学习表按此键查询."""
    parts = parse_number(number)
    return parts[0].series if parts else None


def is_uncensored_number(number: str) -> bool:
    """明显无码的番号不生成 DMM 候选."""
    if "_" in number:
        return True
    return number.upper().replace(" ", "").startswith(_UNCENSORED_PREFIXES)


def prefixes_for(series: str, num: int) -> list[str]:
    """系列在静态表中的候选前缀, 保序去重; 未收录系列返回通用前缀表."""
    extra = _EXTRA_PREFIXES.get(series, [])
    special = _SPECIAL_THRESHOLDS.get(series)
    if special is not None:
        threshold, small, large = special
        return list(dict.fromkeys([small if num <= threshold else large, *extra]))
    for group_prefix, members in _PREFIX_GROUPS.items():
        if series in members:
            return list(dict.fromkeys([group_prefix, "", *extra]))
    return list(dict.fromkeys([*_COMMON_PREFIXES, *extra]))


def cid_candidates(
    number: str,
    *,
    verified: Sequence[str] = (),
    provisional: Sequence[str] = (),
) -> list[str]:
    """番号 → cid 候选, 顺序为学习表已转正前缀、静态表前缀、学习表观察中前缀.

    ``verified`` / ``provisional`` 由调用方按番号系列查学习表取得; 同一番号只解析出一个系列.
    """
    cids: list[str] = []
    seen: set[str] = set()
    for parts in parse_number(number):
        prefix_order = [*verified, *prefixes_for(parts.series, parts.num), *provisional]
        for prefix in prefix_order:
            cid = f"{prefix}{parts.series}{parts.padded}"
            if cid not in seen:
                seen.add(cid)
                cids.append(cid)
    return cids


def poster_candidates(
    number: str,
    *,
    verified: Sequence[str] = (),
    provisional: Sequence[str] = (),
) -> list[str]:
    """竖版高清海报 (ps) 候选."""
    return _image_urls(number, "ps", verified=verified, provisional=provisional)


def cover_candidates(
    number: str,
    *,
    verified: Sequence[str] = (),
    provisional: Sequence[str] = (),
) -> list[str]:
    """横版高清封面 (pl) 候选."""
    return _image_urls(number, "pl", verified=verified, provisional=provisional)


def _image_urls(number: str, suffix: str, *, verified: Sequence[str], provisional: Sequence[str]) -> list[str]:
    base = f"{HIGH_RES_CDN}/{_DIGITAL_PATH}"
    return [
        f"{base}/{cid}/{cid}{suffix}.jpg" for cid in cid_candidates(number, verified=verified, provisional=provisional)
    ]


def cid_from_image_url(url: str) -> str | None:
    """DMM digital 图地址 → cid; 其它地址 (含 mono 老片) 返回 None."""
    if not url.startswith((f"{HIGH_RES_CDN}/", f"{PICS_CDN}/")):
        return None
    match = _CID_URL_RE.search(url)
    return match.group(1).lower() if match else None


def split_cid(number: str, cid: str) -> tuple[str, str] | None:
    """从番号与 cid 反推 (系列, 前缀); 无法对应时返回 None.

    ``cid`` 形如 ``{前缀}{系列}{编号}``, 前缀可为空. 只接受系列名后直接跟数字的形态.
    """
    parts = parse_number(number)
    if not parts:
        return None
    series = parts[0].series
    head, separator, tail = cid.lower().rpartition(series)
    if not separator or not tail.isdigit():
        return None
    return series, head
