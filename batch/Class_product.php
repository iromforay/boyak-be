<?php

class Product {
    private $link;
    private $status;
    public function __construct($link){
        $this->link = $link;
    }
    public function cache($key,$val)
    {    echo "\n\n\n";
        echo "[".$key."] 처리\n";
        $val= addslashes ($val);
        $data = $this->getCache($val);
        if( isset($data->key) and strlen($data->key)>8){//등록된 데이터가 있고, 수정이 안되었으니 패스..

            echo "캐쉬값 있음.. but 값이 같음\n";
            //  $this->setCache($key,$val);
            return "none";
        }else{ //값이 변경 또는 신규 등록
            echo "캐쉬값 없음.\n";
            $code = $this->setCache($key,$val);
            echo "캐쉬 등록결과 : ".$code."\n";
            return $code;
        }
        echo "==[".$key."] 완료\n";
        return $this;
    }
    public function setCache($key,$val)
    {
        //key값으로  데이터가 있다면 수정, 없다면 추가.
        $row = $this->getCacheByKey($key);
        if(isset($row)){
            $query = "UPDATE `cache` SET  `val`=md5('%s'),up_date=now() WHERE `key`='%s'";
            $sql = sprintf($query,$val,$key );
            echo $sql."\n";
            //$this->link->query($sql);
            return "update";
        }else{
            $query = "INSERT INTO `cache` SET `key`='%s', `val`=md5('%s'),reg_date=now()";
            $sql = sprintf($query,$key,$val );
            echo $sql."\n";
            $this->link->query($sql);
            return "insert";
        }

        return $this;
    }
    public function getCache($val)
    {
        $sql = "SELECT `key` FROM `cache` WHERE `val` = md5('".$val."') LIMIT 1";
        $result = $this->link->query($sql);
        echo $sql."\n";
        $row = $result->fetch_object();
        return $row;
    }
    public function getCacheByKey($key)
    {
        $sql = "SELECT `val` FROM `cache` WHERE `key` = '".$key."' LIMIT 1";
        echo $sql."\n";
        $result = $this->link->query($sql);
        $row = $result->fetch_object();
        return $row;
    }


    public function set_product($key,$val)
    {
        $code = $this->cache($key,$val); // insert : 캐쉬추가, update: 캐쉬수정
        $data = json_decode($val);

        if($code == "none"){//등록 데이터와 동일
            echo $key." .. pass \n";
            return $this;
        }else{
            // cache 테이블과 product 테이블이 어긋나 있어도(예: 이전 실행이 중간에 실패한 경우)
            // upsert라 항상 안전하게 반영된다. insert/update를 코드로 나눠서 부르지 않는다.
            echo "값을 등록/변경 한다.\n";
            $this->upsert_product($data);
            return $this;
        }
    }
    // null 필드를 addslashes에 넘기면 PHP 8.1+ 에서 deprecated 경고가 나므로 문자열로 강제 변환.
    private function esc($v)
    {
        return addslashes((string)($v ?? ''));
    }
    public function getProduct($idx)
    {
        $query="select * from product where idx = ".$idx;
        echo "list query : ".$query."\n";

        $result = $this->link->query($query);

        $product = $result->fetch_object();
        return $product;
    }

