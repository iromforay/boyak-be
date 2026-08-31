<?php

// run_parse() 가 사용하는 클래스.
// storage/data/[Y-m-d]/page_*.json (run_collect가 저장해둔 페이지 단위 원본) 을 스캔해서
// 아이템별로 캐시 체크 후 DB(product) 에 반영한다. 처리한 파일은 storage/data/done/[Y-m-d]/ 로 옮긴다.
class StoreProcessor{
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
