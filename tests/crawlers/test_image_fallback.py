"""官方图源直构兜底: 番号解析、前缀候选与学习表."""

import json
from pathlib import Path

import pytest

from amane.crawlers.fallback import dmm, mgstage
from amane.crawlers.fallback.prefix_learn import DmmPrefixStore

_DMM_PS = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video"
_DMM_PL = _DMM_PS
_MGSTAGE = "https://image.mgstage.com/images"


# --- 番号解析 ---


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("MIDV-123", [dmm.NumberParts("midv", 123, "00123")]),
        ("ssis497", [dmm.NumberParts("ssis", 497, "00497")]),
        ("T28-123", [dmm.NumberParts("t28", 123, "00123")]),
        # 数字夹在字母中间或完全无法拆分的番号不解析, 交由跳过逻辑处理.
        ("FC2-PPV-123456", []),
        ("123456", []),
        ("", []),
    ],
)
def test_parse_number(number: str, expected: list[dmm.NumberParts]) -> None:
    assert dmm.parse_number(number) == expected


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("MIDV-123", "midv"),
        ("FC2-PPV-123456", None),
        ("", None),
    ],
)
def test_series_of(number: str, expected: str | None) -> None:
    assert dmm.series_of(number) == expected


# --- 静态前缀表 ---


@pytest.mark.parametrize(
    ("series", "num", "expected"),
    [
        # 静态表空前缀组: 只有空前缀.
        ("midv", 123, [""]),
        # 特殊前缀组: 具体前缀在前, 空前缀兜底.
        ("abf", 295, ["436", ""]),
        ("gvg", 564, ["13", ""]),
        # 阈值切换.
        ("avop", 168, [""]),
        ("avop", 169, ["1"]),
        ("gigl", 643, ["h_860"]),
        ("gigl", 644, [""]),
        # 附加前缀.
        ("sma", 123, ["42", "", "83"]),
        # 未收录系列使用通用前缀.
        ("zzzz", 1, ["", "1", "13", "49", "436", "118", "55", "57", "83", "5642"]),
    ],
)
def test_prefixes_for(series: str, num: int, expected: list[str]) -> None:
    assert dmm.prefixes_for(series, num) == expected


@pytest.mark.parametrize(
    ("number", "verified", "provisional", "expected"),
    [
        # 学习表已转正的前缀排在静态表之前.
        ("MIDV-123", ["118"], [], ["118midv00123", "midv00123"]),
        # 观察中的前缀排在最后.
        ("MIDV-123", [], ["436"], ["midv00123", "436midv00123"]),
        # 静态表与学习表重复时去重.
        ("ABF-295", ["436"], [], ["436abf00295", "abf00295"]),
        # 无法解析的番号无候选.
        ("FC2-PPV-123456", ["118"], [], []),
    ],
)
def test_cid_candidates(number: str, verified: list[str], provisional: list[str], expected: list[str]) -> None:
    assert dmm.cid_candidates(number, verified=verified, provisional=provisional) == expected


def test_image_urls() -> None:
    assert dmm.poster_candidates("ABF-295") == [
        f"{_DMM_PS}/436abf00295/436abf00295ps.jpg",
        f"{_DMM_PS}/abf00295/abf00295ps.jpg",
    ]
    assert dmm.cover_candidates("ABF-295") == [
        f"{_DMM_PS}/436abf00295/436abf00295pl.jpg",
        f"{_DMM_PS}/abf00295/abf00295pl.jpg",
    ]


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("FC2-PPV-123456", True),
        ("200GANA-3327", True),
        ("HEYZO-1234", True),
        ("_dummy", True),
        ("MIDV-123", False),
    ],
)
def test_is_uncensored_number(number: str, expected: bool) -> None:
    assert dmm.is_uncensored_number(number) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{_DMM_PS}/118abf295/118abf295ps.jpg", "118abf295"),
        ("https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg", "ssis00497"),
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg", None),
        ("https://example.com/digital/video/abc/abcpl.jpg", None),
        ("", None),
    ],
)
def test_cid_from_image_url(url: str, expected: str | None) -> None:
    assert dmm.cid_from_image_url(url) == expected


@pytest.mark.parametrize(
    ("number", "cid", "expected"),
    [
        ("ABF-295", "118abf295", ("abf", "118")),
        ("HONB-487", "h_1133honb00487", ("honb", "h_1133")),
        ("MIDV-123", "midv00123", ("midv", "")),
        # 系列名不匹配或系列后不是数字时不学习.
        ("MIDV-123", "ssis00123", None),
        ("MIDV-123", "midv00123a", None),
        ("FC2-PPV-123456", "fc2ppv123456", None),
    ],
)
def test_split_cid(number: str, cid: str, expected: tuple[str, str] | None) -> None:
    assert dmm.split_cid(number, cid) == expected


