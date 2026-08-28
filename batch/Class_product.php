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
        $data = unserialize($val);

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
        $color_key = $this->colorKeyFromLabel($data->COLOR_CLASS1);
        $query = "INSERT INTO product SET item_seq='%s',item_name='%s',entp_seq='%s',entp_name='%s',chart='%s',item_image='%s',print_front='%s',print_back='%s',
        drug_shape='%s',color_class1='%s',color_class2='%s',
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
            $shape_key,$color_key,$this->esc($data->COLOR_CLASS2),
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
        return isset($_shape_key[$label]) ? $_shape_key[$label] : $label;
    }
    public function colorKeyFromLabel($label)
    {
        $_color_key['흰색'] = 'white';
        $_color_key['하양'] = 'white';
        $_color_key['아이보리'] = 'ivory';
        $_color_key['노랑'] = 'yellow';
        $_color_key['주황'] = 'orange';
        $_color_key['분홍'] = 'pink';
        $_color_key['빨강'] = 'red';
        $_color_key['갈색'] = 'brown';
        $_color_key['보라'] = 'purple';
        $_color_key['자주'] = 'purple';
        $_color_key['파랑'] = 'blue';
        $_color_key['남색'] = 'blue';
        $_color_key['초록'] = 'green';
        $_color_key['연두'] = 'green';
        $_color_key['청록'] = 'green';
        $_color_key['회색'] = 'gray';
        $_color_key['검정'] = 'black';
        return isset($_color_key[$label]) ? $_color_key[$label] : $label;
    }
}