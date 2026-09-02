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
 * report
 *   parse_batch/exec_batch/image가 오늘 storage/logs/[Y-m-d]/*.stats.json 에 남긴
 *   결과를 모으고 product 테이블 건수를 조회해 텔레그램으로 요약을 보낸다.
 *   (Reporter::run(), 매일 9시대에 exec.php가 호출)
 *
 * full_sync CONFIRM
 *   product/cache 테이블을 TRUNCATE한 뒤 gather(1페이지부터) -> parse_batch ->
 *   exec_batch -> image를 이어서 한 번에 돌려 DB를 통째로 재구축한다.
 *   cache가 비어있으니 parse_batch가 전체 아이템을 "변경"으로 잡아 전량
 *   재삽입한다. 이미지는 그 "변경" 신호를 무시하고 누락분만 받는다(image
 *   step에 run_image(true) - 로컬에 이미 있는 파일은 TRUNCATE와 무관하게
 *   유효하므로 재다운로드하지 않음). TRUNCATE라 되돌릴 수 없어서 두번째
 *   인자로 정확히 CONFIRM을 넘겨야만 실행되고, exec.php(크론)에는 걸어두지
 *   않았다 - 필요할 때 사람이 직접 실행하는 용도다.
 *   (run_full_sync())
 *
 * 세 명령(gather/parse/image)은 서로 독립적으로 재실행 가능하며, 이 순서
 * (gather -> parse -> image)로 실행해야 의미가 있다. cron 연동은 exec.php(이
 * 파일과 다른 별도 진입점 파일) 참고.
 */
include "get_data.php";
include "Class_product.php";
include "Class_StoreProcessor.php";
include "Class_Report.php";

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

// parse_batch/exec_batch/image가 남긴 결과 통계를 storage/logs/[Y-m-d]/{command}.stats.json 으로 저장한다.
// report 명령이 이 파일들을 읽어 텔레그램 리포트를 만든다.
function write_stats($command, $stats)
{
    if($stats === null) return;
    $dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."logs".DIRECTORY_SEPARATOR.date("Y-m-d");
    if(! is_dir($dir)) mkdir($dir, 0777, true);
    file_put_contents($dir.DIRECTORY_SEPARATOR.$command.".stats.json", json_encode($stats, JSON_UNESCAPED_UNICODE));
}

// product/cache를 TRUNCATE하고 gather->parse_batch->exec_batch->image를
// 순서대로 이어서 실행한다. 사람이 필요할 때 직접 돌리는 전체 재연동 용도.
function run_full_sync($link)
{
    echo "===================\n";
    echo "전체 재연동 시작 : product/cache TRUNCATE\n";
    $link->query("TRUNCATE TABLE cache");
    $link->query("TRUNCATE TABLE product");

    echo "-------------------\n";
    echo "1) gather\n";
    run_collect(1);

    echo "-------------------\n";
    echo "2) parse_batch\n";
    write_stats('parse_batch', (new StoreProcessor($link))->run_batch());

    echo "-------------------\n";
    echo "3) exec_batch\n";
    write_stats('exec_batch', (new StoreProcessor($link))->run_exec_batch());

    echo "-------------------\n";
    echo "4) image (누락분만 - 이미지는 TRUNCATE와 무관하게 유효하므로 이미 있는 파일은 재다운로드 안 함)\n";
    write_stats('image', (new StoreProcessor($link))->run_image(true));

    echo "===================\n";
    echo "전체 재연동 완료\n";
}

// 인자 없이 실행했을 때(php process.php) 뭘 넣어야 하는지 보여준다. 파일 맨 위
// 독스블록 내용 요약 - 자세한 설명은 그쪽 주석 참고.
function print_usage()
{
    echo "사용법 : php process.php {명령어} [인자]\n";
    echo "\n";
    echo "명령어 목록 :\n";
    echo "  gather [page=1]     - 공공API 수집, storage/data/[날짜]/page_NNN.json 저장 (DB 미반영)\n";
    echo "  parse               - gather 결과를 건별로 바로 product/cache에 반영\n";
    echo "  parse_batch         - gather 결과를 비교해 SQL 파일만 생성 (DB 미반영)\n";
    echo "  exec_batch          - parse_batch가 만든 SQL 파일을 실제 실행 (DB 반영)\n";
    echo "  image               - 이미지 다운로드 (변경분+누락분)\n";
    echo "  once_exec {item_seq} - item_seq 하나만 API로 조회해보는 디버깅용\n";
    echo "  report              - 오늘자 통계 + DB 현황을 텔레그램으로 발송\n";
    echo "  full_sync CONFIRM   - product/cache TRUNCATE 후 gather->parse_batch->exec_batch->image 전체 재연동\n";
    echo "\n";
    echo "cron 연동은 exec.php 참고 (매일 3시대 gather, 4시대 parse_batch/exec_batch, 5시대 image, 9시대 report).\n";
}

if(! isset($argv[1]) || $argv[1] == ""){
    print_usage();
    exit(1);
}

if($argv[1] == "gather") {
    // 두번째 인자가 있으면 그 페이지부터, 없으면 1페이지부터 시작
    $page = isset($argv[2]) && $argv[2] !== '' ? (int)$argv[2] : 1;
    run_collect($page);

}
else if($argv[1] =="image"){
    $link = get_connect();
    $processor = new StoreProcessor($link);
    write_stats('image', $processor->run_image());
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
    write_stats('parse_batch', $processor->run_batch());
}
else if($argv[1] == 'exec_batch'){
    $link = get_connect();
    $processor = new StoreProcessor($link);
    write_stats('exec_batch', $processor->run_exec_batch());
}
else if($argv[1] == 'report'){
    $link = get_connect();
    $reporter = new Reporter($link);
    $reporter->run();
}
else if($argv[1] == 'full_sync'){
    if(($argv[2] ?? '') !== 'CONFIRM'){
        echo "전체 재연동은 product/cache 테이블을 TRUNCATE합니다 (되돌릴 수 없음).\n";
        echo "정말 실행하려면 다음과 같이 CONFIRM을 붙여서 실행하세요:\n";
        echo "  php process.php full_sync CONFIRM\n";
        exit(1);
    }
    $link = get_connect();
    run_full_sync($link);
}