    // item_seq가 이미 있으면 UPDATE, 없으면 INSERT를 DB가 알아서 결정한다(upsert).
    // cache 테이블 상태와 무관하게 항상 안전하게 반영되므로, 이전 실행이 중간에
    // 실패해서 cache/product가 어긋나 있어도 다음 실행에서 스스로 복구된다.
    public function upsert_product($data)
    {
        $shape_key = $this->shapeKeyFromLabel($data->DRUG_SHAPE);

        $color1_raw = trim((string)$data->COLOR_CLASS1);
        $color2_raw = isset($data->COLOR_CLASS2) ? trim((string)$data->COLOR_CLASS2) : '';
        [$color1_key, $color2_key] = $this->colorKeysFromBoth($color1_raw, $color2_raw);
        // color_class2가 없으면(단일색) '' 대신 진짜 SQL NULL을 넣어야 하므로,
        // 이 자리만 따옴표 없는 원시 SQL 조각으로 끼워넣는다.
        $color2_sql = $color2_key === null ? 'NULL' : "'" . $this->esc($color2_key) . "'";

        $query = "INSERT INTO product SET item_seq='%s',item_name='%s',entp_seq='%s',entp_name='%s',chart='%s',item_image='%s',print_front='%s',print_back='%s',
        drug_shape='%s',color_class1='%s',color_class2=%s,
        line_front='%s',line_back='%s',leng_long=%f,leng_short=%f,thick=%f,img_regist_ts='%s',class_no='%s',class_name='%s',
        etc_otc_name='%s',item_permit_date='%s',form_code_name='%s',mark_code_front_anal='%s',mark_code_back_anal='%s',mark_code_front_img='%s',mark_code_back_img='%s',
        item_eng_name='%s',change_date='%s',mark_code_front='%s',mark_code_back='%s',edi_code='%s',reg_date=now()
        ON DUPLICATE KEY UPDATE item_name=VALUES(item_name),entp_seq=VALUES(entp_seq),entp_name=VALUES(entp_name),chart=VALUES(chart),
        item_image=VALUES(item_image),print_front=VALUES(print_front),print_back=VALUES(print_back),
        drug_shape=VALUES(drug_shape),color_class1=VALUES(color_class1),color_class2=VALUES(color_class2),
        line_front=VALUES(line_front),line_back=VALUES(line_back),leng_long=VALUES(leng_long),leng_short=VALUES(leng_short),thick=VALUES(thick),
        img_regist_ts=VALUES(img_regist_ts),class_no=VALUES(class_no),class_name=VALUES(class_name),etc_otc_name=VALUES(etc_otc_name),
        item_permit_date=VALUES(item_permit_date),form_code_name=VALUES(form_code_name),mark_code_front_anal=VALUES(mark_code_front_anal),
        mark_code_back_anal=VALUES(mark_code_back_anal),mark_code_front_img=VALUES(mark_code_front_img),mark_code_back_img=VALUES(mark_code_back_img),
        item_eng_name=VALUES(item_eng_name),change_date=VALUES(change_date),mark_code_front=VALUES(mark_code_front),mark_code_back=VALUES(mark_code_back),
        edi_code=VALUES(edi_code),up_date=now()";
        $sql = sprintf($query,$data->ITEM_SEQ,$this->esc($data->ITEM_NAME),$this->esc($data->ENTP_SEQ),$this->esc($data->ENTP_NAME),$this->esc($data->CHART),$this->esc($data->ITEM_IMAGE),$this->esc($data->PRINT_FRONT),$this->esc($data->PRINT_BACK),
            $shape_key,$this->esc($color1_key),$color2_sql,
            $this->esc($data->LINE_FRONT),$this->esc($data->LINE_BACK),(float)$data->LENG_LONG,(float)$data->LENG_SHORT,(float)$data->THICK,$this->esc($data->IMG_REGIST_TS),$this->esc($data->CLASS_NO),$this->esc($data->CLASS_NAME),
            $this->esc($data->ETC_OTC_NAME),$this->esc($data->ITEM_PERMIT_DATE),$this->esc($data->FORM_CODE_NAME),$this->esc($data->MARK_CODE_FRONT_ANAL),$this->esc($data->MARK_CODE_BACK_ANAL),$this->esc($data->MARK_CODE_FRONT_IMG),$this->esc($data->MARK_CODE_BACK_IMG),
            $this->esc($data->ITEM_ENG_NAME),$this->esc($data->CHANGE_DATE),$this->esc($data->MARK_CODE_FRONT),$this->esc($data->MARK_CODE_BACK),$this->esc($data->EDI_CODE));
        echo $sql."\n";
        try {
            $this->link->query($sql);
        } catch (\mysqli_sql_exception $e) {
            // 이 한 건이 실패해도 배치 전체가 죽지 않고 다음 항목으로 넘어간다.
            echo "product 저장 실패 (item_seq=".$data->ITEM_SEQ."): ".$e->getMessage()."\n";
        }
        return $this;
    }
    /*
     * app/services/pill_service.py 의 _SHAPE_LABELS / _COLOR_LABELS 를 그대로 역매핑.
     * FastAPI 쪽과 동일한 key(round, yellow ...)를 써야 pill_master 조회 시 값이 맞는다.
     * 매핑에 없는 원자재청 원문값(정제 전 텍스트)은 임시로 원문 그대로 저장한다.
     */
    public function shapeKeyFromLabel($label)
    {
        $_shape_key['원형'] = 'round';
        $_shape_key['타원형'] = 'oval';
        $_shape_key['캡슐형'] = 'capsule';
        $_shape_key['삼각형'] = 'triangle';
        $_shape_key['사각형'] = 'square';
        $_shape_key['마름모형'] = 'diamond';
        $_shape_key['오각형'] = 'pentagon';
        $_shape_key['육각형'] = 'hexagon';
        $_shape_key['반원형'] = 'semicircle';
        $_shape_key['장방형'] = 'oblong';
        $_shape_key['팔각형'] = 'octagon';
        $_shape_key['기타'] = 'other';
        return isset($_shape_key[$label]) ? $_shape_key[$label] : $label;
    }
    // 단일 색상명(콤마 없는 원자값) 하나를 영문 키로 바꾼다. 모르는 값이면
    // 원본을 그대로 돌려준다 (기존 동작 유지).
    private function _colorBaseMap()
    {
        $m = [];
        $m['흰색'] = 'white';
        $m['하양'] = 'white';
        $m['아이보리'] = 'ivory';
        $m['노랑'] = 'yellow';
        $m['주황'] = 'orange';
        $m['분홍'] = 'pink';
        $m['빨강'] = 'red';
        $m['갈색'] = 'brown';
        $m['보라'] = 'purple';
        $m['자주'] = 'purple';
        $m['파랑'] = 'blue';
        $m['남색'] = 'blue';
        $m['초록'] = 'green';
        $m['연두'] = 'green';
        $m['청록'] = 'green';
        $m['회색'] = 'gray';
        $m['검정'] = 'black';
        $m['투명'] = 'clear';
        return $m;
    }

