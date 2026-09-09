"""하이브리드 앱 출시 버전별 사용유무(`app_version_policy`) 조회.

버전마다 한 행씩 등록해두고 is_active로 접속 허용 여부를 정한다.
등록 자체가 안 된 버전은 조회 결과가 없으므로 실패로 처리한다(main.py).
"""

from app.db import get_connection

_SELECT_VERSION_SQL = (
    "SELECT is_active, store_url FROM app_version_policy"
    " WHERE platform = %s AND version_code = %s"
)

# 등록 안 된(미래/구) 버전을 거절할 때도 이동시킬 스토어 링크는 있어야 하니,
# 그 platform에 가장 최근 등록된(idx 기준 - version_code는 "0.51c" 같은
# 문자열이라 정렬로 최신을 가릴 수 없다) 행의 store_url을 대신 내려준다.
_SELECT_LATEST_STORE_URL_SQL = (
    "SELECT store_url FROM app_version_policy"
    " WHERE platform = %s ORDER BY idx DESC LIMIT 1"
)


class AppVersionModel:
    def get_by_version(self, platform: str, version_code: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_VERSION_SQL, (platform, version_code))
                return cur.fetchone()
        finally:
            conn.close()

    def get_latest_store_url(self, platform: str) -> str | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_LATEST_STORE_URL_SQL, (platform,))
                row = cur.fetchone()
                return row["store_url"] if row else None
        finally:
            conn.close()
