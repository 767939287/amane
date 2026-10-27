"""DMM 厂牌前缀学习表.

静态前缀表未收录的厂牌由刮削期观察到的真实 DMM 图地址补全: 从 cid 反推「系列 → 前缀」,
同一前缀在 2 个不同番号上出现后才参与候选生成. 未转正的前缀仍被记录, 顺序排在静态表之后.

存储为 ``state_dir/dmm_prefix_learned.json``, 原子替换写入; 文件损坏时本次运行只用内存表,
且不覆盖原文件, 避免丢失历史证据.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from .dmm import split_cid

logger = structlog.get_logger()

_VERSION = 1
#: 同一前缀必须出现在这么多个不同番号上才转正.
_VERIFIED_THRESHOLD = 2
_PREFIX_RE = re.compile(r"^[a-z0-9_]{0,8}$")
_SERIES_RE = re.compile(r"^[a-z0-9_]{1,16}$")


@dataclass
class _SeriesRule:
    #: 已转正前缀; 值为累计的番号数.
    verified: dict[str, int] = field(default_factory=dict)
    #: 观察中的前缀; 值为已观察到的番号.
    provisional: dict[str, list[str]] = field(default_factory=dict)


class DmmPrefixStore:
    """学习表读写. 同一实例被刮削 handler 共享, 内部串行化文件访问."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._rules: dict[str, _SeriesRule] | None = None
        self._load_failed = False

    async def learned(self, series: str | None) -> tuple[list[str], list[str]]:
        """返回 (已转正前缀, 观察中前缀); 无记录时均为空."""
        if series is None:
            return [], []
        async with self._lock:
            rule = (await self._ensure_loaded()).get(series)
            if rule is None:
                return [], []
            return list(rule.verified), list(rule.provisional)

    async def record_success(self, number: str, cid: str) -> None:
        """记录一次真实观察到的 cid; 与番号对不上时忽略."""
        split = split_cid(number, cid)
        if split is None:
            return
        series, prefix = split
        if not _PREFIX_RE.match(prefix) or not _SERIES_RE.match(series):
            return

        async with self._lock:
            rules = await self._ensure_loaded()
            rule = rules.setdefault(series, _SeriesRule())
            if prefix in rule.verified:
                return
            codes = rule.provisional.setdefault(prefix, [])
            code = number.lower().replace("-", "").replace(" ", "")
            if code in codes:
                return
            codes.append(code)
            if len(codes) >= _VERIFIED_THRESHOLD:
                rule.verified[prefix] = len(codes)
                del rule.provisional[prefix]
            await self._persist(rules)

    async def _ensure_loaded(self) -> dict[str, _SeriesRule]:
        if self._rules is not None:
            return self._rules
        self._rules = await asyncio.to_thread(self._read)
        return self._rules

    def _read(self) -> dict[str, _SeriesRule]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data: Any = json.loads(raw) if raw.strip() else {}
        except OSError, ValueError:
            logger.warning("prefix table unreadable, learning disabled for this run", path=str(self._path))
            self._load_failed = True
            return {}
        return _parse_state(data)

    async def _persist(self, rules: dict[str, _SeriesRule]) -> None:
        if self._path is None or self._load_failed:
            return
        payload = {
            "version": _VERSION,
            "series": {
                series: {"verified": rule.verified, "provisional": rule.provisional} for series, rule in rules.items()
            },
        }
        try:
            await asyncio.to_thread(self._write, payload)
        except OSError:
            # 写入失败不影响刮削; 内存表仍然可用.
            logger.warning("prefix table write failed", path=str(self._path))

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)


def _parse_state(data: Any) -> dict[str, _SeriesRule]:
    """校验并规范化读入的学习表; 非法条目直接丢弃."""
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        return {}
    series_data = data.get("series")
    if not isinstance(series_data, dict):
        return {}

    rules: dict[str, _SeriesRule] = {}
    for series, value in series_data.items():
        if not isinstance(series, str) or not _SERIES_RE.match(series) or not isinstance(value, dict):
            continue
        rule = _SeriesRule()
        verified = value.get("verified")
        if isinstance(verified, dict):
            for prefix, count in verified.items():
                if isinstance(prefix, str) and _PREFIX_RE.match(prefix) and isinstance(count, int) and count > 0:
                    rule.verified[prefix] = count
        provisional = value.get("provisional")
        if isinstance(provisional, dict):
            for prefix, codes in provisional.items():
                if isinstance(prefix, str) and _PREFIX_RE.match(prefix) and isinstance(codes, list):
                    rule.provisional[prefix] = [code for code in codes if isinstance(code, str)]
        if rule.verified or rule.provisional:
            rules[series] = rule
    return rules
