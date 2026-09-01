"""Python(FastSAM/CV)이 돌려주는 모양/색상 코드값(oval, gray 등)을 화면
표시값(한글)으로 바꾸고, 모양+색상으로 약 이름을 찾는 창구.
"""

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


class PillService:
    def __init__(self):
        self._model = PillModel()

    def get_shape_label(self, shape: str) -> str:
        return _SHAPE_LABELS.get(shape, shape)

    def get_color_label(self, color: str) -> str:
        return _COLOR_LABELS.get(color, color)

    def find_drugs(self, shape: str, color: str, imprint: str | None = None) -> list[dict]:
        """모양+색상(+각인)으로 후보 약을 조회해 화면에 바로 쓸 수 있는 형태로 돌려준다.

        각인 매칭은 3단계로 좁혀간다: 완전일치 -> 부분일치(LIKE) -> (그래도
        없으면) 각인 무시. OCR이 "V"만 읽었는데 DB엔 "V분할선8"로 저장된
        경우처럼, 완전일치는 실패해도 부분일치로는 찾아지는 경우가 많다.
        셋 다 실패하면 모양+색상 후보 전체를 그대로 보여준다.
        """
        norm_imprint = self._normalize_imprint(imprint)
        if not norm_imprint:
            rows = self._model.find_by_shape_color(shape, color)
            return [self._to_drug(row) for row in rows]

        rows = self._model.find_by_shape_color(shape, color, norm_imprint)
        if not rows:
            rows = self._model.find_by_shape_color(shape, color, norm_imprint, imprint_like=True)
        if not rows:
            rows = self._model.find_by_shape_color(shape, color)
        return [self._to_drug(row) for row in rows]

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

    @staticmethod
    def _normalize_imprint(text: str | None) -> str:
        if not text:
            return ""
        return "".join(text.split()).upper()
