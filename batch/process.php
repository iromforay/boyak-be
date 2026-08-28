<?php
include "get_data.php";

$med = new Medicine();
if($argv[1] == "test") {
    // 두번째 인자가 있으면 그 페이지부터, 없으면 1페이지부터 시작
    $page = isset($argv[2]) && $argv[2] !== '' ? (int)$argv[2] : 1;
    $med->go($page);

}
else if($argv[1] =="image"){
    // $med->test_image();
}
else if($argv[1] == 'once_exec'){
    $med->once($argv[2]);
}