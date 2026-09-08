"""사용자(하이브리드 앱 UUID 최소 가입) 조회/저장.

로그인 없이 기기별 UUID로 식별한다 - 이름/나이만 입력받아 튜토리얼 진행
여부 등 사용자별 설정을 서버에도 남겨둔다.
"""

from app.db import get_connection

_INSERT_SQL = "INSERT INTO users (uuid, name, age, platform) VALUES (%s, %s, %s, %s)"

_SELECT_SQL = (
    "SELECT uuid, name, age, tutorial_completed, platform, created_at FROM users WHERE uuid = %s"
)

_UPDATE_TUTORIAL_SQL = "UPDATE users SET tutorial_completed = %s WHERE uuid = %s"


class UserModel:
    def find_by_uuid(self, user_uuid: str) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_SQL, (user_uuid,))
                return cur.fetchone()
        finally:
            conn.close()

    def create(self, user_uuid: str, name: str, age: int, platform: str = "web") -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, (user_uuid, name, age, platform))
            conn.commit()
        finally:
            conn.close()
        return self.find_by_uuid(user_uuid)

    def set_tutorial_completed(self, user_uuid: str, completed: bool) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(_UPDATE_TUTORIAL_SQL, (completed, user_uuid))
            conn.commit()
        finally:
            conn.close()
