"""官方图源直构兜底: 站点未给出图片时按番号构造官方 CDN 地址."""

from .dmm import cid_from_image_url, is_uncensored_number, series_of
from .dmm import cover_candidates as dmm_cover_candidates
from .dmm import poster_candidates as dmm_poster_candidates
from .mgstage import cover_candidates as mgstage_cover_candidates
from .mgstage import poster_candidates as mgstage_poster_candidates
from .prefix_learn import DmmPrefixStore

__all__ = [
    "DmmPrefixStore",
    "cid_from_image_url",
    "dmm_cover_candidates",
    "dmm_poster_candidates",
    "is_uncensored_number",
    "mgstage_cover_candidates",
    "mgstage_poster_candidates",
    "series_of",
]
