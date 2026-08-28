<?php
// process.php 반영 전 테스트용 - 수집(get_data.php)이 돌고 있는 동안은 건드리지 않기 위해 별도 파일로 작업
include "get_data.php";
include "Class_store_processor.php";

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

if($argv[1] == "collect") {
    $page = $argv[2] ? $argv[2] : 1;
    run_collect($page);
}
else if($argv[1] == "parse"){
    run_parse();
}
