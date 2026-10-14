import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import _compute_wilson_score


# Wilson Score 置信下界，review少的游戏即使满分也会被降权
# 公式来自 https://www.evanmiller.org/how-not-to-sort-by-average-rating.html

def test_wilson_score_无评价返回零():
    assert _compute_wilson_score(None, None) == 0.0
    assert _compute_wilson_score(None, 0) == 0.0
    assert _compute_wilson_score(100.0, 0) == 0.0


def test_wilson_score_基本正确性():
    # 10000条好评出来得分接近1，不用纠结精确值
    score = _compute_wilson_score(100.0, 10000)
    assert score > 0.95

    # 0好评应该接近0但不能是负数
    score_zero = _compute_wilson_score(0.0, 1000)
    assert score_zero >= 0.0


def test_wilson_score_少量样本被惩罚():
    # 样本少的得分低，这是置信区间的效果
    s_small = _compute_wilson_score(100.0, 5)
    s_large = _compute_wilson_score(100.0, 5000)
    assert s_small < s_large


def test_wilson_score_结果在01之间():
    # FIXME: 极端输入没覆盖全，将就用着
    for rate in (0.0, 50.0, 100.0):
        for n in (1, 100, 10000):
            score = _compute_wilson_score(rate, n)
            assert 0.0 <= score <= 1.0, f"rate={rate}, n={n} -> {score}"


def test_transaction_dicts_roundtrip_db_id():
    from app.database import (
        _purchase_from_dict,
        _purchase_to_dict,
        _sale_from_dict,
        _sale_to_dict,
    )

    p = _purchase_from_dict({"_db_id": 7, "name": "A", "price": 1.0, "at": 2.0})
    assert p.id == 7
    assert _purchase_to_dict(p)["_db_id"] == 7

    s = _sale_from_dict({"_db_id": 9, "name": "B", "goods_id": 1, "price": 3.0, "at": 4.0})
    assert s.id == 9
    assert _sale_to_dict(s)["_db_id"] == 9


def test_transaction_dicts_without_db_id_leave_id_none():
    from app.database import _purchase_from_dict, _sale_from_dict

    assert _purchase_from_dict({"name": "A", "price": 1.0, "at": 2.0}).id is None
    assert _sale_from_dict({"name": "B", "goods_id": 1, "price": 3.0, "at": 4.0}).id is None
