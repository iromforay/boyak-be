"""알약 이름 조회 모델.

batch(PHP) 배치가 채워 넣은 MySQL `product` 테이블을 조회한다. `drug_shape`,
`color_class1`은 batch 쪽에서 이미 이 서비스의 _SHAPE_LABELS/_COLOR_LABELS와
같은 key(round, yellow ...)로 변환해서 저장하므로, 판독값과 그대로 비교하면 된다.

모양+색상(+각인) 조회는 SQL 조건으로 걸러내지 않는다 - 전 품목의 대조 항목을
서비스가 메모리에 올려 점수로 순위를 매기고(PillService.find_drugs), 여기서는
그 재료(find_match_index)와 상위 품목의 상세(find_by_item_seqs)만 제공한다.
"""

import pymysql

from app.db import get_connection

# 점수 순위 조회(PillService.find_drugs)용 - 전 품목의 판독 대조 항목만 가볍게
# 읽어 서비스가 메모리에 올려둔다. chart(TEXT) 같은 무거운 컬럼은 뺀다.
_MATCH_INDEX_SQL = """
    SELECT item_seq, item_name, drug_shape, color_class1, color_class2,
           print_front, print_back
    FROM product
"""

# 점수 상위 item_seq들의 화면 표시용 상세. IN 절 자리표시자는 개수에 맞춰 채운다.
_DETAIL_BY_ITEM_SEQS_SQL = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE item_seq IN ({placeholders})
"""

_SEARCH_BY_NAME_SQL = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE item_name LIKE %s OR entp_name LIKE %s
    ORDER BY item_name
    LIMIT 50
"""


class PillModel:
    def find_match_index(self) -> list[dict] | None:
        """점수 순위 조회용 전 품목 대조 항목을 읽는다. DB 오류면 None(빈 결과와 구분)."""
        try:
            conn = get_connection()
        except pymysql.MySQLError:
            return None

        try:
            with conn.cursor() as cur:
                cur.execute(_MATCH_INDEX_SQL)
                return cur.fetchall()
        except pymysql.MySQLError:
            return None
        finally:
            conn.close()

    def find_by_item_seqs(self, item_seqs: list[str]) -> list[dict]:
        """item_seq 목록의 상세를 읽는다. 돌려주는 순서는 보장하지 않는다(호출부가 재정렬)."""
        if not item_seqs:
            return []
        try:
            conn = get_connection()
        except pymysql.MySQLError:
            return []

        try:
            with conn.cursor() as cur:
                placeholders = ", ".join(["%s"] * len(item_seqs))
                cur.execute(_DETAIL_BY_ITEM_SEQS_SQL.format(placeholders=placeholders), item_seqs)
                return cur.fetchall()
        finally:
            conn.close()

    def find_by_name(self, keyword: str) -> list[dict]:
        try:
            conn = get_connection()
        except pymysql.MySQLError:
            return []

        try:
            with conn.cursor() as cur:
                like = f"%{keyword}%"
                cur.execute(_SEARCH_BY_NAME_SQL, (like, like))
                return cur.fetchall()
        finally:
            conn.close()