# --- MGStage ---


@pytest.mark.parametrize(
    "number",
    ["259LUXU-1111", "LUXU-1111", "259luxu-1111"],
)
def test_mgstage_accepts_prefixed_and_plain(number: str) -> None:
    assert mgstage.cover_candidates(number) == [f"{_MGSTAGE}/luxutv/259luxu/1111/pb_e_259luxu-1111.jpg"]
    assert mgstage.poster_candidates(number) == [f"{_MGSTAGE}/luxutv/259luxu/1111/pf_e_259luxu-1111.jpg"]


def test_mgstage_leading_zero_variants() -> None:
    assert mgstage.cover_candidates("259LUXU-0001") == [
        f"{_MGSTAGE}/luxutv/259luxu/0001/pb_e_259luxu-0001.jpg",
        f"{_MGSTAGE}/luxutv/259luxu/1/pb_e_259luxu-1.jpg",
    ]


@pytest.mark.parametrize(
    "number",
    ["ABF-389", "LUXU1111", "", "259LUXU-ABCD"],
)
def test_mgstage_unknown_series_yields_nothing(number: str) -> None:
    assert mgstage.cover_candidates(number) == []
    assert mgstage.poster_candidates(number) == []


# --- 学习表 ---


@pytest.fixture
def store(tmp_path: Path) -> DmmPrefixStore:
    return DmmPrefixStore(tmp_path / "dmm_prefix_learned.json")


@pytest.mark.asyncio(loop_scope="function")
async def test_learned_prefix_needs_two_numbers(store: DmmPrefixStore, tmp_path: Path) -> None:
    await store.record_success("HONB-487", "h_1133honb00487")
    assert await store.learned("honb") == ([], ["h_1133"])

    # 同一番号的重复证据不推进状态.
    await store.record_success("HONB-487", "h_1133honb00487")
    assert await store.learned("honb") == ([], ["h_1133"])

    await store.record_success("HONB-498", "h_1133honb00498")
    assert await store.learned("honb") == (["h_1133"], [])

    # 已转正前缀不再重复累加.
    await store.record_success("HONB-500", "h_1133honb00500")
    assert await store.learned("honb") == (["h_1133"], [])

    payload = json.loads((tmp_path / "dmm_prefix_learned.json").read_text(encoding="utf-8"))
    assert payload["series"]["honb"]["verified"] == {"h_1133": 2}


@pytest.mark.asyncio(loop_scope="function")
async def test_learned_prefix_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "dmm_prefix_learned.json"
    first = DmmPrefixStore(path)
    await first.record_success("HONB-487", "h_1133honb00487")
    await first.record_success("HONB-498", "h_1133honb00498")

    assert await DmmPrefixStore(path).learned("honb") == (["h_1133"], [])


@pytest.mark.asyncio(loop_scope="function")
async def test_unknown_series_returns_empty(store: DmmPrefixStore) -> None:
    assert await store.learned("midv") == ([], [])
    assert await store.learned(None) == ([], [])


@pytest.mark.asyncio(loop_scope="function")
async def test_unmatched_cid_is_ignored(store: DmmPrefixStore) -> None:
    await store.record_success("MIDV-123", "ssis00123")
    assert await store.learned("midv") == ([], [])
    assert await store.learned("ssis") == ([], [])


@pytest.mark.asyncio(loop_scope="function")
async def test_corrupt_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "dmm_prefix_learned.json"
    path.write_text("{not json", encoding="utf-8")
    store = DmmPrefixStore(path)

    await store.record_success("HONB-487", "h_1133honb00487")
    assert await store.learned("honb") == ([], ["h_1133"])
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.asyncio(loop_scope="function")
async def test_illegal_entries_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "dmm_prefix_learned.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "series": {
                    "honb": {"verified": {"h_1133": 2, "BAD PREFIX": 3}, "provisional": {"h_999": ["honb487", 1]}},
                    "": {"verified": {}, "provisional": {}},
                    "midv": {"verified": {}, "provisional": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    store = DmmPrefixStore(path)

    assert await store.learned("honb") == (["h_1133"], ["h_999"])
    assert await store.learned("midv") == ([], [])


@pytest.mark.asyncio(loop_scope="function")
async def test_memory_only_store() -> None:
    store = DmmPrefixStore()
    await store.record_success("HONB-487", "h_1133honb00487")
    await store.record_success("HONB-498", "h_1133honb00498")
    assert await store.learned("honb") == (["h_1133"], [])
