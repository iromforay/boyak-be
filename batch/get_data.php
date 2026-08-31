<?php
/*
1일 1회 데이터 연동을 함
동일한 데이터는 넘어가고 ,
수정된 데이터,수정 데이터 만 처리 , 삭제데이터는 알수 없으니 쌓아둔다

#식품의약품안전처_의약품 낱알식별 정보
https://www.data.go.kr/data/15057639/openapi.do#/API%20%EB%AA%A9%EB%A1%9D/getMdcinGrnIdntfcInfoList03
*/
date_default_timezone_set("Asia/Seoul");
include "connect.php";
include "Class_common.php";

class Medicine{
    private $domain;
    private $get_product; //조회
    private $common; //db 처리 - common table (수집 진행 상태만 기록)
    private $stock_product; //재고변경
    private $serviceKey;
    /*
    * 상품등록 기준 :
     */
    public function __construct()
    {
        //https://www.health.kr/
        //https://www.data.go.kr/data/15057639/openapi.do
        $this->domain = "http://apis.data.go.kr/";
        $this->serviceKey = getenv('MFDS_SERVICE_KEY');
        //&pageNo=1&numOfRows=3&type=json

        //$this->get_product ="1471000/MdcinGrnIdntfcInfoService01/getMdcinGrnIdntfcInfoList01?"; //조회
        $this->get_product ="1471000/MdcinGrnIdntfcInfoService03/getMdcinGrnIdntfcInfoList03?"; //조회
        /*
        $this->put_product="list/open_api_process.html?mode=save&type=product&process=create"; //등록
        $this->set_product ="list/open_api_process.html?mode=save&type=product&process=update" ; //수정
        $this->stock_product="list/open_api_process.html?mode=save&type=product&process=stock"; //재고변경
        $this->header='-H "Shopkey:6699ebffcaa6a22a1a6d94b44480b4b1" -H "Licensekey:NDg5NTZkNDk2NzE5YzMwMjgxNzgzMjY3YTQ1NTM0MmU="';
        */
        $link = get_connect();
        $this->common = new Common($link);
        return $this;
    }
    public function is_win()
    {
        echo __DIR__."\n";
        echo PHP_OS."\n";
        //  print_r($_SERVER);
    }
    public function go($start=1)
    {
        /* 기록 데이터 */
        $totalCount = (int) $this->common->getKey('totalCount'); //총 데이터 수
        $numOfRows = 300; // 페이지당 최대수량 - 일딴 고정
        // $numOfRows = $this->common->getKey('numOfRows'); //페이지당 데이터수

        //$numOfRows= $numOfRows=="" ? 300 : $numOfRows;

        if ($totalCount <= 0) {
            // 최초 실행 등으로 totalCount를 아직 모를 때: 일단 1페이지만 돌려서
            // 실제 totalCount를 알아낸다 (curl() 안에서 common에 저장됨).
            $totalCount = $numOfRows;
        }

        if(PHP_OS =="WINNT"){
            //$numOfRows = 3;
            $loop = ceil($totalCount/$numOfRows);

            //  $loop=2;
        }else {

            $loop = ceil($totalCount/$numOfRows);
        }
        echo "===================\n";
        // echo "pageNo : ".$page."\n";
        echo "totalCount : ".$totalCount."\n";
        echo "numOfRows : ".$numOfRows."\n";
        echo "loop count : ".$loop."\n";

        $fail_streak = 0;
        for($page=$start;$page<=$loop;$page++){
            $result = $this->curl($this->get_product,$page,$numOfRows);
            if(is_string($result) && strpos($result,'no|')===0){
                $fail_streak++;
                if($fail_streak>=5){
                    echo "연속 ".$fail_streak."회 실패. 배치를 중단한다. (page ".$page.")\n";
                    break;
                }
            }else{
                $fail_streak = 0;
                $this->down($result);
            }
        }
    }
    public function once($code)
    {

        $result =  $this->curl($this->get_product."item_seq=".$code."&",0,0);
        $this->down($result);
        exit;
    }
    private function curl($url , $page=1, $numOfRows = 300, $retry=3)
    {
        if(PHP_OS =="WINNT"){
            //shell_exec("powershell -Command cls");
            $common = 'Invoke-WebRequest -TimeoutSec 15  -ContentType  \'application/json\' -Method GET';
            $add_url = "";
            if($page>1 && $numOfRows>1){
                $add_url = "&pageNo=".$page."&numOfRows=".$numOfRows;
            }
            $cmd = 'powershell -Command "('.$common." '".$this->domain.$url."serviceKey=".$this->serviceKey.$add_url."&type=json'".').Content"';

        }else{
            $common = "curl --connect-timeout 5 -m 20 -s -X GET";
            $cmd = $common.' "'.$this->domain.$url.'serviceKey='.$this->serviceKey.'&pageNo='.$page.'&numOfRows='.$numOfRows.'&type=json"';


        }
        $rtn = shell_exec($cmd);
        $data = json_decode($rtn);
        echo "===============\n";
        echo "cmd : $cmd\n";

        // echo "rtn: ".$rtn."\n";

        if(!is_object($data)){
            // 타임아웃/네트워크 오류 등으로 응답을 못 받은 경우: 재시도 후에도 안되면
            // 이 페이지만 건너뛰고(로그만 남기고) 배치 전체는 계속 진행한다.
            if($retry>0){
                echo "응답 없음. 재시도 남은 횟수:".$retry."\n";
                sleep(2);
                return $this->curl($url,$page,$numOfRows,$retry-1);
            }
            echo "page ".$page." 요청 실패 (재시도 소진). 이 페이지는 건너뛴다.\n";
            return "no|empty_response";
        }

        if(isset($data->header->resultCode) and $data->header->resultCode != "00"){
            echo "NO resultCode error : ".$data->header->resultCode."\n";
            return "no|".$data->header->resultMsg;
        }else{
            if(! is_object($data->body)) $data->body = (object)$data->body;
            echo "page No ".$data->body->pageNo."\n";
            echo "totalCount ".$data->body->totalCount."\n";
            echo "numOfRows ".$data->body->numOfRows."\n";


            $this->common->setKey('totalCount',$data->body->totalCount);
            // $this->common->setKey('numOfRows',$data->body->numOfRows);

            if(isset($data->body->items)){
                echo "items count:".count($data->body->items)."\n";
            }


        }

        return $data;
    }
    // curl()이 받아온 API 응답(페이지 1건, 최대 300개 아이템)을 통째로
    // storage/data/[Y-m-d]/page_{pageNo}.json 으로 저장한다. (건별이 아니라 페이지 단위 - 쓰기 횟수를 줄이기 위함)
    // DB 처리(캐시 체크, insert/update)는 여기서 하지 않는다 - 별도 파싱 단계(run_parse)의 몫.
    public function down($result)
    {
        if(! is_object($result) || ! isset($result->body->items)) return $this;

        $dir = __DIR__."\..\storage\data\\".date("Y-m-d");
        if(! is_dir($dir)) mkdir($dir, 0777, true);

        $pageNo = isset($result->body->pageNo) ? $result->body->pageNo : 0;
        $file = $dir."\\page_".sprintf("%03d",$pageNo).".json";
        file_put_contents($file, json_encode($result->body->items, JSON_UNESCAPED_UNICODE));
        echo "저장 : ".$file."\n";
        return $this;
    }
}