<?php

// run_parse() 가 사용하는 클래스.
// storage/data/[Y-m-d]/page_*.json (run_collect가 저장해둔 페이지 단위 원본) 을 스캔해서
// 아이템별로 캐시 체크 후 DB(product) 에 반영한다. 처리한 파일은 storage/data/done/[Y-m-d]/ 로 옮긴다.
class StoreProcessor{
    private $link;
    private $product;
    private $store_dir;
    private $done_dir;
    private $image_dir;
    private $image_concurrency = 16; // curl_multi 동시 요청 수 (429 방지 위해 낮게)
    private $image_batch_delay_ms = 300; // 청크 사이 대기 (서버 레이트리밋 방지)
    private $image_max_retry = 3; // 429로 실패한 건 재시도 횟수
    // SSL 인증서 검증 스위치. 로컬(Windows) 개발 환경엔 curl.cainfo(CA 번들)가 없어서 검증이 항상 실패한다(errno 60).
    // 운영 배포 시에는 true로 바꾼다.
    private $ssl_verify = false;

    // DB 연결과 저장 경로(store_dir/done_dir/image_dir)를 초기화한다.
    public function __construct($link)
    {
        $this->link = $link;
        $this->product = new Product($link);
        $this->store_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."data";
        $this->done_dir = $this->store_dir.DIRECTORY_SEPARATOR."done";
        $this->image_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."drug_image";
        if(! is_dir($this->done_dir)) mkdir($this->done_dir, 0777, true);
        if(! is_dir($this->image_dir)) mkdir($this->image_dir, 0777, true);
    }

