<?php
require_once __DIR__ . '/env.php';

function get_connect()
{
    $host = getenv('DB_HOST') ?: 'localhost';
    $user_id = getenv('DB_USER');
    $passwd = getenv('DB_PASS');
    $db = getenv('DB_NAME');

    $link = new mysqli($host, $user_id, $passwd, $db);
    mysqli_set_charset($link,"utf8");
    return $link;
}
?>