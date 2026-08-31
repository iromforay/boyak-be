<?php
/*
 * 실행 방법: php process.php {gather|parse|image} [page]
 *
 * gather [page=1]
 *   식약처 공공 API에서 의약품 정보를 페이지 단위로 수집해
 *   storage/data/[Y-m-d]/page_NNN.json 으로 저장만 한다. DB는 건드리지 않는다.
 *   (Medicine::go() -> curl() -> down())
 *
 * parse
 *   gather가 쌓아둔 storage/data/[Y-m-d]/page_*.json 을 읽어서 아이템별로
 *   캐시(cache 테이블, md5 해시) 체크 후 변경/신규분만 product 테이블에
 *   upsert 한다. 처리 끝난 파일은 storage/data/done/[Y-m-d]/ 로 옮겨서
 *   재실행해도 중복 처리되지 않는다.
 *   (StoreProcessor::run())
 *
 * image
 *   storage/data/(파싱 전) + storage/data/done/(파싱 후) 의 json을 전부 훑어서
 *   item_seq별 이미지를 storage/drug_image/{item_seq}.jpg 로 내려받는다.
 *   curl_multi로 동시에(기본 12개씩) 받고, 429(rate limit)는 대기 후 재시도한다.
 *   추후 각인 인식 학습 데이터로도 쓸 참고 이미지다.
 *   (StoreProcessor::run_image())
 *
 * parse_batch / exec_batch  (parse의 배치 버전, 2단계로 분리됨)
 *   parse와 최종 결과(DB 반영)는 같지만, 아이템마다 쿼리를 날리는 대신
 *   cache 테이블을 한 번에 통째로 읽어와 메모리에서 비교하고, 변경분 SQL을
 *   실행하지 않고 파일로만 모아둔다 (storage/data/sql/{시각}.sql). DB에는
 *   아직 아무것도 반영 안 된 상태.
 *     - parse_batch : json 읽고 비교해서 SQL 파일만 생성 (StoreProcessor::run_batch())
 *     - exec_batch  : 쌓인 SQL 파일들을 순서대로 실제 실행, 끝나면
 *                     storage/data/sql/done/ 으로 이동 (StoreProcessor::run_exec_batch())
 *   두 단계로 나눈 이유: SQL을 사람이 먼저 확인/검수할 수 있고, DB 왕복은
 *   parse_batch에서 캐시 로드 1번, exec_batch에서 실행 1번으로 줄어든다.
 *
 * once_exec {item_seq}
 *   단건 조회 디버깅용. item_seq 하나만 API로 조회해 json 저장까지 해본다.
 *
 * 세 명령(gather/parse/image)은 서로 독립적으로 재실행 가능하며, 이 순서
 * (gather -> parse -> image)로 실행해야 의미가 있다. cron 연동은 exec.php(이
 * 파일과 다른 별도 진입점 파일) 참고.
 */
include "get_data.php";
include "Class_product.php";
include "Class_StoreProcessor.php";

function run_collect($page = 1)
{
    $med = new Medicine();
    $med->go($page);
}

function run_parse()
{
    $link = get_connect();
    $processor = new StoreProcessor($link);
    $processor->run();
}

if($argv[1] == "gather") {
    // 두번째 인자가 있으면 그 페이지부터, 없으면 1페이지부터 시작
    $page = isset($argv[2]) && $argv[2] !== '' ? (int)$argv[2] : 1;
    run_collect($page);

}
else if($argv[1] =="image"){
    $link = get_connect();
    $processor = new StoreProcessor($link);
    $processor->run_image();
}
else if($argv[1] == 'once_exec'){
    $med = new Medicine();
    $med->once($argv[2]);
}
else if($argv[1] == 'parse'){
    run_parse();
}
else if($argv[1] == 'parse_batch'){
    $link = get_connect();
    $processor = new StoreProcessor($link);
    $processor->run_batch();
}
else if($argv[1] == 'exec_batch'){
    $link = get_connect();
    $processor = new StoreProcessor($link);
    $processor->run_exec_batch();
}