    // storage/data/의 페이지 파일을 전부 찾아 process_file()로 DB 반영한다.
    public function run()
    {
        $files = glob($this->store_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json");
        echo "===================\n";
        echo "대상 파일 수 : ".count($files)."\n";

        foreach($files as $file){
            $this->process_file($file);
        }
    }

    // 페이지 파일 하나를 읽어 아이템별로 캐시체크+DB 반영 후 done으로 옮긴다.
    private function process_file($file)
    {
        echo "-------------------\n";
        echo "파일 : ".$file."\n";

        $json = file_get_contents($file);
        $items = json_decode($json);

        if(! is_array($items)){
            echo "파싱 실패, 스킵\n";
            return;
        }

        echo "아이템 수 : ".count($items)."\n";
        foreach($items as $item){
            if(! isset($item->ITEM_SEQ)){
                echo "ITEM_SEQ 없음, 스킵\n";
                continue;
            }
            $this->product->set_product($item->ITEM_SEQ, json_encode($item, JSON_UNESCAPED_UNICODE));
        }

        $this->move_done($file);
    }

    // run()과 같은 파일을 처리하지만, 아이템마다 쿼리를 날리는 대신 캐시를
    // 한 번에 통째로 읽어와 메모리에서 비교하고, 변경분에 대한 SQL은 DB에
    // 바로 실행하지 않고 storage/data/sql/{시각}.sql 로 파일만 남긴다.
    // 실제 DB 반영은 run_exec_batch()가 별도로 담당한다 (parse_batch로 SQL만
    // 만들어두고, exec_batch로 한번에 실행하는 2단계 구조).
    public function run_batch()
    {
        $files = glob($this->store_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json");
        echo "===================\n";
        echo "대상 파일 수 : ".count($files)."\n";

        $cache_map = $this->load_cache_map();
        echo "캐시 로드 : ".count($cache_map)."건\n";

        $rows = [];
        $image_targets = []; // 이번에 변경/신규로 확인된 아이템의 이미지 URL - image 단계가 재다운로드 대상으로 재사용
        $total = 0;

        foreach($files as $file){
            $json = file_get_contents($file);
            $items = json_decode($json);
            if(! is_array($items)){
                echo "파싱 실패, 스킵 : ".$file."\n";
                continue;
            }

            foreach($items as $item){
                if(! isset($item->ITEM_SEQ)) continue;
                $total++;
                $row = $this->build_row_for_item($item, $cache_map);
                if($row){
                    $rows[] = $row;
                    if(! empty($item->ITEM_IMAGE)){
                        $image_targets[$item->ITEM_SEQ] = $item->ITEM_IMAGE;
                    }
                }
            }
        }

        echo "전체 ".$total."건 중 변경 ".count($rows)."건\n";

        if($rows){
            $sql_dir = $this->store_dir.DIRECTORY_SEPARATOR."sql";
            if(! is_dir($sql_dir)) mkdir($sql_dir, 0777, true);
            $timestamp = date("Ymd_His");

            $sql_file = $sql_dir.DIRECTORY_SEPARATOR.$timestamp.".sql";
            $sql_text = $this->build_batch_sql($rows);
            file_put_contents($sql_file, $sql_text);
            echo "SQL 파일 저장 : ".$sql_file." (실행은 exec_batch에서)\n";

            if($image_targets){
                $images_file = $sql_dir.DIRECTORY_SEPARATOR.$timestamp.".images.json";
                file_put_contents($images_file, json_encode($image_targets, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
                echo "변경된 이미지 목록 저장 : ".$images_file." (".count($image_targets)."건, image 단계에서 소비)\n";
            }
        }else{
            echo "변경 없음, 생성할 SQL 없음\n";
        }

        foreach($files as $file){
            $this->move_done($file);
        }
    }

    // run_batch()가 storage/data/sql/ 에 쌓아둔 .sql 파일들을 순서대로(파일명=시각
    // 기준) 하나씩 multi_query로 실행한다. 실행 끝난 파일은 storage/data/sql/done/
    // 으로 옮겨서 재실행해도 중복 실행되지 않는다.
    public function run_exec_batch()
    {
        $sql_dir = $this->store_dir.DIRECTORY_SEPARATOR."sql";
        $done_dir = $sql_dir.DIRECTORY_SEPARATOR."done";
        if(! is_dir($done_dir)) mkdir($done_dir, 0777, true);

        $files = glob($sql_dir.DIRECTORY_SEPARATOR."*.sql");
        sort($files); // 파일명이 시각(Ymd_His)이라 정렬하면 생성 순서대로 실행됨
        echo "===================\n";
        echo "실행 대상 SQL 파일 수 : ".count($files)."\n";

        foreach($files as $file){
            echo "-------------------\n";
            echo "실행 : ".$file."\n";
            $sql_text = file_get_contents($file);
            $ok = $this->run_sql_batch($sql_text);
            if($ok){
                rename($file, $done_dir.DIRECTORY_SEPARATOR.basename($file));
            }else{
                // 실패한 파일은 done으로 옮기지 않고 그대로 둔다 (원인 고친 뒤 재실행하면
                // 다음 exec_batch 호출 때 다시 이 파일부터 시도된다). 이후 파일들도
                // 순서가 꼬이면 안 되니 여기서 멈춘다.
                echo "실패해서 done으로 안 옮김, 뒤 파일은 건너뛴다 : ".$file."\n";
                break;
            }
        }
    }

    // cache 테이블 전체를 한 번에 읽어 [item_seq => hash] 배열로 돌려준다.
    private function load_cache_map()
    {
        $map = [];
        $result = $this->link->query("SELECT `key`, `val` FROM `cache`");
        while($row = $result->fetch_assoc()){
            $map[$row['key']] = $row['val'];
        }
        return $map;
    }

    // product 테이블 컬럼 순서. build_row_for_item()의 VALUES(...) 순서와
    // build_batch_sql()의 INSERT INTO product(...) 순서가 반드시 일치해야 한다.
    private const PRODUCT_COLUMNS = "item_seq,item_name,entp_seq,entp_name,chart,item_image,print_front,print_back,
        drug_shape,color_class1,color_class2,line_front,line_back,leng_long,leng_short,thick,img_regist_ts,
        class_no,class_name,etc_otc_name,item_permit_date,form_code_name,mark_code_front_anal,mark_code_back_anal,
        mark_code_front_img,mark_code_back_img,item_eng_name,change_date,mark_code_front,mark_code_back,edi_code,reg_date";

    // 아이템 1건을 캐시맵과 비교해서, 변경 없으면 null, 변경/신규면 이 아이템의
    // cache VALUES(...) 조각과 product VALUES(...) 조각을 돌려준다 (개별
    // INSERT 문이 아니라, 나중에 build_batch_sql()이 여러 건을 묶어서 하나의
    // 멀티row INSERT로 만들 재료). $cache_map은 참조로 받아 같은 배치 안에서
    // item_seq가 또 나와도 최신 해시 기준으로 비교되게 갱신해둔다.
    private function build_row_for_item($item, &$cache_map)
    {
        $key = $item->ITEM_SEQ;
        $json = json_encode($item, JSON_UNESCAPED_UNICODE);
        $hash = md5($json);

        if(isset($cache_map[$key]) && $cache_map[$key] === $hash){
            return null;
        }
        $cache_map[$key] = $hash;

        $shape_key = $this->product->shapeKeyFromLabel($item->DRUG_SHAPE);
        $color_key = $this->product->colorKeyFromLabel($item->COLOR_CLASS1);

        $cache_values = sprintf("('%s','%s',now())", $this->esc($key), $hash);

        $product_values = sprintf(
            "('%s','%s','%s','%s','%s','%s','%s','%s',
            '%s','%s','%s','%s','%s',%f,%f,%f,'%s',
            '%s','%s','%s','%s','%s','%s','%s',
            '%s','%s','%s','%s','%s','%s','%s',now())",
            $this->esc($item->ITEM_SEQ), $this->esc($item->ITEM_NAME), $this->esc($item->ENTP_SEQ), $this->esc($item->ENTP_NAME), $this->esc($item->CHART), $this->esc($item->ITEM_IMAGE), $this->esc($item->PRINT_FRONT), $this->esc($item->PRINT_BACK),
            $shape_key, $color_key, $this->esc($item->COLOR_CLASS2), $this->esc($item->LINE_FRONT), $this->esc($item->LINE_BACK), (float)$item->LENG_LONG, (float)$item->LENG_SHORT, (float)$item->THICK, $this->esc($item->IMG_REGIST_TS),
            $this->esc($item->CLASS_NO), $this->esc($item->CLASS_NAME), $this->esc($item->ETC_OTC_NAME), $this->esc($item->ITEM_PERMIT_DATE), $this->esc($item->FORM_CODE_NAME), $this->esc($item->MARK_CODE_FRONT_ANAL), $this->esc($item->MARK_CODE_BACK_ANAL),
            $this->esc($item->MARK_CODE_FRONT_IMG), $this->esc($item->MARK_CODE_BACK_IMG), $this->esc($item->ITEM_ENG_NAME), $this->esc($item->CHANGE_DATE), $this->esc($item->MARK_CODE_FRONT), $this->esc($item->MARK_CODE_BACK), $this->esc($item->EDI_CODE)
        );

        return [
            'key' => $key,
            'cache_values' => $cache_values,
            'product_values' => $product_values,
        ];
    }

    // build_row_for_item()이 모은 row들을 멀티row INSERT 3~4문으로 묶는다.
    // (개별 문장 N개 대신 "INSERT ... VALUES (...),(...),(...)" 묶음 문장으로
    // 처리해야 MySQL이 훨씬 빠르다 - 파싱/인덱스 갱신이 문장당 1번씩만 든다.)
    // max_allowed_packet 초과를 피하려고 CHUNK_SIZE개씩 끊어서 여러 배치로 나눈다.
    private const CHUNK_SIZE = 500;

    private function build_batch_sql($rows)
    {
        $sql = "START TRANSACTION;\n";

        foreach(array_chunk($rows, self::CHUNK_SIZE) as $chunk){
            $keys = array_map(fn($r) => "'".$this->esc($r['key'])."'", $chunk);
            $cache_values = array_column($chunk, 'cache_values');
            $product_values = array_column($chunk, 'product_values');

            $sql .= "INSERT INTO `cache` (`key`,`val`,reg_date) VALUES\n"
                . implode(",\n", $cache_values)
                . "\nON DUPLICATE KEY UPDATE `val`=VALUES(`val`), up_date=now();\n";

            $sql .= "DELETE FROM product WHERE item_seq IN (" . implode(",", $keys) . ");\n";

            $sql .= "INSERT INTO product (" . self::PRODUCT_COLUMNS . ") VALUES\n"
                . implode(",\n", $product_values)
                . ";\n";
        }

        $sql .= "COMMIT;\n";
        return $sql;
    }

    private function esc($v)
    {
        return addslashes((string)($v ?? ''));
    }

    // 모아둔 SQL 텍스트(세미콜론으로 구분된 여러 statement)를 한 번에 실행한다.
    // PHP 8.1+에서 mysqli는 기본적으로 에러 시 예외를 던진다 - 여러 statement
    // 중 하나(예: 컬럼 길이 초과)가 실패하면 잡아서 롤백하고 false를 돌려준다.
    // (이걸 안 잡으면 전체 PHP 프로세스가 죽어서, 이 파일 뒤에 든 아직
    // 손도 안 댄 데이터까지 통째로 유실되는 걸 겪은 적이 있다.)
    private function run_sql_batch($sql_text)
    {
        try{
            if($this->link->multi_query($sql_text)){
                do{
                    if($result = $this->link->store_result()){
                        $result->free();
                    }
                }while($this->link->more_results() && $this->link->next_result());
            }
        }catch(\mysqli_sql_exception $e){
            echo "SQL 실행 오류(예외) : ".$e->getMessage()."\n";
            $this->drain_and_rollback();
            return false;
        }

        if($this->link->errno){
            echo "SQL 실행 오류 : ".$this->link->error."\n";
            $this->drain_and_rollback();
            return false;
        }

        echo "SQL 실행 완료\n";
        return true;
    }

    // 실패 후 커넥션에 남아있을 수 있는 결과셋을 비우고(안 하면 다음 쿼리가
    // "commands out of sync" 에러가 날 수 있음) 트랜잭션을 롤백한다.
    private function drain_and_rollback()
    {
        try{
            while($this->link->more_results() && @$this->link->next_result()){
                if($result = $this->link->store_result()){
                    $result->free();
                }
            }
        }catch(\mysqli_sql_exception $e){
            // 결과셋 비우기 중 예외는 무시 - 롤백만 확실히 하면 된다.
        }
        try{
            $this->link->query("ROLLBACK");
        }catch(\mysqli_sql_exception $e){
        }
    }

    // 처리 완료된 파일을 storage/data/done/[Y-m-d]/ 로 이동한다.
    private function move_done($file)
    {
        // storage/data/2026-08-28/page_001.json -> storage/data/done/2026-08-28/page_001.json
        $date_dir = basename(dirname($file));
        $target_dir = $this->done_dir.DIRECTORY_SEPARATOR.$date_dir;
        if(! is_dir($target_dir)) mkdir($target_dir, 0777, true);
        rename($file, $target_dir.DIRECTORY_SEPARATOR.basename($file));
        echo "처리 완료, 이동 : ".$target_dir.DIRECTORY_SEPARATOR.basename($file)."\n";
    }

    // 파싱 전/후 json 파일을 모두 읽어 (item_seq, image_url) 목록을 모은 뒤 download_images()에 넘긴다.
    public function run_image()
    {
        $files = array_merge(
            glob($this->store_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json"),
            glob($this->done_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json")
        );
        echo "===================\n";
        echo "이미지 대상 파일 수 : ".count($files)."\n";

        $targets = [];
        foreach($files as $file){
            $json = file_get_contents($file);
            $items = json_decode($json);
            if(! is_array($items)) continue;

            foreach($items as $item){
                if(! isset($item->ITEM_SEQ) || empty($item->ITEM_IMAGE)) continue;
                $targets[$item->ITEM_SEQ] = $item->ITEM_IMAGE; // item_seq 중복은 마지막 것으로 자연 정리됨
            }
        }

        echo "이미지 대상 건수 : ".count($targets)."\n";
        $this->download_images($targets);
    }

    // (item_seq => image_url) 목록을 curl_multi_exec로 동시에(concurrency 개씩) 다운로드한다.
    // PowerShell/wget shell_exec 방식(건당 프로세스 1개, 순차 대기) 대신 프로세스 스폰 없이 병렬 처리한다.
    // 429(rate limit)로 실패한 건은 대기 후 재시도한다.
    private function download_images($targets)
    {
        $total = count($targets);
        $done = 0;
        $fail = 0;
        $remaining = $targets;

        for($retry=0; $retry<=$this->image_max_retry && ! empty($remaining); $retry++){
            if($retry > 0){
                $wait = $retry * 3;
                echo "429 재시도 대상 ".count($remaining)."건, ".$wait."초 대기 후 재시도 (".$retry."/".$this->image_max_retry.")\n";
                sleep($wait);
            }

            $next_remaining = [];
            foreach(array_chunk($remaining, $this->image_concurrency, true) as $chunk){
                $mh = curl_multi_init();
                $handles = [];

                foreach($chunk as $seq => $url){
                    $path = $this->image_dir.DIRECTORY_SEPARATOR.$seq.".jpg";
                    $fp = fopen($path, 'w');
                    $ch = curl_init($url);
                    curl_setopt($ch, CURLOPT_FILE, $fp);
                    curl_setopt($ch, CURLOPT_TIMEOUT, 15);
                    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
                    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, $this->ssl_verify);
                    curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, $this->ssl_verify ? 2 : 0);
                    curl_multi_add_handle($mh, $ch);
                    $handles[$seq] = ['ch'=>$ch, 'fp'=>$fp, 'path'=>$path];
                }

                $running = null;
                do{
                    curl_multi_exec($mh, $running);
                    curl_multi_select($mh);
                }while($running > 0);

                foreach($handles as $seq => $h){
                    fclose($h['fp']);
                    $code = curl_getinfo($h['ch'], CURLINFO_HTTP_CODE);
                    if($code == 200){
                        $done++;
                        echo "저장 : ".$h['path']."\n";
                    }else if($code == 429){
                        @unlink($h['path']);
                        $next_remaining[$seq] = $chunk[$seq];
                    }else{
                        $fail++;
                        @unlink($h['path']);
                        echo "실패(".$code.") : ".$seq."\n";
                    }
                    curl_multi_remove_handle($mh, $h['ch']);
                    curl_close($h['ch']);
                }
                curl_multi_close($mh);

                usleep($this->image_batch_delay_ms * 1000);
            }

            $remaining = $next_remaining;
        }

        foreach($remaining as $seq => $url){
            $fail++;
            echo "실패(429, 재시도 소진) : ".$seq."\n";
        }

        echo "이미지 다운로드 완료 : ".$done."/".$total." (실패 ".$fail.")\n";
    }
}
