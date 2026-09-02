<?php
/*
 * cron이 매분 호출하는 진입점. 몇 시인지 보고 process.php의 어느 명령을
 * 돌릴지 정한다. 분(minute)은 따로 비교하지 않는다 - _exec() 자체가 오늘
 * 그 명령을 이미 실행했으면 락 파일로 스킵하기 때문에, 그 시간대 안에서
 * 매분 호출돼도 실제로는 하루 한 번만 실행된다. 정각을 놓쳐도(서버
 * 재시작 등) 그 시간대 안 어느 분이든 한 번은 실행되어 자연스럽게
 * 재시도가 되는 것도 이 방식의 장점이다.
 *
 *   cron: * * * * * php /path/to/batch/exec.php >> /path/to/log/exec.log 2>&1
 *
 * 시간대 배정: 03시대 gather, 04시대 parse_batch->exec_batch, 05시대 image,
 * 09시대 report(텔레그램 요약 발송). 07시대는 ML 증분 학습용으로 배정
 * 예정(아직 서버 쪽 학습 스크립트 없음, ML/README.md 참고) - 비워둔다.
 * 뒤 단계가 앞 단계 결과물(json/sql)을 쓰므로 이 순서를 유지해야 한다.
 */

date_default_timezone_set("Asia/Seoul");

$LOCK_DIR = __DIR__ . DIRECTORY_SEPARATOR . ".." . DIRECTORY_SEPARATOR . "storage" . DIRECTORY_SEPARATOR . "exec_lock";
if (! is_dir($LOCK_DIR)) {
    mkdir($LOCK_DIR, 0777, true);
}

// process.php {command}를 실행한다. 오늘 이 command를 이미 실행했으면
// (락 파일 있음) 아무것도 안 하고 조용히 리턴한다.
function _exec($command)
{
    global $LOCK_DIR;

    $lock_file = $LOCK_DIR . DIRECTORY_SEPARATOR . date("Y-m-d") . "_" . $command . ".lock";
    if (is_file($lock_file)) {
        return;
    }
    file_put_contents($lock_file, date("c") . " 시작\n");

    $php_bin = PHP_BINARY;
    $process_php = __DIR__ . DIRECTORY_SEPARATOR . "process.php";
    $cmd = escapeshellarg($php_bin) . " " . escapeshellarg($process_php) . " " . escapeshellarg($command);

    echo "[" . date("c") . "] 실행: {$command}\n";
    passthru($cmd, $exit_code);
    echo "[" . date("c") . "] 종료 코드: {$exit_code}\n";

    file_put_contents($lock_file, date("c") . " 종료 (exit={$exit_code})\n", FILE_APPEND);
}

$hour = (int) date("G");

if ($hour == 3) { // 3시대 - 수집
    _exec("gather");
}

if ($hour == 4) { // 4시대 - DB 반영 (SQL 생성 -> 실행)
    _exec("parse_batch");
    _exec("exec_batch");
}

if ($hour == 5) { // 5시대 - 이미지 다운로드
    _exec("image");
}

if ($hour == 9) { // 9시대 - 연동 결과 텔레그램 리포트
    _exec("report");
}
