<?php

// process.php의 'report' 명령이 사용하는 클래스. 매일 9시대에 exec.php가 호출한다.
// parse_batch/exec_batch/image가 남긴 storage/logs/[Y-m-d]/*.stats.json 을 모으고
// product 테이블 건수를 조회해 텔레그램으로 요약을 보낸다.
// (ML 학습 결과는 아직 없음 - ML/README.md 기준 학습은 로컬 전용이라 서버에
// 붙는 시점에 이 리포트에도 추가하면 됨. ML 증분 학습은 7시대에 배정될 예정.)
class Reporter
{
    private $link;
    private $log_dir;

    public function __construct($link)
    {
        $this->link = $link;
        $this->log_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."logs".DIRECTORY_SEPARATOR.date("Y-m-d");
    }

    public function run()
    {
        $text = $this->build_message();
        echo $text."\n";
        $this->send_telegram($text);
    }

    private function read_stats($command)
    {
        $file = $this->log_dir.DIRECTORY_SEPARATOR.$command.".stats.json";
        if(! is_file($file)) return null;
        $data = json_decode(file_get_contents($file), true);
        return is_array($data) ? $data : null;
    }

    private function build_message()
    {
        $parse = $this->read_stats('parse_batch'); // ['total'=>, 'changed'=>]
        $exec = $this->read_stats('exec_batch');   // ['total'=>, 'success'=>, 'fail'=>]
        $image = $this->read_stats('image');       // ['total'=>, 'done'=>, 'fail'=>]

        $lines = [];
        $lines[] = "[보이는 알약] ".date("Y-m-d")." 연동 리포트";
        $lines[] = "";

        $lines[] = "■ 데이터 연동";
        if($parse){
            $changed = (int) $parse['changed'];
            // exec_batch가 남긴 파일 단위 성공/실패 비율로, parse_batch가 감지한
            // 변경건수 중 실제 DB에 반영된 건수를 추정한다 (파일 내부는
            // 트랜잭션이라 보통 파일 1개 = 그날 실행 전체이므로 대부분 0/전체).
            if($exec && $exec['total'] > 0){
                $processed = (int) round($changed * $exec['success'] / $exec['total']);
                $failed = $changed - $processed;
            }else{
                $processed = 0;
                $failed = 0;
            }
            $lines[] = "- 총 연동 데이터 : ".number_format($parse['total'])."건";
            $lines[] = "- 처리 완료 : ".number_format($processed)."건";
            $lines[] = "- 처리 실패 : ".number_format($failed)."건";
        }else{
            $lines[] = "- 오늘 실행 기록 없음";
        }
        $lines[] = "";

        $lines[] = "■ DB 현황";
        $lines[] = "- 등록된 알약 : ".number_format($this->get_product_count())."건";
        $lines[] = "";

        $lines[] = "■ 이미지 연동";
        if($image){
            $lines[] = "- 성공 : ".number_format($image['done'])."건";
            $lines[] = "- 실패 : ".number_format($image['fail'])."건";
        }else{
            $lines[] = "- 오늘 실행 기록 없음";
        }

        return implode("\n", $lines);
    }

    private function get_product_count()
    {
        $result = $this->link->query("SELECT COUNT(*) AS cnt FROM product");
        if(! $result) return 0;
        return (int) $result->fetch_assoc()['cnt'];
    }

    // .env의 telegram.Secret / telegram.CHAT_ID / telegram.BOT_ID 로 전송한다.
    // telegram.Secret이 "봇숫자:토큰" 전체 형태면 그대로 쓰고, 토큰만 들어있으면
    // telegram.BOT_ID를 앞에 붙여 완성한다.
    private function send_telegram($text)
    {
        $secret = getenv('telegram.Secret');
        $bot_id = getenv('telegram.BOT_ID');
        $chat_id = getenv('telegram.CHAT_ID');

        if(! $secret || ! $chat_id){
            echo "텔레그램 설정 없음 (.env telegram.Secret / telegram.CHAT_ID), 전송 스킵\n";
            return;
        }

        $token = (strpos($secret, ':') !== false || ! $bot_id) ? $secret : ($bot_id.":".$secret);
        $query = http_build_query([
            'chat_id' => $chat_id,
            'text' => $text,
        ]);
        $url = "https://api.telegram.org/bot".$token."/sendMessage?".$query;

        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 15);
        $response = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if($code == 200){
            echo "텔레그램 전송 완료\n";
        }else{
            echo "url : ".$url."\n";
            echo "텔레그램 전송 실패(".$code.") : ".$response."\n";
        }
    }
}
