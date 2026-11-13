"""Python(FastSAM/CV)이 돌려주는 모양/색상 코드값(oval, gray 등)을 화면
표시값(한글)으로 바꾸고, 모양+색상(+각인)으로 약 이름을 찾는 창구.
"""

import re
import threading
import time

from app.models.pill_model import PillModel

_SHAPE_LABELS = {
    "round": "원형",
    "oval": "타원형",
    "capsule": "캡슐형",
    "triangle": "삼각형",
    "square": "사각형",
    "diamond": "마름모형",
    "pentagon": "오각형",
    "hexagon": "육각형",
    "semicircle": "반원형",
    "oblong": "장방형",
    "octagon": "팔각형",
    "other": "기타",
}

_COLOR_LABELS = {
    "white": "흰색",
    "ivory": "아이보리",
    "yellow": "노랑",
    "orange": "주황",
    "pink": "분홍",
    "red": "빨강",
    "brown": "갈색",
    "purple": "보라",
    "blue": "파랑",
    "green": "초록",
    "gray": "회색",
    "black": "검정",
    "clear": "투명",
}

# ---- 점수 순위 조회 설정 (초안값 - 실사진 정답셋으로 조정할 것) ----
# 판독(모양/색상/각인) 중 하나가 틀려도 정답 약이 후보에서 빠지지 않도록, 조건으로
# 걸러내지 않고 항목별 점수를 합산해 순위를 매긴다. 각인이 가장 변별력이 높아서
# (고유값 수천 종) 가중치를 가장 크게 준다 - 각인만 완전히 맞은 약(0.6)이 모양+색상만
# 맞은 약(0.4)보다 위에 온다.
_WEIGHT_IMPRINT = 0.6
_WEIGHT_SHAPE = 0.2
_WEIGHT_COLOR = 0.2
_RESULT_LIMIT = 50

# 타원형/장방형은 2D 윤곽으로는 원래 경계가 애매하고(스튜디오 사진 검증에서도 서로
# 6~7% 혼동), 식약처 라벨링 자체도 캡슐형을 둘 중 하나로 섞어 등록한다.
_SHAPE_NEIGHBORS = {frozenset(("oval", "oblong"))}
_SHAPE_NEIGHBOR_SCORE = 0.7

# 조명에 따라 서로 바뀌어 판독되기 쉬운 색 쌍(흰 알약이 어두운 실내에서 회색으로,
# 누런 조명에서 노랑으로 찍히는 식).
_COLOR_NEIGHBORS = {
    frozenset(pair)
    for pair in (
        ("white", "gray"),
        ("white", "yellow"),
        ("yellow", "orange"),
        ("orange", "pink"),
        ("orange", "brown"),
        ("pink", "red"),
        ("pink", "purple"),
        ("red", "brown"),
        ("purple", "blue"),
        ("blue", "green"),
        ("gray", "black"),
    )
}
_COLOR_NEIGHBOR_SCORE = 0.5

# OCR이 흔히 서로 바꿔 읽는 글자 쌍 - 편집 거리에서 치환 비용을 절반만 매긴다.
_IMPRINT_CONFUSIONS = {
    frozenset(pair) for pair in (("0", "O"), ("1", "I"), ("8", "B"), ("5", "S"), ("2", "Z"), ("0", "D"), ("6", "G"))
}
_IMPRINT_CONFUSION_COST = 0.5

# 판독 차순위 각인 후보의 유사도에 곱하는 값 - 대표 각인(1순위)보다 조금 덜 믿는다.
_IMPRINT_ALT_CANDIDATE_FACTOR = 0.9

# "분할선"은 글자 3개가 아니라 기호 하나로 다뤄야 편집 거리가 공정하다.
_DIV_TOKEN = "분할선"
_DIV_SYMBOL = "|"
_IMPRINT_DROP_RE = re.compile(r"[^A-Z0-9|]")

# 배치(PHP)가 product를 갱신해도 서버 재시작 없이 반영되도록 주기적으로 다시 읽는다.
_INDEX_TTL_SECONDS = 3600


