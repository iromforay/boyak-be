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
    private $image_dir;

    public function __construct($link)
    {
        $this->link = $link;
        $this->log_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."logs".DIRECTORY_SEPARATOR.date("Y-m-d");
        $this->image_dir = __DIR__.DIRECTORY_SEPARATOR."..".DIRECTORY_SEPARATOR."storage".DIRECTORY_SEPARATOR."drug_image";
    }

    // $dry_run=true면 메시지만 만들어서 출력하고 텔레그램 전송은 건너뛴다
    // (php process.php report dev - 개발 중 반복 확인용).
    public function run($dry_run = false)
    {
        $text = $this->build_message();
        echo $text."\n";
        if($dry_run){
            echo "[dev] 텔레그램 전송 스킵\n";
            return;
        }
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

        $product_count = $this->get_product_count();
        $processed = null; // 오늘 parse_batch/exec_batch 기록이 있을 때만 채워짐 - 정합성 체크 1번줄에 씀

        $lines = [];
        $lines[] = "[보이는 알약] ".date("Y-m-d")." 연동 리포트";
        $lines[] = "";

        $lines[] = "■ 데이터 연동";
        if($parse){
            $changed = (int) $parse['changed'];
            // exec_batch가 SQL 파일별로 남긴 item_seq manifest 기준 - 파일 전체가
            // 하나의 트랜잭션이라 실패한 파일에 담긴 item_seq는 전부 롤백된 것이므로
            // 정확한 실패 건수/목록이다 (추정치 아님).
            $failed_seqs = $exec['failed_item_seqs'] ?? [];
            $failed = count($failed_seqs);
            $processed = max(0, $changed - $failed);
            $lines[] = "- 총 연동 데이터 : ".number_format($parse['total'])."건";
            $lines[] = "- 처리 완료 : ".number_format($processed)."건";
            $lines[] = "- 처리 실패 : ".number_format($failed)."건";
            if($failed_seqs){
                $lines[] = "  · 실패 item_seq : ".$this->format_seq_list($failed_seqs);
            }
        }else{
            $lines[] = "- 오늘 실행 기록 없음";
        }
        $lines[] = "";

        $lines[] = "■ DB 현황";
        $lines[] = "- 등록된 알약 : ".number_format($product_count)."건";
        $lines[] = "";

        $lines[] = "■ 이미지 연동";
        [$has_image, $with_url] = $this->get_image_coverage();
        $coverage_percent = $with_url > 0 ? round($has_image / $with_url * 100, 1) : 0;
        $lines[] = "- 전체 보유 : ".number_format($has_image)."/".number_format($with_url)."건 (".$coverage_percent."%)";
        if($image){
            $lines[] = "- 오늘 다운로드 : 성공 ".number_format($image['done'])."건, 실패 ".number_format($image['fail'])."건";
        }else{
            $lines[] = "- 오늘 다운로드 : 실행 기록 없음";
        }
        $lines[] = "";

        $lines[] = "■ 정합성 체크";
        if($processed !== null){
            $sync_rate = $product_count > 0 ? round(min($processed, $product_count) / $product_count * 100, 1) : 0;
            $lines[] = "- 연동데이터수(".number_format($processed).") = 실제데이터수(".number_format($product_count).") ... 연동율 ".$sync_rate."%";
        }else{
            $lines[] = "- 연동데이터수 측정 불가(오늘 parse_batch/exec_batch 실행 기록 없음)";
        }
        $image_rate = $product_count > 0 ? round(min($has_image, $product_count) / $product_count * 100, 1) : 0;
        $lines[] = "- 실제데이터수(".number_format($product_count).") = 이미지개수(".number_format($has_image).") ... 보유율 ".$image_rate."%";

        return implode("\n", $lines);
    }

    // 텔레그램 메시지가 너무 길어지지 않게 앞 20개만 보여주고 나머지는 개수로 요약한다.
    private const SEQ_LIST_LIMIT = 20;

    private function format_seq_list($seqs)
    {
        $shown = array_slice($seqs, 0, self::SEQ_LIST_LIMIT);
        $text = implode(", ", $shown);
        if(count($seqs) > self::SEQ_LIST_LIMIT){
            $text .= " 외 ".number_format(count($seqs) - self::SEQ_LIST_LIMIT)."건";
        }
        return $text;
    }

    private function get_product_count()
    {
        $result = $this->link->query("SELECT COUNT(*) AS cnt FROM product");
        if(! $result) return 0;
        return (int) $result->fetch_assoc()['cnt'];
    }

    // [로컬에 이미지 파일이 있는 알약 수, 이미지 URL을 가진 전체 알약 수]를 돌려준다.
    // 정확히 매칭하려면 product마다 파일 존재를 확인해야 하지만(느림), item_seq.jpg
    // 명명 규칙상 거의 1:1이라 storage/drug_image/의 파일 개수로 근사한다.
    private function get_image_coverage()
    {
        $result = $this->link->query("SELECT COUNT(*) AS cnt FROM product WHERE item_image IS NOT NULL AND item_image != ''");
        $with_url = $result ? (int) $result->fetch_assoc()['cnt'] : 0;

        $files = glob($this->image_dir.DIRECTORY_SEPARATOR."*.jpg");
        $has_image = $files ? count($files) : 0;

        return [$has_image, $with_url];
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
