-- Class_product.php의 INSERT/UPDATE 쿼리를 근거로 역추적한 DDL.
-- idx: getProduct($idx)에서 "WHERE idx = ..."로 조회하는 걸 보면 별도의
--      auto_increment PK가 있음 (item_seq와는 별개).
-- item_seq: insert_product/update_product 둘 다 유니크 키처럼 사용
--           (update는 WHERE item_seq='%s'로 대상을 특정함) -> UNIQUE.
-- 공공데이터포털 "의약품 낱알식별정보" API 필드명(ITEM_SEQ 등)을 그대로
-- 소문자 스네이크케이스로 옮긴 컬럼들.

CREATE TABLE `product` (
  `idx`                    INT UNSIGNED   NOT NULL AUTO_INCREMENT,
  `item_seq`               VARCHAR(20)    NOT NULL COMMENT '품목기준코드 (API ITEM_SEQ)',
  `item_name`              VARCHAR(255)   NOT NULL COMMENT '품목명',
  `item_eng_name`          VARCHAR(255)            COMMENT '영문 품목명',
  `entp_seq`               VARCHAR(20)             COMMENT '업체코드',
  `entp_name`              VARCHAR(255)            COMMENT '업체명',
  `chart`                  TEXT                    COMMENT '성상(설명 문장)',
  `item_image`             VARCHAR(500)            COMMENT '실물 이미지 URL',
  `print_front`            VARCHAR(255)            COMMENT '앞면 표기',
  `print_back`             VARCHAR(255)            COMMENT '뒷면 표기',
  `drug_shape`             VARCHAR(20)             COMMENT '모양 (원형/타원형 등)',
  `color_class1`           VARCHAR(20)             COMMENT '색상 앞',
  `color_class2`           VARCHAR(20)             COMMENT '색상 뒤 (이색인 경우)',
  `line_front`             VARCHAR(20)             COMMENT '앞면 분할선',
  `line_back`              VARCHAR(20)             COMMENT '뒷면 분할선',
  `leng_long`              DECIMAL(6,2)            COMMENT '장축 길이(mm)',
  `leng_short`             DECIMAL(6,2)            COMMENT '단축 길이(mm)',
  `thick`                  DECIMAL(6,2)            COMMENT '두께(mm)',
  `img_regist_ts`          VARCHAR(20)             COMMENT '이미지 등록 일시 (API 원본 문자열)',
  `class_no`               VARCHAR(20)             COMMENT '분류번호',
  `class_name`             VARCHAR(255)            COMMENT '분류명(효능군)',
  `etc_otc_name`           VARCHAR(50)             COMMENT '전문/일반 구분',
  `item_permit_date`       VARCHAR(20)             COMMENT '허가일자 (API 원본 문자열, YYYYMMDD)',
  `form_code_name`         VARCHAR(100)            COMMENT '제형',
  `mark_code_front_anal`   VARCHAR(255)            COMMENT '앞면 각인 분석 코드',
  `mark_code_back_anal`    VARCHAR(255)            COMMENT '뒷면 각인 분석 코드',
  `mark_code_front_img`    VARCHAR(500)            COMMENT '앞면 각인 이미지 URL',
  `mark_code_back_img`     VARCHAR(500)            COMMENT '뒷면 각인 이미지 URL',
  `mark_code_front`        VARCHAR(255)            COMMENT '앞면 각인 코드',
  `mark_code_back`         VARCHAR(255)            COMMENT '뒷면 각인 코드',
  `change_date`            VARCHAR(20)             COMMENT '변경일자 (API 원본 문자열)',
  `edi_code`               VARCHAR(20)             COMMENT '보험코드(EDI 코드)',
  `reg_date`               DATETIME                COMMENT '최초 등록 일시 (배치 자체 기록)',
  `up_date`                DATETIME                COMMENT '최종 수정 일시 (배치 자체 기록)',
  PRIMARY KEY (`idx`),
  UNIQUE KEY `uq_item_seq` (`item_seq`),
  KEY `idx_shape_color` (`drug_shape`, `color_class1`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='의약품 낱알식별정보 (data.go.kr 배치 수집)';

-- Class_common.php가 참조하는 common 테이블 (진행 상태 저장용, 참고로 같이 추론)
CREATE TABLE `common` (
  `idx` INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `key` VARCHAR(100) NOT NULL,
  `val` VARCHAR(255),
  PRIMARY KEY (`idx`),
  UNIQUE KEY `uq_key` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Class_product.php가 참조하는 cache 테이블 (변경 감지용 MD5 해시 저장, 참고로 같이 추론)
CREATE TABLE `cache` (
  `idx` INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `key` VARCHAR(50) NOT NULL COMMENT 'item_seq',
  `val` VARCHAR(32) NOT NULL COMMENT 'MD5(직렬화된 API 원본 데이터)',
  `reg_date` DATETIME,
  `up_date` DATETIME,
  PRIMARY KEY (`idx`),
  UNIQUE KEY `uq_key` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
