"""알약 이름 조회 모델.

batch(PHP) 배치가 채워 넣은 MySQL `product` 테이블을 조회한다. `drug_shape`,
`color_class1`은 batch 쪽에서 이미 이 서비스의 _SHAPE_LABELS/_COLOR_LABELS와
같은 key(round, yellow ...)로 변환해서 저장하므로, 여기서는 그대로 WHERE 조건에
쓰면 된다.
"""

import pymysql

from app.db import get_connection

_SEARCH_SQL = """
    SELECT item_seq, item_name, entp_name, chart, drug_shape, color_class1,
           color_class2, print_front, print_back, class_name, etc_otc_name,
           item_permit_date
    FROM product
    WHERE drug_shape = %s AND color_class1 = %s
    ORDER BY item_name
    LIMIT 50
"""


class PillModel:
    def find_by_shape_color(self, shape: str, color: str) -> list[dict]:
        try:
            conn = get_connection()
        except pymysql.MySQLError:
            return []

        try:
            with conn.cursor() as cur:
                cur.execute(_SEARCH_SQL, (shape, color))
                return cur.fetchall()
        finally:
            conn.close()
