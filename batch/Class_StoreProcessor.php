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
                    // item_seq를 키로 써서, 같은 배치 안에 같은 item_seq가 여러 번
                    // (예: 오래된 미처리 파일이 오늘 파일과 섞여 다른 스냅샷으로
                    // 잡히는 경우) 나와도 한 건만 남는다 - 안 그러면 한 멀티row
                    // INSERT 안에 같은 유니크키가 두 번 들어가서 에러난다.
                    $rows[$row['key']] = $row;
                    if(! empty($item->ITEM_IMAGE)){
                        $image_targets[$item->ITEM_SEQ] = $item->ITEM_IMAGE;
                    }
                }
            }
        }

        echo "전체 ".$total."건 중 변경 ".count($rows)."건\n";

        $changed = count($rows);

        if($rows){
            $sql_dir = $this->store_dir.DIRECTORY_SEPARATOR."sql";
            if(! is_dir($sql_dir)) mkdir($sql_dir, 0777, true);
            $timestamp = date("Ymd_His");

            $sql_file = $sql_dir.DIRECTORY_SEPARATOR.$timestamp.".sql";
            $sql_text = $this->build_batch_sql($rows);
            file_put_contents($sql_file, $sql_text);
            echo "SQL 파일 저장 : ".$sql_file." (실행은 exec_batch에서)\n";

            // 이 파일에 담긴 item_seq 목록. 파일 전체가 하나의 트랜잭션이라 실행이
            // 실패하면 이 목록 전부가 롤백된 것 - exec_batch가 실패 시 어떤
            // item_seq가 반영 안 됐는지 추적하는 데 쓴다.
            $items_file = $this->items_manifest_path($sql_file);
            file_put_contents($items_file, json_encode(array_keys($rows), JSON_UNESCAPED_UNICODE));

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

        return ['total' => $total, 'changed' => $changed];
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

        $success = 0;
        $failed_item_seqs = [];
        foreach($files as $file){
            echo "-------------------\n";
            echo "실행 : ".$file."\n";
            $sql_text = file_get_contents($file);
            $result = $this->run_sql_batch($sql_text);
            $items_file = $this->items_manifest_path($file);
            if($result === true){
                rename($file, $done_dir.DIRECTORY_SEPARATOR.basename($file));
                if(is_file($items_file)){
                    rename($items_file, $done_dir.DIRECTORY_SEPARATOR.basename($items_file));
                }
                $success++;
            }else{
                // 실패한 파일은 done으로 옮기지 않고 그대로 둔다 (원인 고친 뒤 재실행하면
                // 다음 exec_batch 호출 때 다시 이 파일부터 시도된다). 이후 파일들도
                // 순서가 꼬이면 안 되니 여기서 멈춘다.
                echo "실패해서 done으로 안 옮김, 뒤 파일은 건너뛴다 : ".$file."\n";
                // 파일 전체가 한 트랜잭션이라 실패하면 이 파일에 담긴 item_seq 전부가
                // 롤백된 것 - manifest에서 읽어와 실패 목록에 더하고, 원인(에러 메시지)과
                // 함께 storage/logs/sql_failures.jsonl 에 날짜 상관없이 계속 누적 기록한다
                // (스키마/데이터 매칭 문제 디버깅용 - report의 당일 stats.json과 달리
                // 여기는 지우지 않고 계속 쌓인다).
                $seqs = [];
                if(is_file($items_file)){
                    $decoded = json_decode(file_get_contents($items_file), true);
                    if(is_array($decoded)){
                        $seqs = $decoded;
                        $failed_item_seqs = array_merge($failed_item_seqs, $seqs);
                    }
                }
                $this->log_sql_failure($file, $result, $seqs);
                break;
            }
        }

        return [
            'total' => count($files),
            'success' => $success,
            'fail' => count($files) - $success,
            'failed_item_seqs' => $failed_item_seqs,
        ];
    }

    // SQL 실행 실패 건을 storage/logs/sql_failures.jsonl 에 한 줄씩 계속 누적
    // 기록한다(날짜 폴더 구분 없이 append-only). openAPI 스펙이 바뀌어 컬럼
    // 길이 초과/형식 불일치 등으로 실패하는 경우, 나중에 item_seq로 grep해서
    // 언제 무슨 에러로 실패했는지 히스토리를 볼 수 있게 하기 위함 - 당일
    // report용 stats.json(그날 것만 남고 덮어써짐)과는 용도가 다르다.
    private function log_sql_failure($sql_file, $error, $item_seqs)
    {
        $log_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."logs";
        if(! is_dir($log_dir)) mkdir($log_dir, 0777, true);

        $entry = [
            'time' => date('c'),
            'sql_file' => basename($sql_file),
            'error' => $error,
            'item_seq' => $item_seqs,
        ];
        file_put_contents(
            $log_dir.DIRECTORY_SEPARATOR."sql_failures.jsonl",
            json_encode($entry, JSON_UNESCAPED_UNICODE)."\n",
            FILE_APPEND
        );
    }

    // sql 파일 경로에서 대응되는 item_seq manifest(.items.json) 경로를 만든다.
    private function items_manifest_path($sql_file)
    {
        return preg_replace('/\.sql$/', '.items.json', $sql_file);
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
        $color1_raw = trim((string)$item->COLOR_CLASS1);
        $color2_raw = isset($item->COLOR_CLASS2) ? trim((string)$item->COLOR_CLASS2) : '';
        [$color1_key, $color2_key] = $this->product->colorKeysFromBoth($color1_raw, $color2_raw);
        // color_class2가 없으면(단일색) '' 대신 진짜 SQL NULL을 넣어야 하므로,
        // 이 자리만 따옴표 없는 원시 SQL 조각으로 끼워넣는다.
        $color2_sql = $color2_key === null ? 'NULL' : "'" . $this->esc($color2_key) . "'";

        $cache_values = sprintf("('%s','%s',now())", $this->esc($key), $hash);

        $product_values = sprintf(
            "('%s','%s','%s','%s','%s','%s','%s','%s',
            '%s','%s',%s,'%s','%s',%f,%f,%f,'%s',
            '%s','%s','%s','%s','%s','%s','%s',
            '%s','%s','%s','%s','%s','%s','%s',now())",
            $this->esc($item->ITEM_SEQ), $this->esc($item->ITEM_NAME), $this->esc($item->ENTP_SEQ), $this->esc($item->ENTP_NAME), $this->esc($item->CHART), $this->esc($item->ITEM_IMAGE), $this->esc($item->PRINT_FRONT), $this->esc($item->PRINT_BACK),
            $shape_key, $this->esc($color1_key), $color2_sql, $this->esc($item->LINE_FRONT), $this->esc($item->LINE_BACK), (float)$item->LENG_LONG, (float)$item->LENG_SHORT, (float)$item->THICK, $this->esc($item->IMG_REGIST_TS),
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
    // 성공하면 true, 실패하면 원인 파악용으로 에러 메시지(string)를 돌려준다.
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
            return $e->getMessage();
        }

        if($this->link->errno){
            echo "SQL 실행 오류 : ".$this->link->error."\n";
            $this->drain_and_rollback();
            return $this->link->error;
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

    // 매일 전체를 다 재다운로드하지 않도록, 대상을 두 갈래로만 좁힌다:
    //   1) 변경분 - parse_batch가 "오늘 데이터 바뀐 아이템"으로 이미 판단해서
    //      storage/data/sql/*.images.json 에 남겨둔 목록 (무조건 다시 받음)
    //   2) 누락분 - 전체 json을 훑어보되, 로컬에 파일이 아직 없는 것만 (예전
    //      실패분 백필). 이미 파일이 있으면 데이터가 안 바뀌었다는 뜻이므로 건너뜀.
    // $only_missing=true면 변경분 목록은 파일 소비(images_done 이동)만 하고
    // 재다운로드 대상에서는 뺀다 - full_sync처럼 cache를 통째로 비워서
    // 전체가 "변경"으로 잡힌 경우, 이미 로컬에 있는 이미지까지 전부 다시
    // 받는 걸 막기 위함 (이미지 자체는 product/cache 초기화와 무관하게 유효).
    public function run_image($only_missing = false)
    {
        $changed = $this->load_changed_image_targets();
        if($only_missing){
            $changed = [];
        }
        $missing = $this->load_missing_image_targets();
        $targets = $changed + $missing; // 키(item_seq) 겹치면 changed 값이 우선 유지됨

        echo "===================\n";
        echo "이미지 대상 : 변경분 ".count($changed)."건 + 누락분 ".count($missing)."건 = 총 ".count($targets)."건\n";
        return $this->download_images($targets);
    }

    // run_batch()가 남겨둔 storage/data/sql/*.images.json (아직 소비 안 된 것들)을
    // 전부 읽어 [item_seq => image_url]로 합친다. 다 읽은 파일은 sql/images_done/
    // 으로 옮겨서 다음 실행 때 또 안 읽히게 한다.
    private function load_changed_image_targets()
    {
        $sql_dir = $this->store_dir.DIRECTORY_SEPARATOR."sql";
        $sql_done_dir = $sql_dir.DIRECTORY_SEPARATOR."done";
        $images_done_dir = $sql_dir.DIRECTORY_SEPARATOR."images_done";
        if(! is_dir($images_done_dir)) mkdir($images_done_dir, 0777, true);

        $files = array_merge(
            glob($sql_dir.DIRECTORY_SEPARATOR."*.images.json"),
            glob($sql_done_dir.DIRECTORY_SEPARATOR."*.images.json")
        );
        sort($files); // 시각순 - 나중 파일이 같은 item_seq를 덮어쓰게

        $targets = [];
        foreach($files as $file){
            $map = json_decode(file_get_contents($file), true);
            if(is_array($map)){
                foreach($map as $seq => $url){
                    $targets[$seq] = $url;
                }
            }
            rename($file, $images_done_dir.DIRECTORY_SEPARATOR.basename($file));
        }
        return $targets;
    }

    // 파싱 전/후 json을 전부 훑어서, 로컬에 이미지 파일이 없는 item_seq만 골라낸다.
    private function load_missing_image_targets()
    {
        $files = array_merge(
            glob($this->store_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json"),
            glob($this->done_dir.DIRECTORY_SEPARATOR."*".DIRECTORY_SEPARATOR."page_*.json")
        );

        $targets = [];
        foreach($files as $file){
            $json = file_get_contents($file);
            $items = json_decode($json);
            if(! is_array($items)) continue;

            foreach($items as $item){
                if(! isset($item->ITEM_SEQ) || empty($item->ITEM_IMAGE)) continue;
                $path = $this->image_dir.DIRECTORY_SEPARATOR.$item->ITEM_SEQ.".jpg";
                if(! is_file($path)){
                    $targets[$item->ITEM_SEQ] = $item->ITEM_IMAGE;
                }
            }
        }
        return $targets;
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

        return ['total' => $total, 'done' => $done, 'fail' => $fail];
    }
}