    // 라벨 하나(콤마로 여러 토큰이 뭉쳐있을 수 있음)를 실제 색상 키 배열로
    // 바꾼다. "진한/옅은/투명"은 그 자체로는 색이 아니라 재질/명도 수식어라
    // 항상 걸러진다 - "투명"이 단독으로 와도 여기서는 걸러지고, 진짜
    // 무색 알약인지는 colorKeysFromBoth()가 두 필드를 다 걸러낸 뒤에
    // 별도로 판단한다. "갈색, 진한"은 사진으로 확인된 특수 케이스라
    // 예외적으로 black 하나로 취급한다(실제로는 진한 갈색이 우리 팔레트
    // 기준 검정에 가까움).
    private function _colorTokens($label)
    {
        if($label === '') return [];

        $exact = ['갈색, 진한' => ['black']];
        if(isset($exact[$label])) return $exact[$label];

        $map = $this->_colorBaseMap();
        $modifiers = ['진한', '옅은', '투명'];
        $tokens = strpos($label, ',') !== false ? array_map('trim', explode(',', $label)) : [$label];
        $colors = [];
        foreach($tokens as $t){
            if($t === '' || in_array($t, $modifiers, true)) continue;
            $colors[] = isset($map[$t]) ? $map[$t] : $t;
        }
        return $colors;
    }

    // COLOR_CLASS1/COLOR_CLASS2 둘 다에서 실제 색상 토큰을 뽑아 합집합을
    // 만든 뒤 [주색상, 보조색상|null]로 돌려준다. COLOR_CLASS2를 있는 그대로
    // 믿지 않는 이유: 실측 확인 결과 COLOR_CLASS2가 COLOR_CLASS1의 토큰을
    // (첫번째든 두번째든 일관성 없이) 그대로 반복하는 경우가 흔해서, 둘 다
    // 똑같이 토큰 분해 후 합쳐야 안전하다. 이러면 어느 쪽을 반복하든 결과가
    // 같고, 진짜 다른 색을 주는 경우(예: "주황,투명"+"노랑,투명")에도 둘 다
    // 반영된다.
    public function colorKeysFromBoth($color1_label, $color2_label)
    {
        $colors = array_values(array_unique(array_merge(
            $this->_colorTokens($color1_label),
            $this->_colorTokens($color2_label)
        )));

        if(count($colors) >= 2) return [$colors[0], $colors[1]];
        if(count($colors) == 1) return [$colors[0], null];
        // 아무 토큰도 못 뽑았으면(둘 다 비어있거나 수식어뿐이면) - 그 중
        // 하나라도 "투명" 단독값이면 진짜 무색 알약이므로 clear로 취급한다.
        // (예: "빨강, 투명"+"투명"은 재질 수식어 반복일 뿐이라 위에서 이미
        // "red"로 처리됐고, 여기로 오는 건 순수 "투명"+""나 "투명"+"투명"뿐.)
        if($color1_label === '투명' || $color2_label === '투명') return ['clear', null];
        // 그 외엔 원본 COLOR_CLASS1을 그대로 둔다 - 기존 폴백 동작과 동일.
        return [$color1_label, null];
    }
}