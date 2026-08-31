<?php
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

if($argv[1] == "test") {
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