class PillService:
    def __init__(self):
        self._model = PillModel()
        self._index = None  # 전 품목 대조 항목(정규화 각인 포함) - 첫 조회 때 적재
        self._index_loaded_at = 0.0
        self._index_lock = threading.Lock()

    def get_shape_label(self, shape: str) -> str:
        return _SHAPE_LABELS.get(shape, shape)

    def get_color_label(self, color: str) -> str:
        return _COLOR_LABELS.get(color, color)

    def find_drugs(
        self,
        shape: str,
        color: str,
        imprint: str | None = None,
        imprint_candidates: list[str] | None = None,
    ) -> list[dict]:
        """모양+색상(+각인)과 가장 잘 맞는 순서로 후보 약을 돌려준다.

        조건 일치로 걸러내지 않고 전 품목에 점수를 매겨 상위 _RESULT_LIMIT건을
        고른다 - 판독 하나가 틀려도(예: 흰색을 회색으로, 각인 한 글자 오독) 정답
        약이 순위만 조금 밀릴 뿐 후보에서 사라지지 않는다. 각인은 DB 표기에 섞인
        공백/하이픈/기호를 걷어낸 정규화 값끼리 비교한다.

        imprint는 화면에 보이는 대표 각인(판독 1순위 또는 사용자가 고친 값),
        imprint_candidates는 판독이 같이 내놓은 차순위 후보들이다. 품목마다 이들 중
        가장 잘 맞는 것의 점수를 쓴다 - 판독 1순위가 엉뚱한 각도의 오독이어도 차순위에
        정답이 있으면 DB에 실제로 있는 각인 쪽이 높은 점수를 받아 위로 올라온다.
        """
        index = self._get_index()
        if not index:
            return []

        imprint_sims = {}
        primary = self._normalize_imprint(imprint)
        queries = [(primary, 1.0)] if primary else []
        for candidate in imprint_candidates or []:
            norm = self._normalize_imprint(candidate)
            if norm and all(norm != q for q, _ in queries):
                # 대표 각인이 없으면(차순위만 넘어온 경우) 첫 후보를 대표로 본다.
                queries.append((norm, _IMPRINT_ALT_CANDIDATE_FACTOR if queries else 1.0))
        for query, factor in queries:
            for target, sim in self._imprint_similarities(query, index["imprints"]).items():
                if factor * sim > imprint_sims.get(target, 0.0):
                    imprint_sims[target] = factor * sim

        scored = []
        for entry in index["entries"]:
            score = _WEIGHT_SHAPE * self._shape_score(shape, entry["shape"])
            score += _WEIGHT_COLOR * max(
                self._color_score(color, entry["color1"]), self._color_score(color, entry["color2"])
            )
            if imprint_sims:
                score += _WEIGHT_IMPRINT * max(
                    imprint_sims.get(entry["front"], 0.0), imprint_sims.get(entry["back"], 0.0)
                )
            if score > 0:
                scored.append((score, entry["item_name"], entry["item_seq"]))

        # 점수 내림차순, 동점이면 이름순(기존 화면의 정렬 기준 유지).
        scored.sort(key=lambda t: (-t[0], t[1]))
        top_seqs = [item_seq for _, _, item_seq in scored[:_RESULT_LIMIT]]

        rows_by_seq = {row["item_seq"]: row for row in self._model.find_by_item_seqs(top_seqs)}
        return [self._to_drug(rows_by_seq[seq]) for seq in top_seqs if seq in rows_by_seq]

    def search_drugs(self, keyword: str) -> list[dict]:
        """약 이름/제조사명으로 직접 검색 (사진 분석 없이 텍스트 검색)."""
        rows = self._model.find_by_name(keyword)
        return [self._to_drug(row) for row in rows]

    def _to_drug(self, row: dict) -> dict:
        item_seq = row["item_seq"]
        return {
            "item_seq": item_seq,
            "item_name": row["item_name"],
            "entp_name": row["entp_name"],
            "chart": row["chart"],
            "print_front": row["print_front"],
            "print_back": row["print_back"],
            "class_name": row["class_name"],
            "etc_otc_name": row["etc_otc_name"],
            "item_permit_date": row["item_permit_date"],
            "shape_label": self.get_shape_label(row["drug_shape"]),
            "color_label": self.get_color_label(row["color_class1"]),
            "image_url": f"/drug_images/{item_seq}.jpg",
        }

    def _get_index(self) -> dict | None:
        """전 품목 대조 항목을 메모리에서 돌려준다. 없거나 오래됐으면 다시 읽는다.
        다시 읽기에 실패하면(DB 일시 오류) 이전 것을 그대로 쓴다."""
        if self._index is not None and time.time() - self._index_loaded_at < _INDEX_TTL_SECONDS:
            return self._index
        with self._index_lock:
            if self._index is not None and time.time() - self._index_loaded_at < _INDEX_TTL_SECONDS:
                return self._index
            rows = self._model.find_match_index()
            if rows is None:
                return self._index
            entries = []
            imprints = set()
            for row in rows:
                front = self._normalize_imprint(row["print_front"])
                back = self._normalize_imprint(row["print_back"])
                imprints.update((front, back))
                entries.append(
                    {
                        "item_seq": row["item_seq"],
                        "item_name": row["item_name"] or "",
                        "shape": row["drug_shape"] or "",
                        "color1": row["color_class1"] or "",
                        "color2": row["color_class2"] or "",
                        "front": front,
                        "back": back,
                    }
                )
            imprints.discard("")
            self._index = {"entries": entries, "imprints": imprints}
            self._index_loaded_at = time.time()
            return self._index

    @staticmethod
    def _shape_score(query: str, target: str) -> float:
        """모양 일치 점수(0~1). 판독이 "other"면 정보가 없는 것으로 보고 0을 준다 -
        실사용 사진에서 분류기가 확신이 없을 때 "기타"로 빠지는 경우가 많아서다."""
        if not query or not target or query == "other":
            return 0.0
        if query == target:
            return 1.0
        if frozenset((query, target)) in _SHAPE_NEIGHBORS:
            return _SHAPE_NEIGHBOR_SCORE
        return 0.0

    @staticmethod
    def _color_score(query: str, target: str) -> float:
        """색상 일치 점수(0~1). 조명 탓에 바뀌기 쉬운 인접색은 절반만 준다."""
        if not query or not target:
            return 0.0
        if query == target:
            return 1.0
        if frozenset((query, target)) in _COLOR_NEIGHBORS:
            return _COLOR_NEIGHBOR_SCORE
        return 0.0

    def _imprint_similarities(self, query: str, imprints: set) -> dict:
        """판독 각인과 DB의 고유 각인들 사이 유사도(0~1) 표. 겹치는 글자가 하나도
        없는 것은 계산하지 않는다(대부분이 여기서 걸러져 전체 계산이 가벼워진다)."""
        query_chars = set(query)
        sims = {}
        for target in imprints:
            if query_chars.isdisjoint(target):
                continue
            sims[target] = self._imprint_similarity(query, target)
        return sims

    @staticmethod
    def _imprint_similarity(query: str, target: str) -> float:
        """정규화된 두 각인의 유사도(0~1). 편집 거리 기반이라 한 글자 오독도 높은
        점수로 남고, OCR이 일부만 읽은 경우(예: "V" - DB엔 "V|8")는 포함 관계로
        따로 점수를 준다(읽은 비율만큼 0.5~1.0)."""
        if query == target:
            return 1.0
        prev = [float(j) for j in range(len(target) + 1)]
        for i, qc in enumerate(query, 1):
            cur = [float(i)]
            for j, tc in enumerate(target, 1):
                if qc == tc:
                    sub = 0.0
                elif frozenset((qc, tc)) in _IMPRINT_CONFUSIONS:
                    sub = _IMPRINT_CONFUSION_COST
                else:
                    sub = 1.0
                cur.append(min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + sub))
            prev = cur
        sim = 1.0 - prev[-1] / max(len(query), len(target))
        if query in target:
            sim = max(sim, 0.5 + 0.5 * len(query) / len(target))
        return max(sim, 0.0)

    @staticmethod
    def _normalize_imprint(text: str | None) -> str:
        """각인을 비교용으로 정규화한다 - 대문자 영숫자와 분할선 기호만 남긴다.
        각인 인식 모델의 출력 어휘(A-Z, 0-9, 분할선)와 같은 기준이라, DB 표기의
        공백/하이픈/점/로고 기호("TP---100", "JI M", "ΛJ10") 때문에 매칭이 깨지지
        않는다. 판독 실패 안내 문구처럼 영숫자가 없는 값은 빈 문자열이 된다."""
        if not text:
            return ""
        return _IMPRINT_DROP_RE.sub("", text.upper().replace(_DIV_TOKEN, _DIV_SYMBOL))
