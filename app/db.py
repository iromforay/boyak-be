"""batch(PHP)가 채워 넣는 MySQL `product` 테이블 연결.

DB 접속 정보는 batch/.env 를 그대로 재사용한다 (DB_HOST, DB_USER, DB_PASS, DB_NAME) -
같은 DB를 PHP 배치와 FastAPI가 같이 보는 것이므로 자격증명을 두 곳에 중복 관리하지 않는다.
"""

import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

load_dotenv(Path(__file__).resolve().parent.parent / "batch" / ".env")


def get_connection():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        database=os.getenv("DB_NAME"),
        charset="utf8mb4",
        cursorclass=DictCursor,
    )
