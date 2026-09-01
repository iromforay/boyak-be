"""알약 이름 조회 모델.

batch(PHP) 배치가 채워 넣은 MySQL `product` 테이블을 조회한다. `drug_shape`,
`color_class1`은 batch 쪽에서 이미 이 서비스의 _SHAPE_LABELS/_COLOR_LABELS와
같은 key(round, yellow ...)로 변환해서 저장하므로, 여기서는 그대로 WHERE 조건에
쓰면 된다.
"""

import pymysql

from app.db import get_connection

# color_class1/color_class2 어느 쪽이든 일치하면 찾는다 - 이색(투톤) 알약은
# 사용자가 두 색 중 하나만 알아도(또는 하나만 인식돼도) 찾을 수 있어야 한다.
_SEARCH_SQL = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE drug_shape = %s AND (color_class1 = %s OR color_class2 = %s)
    ORDER BY item_name
    LIMIT 50
"""

_SEARCH_SQL_WITH_IMPRINT = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE drug_shape = %s AND (color_class1 = %s OR color_class2 = %s) AND (print_front = %s OR print_back = %s)
    ORDER BY item_name
    LIMIT 50
"""

# OCR이 각인 일부만 읽었을 때(예: "V" - DB엔 "V분할선8") 완전일치가 실패하므로
# 부분일치로 한 번 더 시도하는 용도.
_SEARCH_SQL_WITH_IMPRINT_LIKE = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE drug_shape = %s AND (color_class1 = %s OR color_class2 = %s) AND (print_front LIKE %s OR print_back LIKE %s)
    ORDER BY item_name
    LIMIT 50
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
    def find_by_shape_color(self, shape: str, color: str, imprint: str | None = None, imprint_like: bool = False) -> list[dict]:
        try:
            conn = get_connection()
        except pymysql.MySQLError:
            return []

        try:
            with conn.cursor() as cur:
                if imprint and imprint_like:
                    like = f"%{imprint}%"
                    cur.execute(_SEARCH_SQL_WITH_IMPRINT_LIKE, (shape, color, color, like, like))
                elif imprint:
                    cur.execute(_SEARCH_SQL_WITH_IMPRINT, (shape, color, color, imprint, imprint))
                else:
                    cur.execute(_SEARCH_SQL, (shape, color, color))
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
