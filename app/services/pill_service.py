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

        imprint가 있으면 모양+색상 후보 중 각인이 일치하는 것을 앞에 두고,
        OCR 오차를 감안해 부분 일치도 인정한다. imprint로 하나도 안 맞으면
        (OCR 실패 가능성이 있으니) 모양+색상 후보 전체를 그대로 돌려준다.
        """
        rows = self._model.find_by_shape_color(shape, color)
        drugs = [self._to_drug(row) for row in rows]

        norm_imprint = self._normalize_imprint(imprint)
        if not norm_imprint:
            return drugs

        matched = [d for d in drugs if self._imprint_matches(norm_imprint, d)]
        return matched if matched else drugs

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

    @staticmethod
    def _imprint_matches(norm_imprint: str, drug: dict) -> bool:
        for code in (drug["print_front"], drug["print_back"]):
            if not code:
                continue
            norm_code = "".join(code.split()).upper()
            # 한 글자짜리 각인은 아무 텍스트에나 우연히 포함되기 쉬워
            # (예: "Z" in "ZZZZZZ999") 오탐이 나므로 2자 이상만 비교한다.
            if len(norm_code) < 2:
                continue
            # OCR이 각인 일부만 읽었거나(부분 일치), 반대로 앞/뒤 각인을
            # 한 번에 다 읽어버린 경우(포함 관계) 둘 다 일치로 본다.
            if norm_code in norm_imprint or norm_imprint in norm_code:
                return True
        return False
