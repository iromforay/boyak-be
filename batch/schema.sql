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
  `edi_code`               VARCHAR(100)            COMMENT '보험코드(EDI 코드) - 정상값은 9자리 숫자지만 드물게 더 긴/복합 값이 와서 넉넉히 잡음',
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

-- 하이브리드 앱 최소 가입: 로그인/비밀번호 없이 기기별 UUID로 식별하고
-- 이름/나이만 받는다. 튜토리얼 1회 노출 여부를 서버에도 남겨, 재설치 후
-- 같은 UUID로 다시 붙어도(로컬 스토리지가 남아있다면) 상태를 복구할 수 있게 한다.
-- idx(AUTO_INCREMENT PK) + uuid(UNIQUE KEY) 구조는 product/common/cache와 동일한
-- 저장소 컨벤션을 따른 것.
CREATE TABLE `users` (
  `idx`                 INT UNSIGNED      NOT NULL AUTO_INCREMENT,
  `uuid`                VARCHAR(36)       NOT NULL COMMENT '기기별 UUID (FE crypto.randomUUID)',
  `name`                VARCHAR(50)       NOT NULL,
  `age`                 SMALLINT UNSIGNED NOT NULL,
  `tutorial_completed`  BOOLEAN           NOT NULL DEFAULT FALSE,
  -- 'web'(순수 브라우저) | 'app'(하이브리드 앱 웹뷰) - 웹뷰 래퍼가 아직 없어
  -- 지금은 전부 web으로 들어온다. FCM 푸시는 웹에서는 보낼 수 없으니, 앱이
  -- 생기면 이 값으로 발송 대상을 걸러낸다.
  `platform`            VARCHAR(10)       NOT NULL DEFAULT 'web',
  `created_at`          DATETIME          NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`idx`),
  UNIQUE KEY `uq_uuid` (`uuid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='앱 사용자 (로그인 없는 UUID 기반 최소 가입)';

-- 하이브리드 앱 강제 업데이트 정책. 최소버전 이상이면 통과시키는 방식이
-- 아니라, 출시된 버전을 하나하나 등록해두고 그 버전의 사용유무(is_active)만
-- 본다 - 특정 버전에서만 심각한 버그가 나온 경우에도(반드시 최신이 아니어도)
-- 그 버전만 콕 집어 막을 수 있다. 네이티브 껍데기가 FE(/intro)를 열 때
-- 자기 버전을 같이 넘기고, FE가 그 값 그대로 BE에 질의한다. 등록되지 않은
-- 버전은 무조건 실패로 본다(신규 출시 시 등록을 빼먹으면 그 버전 전체가
-- 막히므로, 배포 체크리스트에 이 테이블 등록을 포함시켜야 한다).
CREATE TABLE `app_version_policy` (
  `idx`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `platform`       VARCHAR(10)  NOT NULL COMMENT '지금은 android만 사용',
  `version_code`   VARCHAR(20)  NOT NULL COMMENT '네이티브 버전 문자열 (예: "3", "0.51", "0.51c") - 숫자 비교 안 하고 문자열로 그대로 대조',
  `is_active`      BOOLEAN      NOT NULL DEFAULT TRUE COMMENT '이 버전 접속 허용 여부',
  `store_url`      VARCHAR(500) NOT NULL COMMENT '거절 시 이동시킬 구글 플레이스토어 URL',
  `reg_date`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `up_date`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`idx`),
  UNIQUE KEY `uq_platform_version` (`platform`, `version_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='하이브리드 앱 출시 버전별 사용유무 (platform+version_code 단위 등록)';
